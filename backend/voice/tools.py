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
from azure.ai.projects.models import AzureAISearchQueryType
from azure.search.documents.models import VectorizableTextQuery

from ..document_titles import display_document_title
from ..logsafe import fingerprint
from .catalog import get_search_client

logger = logging.getLogger(__name__)

SEMANTIC_CONFIG = os.getenv("SEARCH_SEMANTIC_CONFIG", "default-semantic")
VECTOR_FIELD = os.getenv("SEARCH_VECTOR_FIELD", "content_vector")

# Passages returned to the model. Five matches the agent-mode default
# (AI_SEARCH_TOP_K) so the two modes are evidence-comparable; the model may
# still ask for fewer/more per call, up to MAX_TOP.
DEFAULT_TOP = 5
MIN_TOP = 1
MAX_TOP = 8

# Retrieval mode. Mirrors setup_foundry_agent.py's AI_SEARCH_QUERY_TYPE so the
# two run modes are the same knob: "semantic" is BM25 lexical candidates
# reranked semantically, with no vector query at all. That is the default now
# (it used to always run vector+semantic hybrid) because it is the mode the
# agent-mode study was measured against; vector recall is opt-in via
# AI_SEARCH_QUERY_TYPE=vector / vector_simple_hybrid / vector_semantic_hybrid.
DEFAULT_QUERY_TYPE = AzureAISearchQueryType.SEMANTIC.value
VALID_QUERY_TYPES = frozenset(item.value for item in AzureAISearchQueryType)
_VECTOR_QUERY_TYPES = frozenset({
    AzureAISearchQueryType.VECTOR.value,
    AzureAISearchQueryType.VECTOR_SIMPLE_HYBRID.value,
    AzureAISearchQueryType.VECTOR_SEMANTIC_HYBRID.value,
})
_SEMANTIC_QUERY_TYPES = frozenset({
    AzureAISearchQueryType.SEMANTIC.value,
    AzureAISearchQueryType.VECTOR_SEMANTIC_HYBRID.value,
})

# Legacy explicit profile, preserved only for reference/comparison against the
# earlier always-vector+semantic behaviour; no longer the default.
LEGACY_QUERY_TYPE = AzureAISearchQueryType.VECTOR_SEMANTIC_HYBRID.value
LEGACY_TOP = 4
LEGACY_SNIPPET_CHARS = 1200

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

# Characters of each passage handed back. The indexed sections run up to
# 12,000 characters (backend/document_sections.py's versioned layout cap), so
# the default now lets a whole section through untouched; the old 1200-char
# cut sliced a section mid-thought before the model ever saw the rest of it.
# Anything genuinely larger is truncated explicitly (passage["truncated"]) and
# `original_chars` records what was cut, rather than silently shrinking a
# passage with no signal to the model that content is missing.
SNIPPET_CHARS = 12000

# Vector recall before reranking. The reranker only ever sees candidates the
# retrieval stage surfaced, so this is the real recall knob. Only spent when
# the query type actually uses a vector query.
K_NEAREST = 40


