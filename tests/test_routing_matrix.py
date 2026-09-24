"""Offline checks for retrieval modes, resume guards, pacing, and routing scores."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import APIStatusError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bench_routing_matrix as matrix


class Clock:
    now = 0.0

    def sleep(self, seconds):
        assert seconds > 0
        self.now += seconds


class Stream:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return iter(self.events)

    def __exit__(self, *args):
        return False


def client(events):
    return SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: Stream(events)))


def main():
    source = {
        "kind": "prompt", "model": "live-model", "instructions": "Unchanged prompt",
        "reasoning": {"effort": "none"},
        "tools": [
            {"type": "azure_ai_search", "azure_ai_search": {"indexes": [{"top_k": 8, "index_name": "same-index", "query_type": "semantic", "project_connection_id": "/connections/search", "filter": "year eq 2026"}]}},
            {"type": "bing_custom_search_preview", "bing_custom_search_preview": {"search_configurations": [{"count": 8, "instance_name": "same-sites", "market": "en-ZA", "set_lang": "en"}]}},
        ],
        "text": {"format": {"type": "text"}},
    }
    definition = matrix.benchmark_definition(source, "comparison-model", "low", 5)
    assert source["model"] == "live-model"
    assert source["reasoning"]["effort"] == "none"
    assert source["tools"][0]["azure_ai_search"]["indexes"][0]["top_k"] == 8
    assert definition["model"] == "comparison-model"
    assert definition["reasoning"]["effort"] == "low"
    assert definition["instructions"] == source["instructions"]
    expected = copy.deepcopy(source)
    expected["model"] = "comparison-model"
    expected["reasoning"] = {"effort": "low"}
    expected["tools"][0]["azure_ai_search"]["indexes"][0]["top_k"] = 5
    expected["tools"][1]["bing_custom_search_preview"]["search_configurations"][0]["count"] = 5
    assert definition == expected
    try:
        matrix.benchmark_definition({**source, "tools": []}, "comparison-model", "none", 8)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Missing hosted tools accepted")
    print("PASS model override changes only the isolated benchmark definition")

    expected = copy.deepcopy(source)
    expected["model"] = "comparison-model"
    expected["reasoning"] = {"effort": "low"}
    fixed = matrix.benchmark_definition(source, "comparison-model", "low", None)
    assert fixed == expected
    assert fixed["tools"] == source["tools"]
    fixed["tools"][0]["azure_ai_search"]["indexes"][0]["index_name"] = "local-change"
    assert source["tools"][0]["azure_ai_search"]["indexes"][0]["index_name"] == "same-index"
    search_only = {**source, "tools": source["tools"][:1]}
    assert matrix.benchmark_definition(search_only, "comparison-model", "none", None)["tools"] == search_only["tools"]
    assert matrix.configuration_label("none", None) == "none_fixed"
    assert matrix.configuration_label("low", None) == "low_fixed"
    assert matrix.configuration_label("none", 5) == "none_k5"
    print("PASS fixed retrieval preserves all tool fields and optional tool absence without aliasing")

    cli = ["--env-file", "private.env", "--output-dir", "evidence", "--model", "comparison-model"]
    assert matrix.parse_args(cli).breadths == [5, 8]
    assert not matrix.parse_args(cli).fixed_retrieval
    assert matrix.parse_args([*cli, "--breadths", "8"]).breadths == [8]
    assert matrix.parse_args([*cli, "--fixed-retrieval"]).fixed_retrieval
    assert matrix.parse_args([*cli, "--fixed-retrieval"]).breadths is None
    for flags in (
        ["--fixed-retrieval", "--breadths", "5", "8"],
        ["--breadths", "5", "--fixed-retrieval"],
    ):
        with redirect_stderr(StringIO()):
            try:
                matrix.parse_args([*cli, *flags])
            except SystemExit as exc:
                assert exc.code == 2
            else:
                raise AssertionError("Explicit breadths were silently ignored in fixed mode")
    print("PASS fixed/swept CLI defaults and mutual exclusion")

    manifest = {
        "model": "comparison-model", "retrieval_mode": "fixed",
        "project_endpoint": "https://example.invalid", "source_agent": "evaluation-agent",
        "source_definition_sha256": hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(),
    }
    resume_args = {
        "model": "comparison-model", "fixed_retrieval": True,
        "project_endpoint": manifest["project_endpoint"], "source_agent": manifest["source_agent"],
    }
    matrix.validate_resume(manifest, source, source, **resume_args)
    swept_manifest = {**manifest, "retrieval_mode": "swept"}
    matrix.validate_resume(swept_manifest, source, source, **{**resume_args, "fixed_retrieval": False})
    legacy_manifest = {key: value for key, value in manifest.items() if key != "retrieval_mode"}
    matrix.validate_resume(legacy_manifest, source, source, **{**resume_args, "fixed_retrieval": False})
    for previous, current, changed_args in (
        (swept_manifest, source, {}),
        (legacy_manifest, source, {}),
        (manifest, source, {"fixed_retrieval": False}),
        (manifest, source, {"model": "different-model"}),
        (manifest, source, {"project_endpoint": "https://different.invalid"}),
        (manifest, source, {"source_agent": "different-agent"}),
        (manifest, {**source, "instructions": "changed"}, {}),
        (manifest, definition, {}),
        ({**manifest, "source_definition_sha256": "inconsistent saved definition"}, source, {}),
    ):
        try:
            matrix.validate_resume(previous, source, current, **{**resume_args, **changed_args})
        except RuntimeError:
            pass
        else:
            raise AssertionError("Incompatible source or retrieval mode accepted for resume")
    changed_tools = copy.deepcopy(source)
    changed_tools["tools"][1]["bing_custom_search_preview"]["search_configurations"][0]["count"] = 1
    try:
        matrix.validate_resume(manifest, source, changed_tools, **resume_args)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Changed source tools accepted for resume")
    print("PASS resume rejects changed source/model/mode and recognizes legacy swept manifests")

    clock = Clock()
    with patch.object(matrix.time, "monotonic", lambda: clock.now), patch.object(matrix.time, "sleep", clock.sleep):
        pacer = matrix.Pacer(interval=0, budget=240000, reserve=60000)
        for _ in range(4):
            pacer.wait()
        assert clock.now == 0
        pacer.wait()
        assert clock.now == 61
        pacer.account(90000)
        assert pacer.reserve == 90000
        assert pacer.calls[-1][1] == 90000
        pacer.wait()
        pacer.wait()
        assert clock.now == 122
    clock = Clock()
    with patch.object(matrix.time, "monotonic", lambda: clock.now), patch.object(matrix.time, "sleep", clock.sleep):
        pacer = matrix.Pacer(interval=8, budget=240000, reserve=30000)
        pacer.wait()
        pacer.wait()
        assert clock.now == 8
        pacer.account(1000)
        assert pacer.calls[-1][1] == 30000
    print("PASS pacing preserves spacing, reserves, and the rolling budget")

    final = {"status": "completed", "usage": {"total_tokens": 9000}}
    events = [
        SimpleNamespace(type="response.output_item.done", item=SimpleNamespace(type="azure_ai_search_call")),
        SimpleNamespace(type="response.output_text.delta", delta="Grounded answer"),
        SimpleNamespace(type="response.completed", response=SimpleNamespace(model_dump=lambda **kwargs: final)),
    ]
    result = matrix.stream_turn(client(events), "catalogue", "question")
    assert result["tools"] == ["azure_ai_search_call"]
    assert result["text"] == "Grounded answer"
    assert result["first_token_s"] is not None
    assert result["usage"]["total_tokens"] == 9000
    for terminal in ("response.failed", "response.incomplete", "error"):
        try:
            matrix.stream_turn(client([SimpleNamespace(type=terminal, model_dump=lambda **kwargs: {"type": terminal})]), "catalogue", "question")
        except matrix.ResponseFailure:
            pass
        else:
            raise AssertionError(f"{terminal} incorrectly counted as success")
    try:
        matrix.stream_turn(client(events[:-1]), "catalogue", "question")
    except matrix.ResponseFailure:
        pass
    else:
        raise AssertionError("Truncated stream incorrectly counted as success")
    print("PASS stream capture rejects incomplete or failed responses")

    for status, retryable in ((400, False), (401, False), (429, True), (503, True)):
        response = httpx.Response(status, headers={"retry-after-ms": "65000"}, request=httpx.Request("POST", "https://example.invalid"))
        _, actual, delay = matrix.error_details(APIStatusError("test", response=response, body={"error": "test"}))
        assert actual == retryable
        assert delay == 65
    print("PASS retries honor Retry-After and distinguish permanent errors")

    completed = {
        "status": "completed", "routing_ok": True, "group": "minutes",
        "first_token_s": 2.0, "completion_s": 3.0, "attempt_errors": [],
        "usage": {"total_tokens": 100},
    }
    records = [
        completed,
        {**completed, "group": "boundary", "routing_ok": False},
        {"status": "error", "group": "web", "attempt_errors": [{"attempt": 1}]},
    ]
    summary = matrix.summarize(records)
    assert summary["routing_correct"] == 1
    assert summary["attempted_turns"] == 3
    assert summary["completed"] == 2
    assert summary["errors"] == 1
    assert summary["historical10_correct"] == 1
    assert summary["historical10_total"] == 2
    assert summary["first_token_s"]["n"] == 2
    assert summary["retries"] == 1
    print("PASS errors stay in score denominators but out of inference latency")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
