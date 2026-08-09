"""Can this deployment actually reach Web IQ, and is it asking the right way?

Runs the **production** ``search_web`` ([`backend/voice/tools.py`](../backend/voice/tools.py))
against the live service, then A/B-tests the one request parameter that decides
whether the model reads the answer or the page furniture.

    uv run python scripts/smoke_webiq_search.py
    uv run python scripts/smoke_webiq_search.py "MTN group results"

Reads ``WEBIQ_API_KEY`` from the environment or ``.env``. With no key it falls
through to the managed identity, which only works where the identity's client id
has been bound in the Web IQ portal — see
[auth.md](../docs/auth.md#the-keyless-web-iq-route-needs-one-thing-azure-cannot-give-you).

**The key is never printed**, and neither is any part of it.

Why the A/B is here. The web API takes a ``contentFormat``, and two of its values
look interchangeable until you read what they mean:

    passage   a model extracts the paragraphs of the page most relevant to the
              query, up to maxLength
    text      the full document in plain text, from the top

With ``text`` and an 800-character cap, "the top" is a cookie banner and a nav
menu. This script prints both for the same query so the difference is visible
rather than argued about.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(override=True)

from backend.voice import tools  # noqa: E402

DEFAULT_QUERY = "MTN group latest financial results"
PREVIEW = 260


def rule(title: str) -> None:
    print()
    print(title)
    print("-" * 70)


async def show_route() -> None:
    rule("1. which credential this run will use")
    if os.getenv("WEBIQ_API_KEY"):
        print("  API key found - authenticating with the x-apikey header.")
        print("  (the key itself is never printed by this script)")
    else:
        print("  No API key. Falling back to the managed identity, which needs")
        print("  its client id bound in the Web IQ portal to be accepted.")
    available = await tools.web_search_available()
    print(f"  search_web would be offered to the model: {available}")
    if not available:
        print()
        print("  Stopping here - the tool is not usable on this machine, so the")
        print("  calls below would only repeat that. Set WEBIQ_API_KEY to test.")
    return available


async def show_production_call(query: str) -> bool:
    rule(f"2. the production search_web(), allow-list and all")
    domains = tools._allowed_domains()
    print(f"  allow-list: {domains or '(none - open web)'}")
    print(f"  query sent: {tools.build_query(query, domains)[:200]}")
    print()

    result = await tools.search_web(query)
    if "error" in result:
        print(f"  FAILED: {result['error']}")
        print()
        print("  A 401/403 here means the credential was rejected: an invalid key,")
        print("  or an identity whose client id is not bound in the Web IQ portal.")
        return False

    results = result.get("results", [])
    print(f"  {len(results)} result(s) after host filtering")
    for i, r in enumerate(results, 1):
        print()
        print(f"  [{i}] {r.get('title', '')}")
        print(f"      {r.get('url', '')}")
        if r.get("source") or r.get("published"):
            print(f"      {r.get('source', '')}  {r.get('published', '')}".rstrip())
        extract = (r.get("extract") or "").strip().replace("\n", " ")
        print(f"      {extract[:PREVIEW]}")
    return True


async def compare_formats(query: str) -> None:
    rule("3. contentFormat: passage vs text, same query, same budget")
    print("  Both requests are identical except for contentFormat. Read the two")
    print("  extracts: one should be about the query, the other about the site.")

    client = await tools._get_web_client()
    headers = await tools._auth_headers()
    domains = tools._allowed_domains()

    for fmt in ("passage", "text"):
        body = {
            "query": tools.build_query(query, domains),
            "maxResults": 1,
            "language": os.getenv("WEBIQ_LANGUAGE", "en"),
            "region": os.getenv("WEBIQ_REGION", "ZA"),
            "contentFormat": fmt,
            "maxLength": tools.WEB_MAX_LENGTH,
        }
        try:
            resp = await client.post("/search/web", json=body, headers=headers)
        except Exception as e:
            print(f"\n  {fmt}: request failed: {type(e).__name__}: {e}")
            continue
        if resp.status_code != 200:
            print(f"\n  {fmt}: HTTP {resp.status_code}")
            continue
        hits = (resp.json() or {}).get("webResults") or []
        print()
        print(f"  --- contentFormat={fmt} ---")
        if not hits:
            print("  (no results)")
            continue
        hit = hits[0]
        print(f"  {hit.get('title', '')}")
        content = (hit.get("content") or "").strip().replace("\n", " ")
        print(f"  {content[:PREVIEW]}")


async def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY
    print(f"Web IQ smoke test - query: {query!r}")

    try:
        if not await show_route():
            return 1
        if not await show_production_call(query):
            return 1
        await compare_formats(query)
    finally:
        await tools.close_web_client()

    print()
    print("Done. If the passage extract reads like an answer and the text extract")
    print("reads like a menu, contentFormat=passage is doing its job.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