SEARCH_MINUTES_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "search_minutes",
    "description": (
        "Search MTN's board and executive meeting minutes. Use it for what "
        "a meeting discussed, decided, approved, reported or actioned. Always call "
        "this rather than answering meeting content from memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "What to look for in the meeting minutes, in natural language. "
                    "Include the exact meeting date when known."
                ),
            },
            "top": {
                "type": "integer",
                "minimum": MIN_TOP,
                "maximum": MAX_TOP,
                "description": (
                    f"Optional number of passages to return ({MIN_TOP}-{MAX_TOP}). "
                    "Omit to use the configured default."
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

# Web IQ documents `query` as "maximum length is 1000 characters", and enforces
# it: measured against the live endpoint this module calls, 1000 returns results
# and 1024 returns HTTP 400 HandlerInvalidInput. There is no truncation and no
# partial success.
#
# The limit is documented on the REST API, which is precisely the path taken
# here — `search_web` POSTs to /search/web with httpx and there is no Web IQ SDK
# in the dependency set. An SDK could only add client-side validation in front
# of the same server-side rejection, so this is the binding constraint either
# way.
#
# This is a shared budget, not a question budget. The allow-list is compiled
# into the query text (build_query), so every allowed domain spends characters
# the question can no longer use: the 13-host list costs 284 of the 1000, the
# 21-host list costs 471. Without a guard an over-long query is a hard 400 that
# reaches the user as "Web search failed" — a search that silently stops working
# once the list or the model's phrasing grows a little.
#
# The same documentation adds that `site:` operators "inherently reduce result
# relevance", which is the reason to keep the list as short as the security
# boundary allows rather than as long as it will fit.
WEBIQ_MAX_QUERY_CHARS = 1000

WEBIQ_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=3.0, pool=3.0)

# How long an idle pooled connection is kept for reuse. httpx's default is 5 s,
# but searches in a conversation arrive 15-40 s apart, so with the default
# almost every one paid a fresh TCP+TLS handshake: measured from a laptop, a
# call after 20 s idle took 1014-1120 ms against 346-410 ms back to back. With a
# long expiry, reuse held at 20 s and 60 s gaps (328-396 ms) and was lost
# between 60 and 120 s (1041-1180 ms) — something in the path drops idle
# connections there, and httpx then reconnects cleanly. 55 s stays under that.
WEBIQ_KEEPALIVE_EXPIRY_S = 55.0

# Cap on the startup capability probe. Generous next to WEBIQ_TIMEOUT because a
# cold credential chain legitimately takes seconds, but finite because the
# answer gates session setup.
WEBIQ_PROBE_TIMEOUT_S = 10.0

_web_client: httpx.AsyncClient | None = None
_web_client_lock = asyncio.Lock()
_aad_credential: Any = None
_web_probe: "asyncio.Task[bool] | None" = None
_web_probe_lock = asyncio.Lock()


async def _credential() -> Any:
    """The lazily-created Entra credential, shared by the probe and the caller."""
    global _aad_credential
    if _aad_credential is None:
        from azure.identity.aio import DefaultAzureCredential

        _aad_credential = DefaultAzureCredential()
    return _aad_credential


async def _probe_webiq_scope() -> bool:
    """Ask for a Web IQ token once, and report whether one came back.

    Bounded, because "no answer" is a real outcome and not a rare one: a token
    request for an unknown or consent-requiring resource can stall rather than
    refuse. Measured on a dev box, ``az account get-access-token --resource
    https://api.microsoft.ai`` returns nothing for minutes while the same call
    for ``ai.azure.com`` succeeds immediately. Unbounded, that stall would
    propagate through ``build_realtime_tools()`` into session setup and hang the
    first conversation — strictly worse than the missing tool this replaced.
    A timeout is therefore a "no": the tool is not usable if we cannot find out
    in time.
    """
    try:
        credential = await _credential()
        await asyncio.wait_for(
            credential.get_token(WEBIQ_API_SCOPE), WEBIQ_PROBE_TIMEOUT_S
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "Web IQ: no answer for %s within %.0fs - search_web will not be "
            "offered. A stalled token request usually means the scope is not "
            "consented in this tenant.",
            WEBIQ_API_SCOPE,
            WEBIQ_PROBE_TIMEOUT_S,
        )
        return False
    except Exception as e:
        logger.info(
            "Web IQ: no token for %s (%s) - search_web will not be offered.",
            WEBIQ_API_SCOPE,
            e,
        )
        return False
    logger.info("Web IQ: token acquired for %s - search_web enabled.", WEBIQ_API_SCOPE)
    return True


async def web_search_available() -> bool:
    """True when Web IQ can actually be called.

    Checked before the tool is advertised, because offering a tool that always
    errors is worse than not having one: the model will keep trying it, and each
    attempt costs a turn of silence before she can say she cannot answer.

    An API key is taken at face value — it is either right or the call fails
    visibly. With no key we *probe* the Entra scope instead of trusting a
    configuration flag, because ``DefaultAzureCredential`` always constructs
    successfully and only fails when a token is actually requested. A flag can
    claim an entitlement the deployment does not have; a token cannot. This
    matters because ``api.microsoft.ai`` is a Microsoft-internal endpoint and
    this repository is public, so most deployments genuinely cannot reach it.

    The probe runs once per process and the result is cached, including a
    negative one, so the answer is stable for the life of a revision rather than
    varying between sessions. ``backend/main.py`` kicks it off at startup so no
    conversation waits on it.

    A token is necessary but not sufficient: the service can still refuse an
    authenticated caller, which surfaces as a 4xx from ``search_web``.
    """
    if os.getenv("WEBIQ_API_KEY"):
        return True
    global _web_probe
    async with _web_probe_lock:
        if _web_probe is None:
            _web_probe = asyncio.create_task(_probe_webiq_scope())
    return await _web_probe


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
    {"dev", "development", "staging", "stage", "stg", "test", "testing", "uat",
     "qa", "preview", "preprod", "nonprod", "beta", "sandbox", "demo", "local"}
)


