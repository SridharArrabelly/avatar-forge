"""Grounding tools the realtime model calls directly.

In agent mode the Foundry agent owns its tools and runs them inside the agent
runtime. Binding Voice Live to a model removes the agent, and its tools go with
it — the Voice Live session schema offers only ``FunctionTool`` and ``MCPTool``,
with no managed ``azure_ai_search`` equivalent. So when the model is the brain,
retrieval has to live here.

That is a cost, but it is also the point: owning the call means we can hold a
warm client, reach for hybrid + semantic ranking explicitly, and trim the
payload to what actually has to cross the wire — none of which is reachable
inside a managed tool.

Retrieval quality is the thing to protect. A plain keyword ``search()`` would be
faster to write and would quietly answer worse, which is the one failure mode
that would make a latency win meaningless. Measured against the live index:

    keyword only      218-228ms   top hit often the wrong meeting entirely
    hybrid + semantic 598-776ms   correct meeting, reranker scores present

Both numbers sit under the 1.3-1.9s the managed tool costs, so the better
retrieval is affordable. Hybrid is what ships; the difference is not a tuning
knob to be relaxed later without re-measuring answer quality.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from azure.search.documents.models import VectorizableTextQuery

from .catalog import get_search_client

logger = logging.getLogger(__name__)

SEMANTIC_CONFIG = os.getenv("SEARCH_SEMANTIC_CONFIG", "default-semantic")
VECTOR_FIELD = os.getenv("SEARCH_VECTOR_FIELD", "content_vector")

# Passages returned to the model. Four is enough to answer from and short
# enough that the model starts speaking sooner: every extra passage is more
# prompt to read before the first token.
DEFAULT_TOP = 4
MAX_TOP = 8

# Repeated back to the model with every tool result. The session instructions
# are prefilled once at the top of a long context; a tool result is the last
# thing read before the answer is generated, and a realtime model weights that
# recency heavily. Measured: prompt-only pressure left broad questions at ~30s
# of speech even after two rewrites, because "what was discussed" invites a
# summary and the brevity rule was thousands of tokens away by then.
BREVITY_NOTE = (
    "Answer in three sentences or fewer, spoken aloud. Give only the two or "
    "three points that mattered most; do not walk through everything above."
)

# Characters of each passage handed back. The chunks are larger than this, but
# the tail is rarely what answers the question and it is paid for twice — once
# on the wire, once in the model's prefill.
SNIPPET_CHARS = 1200

# Vector recall before reranking. The reranker only ever sees candidates the
# retrieval stage surfaced, so this is the real recall knob.
K_NEAREST = 40


SEARCH_MINUTES_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "search_minutes",
    "description": (
        "Search the indexed board and executive meeting minutes. Use this for any "
        "question about what was discussed, decided, approved, reported or raised "
        "in a meeting, and for anything about figures, people or commitments that "
        "would appear in the minutes. Always call this rather than answering from "
        "memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "What to look for, in natural language. Include the meeting "
                    "date when the question names one — the minutes are indexed "
                    "per meeting and the date sharpens the match."
                ),
            },
        },
        "required": ["query"],
    },
}

REALTIME_TOOLS: list[dict[str, Any]] = [SEARCH_MINUTES_TOOL]


# ---------------------------------------------------------------------------
# Web / news — Microsoft Web IQ
# ---------------------------------------------------------------------------
#
# Grounding-with-Bing cannot come along to model mode: it executes inside the
# Foundry agent runtime, and its key is rejected (401) against the public Bing
# REST API, so there is no way to call it from our own process. Web IQ has the
# opposite property — it is a plain REST endpoint we call ourselves, which is
# exactly what a FunctionTool needs.
#
# Of Web IQ's three access paths this takes the REST one. The MCP transport
# needs a Streamable-HTTP session (initialize -> tools/list -> tools/call)
# before it can search, and the official SDK wraps the same REST call in
# another layer; both cost hops on the answer path for no benefit here. The
# REST path is also the only one that is trivially async, which matters more
# than it sounds: a synchronous call would block the event loop that is
# concurrently pumping 20ms audio frames and the Voice Live socket.

WEBIQ_BASE_URL = os.getenv("WEBIQ_BASE_URL", "https://api.microsoft.ai/v3")
WEBIQ_API_SCOPE = "https://api.microsoft.ai/.default"


def _allowed_domains() -> list[str]:
    """Same security boundary the Bing custom-search allow-list provided.

    An open-web tool answering to an executive should not be able to cite
    anywhere at all. Comma-separated hostnames; empty means the open web.

    Read per call rather than at import so the value tracks the environment the
    process is actually running in — a module-level read bakes in whatever was
    set at import time, which is the bug class that made `setup_foundry_agent`
    silently ignore `.env`.
    """
    return [
        d.strip() for d in os.getenv("WEBIQ_ALLOWED_DOMAINS", "").split(",") if d.strip()
    ]

# Deliberately tighter than the Web IQ defaults (5 results x 2000 chars). Every
# character here is prefill the model reads before it starts speaking, and a
# spoken answer quotes a sentence, not a page.
WEB_MAX_RESULTS = 4
WEB_MAX_LENGTH = 800

WEBIQ_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=3.0)

_web_client: httpx.AsyncClient | None = None
_web_client_lock = asyncio.Lock()
_aad_credential: Any = None


def web_search_configured() -> bool:
    """True when a Web IQ credential is available.

    Checked at session start so the tool is only advertised to the model when it
    can actually run — offering a tool that always errors is worse than not
    having one, because the model will keep trying it.
    """
    return bool(os.getenv("WEBIQ_API_KEY") or os.getenv("WEBIQ_USE_ENTRA"))


SEARCH_WEB_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "search_web",
    "description": (
        "Search the public web for current, external information: news, market "
        "and competitor activity, regulatory developments, share price "
        "commentary, and anything that happened recently. Use this when the "
        "question is about the outside world rather than about what was said in "
        "a meeting. Do not use it for the organisation's own minutes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "What to search for, in natural language. Do not add "
                    "'site:' operators or domain names — the trusted sources "
                    "are applied automatically."
                ),
            },
        },
        "required": ["query"],
    },
}


_HOSTISH = r"[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}"
# `site:x.com`, `-site:x.com`, and the colon-less `site x.com` the model writes.
# The lookbehind admits a preceding `(` so an OR-group's first operator is seen,
# and the value stops at `)` so the group's own bracket survives to be cleaned
# up as a pair rather than half-eaten.
_SITE_OP_RE = re.compile(r"(?<![^\s(])-?site\s*:\s*[^\s)]+", re.IGNORECASE)
_BARE_SITE_RE = re.compile(rf"(?<![^\s(])-?site\s+{_HOSTISH}(?!\S)", re.IGNORECASE)
# Leftovers once the operators inside an OR-group are gone: `( OR )`, `(OR x`,
# `x OR )`, doubled `OR OR`, and a dangling `OR` at either end.
_LEFTOVERS = (
    (re.compile(r"\(\s*(?:OR\s*)*\)", re.IGNORECASE), " "),
    (re.compile(r"\(\s*(?:OR\s+)+", re.IGNORECASE), "("),
    (re.compile(r"(?:\s+OR)+\s*\)", re.IGNORECASE), ")"),
    (re.compile(r"(?<!\S)OR(?:\s+OR)+(?!\S)", re.IGNORECASE), "OR"),
    (re.compile(r"^\s*(?:OR\s+)+", re.IGNORECASE), ""),
    (re.compile(r"(?:\s+OR)+\s*$", re.IGNORECASE), ""),
)

# Hostname labels that mark a non-production copy of an allowed site. `site:`
# matches a domain *and all its subdomains*, so an allow-list of bare hosts
# silently admits these.
NONPROD_LABELS = frozenset(
    {"dev", "development", "staging", "stage", "test", "testing", "uat", "qa",
     "preview", "beta", "sandbox", "demo", "local"}
)


def strip_scope_operators(query: str) -> str:
    """Remove any site scoping the model wrote into its own query.

    The tool schema asks for natural language, but the model writes operators
    anyway — observed live as ``'MTN Group CFO name site mtn.com'``. Note the
    missing colon: Web IQ then matches ``site`` and ``mtn.com`` as ordinary
    keywords, and :func:`build_query` appends the real allow-list on top, so the
    wire query carries two overlapping scopes.

    Measured against the live API, this is **not** catastrophic — the polluted
    query still returned five relevant results. But it is not free either: for
    "MTN Group share price" the model's ``site mtn.com`` pushed the live price
    pages (``sashares.co.za``, ``jse.co.za``) below MTN's own investor landing
    page, which does not carry a price. Stripping it costs one regex pass and
    makes the wire query a deterministic function of the allow-list, which is
    what makes retrieval behaviour reproducible between runs.
    """
    cleaned = _BARE_SITE_RE.sub(" ", _SITE_OP_RE.sub(" ", query))
    if cleaned != query:
        for pattern, repl in _LEFTOVERS:
            cleaned = pattern.sub(repl, cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    # Never hand back an empty query: if the model wrote nothing but operators,
    # the original is a worse query but still a real one.
    return cleaned or query.strip()


def host_of(d: str) -> str:
    """Reduce a domain or full URL to its bare host.

    `site:` matches a domain, not a path, so an allow-list entry written as a
    URL has to be cut back to its host before it becomes an operator.
    """
    return (urlsplit(d if "//" in d else f"//{d}").netloc or d).strip("/").lower()


def is_nonprod_host(host: str, domains: list[str]) -> bool:
    """True for `dev.`/`staging.`/... copies of an otherwise allowed domain.

    ``site:sashares.co.za`` matches every subdomain, so the allow-list admits
    ``dev.sashares.co.za`` — and Web IQ ranked exactly that **first** for "MTN
    Group share price", quoting ``R211.74`` last updated 2026-06-05 while the
    production host carried ``R204.97`` from 2026-07-31. Two months stale, from
    a staging site, offered as the current share price.

    This is a regression against the Bing custom-search config it replaced,
    whose entries were path-scoped and so could not match a sibling subdomain.
    It cannot be expressed as a ``site:`` operator, so it is enforced on the way
    out instead.

    The test is deliberately anchored to the allow-list rather than to label
    shapes: a host is rejected only when it is a *strict subdomain* of an
    allowed domain **and** the extra labels are all non-production markers.
    Guessing from the leftmost label alone would misfire on two-level TLDs — a
    hypothetical allowed ``test.co.za`` is its own site, not a staging copy —
    and would reject legitimate subdomains such as ``senspdf.jse.co.za``, which
    carries the JSE's SENS filings.
    """
    host = host_of(host)
    if not host:
        return False
    for raw in domains:
        allowed = host_of(raw.strip().lstrip("-"))
        if not allowed or not host.endswith(f".{allowed}"):
            continue
        prefix = host[: -len(allowed) - 1].split(".")
        # `www` is a production host; ignore it when judging the rest.
        extra = [p for p in prefix if p and p != "www"]
        if extra and all(p in NONPROD_LABELS for p in extra):
            return True
    return False


def build_query(query: str, domains: list[str]) -> str:
    """Append Web IQ `site:` operators for the allow-list.

    Web IQ has no server-side allow-list — its request model carries `query`,
    `maxResults`, `language`, `region`, `location`, `contentFormat`, `maxLength`
    and `safeSearch`, and nothing for site scoping. So the scope has to be
    expressed in the query text, which is what the API documents.

    Includes are OR-ed, so results may come from any of them. A leading `-`
    excludes a domain and must render as `-site:host`; the naive `site:-host`
    is accepted as a nonsense hostname and silently matches nothing.

    A full URL is reduced to its host because `site:` matches a domain, not a
    path.

    Any scoping the model wrote itself is stripped first, so the operators here
    are the only ones on the wire.
    """
    query = strip_scope_operators(query)
    if not domains:
        return query

    include: list[str] = []
    exclude: list[str] = []
    for raw in domains:
        raw = raw.strip()
        if raw.startswith("-"):
            host = host_of(raw[1:])
            if host:
                exclude.append(f"-site:{host}")
        else:
            host = host_of(raw)
            if host:
                include.append(f"site:{host}")

    parts: list[str] = []
    if include:
        parts.append(f"({' OR '.join(include)})")
    parts.extend(exclude)
    return f"{query} {' '.join(parts)}" if parts else query


async def _get_web_client() -> httpx.AsyncClient:
    """One pooled client for the process — keeps the TLS session warm."""
    global _web_client
    if _web_client is None:
        async with _web_client_lock:
            if _web_client is None:
                _web_client = httpx.AsyncClient(
                    base_url=WEBIQ_BASE_URL, timeout=WEBIQ_TIMEOUT
                )
    return _web_client


async def _auth_headers() -> dict[str, str]:
    """API key when set, otherwise an Entra token (matching our keyless posture)."""
    global _aad_credential
    key = os.getenv("WEBIQ_API_KEY")
    if key:
        return {"x-apikey": key}
    if _aad_credential is None:
        from azure.identity.aio import DefaultAzureCredential

        _aad_credential = DefaultAzureCredential()
    token = await _aad_credential.get_token(WEBIQ_API_SCOPE)
    return {"Authorization": f"Bearer {token.token}"}


async def close_web_client() -> None:
    """Release the pooled client and credential on shutdown."""
    global _web_client, _aad_credential
    if _web_client is not None:
        await _web_client.aclose()
        _web_client = None
    if _aad_credential is not None:
        await _aad_credential.close()
        _aad_credential = None


def _pick(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = item.get(k)
        if v:
            return str(v)
    return ""


async def search_web(query: str) -> dict[str, Any]:
    """One POST to Web IQ's ``/search/web``. No SDK layer, no MCP session, no agent.

    **Why there is no ``news`` option.** Web IQ exposes several verticals — web,
    news, images, video. ``/search/news`` queries a *news-publisher* index, and
    that index is fundamentally incompatible with a domain allow-list built from
    corporate and exchange sites. Measured against the live API:

    ===========================================  =======================================
    ``site:mtn.com`` + ``/search/news``          ``[]``
    ``site:mtn.com`` + ``/search/web``           executive-committee, leadership, and
                                                 the new-group-CFO announcement pages
    no allow-list + ``/search/news``             Moneyweb, MyBroadband, Yahoo Finance
    ===========================================  =======================================

    Of the allowed domains only ITWeb is a news publisher, so every news query
    collapsed onto ITWeb and returned articles unrelated to the question —
    "MTN share price" came back as a 6G-spectrum story. The model then correctly
    reported it had nothing to answer from.

    ``/search/web`` carries ``lastUpdatedAt``/``crawledAt`` per result, so recency
    is still available where it matters; it is a ranking signal rather than a
    separate index. Recency was never worth a vertical that the allow-list empties.
    """
    query = (query or "").strip()
    if not query:
        return {"error": "query is required"}
    feature = "web"

    if not web_search_configured():
        return {"error": "Web search is not configured on this deployment."}

    domains = _allowed_domains()
    body = {
        "query": build_query(query, domains),
        # Over-fetch by two so dropping a staging mirror does not starve the
        # answer; the list is trimmed back to WEB_MAX_RESULTS after filtering.
        "maxResults": WEB_MAX_RESULTS + 2,
        "language": os.getenv("WEBIQ_LANGUAGE", "en"),
        "region": os.getenv("WEBIQ_REGION", "ZA"),
        "contentFormat": "text",
        "maxLength": WEB_MAX_LENGTH,
    }

    started = time.monotonic()
    try:
        client = await _get_web_client()
        resp = await client.post(
            f"/search/{feature}", json=body, headers=await _auth_headers()
        )
        if resp.status_code != 200:
            detail = resp.text[:160].replace("\n", " ")
            logger.warning(f"search_web {feature} HTTP {resp.status_code}: {detail}")
            return {"error": f"Web search failed (HTTP {resp.status_code})."}
        payload = resp.json() or {}
    except Exception as e:
        logger.warning(f"search_web failed for {query!r}: {type(e).__name__}: {e}")
        return {"error": f"The web search failed: {type(e).__name__}"}

    # Web IQ names the result list per feature; take whichever is present.
    raw: list[dict[str, Any]] = []
    for key in ("webResults", "newsResults", "value", "results"):
        if payload.get(key):
            raw = [i for i in payload[key] if isinstance(i, dict)]
            break

    # Field names measured against the live API rather than assumed — Web IQ
    # uses `lastUpdatedAt`/`crawledAt`, not the Bing-style `datePublished`, and
    # carries a clean `source` ("Moneyweb") so the model can attribute a claim
    # without parsing a hostname. `clickUrl` is a redirect tracker; `url` is the
    # real link. `thumbnail`/`isAdult` are dropped as noise.
    results: list[dict[str, Any]] = []
    dropped: list[str] = []
    for i in raw:
        url = _pick(i, ("url", "contentUrl", "hostPageUrl"))
        host = urlsplit(url).netloc
        if is_nonprod_host(host, domains):
            dropped.append(host)
            continue
        results.append(
            {
                "title": _pick(i, ("title", "name")),
                "source": _pick(i, ("source",)),
                "url": url,
                "published": _pick(
                    i, ("lastUpdatedAt", "crawledAt", "datePublished")
                )[:10],
                "extract": _pick(i, ("content", "snippet", "description"))[
                    :WEB_MAX_LENGTH
                ],
            }
        )

    if dropped:
        logger.info(f"[TOOL] search_web dropped non-production hosts: {dropped}")

    # Back to the answer budget: the over-fetch existed only to survive filtering.
    results = results[:WEB_MAX_RESULTS]

    elapsed_ms = (time.monotonic() - started) * 1000
    logger.info(
        f"[TOOL] search_web({feature}) {elapsed_ms:.0f}ms  n={len(results)}  q={query!r}"
    )
    if not results:
        return {"results": [], "note": "No results found on the web for that."}
    return {"results": results, "note": BREVITY_NOTE}


def build_realtime_tools() -> list[dict[str, Any]]:
    """The tool set advertised for this session.

    The web tool appears only when it is actually usable, so a deployment
    without a Web IQ credential degrades to minutes-only rather than to a model
    that keeps calling a tool that always fails.
    """
    tools = list(REALTIME_TOOLS)
    if web_search_configured():
        tools.append(SEARCH_WEB_TOOL)
    else:
        logger.info("Web IQ not configured — model mode will ground on minutes only.")
    return tools


def _format_date(raw: Any) -> str:
    """`2026-02-15T00:00:00Z` -> `15 February 2026`, best effort."""
    from datetime import datetime

    try:
        text = raw if isinstance(raw, str) else raw.isoformat()
        dt = datetime.strptime(text.split("T", 1)[0], "%Y-%m-%d")
        return f"{dt.day} {dt.strftime('%B %Y')}"
    except Exception:
        return str(raw or "")


async def search_minutes(query: str, top: int = DEFAULT_TOP) -> dict[str, Any]:
    """Hybrid (keyword + vector) search with semantic reranking, one round trip.

    The index carries an integrated vectorizer, so the search service embeds the
    query itself. That matters on the answer path: it keeps this to a single
    call instead of an embedding round trip followed by a search round trip.
    """
    query = (query or "").strip()
    if not query:
        return {"error": "query is required"}

    client = get_search_client()
    if client is None:
        return {
            "error": (
                "The minutes index is not configured on this deployment "
                "(AZURE_SEARCH_ENDPOINT / SEARCH_INDEX_NAME are unset)."
            )
        }

    top = max(1, min(int(top or DEFAULT_TOP), MAX_TOP))
    started = time.monotonic()
    try:
        results = await client.search(
            search_text=query,
            vector_queries=[
                VectorizableTextQuery(
                    text=query, k_nearest_neighbors=K_NEAREST, fields=VECTOR_FIELD
                )
            ],
            query_type="semantic",
            semantic_configuration_name=SEMANTIC_CONFIG,
            top=top,
            select=["title", "meeting_date", "content"],
        )
        passages = [
            {
                "meeting": doc.get("title") or "",
                "date": _format_date(doc.get("meeting_date")),
                "extract": (doc.get("content") or "")[:SNIPPET_CHARS],
            }
            async for doc in results
        ]
    except Exception as e:
        # Never raise into the tool loop: the model handles "nothing found"
        # gracefully and can say so, but an exception strands the turn with the
        # user listening to silence.
        logger.warning(f"search_minutes failed for {query!r}: {type(e).__name__}: {e}")
        return {"error": f"The minutes search failed: {type(e).__name__}"}

    elapsed_ms = (time.monotonic() - started) * 1000
    logger.info(
        f"[TOOL] search_minutes {elapsed_ms:.0f}ms  n={len(passages)}  q={query!r}"
    )
    if not passages:
        return {"passages": [], "note": "No matching passages in the minutes."}
    return {"passages": passages, "note": BREVITY_NOTE}
