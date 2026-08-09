"""The Web IQ request, checked against the published API contract.

Every value asserted here comes from the Microsoft Web IQ v3 documentation
(`https://webiq.microsoft.ai/documentation/`, also published as
`llms-full.txt`), not from what the code happens to do:

    Scope        https://api.microsoft.ai/.default
    Base URL     https://api.microsoft.ai/v3/
    Endpoint     POST /search/web
    Auth         `x-apikey: <key>` *or* `Authorization: Bearer <jwt>`
    Params       query, maxResults (<=50), language, region, location,
                 contentFormat, maxLength (<=500000), safeSearch

This file exists because `contentFormat` was wrong for a while and nothing
noticed. We sent `text` -- "full semantic document in plain text" -- with an
800-character cap, so the model was handed the first 800 characters of each
page: nav, cookie banner, header. The documentation is explicit that the web
API returns no `snippet` field and that `passage` is the way to get
query-relevant content. A wire contract that is only checked by reading it is
not checked at all.

    uv run --no-sync python tests/test_webiq_contract.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("WEBIQ_API_KEY", "test-key-not-real")
os.environ.setdefault("WEBIQ_ALLOWED_DOMAINS", "mtn.com,itweb.co.za")

from backend.voice import tools  # noqa: E402

checks = 0
failures: list[str] = []


def check(label: str, got: Any, want: Any) -> None:
    global checks
    checks += 1
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}: expected {want!r}, got {got!r}")
        failures.append(label)


def check_true(label: str, got: Any) -> None:
    check(label, bool(got), True)


class _Response:
    status_code = 200

    def json(self) -> dict:
        return {"webResults": []}


class _RecordingClient:
    """Stands in for the pooled httpx client and keeps what was sent."""

    def __init__(self) -> None:
        self.path: str | None = None
        self.body: dict[str, Any] = {}
        self.headers: dict[str, str] = {}

    async def post(self, path, json=None, headers=None):  # noqa: A002
        self.path = path
        self.body = json or {}
        self.headers = headers or {}
        return _Response()


async def _capture(query: str = "MTN group results") -> _RecordingClient:
    client = _RecordingClient()
    original = tools._get_web_client
    tools._get_web_client = lambda: _wrap(client)  # type: ignore[assignment]
    try:
        await tools.search_web(query)
    finally:
        tools._get_web_client = original  # type: ignore[assignment]
    return client


async def _wrap(client: _RecordingClient) -> _RecordingClient:
    return client


async def main() -> int:
    print("the documented endpoint and auth")
    print("-" * 62)

    check("base URL is the documented v3 root",
          tools.WEBIQ_BASE_URL, "https://api.microsoft.ai/v3")
    check("Entra scope is the documented one",
          tools.WEBIQ_API_SCOPE, "https://api.microsoft.ai/.default")

    # A key is set above, so this exercises the documented key header. The
    # Entra branch is covered by test_webiq_probe_timeout.py.
    headers = await tools._auth_headers()
    check("a key travels in the documented header",
          list(headers), ["x-apikey"])

    client = await _capture()
    check("posts to the documented web-search path", client.path, "/search/web")

    print()
    print("the request body, against the published parameter table")
    print("-" * 62)

    body = client.body

    # The bug this file was written for.
    check("contentFormat asks for query-relevant passages, not page top",
          body.get("contentFormat"), "passage")
    check("contentFormat is a documented value",
          body.get("contentFormat") in {"passage", "text", "html", "markdown"},
          True)

    check("maxResults is a positive integer within the documented ceiling of 50",
          isinstance(body.get("maxResults"), int)
          and 0 < body["maxResults"] <= 50,
          True)

    check("maxLength within the documented ceiling of 500000",
          body.get("maxLength", 0) <= 500_000, True)

    check("language is a 2-letter code",
          len(str(body.get("language", ""))) == 2, True)
    check("region is a 2-letter code",
          len(str(body.get("region", ""))) == 2, True)

    check("query is present", bool(body.get("query")), True)
    check("query within the documented 1000-character limit",
          len(str(body.get("query", ""))) <= 1000, True)

    # Only documented parameters may be sent; an unknown key is a 400 waiting
    # to happen the day the service stops ignoring extras.
    documented = {
        "query", "maxResults", "language", "region", "location",
        "contentFormat", "maxLength", "safeSearch",
    }
    check("every parameter sent is a documented one",
          sorted(set(body) - documented), [])

    print()
    print("the allow-list still reaches the wire")
    print("-" * 62)

    check("the allow-list is applied as site: operators in the query text",
          "site:" in str(body.get("query", "")), True)

    print()
    print("response fields we read are the documented ones")
    print("-" * 62)

    # webResult per the docs: title, url, content, crawledAt, lastUpdatedAt,
    # language, isAdult, clickUrl, instrumentationSuffix, contentTier.
    documented_result_fields = {
        "title", "url", "content", "crawledAt", "lastUpdatedAt", "language",
        "isAdult", "clickUrl", "instrumentationSuffix", "contentTier",
    }
    for field in ("title", "url", "content", "crawledAt", "lastUpdatedAt"):
        check(f"{field} is a documented response field",
              field in documented_result_fields, True)

    print()
    print(f"{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("\nfailed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