def _is_nonprod_label(label: str) -> bool:
    """True when a single host label marks a non-production environment.

    Matched on a normalised *stem* rather than the literal label, because real
    deployments number their environments. The miss that prompted this was
    ``stg18326.businessday.ng``, observed live in a search result; ``dev2``,
    ``staging-01`` and ``uat_3`` are the same shape, and an exact-match test
    admitted every one of them.

    Separators split the label, trailing digits are dropped, and each remaining
    stem must match a marker **exactly**. Exactly rather than by prefix, because
    prefix matching quietly rejects real sites: ``local`` is a marker but
    ``localnews`` is a newspaper, ``test`` is a marker but ``testimonials`` is a
    page, and ``demo`` is a marker but ``democracy`` is a subject.

    A purely numeric part contributes nothing either way, so ``staging-01`` is
    judged on ``staging`` alone, while a bare ``2.example.com`` is left alone.
    """
    stems = [re.sub(r"\d+$", "", part) for part in re.split(r"[-_]", label)]
    stems = [s for s in stems if s]
    return bool(stems) and all(s in NONPROD_LABELS for s in stems)


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

    A consequence of that anchoring: with **no** allow-list this returns False
    for everything, including the ``dev.`` host it was written for, because there
    is no allowed domain left to be a staging copy *of*. Opening the search to
    the whole web therefore disables this protection outright rather than
    weakening it — which is a reason to widen the allow-list rather than to
    empty it.
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
        if extra and all(_is_nonprod_label(p) for p in extra):
            return True
    return False


def _truncate_words(text: str, limit: int) -> str:
    """Cut `text` to `limit` characters, preferring a word boundary."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    head, sep, _ = cut.rpartition(" ")
    # Only honour the boundary if it does not throw away most of the query.
    return head if sep and len(head) >= limit // 2 else cut


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

    The result is held under ``WEBIQ_MAX_QUERY_CHARS``, because the operators and
    the question share one budget and exceeding it is a hard HTTP 400. When the
    two do not both fit, **the question is trimmed and the scope is kept whole**:
    the allow-list is a security boundary, so dropping hosts from it to make room
    would silently widen where the assistant may look — the one failure this
    scoping exists to prevent. A shorter question searches worse; a dropped
    domain searches somewhere it was told not to.
    """
    query = strip_scope_operators(query)

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
    if not parts:
        return _truncate_words(query, WEBIQ_MAX_QUERY_CHARS)

    suffix = " ".join(parts)
    budget = WEBIQ_MAX_QUERY_CHARS - len(suffix) - 1  # -1 for the joining space
    if budget <= 0:
        # The scope alone will not fit. Unreachable with any sane allow-list —
        # 21 hosts cost 471 of 1000 — so this means the list has been grown far
        # past what the API can carry, and every search is about to be scope with
        # no question. Loud, because the symptom otherwise is uniformly useless
        # results rather than an error.
        logger.error(
            "Web IQ allow-list is %d chars, at or over the %d-char query cap: "
            "no room is left for the question. Shorten WEBIQ_ALLOWED_DOMAINS.",
            len(suffix), WEBIQ_MAX_QUERY_CHARS,
        )
        return suffix[:WEBIQ_MAX_QUERY_CHARS]
    if len(query) > budget:
        logger.warning(
            "search query trimmed %d->%d chars to fit the %d-char Web IQ cap "
            "(%d scoped hosts cost %d chars)",
            len(query), budget, WEBIQ_MAX_QUERY_CHARS,
            len(include) + len(exclude), len(suffix),
        )
    return f"{_truncate_words(query, budget)} {suffix}"


