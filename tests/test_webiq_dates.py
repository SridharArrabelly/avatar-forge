"""Offline checks for Web IQ date-metadata semantics in ``search_web``.

``web_search_available`` is mocked True and ``_get_web_client``/``_auth_headers``
are replaced with fakes, so this never attempts a live token acquisition or
network call.

Run from the repository root:

    uv run python tests/test_webiq_dates.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("WEBIQ_API_KEY", "test-key-not-real")
os.environ.setdefault("TRUSTED_WEB_SITES", "mtn.com")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backend.voice.tools as tools  # noqa: E402


class _Response:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _RecordingClient:
    def __init__(self, payload: dict):
        self.payload = payload

    async def post(self, path, json=None, headers=None):
        return _Response(self.payload)


def _search(payload: dict) -> dict:
    client = _RecordingClient(payload)

    async def _run():
        with patch.object(tools, "web_search_available", AsyncMock(return_value=True)), \
             patch.object(tools, "_get_web_client", AsyncMock(return_value=client)), \
             patch.object(tools, "_auth_headers", AsyncMock(return_value={"x-apikey": "test-key-not-real"})):
            return await tools.search_web("MTN group results")

    return asyncio.run(_run())


class WebIqDateMetadataTests(unittest.TestCase):
    def test_published_comes_only_from_datePublished(self):
        payload = {"webResults": [{
            "title": "MTN reports", "url": "https://mtn.com/a",
            "content": "text", "datePublished": "2026-03-01T00:00:00Z",
            "lastUpdatedAt": "2026-03-05T00:00:00Z", "crawledAt": "2026-03-06T00:00:00Z",
        }]}
        result = _search(payload)
        passage = result["results"][0]
        self.assertEqual(passage["published"], "2026-03-01")

    def test_published_is_empty_when_datePublished_is_absent(self):
        payload = {"webResults": [{
            "title": "MTN reports", "url": "https://mtn.com/a", "content": "text",
            "lastUpdatedAt": "2026-03-05T00:00:00Z", "crawledAt": "2026-03-06T00:00:00Z",
        }]}
        result = _search(payload)
        passage = result["results"][0]
        self.assertEqual(passage["published"], "")

    def test_last_updated_and_crawled_at_carry_the_full_timestamp(self):
        payload = {"webResults": [{
            "title": "MTN reports", "url": "https://mtn.com/a", "content": "text",
            "lastUpdatedAt": "2026-03-05T12:34:56Z", "crawledAt": "2026-03-06T01:02:03Z",
        }]}
        result = _search(payload)
        passage = result["results"][0]
        self.assertEqual(passage["last_updated"], "2026-03-05T12:34:56Z")
        self.assertEqual(passage["crawled_at"], "2026-03-06T01:02:03Z")

    def test_crawl_and_update_dates_never_masquerade_as_published(self):
        payload = {"webResults": [{
            "title": "MTN reports", "url": "https://mtn.com/a", "content": "text",
            "lastUpdatedAt": "2026-03-05T00:00:00Z", "crawledAt": "2026-03-06T00:00:00Z",
        }]}
        result = _search(payload)
        passage = result["results"][0]
        self.assertNotEqual(passage["published"], passage["last_updated"])
        self.assertEqual(passage["published"], "")

    def test_note_warns_against_treating_crawl_dates_as_publication_or_quote_time(self):
        payload = {"webResults": [{
            "title": "MTN reports", "url": "https://mtn.com/a", "content": "text",
        }]}
        result = _search(payload)
        self.assertIn("crawl", result["note"].lower())
        self.assertIn("publish", result["note"].lower())

    def test_budgets_hosts_and_query_filtering_are_unaffected(self):
        payload = {"webResults": [{
            "title": "MTN reports", "url": "https://mtn.com/a", "content": "text",
        }]}
        result = _search(payload)
        self.assertLessEqual(len(result["results"]), tools.WEB_MAX_RESULTS)
        self.assertLessEqual(len(result["results"][0]["extract"]), tools.WEB_MAX_LENGTH)


if __name__ == "__main__":
    unittest.main()
