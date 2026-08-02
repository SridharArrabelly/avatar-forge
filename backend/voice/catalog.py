"""Live meeting catalogue, fetched from Azure AI Search and cached in-process.

The Voice Live handler injects this catalogue as a SystemMessageItem at
session start so the model can answer first/last/count/listing questions
without calling AI Search and can phrase precise content searches using
exact meeting dates.

The catalogue is fetched once per process-lifetime window (TTL ~5 min by
default) and shared across sessions. New ingest runs become visible to
new sessions within the TTL; in-flight sessions keep their snapshot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Optional

from azure.core.credentials import AzureKeyCredential
from azure.search.documents.aio import SearchClient

from .auth import create_credential

logger = logging.getLogger(__name__)

# Cache TTL: short enough that newly-ingested meetings appear within a
# few minutes; long enough that bursts of session connects share one fetch.
_CACHE_TTL_S = int(os.getenv("MEETING_CATALOG_TTL_S", "900"))

# (value, monotonic-timestamp) tuple. value is None until first successful fetch.
_cache: tuple[Optional[str], float] = (None, 0.0)
_cache_lock = asyncio.Lock()


def _format_date(iso_date: str) -> str:
    """`2026-02-15T00:00:00Z` → `15 February 2026` (drops leading zero on day)."""
    try:
        # Strip Z / +00:00 / sub-second suffix; just need the date.
        date_part = iso_date.split("T", 1)[0]
        dt = datetime.strptime(date_part, "%Y-%m-%d")
        return f"{dt.day} {dt.strftime('%B %Y')}"
    except Exception:
        return iso_date


# Single process-wide SearchClient + credential. azure-identity caches AAD
# tokens internally per-scope, and SearchClient holds a long-lived aiohttp
# session, so reusing one instance saves ~200-500ms per refresh (no new TLS
# handshake, no fresh token chain walk). Shared with backend/voice/tools.py so
# a grounding call on the answer path never pays that cost either.
_search_client: Optional[SearchClient] = None


def get_search_client() -> Optional[SearchClient]:
    global _search_client
    if _search_client is not None:
        return _search_client
    endpoint = os.getenv("AZURE_SEARCH_ENDPOINT", "").strip().rstrip("/")
    index = os.getenv("SEARCH_INDEX_NAME", "").strip()
    if not endpoint or not index:
        logger.warning(
            "Catalog: AZURE_SEARCH_ENDPOINT or SEARCH_INDEX_NAME not set; "
            "catalogue injection disabled."
        )
        return None
    api_key = os.getenv("AZURE_SEARCH_API_KEY", "").strip()
    credential = AzureKeyCredential(api_key) if api_key else create_credential("")
    _search_client = SearchClient(endpoint=endpoint, index_name=index, credential=credential)
    return _search_client


async def _fetch_catalog() -> Optional[str]:
    """Single round-trip fetch — pulls title + meeting_date for one chunk per
    meeting (using the indexed ``chunk_index`` field as a per-meeting key),
    formats into a model-friendly catalogue.

    Filtering on ``chunk_index eq 0`` returns exactly one document per
    meeting (the indexer assigns ``chunk_index`` 0..N-1 per source file
    via ``enumerate``), so the service scans ~10 docs instead of the full
    ~100+ chunks. This is dramatically cheaper than ``search_text='*'``
    with ``top=1000`` followed by client-side dedup.
    """
    client = get_search_client()
    if client is None:
        return None
    started = time.monotonic()
    try:
        # Filter pushes work to the search service; we get one row per meeting.
        # `top=200` is a safety margin — we expect ~10 rows but a future
        # bulk-ingest could exceed that. The filter does the real work.
        results = await client.search(
            search_text="*",
            filter="chunk_index eq 0",
            select=["title", "meeting_date"],
            top=200,
        )

        by_date: dict[str, str] = {}
        async for r in results:
            date_iso = r.get("meeting_date")
            title = r.get("title") or ""
            if not date_iso:
                continue
            # First title wins per date — they should all be the same anyway.
            if date_iso not in by_date:
                by_date[date_iso] = title

        if not by_date:
            logger.warning("Catalog: AI Search returned 0 meetings.")
            return None

        # Sort ascending by date so "first" is the first bullet, "last" the last.
        ordered = sorted(by_date.items(), key=lambda kv: kv[0])

        # Preamble framing matters: without an explicit "silent reference"
        # marker, the model occasionally reads the list aloud as its first
        # utterance (especially when the agent prompt also says "read this
        # BEFORE thinking about tools"). Marking it as silent reference
        # data tells the model NOT to volunteer the list; only use when
        # the user asks a relevant question.
        # TODAY is injected per-session (not baked into the agent definition)
        # so the agent stays date-agnostic and idempotent across re-registrations.
        now = datetime.utcnow()
        today_line = f"TODAY: {now.strftime('%A, %d %B %Y')} (UTC)."
        lines = [
            "[SILENT REFERENCE DATA — do not speak this aloud, do not "
            "summarise it, do not volunteer it. Only USE it when the user "
            "asks a question that this data helps answer.]",
            "",
            today_line,
            "",
            "MEETINGS LIST — the complete authoritative roster of board / "
            "executive meetings currently in the AI Search index. Use this "
            "to answer first / last / count / listing questions directly "
            "(no tool call), and to phrase precise content searches by "
            "exact meeting date."
        ]
        for date_iso, title in ordered:
            pretty = _format_date(date_iso)
            if title and title.strip():
                lines.append(f"- {pretty}  ({title.strip()})")
            else:
                lines.append(f"- {pretty}")
        lines.append(
            f"Total: {len(ordered)} meeting(s). Earliest is the first entry, "
            "latest is the last entry."
        )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        catalog = "\n".join(lines)
        logger.info(
            "Catalog: fetched %d meetings in %dms (%d chars)",
            len(ordered), elapsed_ms, len(catalog),
        )
        return catalog
    except Exception as e:
        logger.warning("Catalog: fetch failed: %s", e)
        return None
    # Note: do NOT close `client` here — it is a process-wide singleton
    # reused across refreshes. It is closed on app shutdown if needed.


async def get_meeting_catalog(*, force_refresh: bool = False) -> Optional[str]:
    """Return the cached meeting catalogue string, refreshing if stale.

    Returns None if the index is unreachable AND we have no prior cache.
    On a refresh failure with a stale cache, the stale value is returned
    (better than nothing for the session).
    """
    global _cache
    now = time.monotonic()
    cached_value, cached_at = _cache
    if not force_refresh and cached_value is not None and (now - cached_at) < _CACHE_TTL_S:
        return cached_value

    async with _cache_lock:
        cached_value, cached_at = _cache
        # Re-check under lock — another waiter may have populated it.
        if not force_refresh and cached_value is not None and (time.monotonic() - cached_at) < _CACHE_TTL_S:
            return cached_value

        fresh = await _fetch_catalog()
        if fresh is not None:
            _cache = (fresh, time.monotonic())
            return fresh
        # Keep stale on failure.
        return cached_value


async def prewarm_catalog() -> None:
    """Fire-and-forget startup hook — populates the cache so the first
    session connect doesn't pay the ~50-100ms fetch cost."""
    try:
        await get_meeting_catalog(force_refresh=True)
    except Exception as e:
        # Never block startup on this.
        logger.warning("Catalog: pre-warm failed (will retry on first session): %s", e)


async def close_search_client() -> None:
    """Close the process-wide SearchClient, if it was created.

    `azure.search.documents.aio.SearchClient` holds an internal
    `aiohttp.ClientSession`. If we don't await its `close()` on shutdown,
    the asyncio loop logs:

        ERROR asyncio Unclosed client session
        client_session: <aiohttp.client.ClientSession object at 0x...>

    Call this from the FastAPI lifespan shutdown phase BEFORE
    `close_credential()`, since SearchClient uses the credential.
    """
    global _search_client
    if _search_client is None:
        return
    client = _search_client
    _search_client = None
    try:
        await client.close()
        logger.info("Catalog: SearchClient closed")
    except Exception as e:
        logger.warning("Catalog: error closing SearchClient (ignored): %s", e)