async def _get_web_client() -> httpx.AsyncClient:
    """One pooled client for the process — keeps the TLS session warm."""
    global _web_client
    if _web_client is None:
        async with _web_client_lock:
            if _web_client is None:
                _web_client = httpx.AsyncClient(
                    base_url=WEBIQ_BASE_URL,
                    timeout=WEBIQ_TIMEOUT,
                    limits=httpx.Limits(keepalive_expiry=WEBIQ_KEEPALIVE_EXPIRY_S),
                )
    return _web_client


async def _auth_headers() -> dict[str, str]:
    """API key when set, otherwise an Entra token (matching our keyless posture)."""
    key = os.getenv("WEBIQ_API_KEY")
    if key:
        return {"x-apikey": key}
    credential = await _credential()
    token = await credential.get_token(WEBIQ_API_SCOPE)
    return {"Authorization": f"Bearer {token.token}"}


async def close_web_client() -> None:
    """Release the pooled client and credential on shutdown."""
    global _web_client, _aad_credential, _web_probe
    if _web_client is not None:
        await _web_client.aclose()
        _web_client = None
    # Drop the cached probe with the credential that produced it, so a restarted
    # app re-probes rather than inheriting a verdict from a dead credential.
    _web_probe = None
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

    if not await web_search_available():
        return {"error": "Web search is not configured on this deployment."}

    domains = _allowed_domains()
    body = {
        "query": build_query(query, domains),
        # Over-fetch by two so dropping a staging mirror does not starve the
        # answer; the list is trimmed back to WEB_MAX_RESULTS after filtering.
        "maxResults": WEB_MAX_RESULTS + 2,
        "language": os.getenv("WEBIQ_LANGUAGE", "en"),
        "region": os.getenv("WEBIQ_REGION", "ZA"),
        # `passage`, not `text`. The API documents four content formats, and the
        # difference decides whether the model reads the answer or the navbar:
        #
        #   passage  query-contextual extraction -- a model picks the paragraphs
        #            of the page most relevant to *this* query, up to maxLength
        #   text     the full document in plain text, from the top
        #
        # With `text` and an 800-character cap we were sending the first 800
        # characters of each page, which on a typical corporate site is a cookie
        # banner and a menu. The documentation is explicit that there is no
        # `snippet` field on the web API and that `passage` is how you get
        # query-dependent content; the quick start recommends it as the default
        # operating point. Same budget, spent on the part that answers.
        "contentFormat": "passage",
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
        logger.warning(f"search_web failed [{fingerprint(query)}]: {type(e).__name__}: {e}")
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
    #
    # `lastUpdatedAt`/`crawledAt` are index-maintenance timestamps, not
    # evidence of when a page was published or of a live quote's as-of time; a
    # crawl date only proves the crawler visited on that day. Labelling them
    # `published` claimed a fact Web IQ never asserted. `published` is now
    # populated only from `datePublished` when the API actually returns one,
    # and stays empty otherwise rather than being backfilled from a crawl
    # timestamp. The crawl/update timestamps are still returned, under their
    # own names, in full (not date-truncated) — they are useful for recency
    # ranking, just not for "when was this published".
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
                "published": _pick(i, ("datePublished",))[:10],
                "last_updated": _pick(i, ("lastUpdatedAt",)),
                "crawled_at": _pick(i, ("crawledAt",)),
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
        f"[TOOL] search_web({feature}) {elapsed_ms:.0f}ms  n={len(results)}  [{fingerprint(query)}]"
    )
    if not results:
        return {"results": [], "note": "No results found on the web for that."}
    return {
        "results": results,
        "note": (
            f"{BREVITY_NOTE} last_updated and crawled_at are index-maintenance "
            "timestamps, not proof of when a page was published or of a live "
            "quote's as-of time; treat published (when present) as the "
            "publication date and do not infer a current price/date from a "
            "crawl or update timestamp alone."
        ),
    }


