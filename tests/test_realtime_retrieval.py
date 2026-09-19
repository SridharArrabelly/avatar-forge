"""Offline parity checks for ``backend/voice/tools.py::search_minutes``.

No Azure calls: the Search client is a fake that records the kwargs it was
called with. Run from the repository root:

    uv run python tests/test_realtime_retrieval.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backend.voice.tools as tools  # noqa: E402


class FakeResults:
    def __init__(self, rows: list[dict]):
        self._rows = list(rows)

    def __aiter__(self):
        self._iter = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeSearchClient:
    def __init__(self, rows: list[dict] | None = None, error: Exception | None = None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.kwargs: dict = {}
        self.calls = 0

    async def search(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return FakeResults(self.rows)


def _row(content: str = "hello world", **overrides) -> dict:
    row = {
        "id": "doc-1",
        "title": "Board Meeting 15 March 2026",
        "documentType": "MeetingMinutes",
        "meeting_date": "2026-03-15T00:00:00Z",
        "content": content,
        "source": "Board Meeting 15 March 2026.docx",
    }
    row.update(overrides)
    return row


async def _run(client: FakeSearchClient, query: str = "what was decided", **kwargs):
    original = tools.get_search_client
    tools.get_search_client = lambda: client
    try:
        return await tools.search_minutes(query, **kwargs)
    finally:
        tools.get_search_client = original


class QueryTypeMatrixTests(unittest.IsolatedAsyncioTestCase):
    """The five AzureAISearchQueryType values map to distinct SDK kwargs."""

    async def test_simple_uses_no_vector_no_semantic(self):
        with patch.dict(os.environ, {"AI_SEARCH_QUERY_TYPE": "simple"}):
            client = FakeSearchClient([_row()])
            result = await _run(client)
        self.assertNotIn("vector_queries", client.kwargs)
        self.assertEqual(client.kwargs.get("query_type"), "simple")
        self.assertNotIn("semantic_configuration_name", client.kwargs)
        self.assertIn("passages", result)

    async def test_semantic_is_the_default_and_uses_no_vector(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_SEARCH_QUERY_TYPE", None)
            client = FakeSearchClient([_row()])
            await _run(client)
        self.assertNotIn("vector_queries", client.kwargs)
        self.assertEqual(client.kwargs.get("query_type"), "semantic")
        self.assertEqual(client.kwargs.get("semantic_configuration_name"), tools.SEMANTIC_CONFIG)

    async def test_vector_uses_vector_query_and_simple_ranking(self):
        with patch.dict(os.environ, {"AI_SEARCH_QUERY_TYPE": "vector"}):
            client = FakeSearchClient([_row()])
            await _run(client)
        self.assertIn("vector_queries", client.kwargs)
        self.assertEqual(client.kwargs.get("query_type"), "simple")
        self.assertNotIn("semantic_configuration_name", client.kwargs)

    async def test_vector_simple_hybrid_uses_both_without_semantic_config(self):
        with patch.dict(os.environ, {"AI_SEARCH_QUERY_TYPE": "vector_simple_hybrid"}):
            client = FakeSearchClient([_row()])
            await _run(client)
        self.assertIn("vector_queries", client.kwargs)
        self.assertEqual(client.kwargs.get("query_type"), "simple")
        self.assertNotIn("semantic_configuration_name", client.kwargs)

    async def test_vector_semantic_hybrid_uses_both_with_semantic_config(self):
        with patch.dict(os.environ, {"AI_SEARCH_QUERY_TYPE": "vector_semantic_hybrid"}):
            client = FakeSearchClient([_row()])
            await _run(client)
        self.assertIn("vector_queries", client.kwargs)
        self.assertEqual(client.kwargs.get("query_type"), "semantic")
        self.assertEqual(client.kwargs.get("semantic_configuration_name"), tools.SEMANTIC_CONFIG)


class SettingsValidationTests(unittest.IsolatedAsyncioTestCase):
    """Blank/invalid explicit settings fail safely instead of substituting a default."""

    async def test_blank_query_type_is_a_safe_error(self):
        with patch.dict(os.environ, {"AI_SEARCH_QUERY_TYPE": "   "}):
            client = FakeSearchClient([_row()])
            result = await _run(client)
        self.assertIn("error", result)
        self.assertEqual(client.calls, 0)

    async def test_unrecognised_query_type_is_a_safe_error(self):
        with patch.dict(os.environ, {"AI_SEARCH_QUERY_TYPE": "hybrid"}):
            client = FakeSearchClient([_row()])
            result = await _run(client)
        self.assertIn("error", result)
        self.assertEqual(client.calls, 0)

    async def test_invalid_snippet_chars_is_a_safe_error(self):
        with patch.dict(os.environ, {"AI_SEARCH_SNIPPET_CHARS": "not-a-number"}):
            client = FakeSearchClient([_row()])
            result = await _run(client)
        self.assertIn("error", result)
        self.assertEqual(client.calls, 0)

    async def test_zero_snippet_chars_is_a_safe_error(self):
        with patch.dict(os.environ, {"AI_SEARCH_SNIPPET_CHARS": "0"}):
            client = FakeSearchClient([_row()])
            result = await _run(client)
        self.assertIn("error", result)

    async def test_out_of_range_default_top_is_a_safe_error(self):
        with patch.dict(os.environ, {"AI_SEARCH_TOP_K": "99"}):
            client = FakeSearchClient([_row()])
            result = await _run(client)
        self.assertIn("error", result)
        self.assertEqual(client.calls, 0)

    async def test_model_supplied_top_out_of_range_is_a_safe_error_not_a_clamp(self):
        client = FakeSearchClient([_row()])
        result = await _run(client, top=20)
        self.assertIn("error", result)
        self.assertEqual(client.calls, 0)

    async def test_model_supplied_top_is_honoured_over_the_env_default(self):
        with patch.dict(os.environ, {"AI_SEARCH_TOP_K": "5"}):
            client = FakeSearchClient([_row()])
            await _run(client, top=2)
        self.assertEqual(client.kwargs.get("top"), 2)


class ContentAndMetadataTests(unittest.IsolatedAsyncioTestCase):
    """Full sections pass through untouched; oversized content is marked explicitly."""

    async def test_full_section_under_the_cap_is_not_truncated(self):
        content = "x" * 11000
        client = FakeSearchClient([_row(content=content)])
        result = await _run(client)
        passage = result["passages"][0]
        self.assertEqual(passage["extract"], content)
        self.assertNotIn("truncated", passage)

    async def test_oversized_content_is_explicitly_truncated(self):
        with patch.dict(os.environ, {"AI_SEARCH_SNIPPET_CHARS": "100"}):
            content = "y" * 500
            client = FakeSearchClient([_row(content=content)])
            result = await _run(client)
        passage = result["passages"][0]
        self.assertEqual(len(passage["extract"]), 100)
        self.assertTrue(passage["truncated"])
        self.assertEqual(passage["original_chars"], 500)
        self.assertIn("truncated", result["note"])

    async def test_id_and_source_are_additive_alongside_existing_fields(self):
        client = FakeSearchClient([_row(id="doc-77", source="Board Meeting.docx")])
        result = await _run(client)
        passage = result["passages"][0]
        for field in ("id", "source", "title", "type", "date", "extract"):
            self.assertIn(field, passage)
        self.assertEqual(passage["id"], "doc-77")
        self.assertEqual(passage["source"], "Board Meeting.docx")

    async def test_select_requests_id_and_source(self):
        client = FakeSearchClient([_row()])
        await _run(client)
        self.assertIn("id", client.kwargs.get("select", []))
        self.assertIn("source", client.kwargs.get("select", []))


class SourceErrorSafetyTests(unittest.IsolatedAsyncioTestCase):
    """Unconfigured, empty and failed sources are three distinct outcomes."""

    async def test_unconfigured_client_is_an_error_not_empty_results(self):
        original = tools.get_search_client
        tools.get_search_client = lambda: None
        try:
            result = await tools.search_minutes("anything")
        finally:
            tools.get_search_client = original
        self.assertIn("error", result)
        self.assertNotIn("passages", result)

    async def test_genuinely_empty_results_are_reported_as_empty(self):
        client = FakeSearchClient([])
        result = await _run(client)
        self.assertEqual(result["passages"], [])
        self.assertIn("note", result)

    async def test_search_failure_is_a_safe_error_without_raising(self):
        client = FakeSearchClient(error=RuntimeError("boom"))
        result = await _run(client)
        self.assertIn("error", result)

    async def test_search_failure_does_not_log_the_raw_query(self, ):
        client = FakeSearchClient(error=RuntimeError("boom"))
        with self.assertLogs(tools.logger, level="WARNING") as captured:
            await _run(client, query="a very specific secret question")
        joined = " ".join(captured.output)
        self.assertNotIn("a very specific secret question", joined)


if __name__ == "__main__":
    unittest.main()