async def build_realtime_tools() -> list[dict[str, Any]]:
    """The tool set advertised for this session.

    The web tool appears only when it is actually usable, so a deployment
    without a reachable Web IQ degrades to the internal meeting-minutes
    corpus rather than to a model that keeps calling a tool that always fails.
    """
    tools = list(REALTIME_TOOLS)
    if await web_search_available():
        tools.append(SEARCH_WEB_TOOL)
    else:
        logger.info(
            "Web IQ unavailable - model mode will ground on internal "
            "meeting minutes only."
        )
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


def _read_query_type() -> str:
    """AI_SEARCH_QUERY_TYPE, read per call so a running process picks up a
    changed environment the same way it would a changed .env at next launch.

    An explicitly-set but blank or unrecognised value is a configuration
    mistake, not permission to fall back to the default silently — it is
    surfaced as a ``ValueError`` so the caller can turn it into a safe tool
    error instead of quietly searching in the wrong mode.
    """
    raw = os.getenv("AI_SEARCH_QUERY_TYPE", DEFAULT_QUERY_TYPE)
    value = (raw or "").strip()
    if not value:
        raise ValueError("AI_SEARCH_QUERY_TYPE is set but empty")
    if value not in VALID_QUERY_TYPES:
        raise ValueError(
            f"AI_SEARCH_QUERY_TYPE must be one of: {', '.join(sorted(VALID_QUERY_TYPES))}; "
            f"got {value!r}"
        )
    return value


def _read_snippet_chars() -> int:
    """AI_SEARCH_SNIPPET_CHARS, read per call; same explicit-failure contract
    as `_read_query_type`."""
    raw = os.getenv("AI_SEARCH_SNIPPET_CHARS")
    if raw is None:
        return SNIPPET_CHARS
    value = raw.strip()
    if not value:
        raise ValueError("AI_SEARCH_SNIPPET_CHARS is set but empty")
    try:
        parsed = int(value)
    except ValueError:
        raise ValueError(f"AI_SEARCH_SNIPPET_CHARS must be a positive integer; got {value!r}") from None
    if parsed <= 0:
        raise ValueError(f"AI_SEARCH_SNIPPET_CHARS must be a positive integer; got {value!r}")
    return parsed


def _read_default_top() -> int:
    """AI_SEARCH_TOP_K, read per call; same explicit-failure contract as
    `_read_query_type`. Independent of the model's own per-call `top`."""
    raw = os.getenv("AI_SEARCH_TOP_K")
    if raw is None:
        return DEFAULT_TOP
    value = raw.strip()
    if not value:
        raise ValueError("AI_SEARCH_TOP_K is set but empty")
    try:
        parsed = int(value)
    except ValueError:
        raise ValueError(f"AI_SEARCH_TOP_K must be a positive integer; got {value!r}") from None
    if not (MIN_TOP <= parsed <= MAX_TOP):
        raise ValueError(
            f"AI_SEARCH_TOP_K must be between {MIN_TOP} and {MAX_TOP}; got {value!r}"
        )
    return parsed


async def search_minutes(query: str, top: int | None = None) -> dict[str, Any]:
    """Search the mixed internal corpus.

    The retrieval mode is AI_SEARCH_QUERY_TYPE (default: ``semantic`` — BM25
    lexical candidates reranked semantically, no vector query). Vector recall
    is opt-in: ``vector``, ``vector_simple_hybrid`` and ``vector_semantic_hybrid``
    all issue the integrated-vectorizer query, so the search service embeds
    the query itself and this stays a single round trip rather than an
    embedding call followed by a search call.

    The configured index supplies the board and executive meeting minutes.
    """
    query = (query or "").strip()
    if not query:
        return {"error": "query is required"}

    client = get_search_client()
    if client is None:
        # Unconfigured is a distinct outcome from "searched and found
        # nothing" — it must not be reported as an empty result set, which
        # would read to the model (and the user) as "no such passages exist".
        return {
            "error": (
                "The internal document index is not configured on this deployment "
                "(AZURE_SEARCH_ENDPOINT / SEARCH_INDEX_NAME are unset)."
            )
        }

    try:
        query_type = _read_query_type()
        snippet_chars = _read_snippet_chars()
        default_top = _read_default_top()
    except ValueError as e:
        logger.warning(f"search_minutes configuration error: {e}")
        return {"error": f"Search is misconfigured: {e}"}

    if top is None:
        top = default_top
    else:
        try:
            top = int(top)
        except (TypeError, ValueError):
            return {"error": f"top must be an integer between {MIN_TOP} and {MAX_TOP}; got {top!r}"}
        if not (MIN_TOP <= top <= MAX_TOP):
            return {"error": f"top must be between {MIN_TOP} and {MAX_TOP}; got {top}"}

    uses_vector = query_type in _VECTOR_QUERY_TYPES
    uses_semantic = query_type in _SEMANTIC_QUERY_TYPES

    is_vector_only = query_type == AzureAISearchQueryType.VECTOR.value
    search_kwargs: dict[str, Any] = {
        # Pure "vector" mode must omit the lexical query entirely: passing
        # search_text alongside vector_queries makes the service also rank on
        # BM25 text, which is exactly what vector_simple_hybrid already does.
        # Sending it here would silently collapse "vector" into a hybrid mode.
        "search_text": None if is_vector_only else query,
        "top": top,
        "select": ["id", "title", "documentType", "meeting_date", "content", "source"],
    }
    if uses_vector:
        search_kwargs["vector_queries"] = [
            VectorizableTextQuery(
                text=query, k_nearest_neighbors=K_NEAREST, fields=VECTOR_FIELD
            )
        ]
    if uses_semantic:
        # Semantic configuration only applies to modes that actually rerank
        # semantically; sending it otherwise is a request the service ignores
        # at best and rejects at worst depending on index configuration.
        search_kwargs["query_type"] = "semantic"
        search_kwargs["semantic_configuration_name"] = SEMANTIC_CONFIG
    else:
        search_kwargs["query_type"] = "simple"

    started = time.monotonic()
    try:
        results = await client.search(**search_kwargs)
        passages = []
        async for doc in results:
            content = doc.get("content") or ""
            truncated = len(content) > snippet_chars
            passage: dict[str, Any] = {
                "id": doc.get("id") or "",
                "source": doc.get("source") or "",
                "title": display_document_title(
                    doc.get("title") or "", doc.get("documentType") or ""
                ),
                "type": doc.get("documentType") or "",
                "date": _format_date(doc.get("meeting_date")),
                "extract": content[:snippet_chars],
            }
            if truncated:
                passage["truncated"] = True
                passage["original_chars"] = len(content)
            passages.append(passage)
    except Exception as e:
        # Never raise into the tool loop: the model handles "nothing found"
        # gracefully and can say so, but an exception strands the turn with the
        # user listening to silence.
        logger.warning(
            f"search_minutes failed [{fingerprint(query)}]: {type(e).__name__}: {e}"
        )
        return {"error": f"The internal document search failed: {type(e).__name__}"}

    elapsed_ms = (time.monotonic() - started) * 1000
    logger.info(
        f"[TOOL] search_minutes {elapsed_ms:.0f}ms  n={len(passages)}  "
        f"mode={query_type}  top={top}  [{fingerprint(query)}]"
    )
    if not passages:
        return {
            "passages": [],
            "note": "No matching passages in the meeting minutes.",
        }
    note = BREVITY_NOTE
    if any(p.get("truncated") for p in passages):
        note = f"{BREVITY_NOTE} Some passages were truncated; original_chars records the full length."
    return {"passages": passages, "note": note}
