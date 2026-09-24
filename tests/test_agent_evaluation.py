"""Offline evaluation-runner tests; no Azure calls or on-disk test artifacts.

Run from the repository root:
    python -B tests\\test_agent_evaluation.py
All credentials, SDK clients, clocks/backoffs and evidence files are fakes.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
import sys
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import httpx
from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError
from openai import APIError, APIStatusError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bench_agent_evaluation as evaluation


SOURCE = {
    "kind": "prompt", "model": "source-deployment", "instructions": "Exact source instructions.\n{{unchanged}}",
    "reasoning": {"effort": "none", "summary": "auto"}, "tool_choice": "auto",
    "text": {"format": {"type": "text"}},
    "tools": [
        {"type": "bing_custom_search_preview", "bing_custom_search_preview": {"search_configurations": [{
            "project_connection_id": "/connections/bing", "instance_name": "same-sites",
            "count": 3, "market": "en-ZA", "set_lang": "en",
        }]}},
        {"type": "azure_ai_search", "azure_ai_search": {"indexes": [{
            "project_connection_id": "/connections/search", "index_name": evaluation.SOURCE_INDEX,
            "query_type": "semantic", "top_k": 5, "filter": "category eq 'minutes'",
        }]}},
        {"type": "code_interpreter", "container": {"type": "auto"}},
        {"type": "function", "name": "other_tool", "parameters": {"type": "object"}},
    ],
    "additional_settings": {"nested": ["preserve", "exactly"]},
}
CFG = {
    "PROJECT_ENDPOINT": "https://example.invalid/api/projects/evaluation",
    "AGENT_NAME": evaluation.SOURCE_AGENT,
    "AZURE_SEARCH_ENDPOINT": "https://search.invalid",
    "SEARCH_INDEX_NAME": evaluation.SOURCE_INDEX,
}
CASES = [
    {"id": "minutes-1", "question": "What was decided?", "group": "minutes", "expected": "internal",
     "cohort": "legacy", "answerable": True, "oracle_context": "EXACT oracle context one."},
    {"id": "web-2", "question": "What is the latest news?", "group": "web", "expected": "external",
     "cohort": "holdout", "answerable": True, "oracle_context": "EXACT oracle context two."},
    {"id": "negative-3", "question": "An unanswerable question?", "group": "minutes", "expected": "internal",
     "cohort": "negative", "answerable": False},
]
CLI = ["--env-file", "unused.env", "--cases", "unused.json", "--output-dir", "unused-output"]


class MemoryLog(StringIO):
    def __init__(self):
        super().__init__()
        self.flush_count = 0
        self.was_closed = False

    def flush(self):
        self.flush_count += 1

    def fileno(self):
        return -1

    def close(self):
        self.was_closed = True

    def records(self):
        return [json.loads(line) for line in self.getvalue().split("\n") if line.strip()]


class MemoryEvidence:
    def __init__(self):
        self.root = ROOT.parent / "in-memory-only-evaluation"
        self.documents, self.logs = {}, {}
        self.lock = threading.Lock()

    def write_json(self, path, value):
        self.write_text(path, json.dumps(value, allow_nan=False))

    def write_text(self, path, text):
        key = str(Path(path))
        with self.lock:
            if key in self.documents or key in self.logs:
                raise FileExistsError(key)
            self.documents[key] = text

    def open_log(self, path):
        key = str(Path(path))
        with self.lock:
            if key in self.logs or key in self.documents:
                raise FileExistsError(key)
            log = self.logs[key] = MemoryLog()
            return log

    def json(self, path):
        return json.loads(self.documents[str(Path(path))])


def response_events(model="gpt-5.4", effort="none", tools=()):
    items = [{"id": f"tool-{index}", "type": tool, "status": "completed"} for index, tool in enumerate(tools)]
    message = {"id": "answer", "type": "message", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": "Grounded answer.", "annotations": []}]}
    final = {
        "id": "response-id", "status": "completed", "model": model, "reasoning": {"effort": effort},
        "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
                  "output_tokens_details": {"reasoning_tokens": 5}},
        "created_at": 100.0, "completed_at": 103.0, "error": None, "incomplete_details": None,
        "output": [*items, message],
    }
    events = [{"type": "response.created", "response": {**final, "status": "in_progress", "output": [], "usage": None, "completed_at": None}}]
    events += [{"type": "response.output_item.added", "output_index": i, "item": {**item, "status": "in_progress"}} for i, item in enumerate(items)]
    events += [{"type": "response.output_item.done", "output_index": i, "item": item} for i, item in enumerate(items)]
    events += [
        {"type": "response.output_item.added", "output_index": len(items), "item": {**message, "status": "in_progress"}},
        {"type": "response.output_text.delta", "delta": ""},
        {"type": "response.output_text.delta", "delta": "Grounded "},
        {"type": "response.output_text.delta", "delta": "answer."},
        {"type": "response.output_item.done", "output_index": len(items), "item": message},
        {"type": "response.completed", "response": final},
    ]
    return events


class FakeStream:
    def __init__(self, events, finish=lambda: None):
        self.events, self.finish, self.closed = events, finish, False

    def __enter__(self):
        return self

    def __iter__(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event

    def __exit__(self, *args):
        self.closed = True
        self.finish()


def stream_client(events):
    stream = FakeStream(events)
    return SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=stream))), stream


def status_error(status=429, headers=None):
    response = httpx.Response(status, headers=headers or {}, request=httpx.Request("POST", "https://example.invalid"))
    return APIStatusError("offline test error", response=response, body={"error": {"code": "offline"}})


class FakePacer:
    instances = []

    def __init__(self, interval, budget, reserve):
        self.interval, self.budget, self.reserve = interval, budget, reserve
        self.wait_calls, self.accounted = 0, []
        self.instances.append(self)

    def wait(self):
        self.wait_calls += 1

    def account(self, tokens):
        self.accounted.append(tokens)


class FakeVersion:
    def __init__(self, definition, metadata=None, version="7"):
        self.data, self.metadata, self.version = copy.deepcopy(definition), copy.deepcopy(metadata or {}), version
        self.definition = SimpleNamespace(as_dict=lambda: copy.deepcopy(self.data))


class FakeCredential:
    def __init__(self, process_timeout):
        self.process_timeout, self.closed = process_timeout, False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class Harness:
    """Each factory call creates distinct clients; only fake remote storage is shared."""

    def __init__(self):
        self.versions = {evaluation.SOURCE_AGENT: FakeVersion(SOURCE)}
        self.lock = threading.Lock()
        self.created, self.deleted, self.requests = [], [], []
        self.credentials, self.projects, self.clients, self.providers = [], [], [], []
        self.active, self.max_active = {}, {}
        self.max_total_active = 0
        self.on_create, self.on_delete, self.on_request = None, None, None
        self.mismatch = False

    def credential(self, **kwargs):
        credential = FakeCredential(**kwargs)
        self.credentials.append(credential)
        return credential

    def provider(self, credential, scope):
        assert scope == "https://ai.azure.com/.default"
        calls = []

        def token():
            calls.append(len(calls) + 1)
            return f"fake-refreshable-token-{len(calls)}"

        self.providers.append((credential, token, calls))
        return token

    def project(self, **kwargs):
        harness = self

        class Agents:
            def get(self, name):
                with harness.lock:
                    if name not in harness.versions:
                        raise ResourceNotFoundError("Not found")
                    return SimpleNamespace(versions=SimpleNamespace(latest=harness.versions[name]))

            def get_version(self, name, version):
                live = self.get(name).versions.latest
                assert live.version == version
                return live

            def create_version(self, name, body):
                assert name != evaluation.SOURCE_AGENT
                with harness.lock:
                    assert name not in harness.versions, "Never create a new version of an existing agent"
                    harness.created.append((name, copy.deepcopy(body)))
                    live = FakeVersion(body["definition"], body["metadata"], "1")
                    if harness.mismatch:
                        live.data["instructions"] = "Unexpected server normalization"
                    harness.versions[name] = live
                if harness.on_create:
                    harness.on_create(name, live)
                return live

            def delete(self, name):
                assert name != evaluation.SOURCE_AGENT, "Source must never be deleted"
                assert any(created[0] == name for created in harness.created), "Only created agents can be deleted"
                if harness.on_delete:
                    harness.on_delete(name)
                with harness.lock:
                    harness.deleted.append(name)
                    harness.versions.pop(name, None)

        class Project:
            def __init__(self):
                self.agents, self.kwargs, self.closed = Agents(), kwargs, False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.closed = True

        project = Project()
        self.projects.append(project)
        return project

    def client(self, **kwargs):
        harness = self
        name = urlsplit(kwargs["base_url"]).path.split("/agents/")[1].split("/")[0]
        definition = self.versions[name].data
        model, effort = definition["model"], definition["reasoning"]["effort"]

        class Client:
            def __init__(self):
                self.kwargs, self.closed = kwargs, False
                self.responses = SimpleNamespace(create=self.create)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.closed = True

            def create(self, **request):
                assert callable(kwargs["api_key"]), "Do not freeze a bearer token for long runs"
                kwargs["api_key"]()
                with harness.lock:
                    harness.requests.append({"name": name, "model": model, "effort": effort, "request": copy.deepcopy(request)})
                    harness.active[model] = harness.active.get(model, 0) + 1
                    harness.max_active[model] = max(harness.max_active.get(model, 0), harness.active[model])
                    harness.max_total_active = max(harness.max_total_active, sum(harness.active.values()))

                def finish():
                    with harness.lock:
                        harness.active[model] -= 1

                try:
                    if harness.on_request:
                        harness.on_request(name, request)
                    time.sleep(0.002)
                    tools = ["azure_ai_search_call"] if definition["tools"] else []
                    return FakeStream(response_events(model, effort, tools), finish)
                except Exception:
                    finish()
                    raise

        client = Client()
        self.clients.append(client)
        return client

    def patches(self):
        stack = ExitStack()
        stack.enter_context(patch.object(evaluation, "AzureCliCredential", self.credential))
        stack.enter_context(patch.object(evaluation, "AIProjectClient", self.project))
        stack.enter_context(patch.object(evaluation, "OpenAI", self.client))
        stack.enter_context(patch.object(evaluation, "get_bearer_token_provider", self.provider))
        stack.enter_context(patch.object(evaluation, "fetch_catalogue", return_value="ONE IMMUTABLE CATALOGUE"))
        stack.enter_context(patch.object(evaluation.matrix, "Pacer", FakePacer))
        stack.enter_context(redirect_stdout(StringIO()))
        return stack


class PriorSnapshot:
    """Synthetic 792-turn plan matching the reported counts; never reads real answers."""

    def __init__(self):
        self.root = (ROOT.parent / "offline-prior-evaluation").resolve()
        self.output = (ROOT.parent / "offline-new-evaluation").resolve()
        self.args = evaluation.parse_args([*CLI, "--output-dir", str(self.output), "--continue-from", str(self.root)])
        self.cases = []
        for case_id in [f"Q{n}" for n in range(1, 13)] + [f"W{n}" for n in range(1, 6)]:
            web = case_id.startswith("W")
            case = {
                "id": case_id, "question": f"Synthetic {case_id} question.",
                "group": "web" if web else "minutes", "expected": "external" if web else "internal",
                "cohort": "negative" if case_id in ("Q11", "Q12") else "holdout",
                "answerable": case_id not in ("Q11", "Q12"),
            }
            if case_id != "Q12":
                case["oracle_context"] = f"Synthetic fixed evidence for {case_id}."
            self.cases.append(case)
        self.cases[1]["question"] += "\u2028Unicode line separator inside a JSON string."
        self.cases_sha256 = hashlib.sha256(json.dumps(self.cases).encode()).hexdigest()
        self.schedules = evaluation.build_schedules(self.cases, self.args.efforts, self.args.stages, 3, self.args.seed)
        self.catalogue = "ONE IMMUTABLE CATALOGUE\nSecond line."
        self.run_id = "original-offline-run"
        self.files, self.rows = {}, {}
        manifest = {
            **evaluation.frozen_configuration(self.args, CFG, self.cases, self.cases_sha256, self.schedules),
            "run_id": self.run_id, "started_at": "2020-01-01T00:00:00+00:00",
            "source_version": "7", "source_definition_sha256": evaluation.fingerprint(SOURCE),
            "instructions_sha256": hashlib.sha256(SOURCE["instructions"].encode()).hexdigest(),
            "catalogue_sha256": hashlib.sha256(self.catalogue.encode()).hexdigest(),
        }
        self.put_json("manifest.json", manifest)
        self.put_json("cases.json", self.cases)
        self.put_json("source-definition.json", SOURCE)
        self.files[str(self.root / "catalog.txt")] = self.catalogue.replace("\n", "\r\n").encode()
        self.put_json("source-unchanged.json", {
            "status": "checked", "unchanged": True, "source_version": "7", "current_version": "7",
            "source_definition_sha256": evaluation.fingerprint(SOURCE),
            "current_definition_sha256": evaluation.fingerprint(SOURCE), "current_definition": SOURCE,
        })
        completed_counts = {"gpt-5.4": 170, "gpt-5.4-mini": 198, "gpt-5.6-luna": 172, "gpt-5.6-terra": 173}
        cleanup, summary = {}, {}
        for model in self.args.models:
            cleanup[model] = {"status": "completed", "agents": []}
            summary[model] = {
                "model": model, "completed_turns": completed_counts[model], "planned_turns": 198,
                "status": "completed" if completed_counts[model] == 198 else "error",
            }
            self.put_json(Path(model) / "manifest.json", {
                "model": model, "source_version": "7", "source_definition_sha256": evaluation.fingerprint(SOURCE),
                "schedule": self.schedules, "client_token_budget_per_61s": self.args.budgets[model],
                "interval_s": self.args.interval, "reserve_tokens": self.args.reserve_tokens,
                "oracle_system_overlay": evaluation.ORACLE_SYSTEM_OVERLAY,
            })
            for stage, schedule in self.schedules.items():
                selected = schedule if stage == "oracle" else schedule[:completed_counts[model] - 96]
                for scheduled in selected:
                    record = self.record(model, stage, scheduled)
                    self.rows.setdefault(str(Path(model) / f"{stage}-{scheduled['effort']}.jsonl"), []).append(record)
                if stage == "agent" and model in ("gpt-5.4", "gpt-5.6-luna"):
                    scheduled = schedule[completed_counts[model] - 96]
                    record = self.record(model, stage, scheduled)
                    for name in ("response", "usage", "text", "actual_model", "actual_effort"):
                        record.pop(name, None)
                    record["status"] = "error" if model == "gpt-5.4" else "cancelled"
                    if model == "gpt-5.4":
                        partial = {
                            "text": "", "first_token_s": None, "completion_s": None, "usage": None,
                            "tool_events": [{"type": "bing_custom_search_preview_call", "start_s": 1, "end_s": 2}],
                        }
                        record["partial"] = partial
                        record["attempt_errors"] = [{
                            "attempt": 1, "error": {"type": "APIError", "message": evaluation.STREAMED_PROVIDER_MESSAGE},
                            "retryable": False, "retry_delay_s": None, "partial": partial, "at": record["finished_at"],
                        }]
                    else:
                        record.pop("attempts")
                        record["error"] = {"type": "PeerStopped", "message": "A peer failed"}
                    self.rows.setdefault(str(Path(model) / f"{stage}-{scheduled['effort']}.jsonl"), []).append(record)
            for stage in self.args.stages:
                for effort in self.args.efforts:
                    path = str(Path(model) / f"{stage}-{effort}.jsonl")
                    self.write_rows(path)
        self.put_json("cleanup.json", {"status": "completed", "workers": cleanup})
        self.put_json("summary.json", {"status": "error", "source_unchanged": True, "workers": summary})

    def record(self, model, stage, scheduled):
        case = next(case for case in self.cases if case["id"] == scheduled["case_id"])
        response = response_events(model, scheduled["effort"], () if stage == "oracle" else ("azure_ai_search_call",))[-1]["response"]
        return {
            **scheduled, "model": model, "stage": stage, "agent_name": "previous-deleted-agent", "agent_version": "1",
            **{name: case[name] for name in ("question", "group", "expected", "cohort", "answerable")},
            "status": "completed", "attempts": 1, "attempt_errors": [], "text": "Grounded answer.",
            "response": response, "usage": response["usage"], "actual_model": model,
            "actual_effort": scheduled["effort"], "first_token_s": 1, "completion_s": 3,
            "started_at": "2020-01-01T00:00:00+00:00", "finished_at": "2020-01-01T00:00:03+00:00",
            "wall_s_including_pacing_and_retries": 3,
        }

    def write_rows(self, relative):
        self.files[str(self.root / relative)] = "".join(
            json.dumps(record, ensure_ascii=False, allow_nan=False) + "\r\n" for record in self.rows.get(str(relative), [])
        ).encode()

    def put_json(self, relative, value):
        self.files[str(self.root / relative)] = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()

    def json(self, relative):
        return json.loads(self.files[str(self.root / relative)])

    def patches(self):
        def read(path):
            try:
                return self.files[str(path.resolve())]
            except KeyError:
                raise FileNotFoundError(str(path)) from None

        stack = ExitStack()
        stack.enter_context(patch.object(Path, "read_bytes", read))
        stack.enter_context(patch.object(Path, "is_dir", lambda path: path.resolve() == self.root))
        return stack

    def load(self):
        return evaluation.load_continuation(self.args, CFG, self.cases, self.cases_sha256, self.schedules)

    def from_output(self, evidence):
        self.root = self.output
        self.output = (ROOT.parent / "offline-next-evaluation").resolve()
        self.args = evaluation.parse_args([*CLI, "--output-dir", str(self.output), "--continue-from", str(self.root)])
        self.files = {str(self.root / name): text.encode() for name, text in evidence.documents.items()}
        self.files.update({str(self.root / name): log.getvalue().encode() for name, log in evidence.logs.items()})


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(evaluation.os, "fsync", return_value=None).start()
        FakePacer.instances = []

    def test_cli_defaults_and_canonical_stage_order(self):
        args = evaluation.parse_args(CLI)
        self.assertEqual(args.models, list(evaluation.DEFAULT_BUDGETS))
        self.assertEqual(args.efforts, ["none", "low"])
        self.assertEqual((args.runs, args.workers, args.interval, args.reserve_tokens), (3, 4, 8, 30000))
        self.assertEqual(args.budgets, evaluation.DEFAULT_BUDGETS)
        args = evaluation.parse_args([*CLI, "--stages", "agent", "oracle", "--models", "gpt-5.4-mini",
                                      "--budgets", '{"gpt-5.4-mini":123456}'])
        self.assertEqual(args.stages, ["oracle", "agent"])
        self.assertEqual(args.budgets["gpt-5.4-mini"], 123456)

    def test_cli_rejects_unsafe_limits_duplicates_and_resume(self):
        invalid = [
            ["--workers", "0"], ["--workers", "5"], ["--runs", "0"], ["--interval", "nan"],
            ["--interval", "-1"], ["--interval", "inf"], ["--reserve-tokens", "0"],
            ["--reserve-tokens", "300001"], ["--budgets", "[]"], ["--budgets", "not-json"],
            ["--budgets", '{"gpt-5.4":true}'], ["--budgets", '{"gpt-5.4":29999}'],
            ["--budgets", '{"unapproved":999999}'], ["--models", "gpt-5.4", "gpt-5.4"],
            ["--efforts", "none", "none"], ["--stages", "agent", "agent"], ["--resume"],
            ["--case-ids", "one", "one"], ["--models", "different-model"], ["--efforts", "medium"],
        ]
        for flags in invalid:
            with self.subTest(flags=flags), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                evaluation.parse_args([*CLI, *flags])
            self.assertEqual(error.exception.code, 2)

    def test_settings_pin_evaluation_targets_and_do_not_expand_ambient_env(self):
        with patch.object(Path, "is_file", return_value=True), patch.object(evaluation, "dotenv_values", return_value=CFG) as reader:
            self.assertEqual(evaluation.load_settings(Path("offline.env")), CFG)
            reader.assert_called_once_with(Path("offline.env"), interpolate=False)
        for changes in (
            {"AGENT_NAME": "production-agent"}, {"SEARCH_INDEX_NAME": "production-index"},
            {"PROJECT_ENDPOINT": ""}, {"AZURE_SEARCH_ENDPOINT": "http://unsafe.invalid"},
            {"PROJECT_ENDPOINT": "https://user:password@example.invalid"},
            {"PROJECT_ENDPOINT": "https://example.invalid?secret=x"},
        ):
            with self.subTest(changes=changes), patch.object(Path, "is_file", return_value=True), \
                    patch.object(evaluation, "dotenv_values", return_value={**CFG, **changes}), self.assertRaises(ValueError):
                evaluation.load_settings(Path("offline.env"))

    def test_case_hash_selection_and_gold_fields_are_discarded(self):
        private = [{
            **case, "gold_answer": "SECRET RUBRIC", "rubric": {"secret": "NEVER SEND"},
            "required_facts": ["SECRET FACT"], "gold": {"answer": "SECRET GOLD"},
            "scoring": {"points": "SECRET SCORE"}, "scoring_fields": ["SECRET FIELD"],
        } for case in CASES]
        raw = json.dumps(private).encode()
        with patch.object(Path, "read_bytes", return_value=raw):
            cases, digest = evaluation.load_cases(Path("offline.json"), ["negative-3", "minutes-1"])
        self.assertEqual(cases, [CASES[0], CASES[2]])
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
        self.assertNotIn("SECRET", json.dumps(cases))
        for stage in evaluation.STAGES:
            request = evaluation.response_request("CATALOGUE", private[0], stage)
            serialized = json.dumps(request)
            for forbidden in ("SECRET", "rubric", "gold", "required_facts", "scoring", "answerable", "cohort", "expected"):
                self.assertNotIn(forbidden, serialized)
            self.assertNotIn("previous_response_id", request)
            self.assertNotIn("conversation", request)
            self.assertEqual(request["input"][-1]["content"], private[0]["question"])
            if stage == "oracle":
                self.assertEqual(request["tools"], [])
                self.assertEqual(request["tool_choice"], "none")
                self.assertEqual(request["input"][1]["content"], private[0]["oracle_context"])
            else:
                self.assertNotIn("tools", request)
                self.assertNotIn("EXACT oracle", serialized)

    def test_oracle_overlay_is_exact_stage_scoped_and_preserves_source_instructions(self):
        overlay = (
            "Fixed-evidence evaluation: the supplied source passages are the evidence available for this turn. "
            "No tools are available. Answer from those passages; state when evidence is insufficient. "
            "Do not claim to have performed a search."
        )
        self.assertEqual(evaluation.ORACLE_SYSTEM_OVERLAY, overlay)
        source = {**copy.deepcopy(SOURCE), "instructions": "For internal questions, a search MUST be called."}
        before = copy.deepcopy(source)
        case = {**CASES[0], "oracle_system_overlay": "Untrusted case override"}
        for stage in evaluation.STAGES:
            definition = evaluation.evaluation_definition(source, "gpt-5.4", "none", stage)
            self.assertEqual(definition["instructions"], before["instructions"])
            request = evaluation.response_request("CATALOGUE", case, stage)
            system_messages = [message["content"] for message in request["input"] if message["role"] == "system"]
            self.assertEqual(system_messages, ["CATALOGUE", case["oracle_context"], overlay] if stage == "oracle" else ["CATALOGUE"])
            self.assertEqual(request["input"][-1]["content"], case["question"])
            self.assertNotIn("Untrusted case override", json.dumps(request))
        self.assertEqual(source, before)

    def test_cases_schema_and_unknown_selection_fail_closed(self):
        invalid = [
            {}, [], ["text"], [{**CASES[0], "id": ""}], [CASES[0], CASES[0]],
            [{**CASES[0], "question": False}], [{**CASES[0], "group": "finance"}],
            [{**CASES[0], "cohort": "invalid"}], [{**CASES[0], "expected": "web"}],
            [{**CASES[0], "answerable": "false"}], [{**CASES[0], "oracle_context": {"text": "unsafe"}}],
        ]
        for data in invalid:
            with self.subTest(data=data), patch.object(Path, "read_bytes", return_value=json.dumps(data).encode()), self.assertRaises(ValueError):
                evaluation.load_cases(Path("offline.json"))
        with patch.object(Path, "read_bytes", return_value=json.dumps(CASES).encode()), self.assertRaises(ValueError):
            evaluation.load_cases(Path("offline.json"), ["missing"])

    def test_schedule_is_exact_reproducible_and_interleaves_efforts(self):
        for seed in range(20):
            schedule = evaluation.build_schedules(CASES, ["none", "low"], ["oracle", "agent"], 3, seed)
            self.assertEqual(schedule, evaluation.build_schedules(CASES, ["none", "low"], ["oracle", "agent"], 3, seed))
            for stage, entries in schedule.items():
                eligible = CASES[:2] if stage == "oracle" else CASES
                expected = {(effort, case["id"]) for effort in ("none", "low") for case in eligible}
                for run in (1, 2, 3):
                    pairs = [(entry["effort"], entry["case_id"]) for entry in entries if entry["run"] == run]
                    self.assertEqual(set(pairs), expected)
                    self.assertEqual(len(pairs), len(expected))
                    self.assertGreater(sum(a[0] != b[0] for a, b in zip(pairs, pairs[1:])), 1)
                self.assertEqual([entry["sequence"] for entry in entries], list(range(1, len(entries) + 1)))
        self.assertNotEqual(
            evaluation.build_schedules(CASES, ["none", "low"], ["agent"], 3, 1),
            evaluation.build_schedules(CASES, ["none", "low"], ["agent"], 3, 2),
        )
        with self.assertRaises(ValueError):
            evaluation.build_schedules([CASES[2]], ["none"], ["oracle"], 1, 1)

    def test_definition_preserves_every_source_field_except_explicit_axes(self):
        original = copy.deepcopy(SOURCE)
        evaluation.validate_baseline(SOURCE)
        agent = evaluation.evaluation_definition(SOURCE, "gpt-5.6-terra", "low", "agent")
        expected = copy.deepcopy(SOURCE)
        expected["model"] = "gpt-5.6-terra"
        expected["reasoning"]["effort"] = "low"
        self.assertEqual(agent, expected)
        oracle = evaluation.evaluation_definition(SOURCE, "gpt-5.6-terra", "low", "oracle")
        self.assertEqual(oracle, {**expected, "tools": [], "tool_choice": "none"})
        agent["tools"][1]["azure_ai_search"]["indexes"][0]["filter"] = "mutated local clone"
        self.assertEqual(SOURCE, original)

    def test_baseline_rejects_k8_vector_modes_wrong_index_and_ambiguous_tools(self):
        for changes in ({"top_k": 8}, {"top_k": "5"}, {"query_type": "vector_semantic_hybrid"},
                        {"query_type": "simple"}, {"index_name": "production"}):
            source = copy.deepcopy(SOURCE)
            source["tools"][1]["azure_ai_search"]["indexes"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                evaluation.validate_baseline(source)
        for tools in ([], SOURCE["tools"][1:], [*SOURCE["tools"], SOURCE["tools"][1]]):
            with self.assertRaises(ValueError):
                evaluation.validate_baseline({**SOURCE, "tools": tools})

    def test_stream_measures_first_token_all_tools_and_server_metadata(self):
        events = response_events(tools=("azure_ai_search_call", "bing_custom_search_preview_call", "function_call"))
        client, stream = stream_client(events)
        request = evaluation.response_request("CAT", CASES[0], "agent")
        clock = itertools.count(0, 0.125)
        with patch.object(evaluation.time, "perf_counter", side_effect=lambda: next(clock)):
            result = evaluation.stream_turn(client, request, "gpt-5.4", "none")
        self.assertTrue(stream.closed)
        client.responses.create.assert_called_once_with(**request)
        self.assertEqual(result["actual_model"], "gpt-5.4")
        self.assertEqual(result["actual_effort"], "none")
        self.assertEqual((result["server_created_at"], result["server_completed_at"]), (100.0, 103.0))
        self.assertEqual(result["usage"]["total_tokens"], 150)
        self.assertEqual(result["text"], "Grounded answer.")
        self.assertEqual(result["response"], events[-1]["response"])
        self.assertLess(result["first_tool_start_s"], result["first_tool_end_s"])
        self.assertLess(result["first_tool_end_s"], result["first_token_s"])
        self.assertLess(result["first_token_s"], result["completion_s"])
        self.assertEqual(len(result["tool_events"]), 3)
        for tool in result["tool_events"]:
            self.assertEqual(tool["duration_s"], tool["end_s"] - tool["start_s"])

    def test_failed_incomplete_error_and_truncated_streams_are_not_success(self):
        for kind in ("response.failed", "response.incomplete", "error", "response.error", "truncated"):
            events = response_events()
            events.pop()
            if kind != "truncated":
                events.append({"type": kind, "response": {"status": "failed", "error": {"code": "invalid_request"}}})
            client, stream = stream_client(events)
            with self.subTest(kind=kind), self.assertRaises(evaluation.StreamFailure) as error:
                evaluation.stream_turn(client, evaluation.response_request("CAT", CASES[0], "agent"), "gpt-5.4", "none")
            self.assertTrue(stream.closed)
            self.assertEqual(error.exception.partial["text"], "Grounded answer.")
            self.assertFalse(evaluation.failure_details(error.exception)[1])

    def test_stream_rejects_missing_tool_end_missing_deltas_and_mismatched_config(self):
        for mutation in ("missing_start", "missing_end", "delta", "status", "error", "usage", "usage_total", "model", "effort", "item_failed"):
            events = response_events(tools=("azure_ai_search_call",))
            if mutation == "missing_start":
                events = [e for e in events if not (e["type"] == "response.output_item.added" and e["item"]["type"].endswith("_call"))]
            elif mutation == "missing_end":
                events = [e for e in events if not (e["type"] == "response.output_item.done" and e["item"]["type"].endswith("_call"))]
            elif mutation == "delta":
                events = [e for e in events if e.get("delta") != "answer."]
            elif mutation == "item_failed":
                events[2]["item"]["status"] = "failed"
            elif mutation == "usage_total":
                events[-1]["response"]["usage"]["total_tokens"] = 1
            else:
                events[-1]["response"][mutation if mutation != "effort" else "reasoning"] = {
                    "status": "incomplete", "error": {"code": "invalid_request"}, "usage": None,
                    "model": "gpt-5.4-mini", "effort": {"effort": "low"},
                }[mutation]
            client, _ = stream_client(events)
            with self.subTest(mutation=mutation), self.assertRaises(evaluation.StreamFailure):
                evaluation.stream_turn(client, evaluation.response_request("CAT", CASES[0], "agent"), "gpt-5.4", "none")

    def test_oracle_rejects_unexpected_tools_and_reports_real_dated_model(self):
        client, _ = stream_client(response_events(tools=("azure_ai_search_call",)))
        with self.assertRaises(evaluation.StreamFailure) as error:
            evaluation.stream_turn(client, evaluation.response_request("CAT", CASES[0], "oracle"), "gpt-5.4", "none")
        self.assertEqual(error.exception.detail["code"], "oracle_used_tools")
        events = response_events(model="gpt-5.4-2026-03-05")
        events[-1]["response"].pop("completed_at")
        client, _ = stream_client(events)
        result = evaluation.stream_turn(client, evaluation.response_request("CAT", CASES[0], "oracle"), "gpt-5.4", "none")
        self.assertEqual(result["actual_model"], "gpt-5.4-2026-03-05")
        self.assertIsNone(result["server_completed_at"])

    def test_late_tool_ids_and_post_terminal_events(self):
        events = response_events(tools=("azure_ai_search_call",))
        events[1]["item"].pop("id")
        client, _ = stream_client(events)
        result = evaluation.stream_turn(client, evaluation.response_request("CAT", CASES[0], "agent"), "gpt-5.4", "none")
        self.assertEqual(result["tool_events"][0]["id"], "tool-0")
        events.append({"type": "response.output_text.delta", "delta": ""})
        client, _ = stream_client(events)
        with self.assertRaises(evaluation.StreamFailure) as error:
            evaluation.stream_turn(client, evaluation.response_request("CAT", CASES[0], "agent"), "gpt-5.4", "none")
        self.assertEqual(error.exception.detail["code"], "event_after_completed")

    def test_real_openai_sdk_with_mock_transport_refreshes_tokens_and_preserves_hosted_events(self):
        tokens, headers = [], []

        def provider():
            tokens.append(f"offline-token-{len(tokens) + 1}")
            return tokens[-1]

        def respond(request):
            headers.append(request.headers["authorization"])
            events = response_events(tools=("azure_ai_search_call", "bing_custom_search_preview_call"))
            events[-1]["response"]["output"][0]["hosted_result"] = {"retained": True}
            content = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=content + "data: [DONE]\n\n")

        with evaluation.OpenAI(
            base_url="https://example.invalid/agents/offline/endpoint/protocols/openai",
            api_key=provider, max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        ) as client:
            for _ in range(2):
                result = evaluation.stream_turn(client, evaluation.response_request("CAT", CASES[0], "agent"), "gpt-5.4", "none")
                self.assertTrue(result["response"]["output"][0]["hosted_result"]["retained"])
                self.assertEqual(result["tools"], ["azure_ai_search_call", "bing_custom_search_preview_call"])
        self.assertEqual(tokens, ["offline-token-1", "offline-token-2"])
        self.assertEqual(headers, [f"Bearer {token}" for token in tokens])

    def test_real_openai_sdk_does_not_retry_throttled_requests(self):
        calls = []

        def throttled(request):
            calls.append(request)
            return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded", "message": "offline"}})

        with evaluation.OpenAI(
            base_url="https://example.invalid", api_key=lambda: "offline", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(throttled)),
        ) as client, self.assertRaises(APIStatusError):
            evaluation.stream_turn(client, evaluation.response_request("CAT", CASES[0], "agent"), "gpt-5.4", "none")
        self.assertEqual(len(calls), 1)

    def test_transience_uses_codes_not_private_prose_and_honors_retry_after(self):
        for status, retryable in ((400, False), (401, False), (403, False), (408, True), (409, True), (429, True), (503, True)):
            error = status_error(status, {"retry-after-ms": "80000", "retry-after": "95"})
            _, actual, delay = evaluation.failure_details(error)
            self.assertEqual(actual, retryable)
            self.assertEqual(delay, 95)
        after = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=150), usegmt=True)
        _, retryable, delay = evaluation.failure_details(status_error(429, {"Retry-After": after}))
        self.assertTrue(retryable)
        self.assertGreater(delay, 148)
        self.assertLessEqual(delay, 150)
        self.assertEqual(evaluation.failure_details(status_error(429, {"retry-after-ms": "invalid"}))[2], 0)
        self.assertEqual(evaluation.failure_details(status_error(429, {"retry-after-ms": "inf"}))[2], 0)
        self.assertFalse(evaluation.failure_details(evaluation.StreamFailure({
            "code": "missing_completed_event", "response": {"text": "server_error rate_limit temporarily"},
        }))[1])
        self.assertTrue(evaluation.failure_details(evaluation.StreamFailure({
            "code": "response.failed", "event": {"response": {"error": {"code": "server_error"}}},
        }))[1])
        self.assertFalse(evaluation.failure_details(RuntimeError("temporarily unavailable"))[1])

    def test_streamed_api_errors_retry_only_known_codes_or_exact_provider_message(self):
        request = httpx.Request("POST", "https://example.invalid")
        for field in ("code", "type"):
            for code in ("server_error", "internal_server_error", "rate_limit_exceeded"):
                for nested in (False, True):
                    payload = {field: code, "message": "A provider error"}
                    body = {"error": payload} if nested else payload
                    detail, retryable, _ = evaluation.failure_details(APIError("A provider error", request, body=body))
                    self.assertTrue(retryable)
                    self.assertEqual(detail["body"], body)
                    self.assertEqual(detail["code" if field == "code" else "error_type"], code)
                    self.assertIn("status", detail)
        for body in (None, {"message": evaluation.STREAMED_PROVIDER_MESSAGE}, {"type": "api_error", "message": evaluation.STREAMED_PROVIDER_MESSAGE}):
            self.assertTrue(evaluation.failure_details(APIError(evaluation.STREAMED_PROVIDER_MESSAGE, request, body=body))[1])
        self.assertTrue(evaluation.failure_details(APIError(
            "Provider failure", request, body={"code": "provider_specific_error", "type": "server_error", "status": 200},
        ))[1])
        for body, message in (
            ({"code": "invalid_request_error"}, evaluation.STREAMED_PROVIDER_MESSAGE),
            ({"type": "authentication_error"}, evaluation.STREAMED_PROVIDER_MESSAGE),
            ({"code": "invalid_api_key", "type": "server_error"}, evaluation.STREAMED_PROVIDER_MESSAGE),
            ({"code": "validation_error"}, "server_error"),
            ({}, evaluation.STREAMED_PROVIDER_MESSAGE + " Other text."),
            ({}, "Please retry this arbitrary bad request."),
            ({"status": 401, "code": "server_error"}, evaluation.STREAMED_PROVIDER_MESSAGE),
            ({"status": 403, "error": {"code": "server_error"}}, evaluation.STREAMED_PROVIDER_MESSAGE),
        ):
            with self.subTest(body=body):
                self.assertFalse(evaluation.failure_details(APIError(message, request, body=body))[1])

    def test_real_sdk_streamed_provider_error_keeps_partial_tool_evidence_and_retries(self):
        requests = []

        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                events = response_events(tools=("bing_custom_search_preview_call",))[:3]
                events.append({"type": "error", "error": {"message": evaluation.STREAMED_PROVIDER_MESSAGE}})
            else:
                events = response_events()
            content = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=content + "data: [DONE]\n\n")

        stop = threading.Event()
        with evaluation.OpenAI(
            base_url="https://example.invalid", api_key=lambda: "offline", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        ) as client, patch.object(stop, "wait", return_value=False):
            log, _, _ = self.invoke_turn(client, stop=stop)
        record = log.records()[0]
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["attempts"], 2)
        prior = record["attempt_errors"][0]
        self.assertTrue(prior["retryable"])
        self.assertEqual(prior["error"]["type"], "APIError")
        self.assertEqual(prior["error"]["body"], {"message": evaluation.STREAMED_PROVIDER_MESSAGE})
        self.assertEqual(prior["partial"]["text"], "")
        self.assertIsNotNone(prior["partial"]["tool_events"][0]["end_s"])

    def invoke_turn(self, client, *, stop=None, prior=None):
        log, events = MemoryLog(), MemoryLog()
        pacer = FakePacer(8, 180000, 30000)
        stop = stop or threading.Event()
        scheduled = {"case_id": CASES[0]["id"], "run": 1, "sequence": 1, "effort": "none"}
        try:
            evaluation.run_turn(client, pacer, "CAT", CASES[0], "agent", "gpt-5.4",
                                {"name": "owned", "version": "1"}, scheduled, log, events, stop, "auto",
                                prior_record=prior, prior_run_id="prior-run" if prior else None)
        except RuntimeError:
            pass
        return log, events, pacer

    def test_turn_retries_at_most_six_times_and_checkpoints_failure(self):
        client = SimpleNamespace(responses=SimpleNamespace(create=Mock(side_effect=status_error(429, {"Retry-After": "120"}))))
        stop = threading.Event()
        with patch.object(stop, "wait", return_value=False) as backoff:
            log, events, pacer = self.invoke_turn(client, stop=stop)
        self.assertEqual(client.responses.create.call_count, 6)
        self.assertEqual(pacer.wait_calls, 6)
        self.assertEqual([call.args[0] for call in backoff.call_args_list], [121, 130, 195, 260, 325])
        record = log.records()[0]
        self.assertEqual(record["status"], "error")
        self.assertEqual(len(record["attempt_errors"]), 6)
        self.assertEqual(len(events.records()), 6)
        self.assertGreaterEqual(log.flush_count, 1)
        self.assertGreaterEqual(events.flush_count, 6)

    def test_retry_can_succeed_without_losing_failed_attempts(self):
        client = SimpleNamespace(responses=SimpleNamespace(create=Mock(side_effect=[
            status_error(503), FakeStream(response_events()),
        ])))
        stop = threading.Event()
        with patch.object(stop, "wait", return_value=False):
            log, _, pacer = self.invoke_turn(client, stop=stop)
        record = log.records()[0]
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["attempts"], 2)
        self.assertEqual(len(record["attempt_errors"]), 1)
        self.assertEqual(pacer.accounted, [0, 150])
        self.assertEqual(record["response"]["status"], "completed")

    def test_permanent_unknown_and_truncated_failures_do_not_retry(self):
        for error in (status_error(401), RuntimeError("unexpected"), None):
            create = Mock(side_effect=error) if error else Mock(return_value=FakeStream(response_events()[:-1]))
            client = SimpleNamespace(responses=SimpleNamespace(create=create))
            log, _, _ = self.invoke_turn(client)
            self.assertEqual(create.call_count, 1)
            self.assertEqual(log.records()[0]["status"], "error")

    def test_output_guards_and_exclusive_claim_without_writing_files(self):
        with self.assertRaises(ValueError):
            evaluation.Evidence(ROOT / "must-not-create")
        outside = ROOT.parent / "offline-never-created"
        with patch.object(Path, "exists", return_value=True), patch.object(Path, "is_dir", return_value=True), \
                patch.object(Path, "iterdir", return_value=iter([Path("old-evidence")])), \
                patch.object(Path, "mkdir") as mkdir, self.assertRaises(ValueError):
            evaluation.Evidence(outside)
        mkdir.assert_not_called()
        log = MemoryLog()
        with patch.object(Path, "exists", return_value=False), patch.object(Path, "mkdir"), \
                patch.object(Path, "open", return_value=log) as opened:
            evidence = evaluation.Evidence(outside)
        self.assertEqual(evidence.root, outside.resolve())
        opened.assert_called_once_with("x", encoding="utf-8", buffering=1, newline="")
        self.assertIn("claimed_at", json.loads(log.getvalue()))
        with patch.object(Path, "exists", return_value=False), patch.object(Path, "mkdir"), \
                patch.object(Path, "open", side_effect=FileExistsError), self.assertRaises(FileExistsError):
            evaluation.Evidence(outside)

    def test_smoke_catalogue_adapter_restores_environment_and_disables_retries(self):
        smoke = evaluation.matrix.bench.tfa
        original_environment = {key: os.environ.get(key) for key in ("SEARCH_INDEX_NAME", "AZURE_SEARCH_ENDPOINT")}
        credential, seen = object(), []
        constructor = Mock(return_value=SimpleNamespace(
            search=lambda **kwargs: [{"meeting_date": "2026-01-01", "title": "Meeting"}],
            close=lambda: None,
        ))
        with patch.object(smoke, "SearchClient", constructor):
            catalogue = evaluation.fetch_catalogue(credential, CFG)
            self.assertIs(smoke.SearchClient, constructor)
            seen = constructor.call_args.kwargs
        self.assertIn("MEETINGS LIST", catalogue)
        self.assertIn("TODAY:", catalogue)
        self.assertEqual(seen["index_name"], evaluation.SOURCE_INDEX)
        self.assertIs(seen["credential"], credential)
        for key, value in evaluation.NO_RETRIES.items():
            self.assertEqual(seen[key], value)
        self.assertEqual(original_environment, {key: os.environ.get(key) for key in original_environment})

    def execute(self, harness, flags=None):
        args = evaluation.parse_args([*CLI, *(flags or [])])
        evidence = MemoryEvidence()
        with harness.patches():
            code = evaluation.evaluate(args, CFG, CASES, "raw-case-file-sha256", evidence)
        return code, evidence, args

    def test_four_workers_full_flow_preserves_source_tools_and_fresh_sessions(self):
        harness = Harness()
        before = copy.deepcopy(harness.versions[evaluation.SOURCE_AGENT].data)
        code, evidence, args = self.execute(harness)
        self.assertEqual(code, 0)
        self.assertEqual(len(harness.created), 16)
        self.assertEqual(len(harness.deleted), 16)
        self.assertEqual(set(harness.versions), {evaluation.SOURCE_AGENT})
        self.assertEqual(harness.versions[evaluation.SOURCE_AGENT].data, before)
        self.assertEqual(harness.max_active, dict.fromkeys(evaluation.DEFAULT_BUDGETS, 1))
        self.assertLessEqual(harness.max_total_active, 4)
        self.assertEqual(len(FakePacer.instances), 4)
        self.assertEqual(sorted(p.budget for p in FakePacer.instances), sorted(evaluation.DEFAULT_BUDGETS.values()))
        self.assertEqual(len(harness.credentials), 5)
        self.assertEqual(len({id(credential) for credential in harness.credentials}), 5)
        self.assertTrue(all(credential.closed and credential.process_timeout == 120 for credential in harness.credentials))
        self.assertEqual(len({id(provider[0]) for provider in harness.providers}), 4)
        self.assertTrue(all(len(calls) == 30 for _, _, calls in harness.providers))
        for project in harness.projects:
            self.assertTrue(project.closed)
            self.assertTrue(all(project.kwargs[key] == 0 for key in evaluation.NO_RETRIES))
        for client in harness.clients:
            self.assertTrue(client.closed)
            self.assertEqual(client.kwargs["max_retries"], 0)
            self.assertTrue(callable(client.kwargs["api_key"]))
        for name, body in harness.created:
            self.assertRegex(name, r"^[a-zA-Z0-9-]{1,63}$")
            stage = body["metadata"]["evaluation_config"].split(":")[1]
            self.assertEqual(body["definition"]["instructions"], SOURCE["instructions"])
            self.assertEqual(body["definition"]["tools"], [] if stage == "oracle" else SOURCE["tools"])
            self.assertEqual(body["definition"]["reasoning"]["summary"], "auto")
        for model in args.models:
            self.assertEqual(
                evidence.json(Path(model) / "manifest.json")["oracle_system_overlay"],
                evaluation.ORACLE_SYSTEM_OVERLAY,
            )
            requests = [entry for entry in harness.requests if entry["model"] == model]
            stages = ["oracle" if entry["request"]["tool_choice"] == "none" else "agent" for entry in requests]
            self.assertEqual(stages, ["oracle"] * 12 + ["agent"] * 18)
            for stage in args.stages:
                observed = []
                for effort in args.efforts:
                    log = evidence.logs[str(Path(model) / f"{stage}-{effort}.jsonl")]
                    records = log.records()
                    self.assertTrue(log.was_closed)
                    self.assertGreaterEqual(log.flush_count, len(records))
                    self.assertTrue(all(record["status"] == "completed" for record in records))
                    observed.extend({key: record[key] for key in ("sequence", "run", "round_seed", "effort", "case_id")} for record in records)
                observed.sort(key=lambda entry: entry["sequence"])
                self.assertEqual(observed, evidence.json("manifest.json")["schedule"][stage])
        for call in harness.requests:
            request = call["request"]
            self.assertNotIn("conversation", request)
            self.assertNotIn("previous_response_id", request)
            self.assertNotIn("model", request)
            self.assertNotIn("reasoning", request)
            self.assertEqual(request["input"][0]["content"], "ONE IMMUTABLE CATALOGUE")
            overlays = [message for message in request["input"] if message["content"] == evaluation.ORACLE_SYSTEM_OVERLAY]
            self.assertEqual(overlays, [{"type": "message", "role": "system", "content": evaluation.ORACLE_SYSTEM_OVERLAY}] if request["tool_choice"] == "none" else [])
        self.assertTrue(evidence.json("source-unchanged.json")["unchanged"])
        self.assertEqual(evidence.json("cleanup.json")["status"], "completed")
        self.assertEqual(evidence.json("manifest.json")["cases_sha256"], "raw-case-file-sha256")
        self.assertFalse(evidence.json("manifest.json")["audio_measured"])
        self.assertEqual(evidence.json("manifest.json")["oracle_system_overlay"], evaluation.ORACLE_SYSTEM_OVERLAY)
        self.assertEqual(evidence.json("plan.json")["oracle_system_overlay"], evaluation.ORACLE_SYSTEM_OVERLAY)

    def test_agent_only_manifests_do_not_claim_an_oracle_overlay(self):
        harness = Harness()
        code, evidence, _ = self.execute(
            harness, ["--models", "gpt-5.4", "--runs", "1", "--efforts", "none", "--stages", "agent"],
        )
        self.assertEqual(code, 0)
        for path in ("plan.json", "manifest.json", Path("gpt-5.4") / "manifest.json"):
            self.assertIsNone(evidence.json(path)["oracle_system_overlay"])
        self.assertTrue(all(
            evaluation.ORACLE_SYSTEM_OVERLAY not in [message["content"] for message in call["request"]["input"]]
            for call in harness.requests
        ))

    def test_permanent_failure_stops_worker_but_cleans_agents_and_checks_source(self):
        harness = Harness()
        harness.on_request = Mock(side_effect=status_error(400))
        code, evidence, _ = self.execute(harness, ["--models", "gpt-5.4"])
        self.assertEqual(code, 1)
        self.assertEqual(len(harness.requests), 1)
        self.assertEqual(len(harness.deleted), 2)
        self.assertTrue(evidence.json("source-unchanged.json")["unchanged"])
        self.assertEqual(evidence.json("summary.json")["status"], "error")
        self.assertEqual(evidence.json("cleanup.json")["status"], "completed")
        records = [record for key, log in evidence.logs.items() if "oracle-" in key for record in log.records()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "error")

    def test_one_failed_model_propagates_and_other_workers_cleanup(self):
        harness = Harness()

        def fail_one(name, request):
            if harness.versions[name].data["model"] == "gpt-5.4-mini":
                raise status_error(401)

        harness.on_request = fail_one
        code, evidence, _ = self.execute(harness)
        self.assertEqual(code, 1)
        self.assertEqual(set(harness.versions), {evaluation.SOURCE_AGENT})
        self.assertEqual(len(harness.created), len(harness.deleted))
        summary = evidence.json("summary.json")
        self.assertEqual(summary["workers"]["gpt-5.4-mini"]["status"], "error")
        self.assertTrue(summary["source_unchanged"])

    def test_readback_mismatch_aborts_before_inference_and_still_deletes_copy(self):
        harness = Harness()
        harness.mismatch = True
        code, evidence, _ = self.execute(harness, ["--models", "gpt-5.4"])
        self.assertEqual(code, 1)
        self.assertEqual(harness.requests, [])
        self.assertEqual(len(harness.deleted), 1)
        self.assertEqual(evidence.json("cleanup.json")["status"], "completed")

    def test_ambiguous_create_is_never_retried_and_owned_copy_is_reconciled(self):
        harness = Harness()
        harness.on_create = Mock(side_effect=ServiceRequestError("timed out after server create"))
        code, evidence, _ = self.execute(harness, ["--models", "gpt-5.4"])
        self.assertEqual(code, 1)
        self.assertEqual(len(harness.created), 1)
        self.assertEqual(len(harness.deleted), 1)
        entry = evidence.json(Path("gpt-5.4") / "cleanup.json")["agents"][0]
        self.assertTrue(entry["creation_confirmed"])
        self.assertEqual(entry["cleanup"], "deleted")

    def test_cleanup_continues_after_failure_and_never_deletes_changed_owner(self):
        for mode in ("delete_error", "owner_changed"):
            harness = Harness()
            if mode == "delete_error":
                harness.on_delete = lambda name: (_ for _ in ()).throw(RuntimeError("delete refused")) if "-low-" in name else None
            else:
                def change_owner(name, live):
                    if "-low-" in name:
                        live.metadata["evaluation_run_id"] = "another-owner"
                harness.on_create = change_owner
            code, evidence, _ = self.execute(harness, ["--models", "gpt-5.4", "--runs", "1", "--stages", "oracle"])
            self.assertEqual(code, 1)
            self.assertEqual(len(harness.created), 2)
            self.assertEqual(len(harness.deleted), 1)
            self.assertNotIn("-low-", harness.deleted[0])
            self.assertEqual(evidence.json("cleanup.json")["status"], "error")
            self.assertTrue(evidence.json("source-unchanged.json")["unchanged"])

    def test_cleanup_attempts_all_agents_even_if_journal_write_fails(self):
        harness = Harness()
        copies = evaluation.AgentCopies(harness.project(), MemoryEvidence(), "gpt-5.4", "owner", MemoryLog())
        for effort in ("none", "low"):
            copies.create(evaluation.evaluation_definition(SOURCE, "gpt-5.4", effort, "oracle"), "oracle", effort)
        original = evaluation.append_record
        failures = []

        def fail_one_write(log, record):
            if record["kind"] == "cleanup" and not failures:
                failures.append(True)
                raise OSError("offline simulated journal failure")
            original(log, record)

        with patch.object(evaluation, "append_record", side_effect=fail_one_write):
            receipt = copies.cleanup()
        self.assertEqual(len(harness.deleted), 2)
        self.assertEqual(set(harness.versions), {evaluation.SOURCE_AGENT})
        self.assertEqual(receipt["status"], "error")
        self.assertEqual(len(receipt["evidence_errors"]), 1)

    def test_source_definition_or_version_drift_is_fatal_even_after_successful_turns(self):
        for mutation in ("definition", "version"):
            harness = Harness()

            def drift(name):
                source = harness.versions[evaluation.SOURCE_AGENT]
                if mutation == "definition":
                    source.data["instructions"] = "Concurrent external edit"
                else:
                    source.version = "8"

            harness.on_delete = drift
            code, evidence, _ = self.execute(harness, ["--models", "gpt-5.4", "--runs", "1", "--stages", "agent"])
            self.assertEqual(code, 1)
            self.assertFalse(evidence.json("source-unchanged.json")["unchanged"])
            self.assertEqual(evidence.json(Path("gpt-5.4") / "summary.json")["status"], "completed")

    def test_invalid_baseline_is_saved_and_never_cloned(self):
        harness = Harness()
        harness.versions[evaluation.SOURCE_AGENT].data["tools"][1]["azure_ai_search"]["indexes"][0]["top_k"] = 8
        code, evidence, _ = self.execute(harness)
        self.assertEqual(code, 1)
        self.assertFalse(harness.created)
        self.assertFalse(harness.requests)
        self.assertEqual(evidence.json("source-definition.json")["tools"][1]["azure_ai_search"]["indexes"][0]["top_k"], 8)
        self.assertTrue(evidence.json("source-unchanged.json")["unchanged"])
        self.assertEqual(evidence.json("summary.json")["status"], "error")

    def test_unowned_name_collision_is_never_created_or_deleted(self):
        harness = Harness()
        evidence = MemoryEvidence()
        name = "bench-eval-gpt-5-4-oracle-none-" + "a" * 20
        harness.versions[name] = FakeVersion(SOURCE)
        project = harness.project()
        copies = evaluation.AgentCopies(project, evidence, "gpt-5.4", "owner", MemoryLog())
        with patch.object(evaluation.uuid, "uuid4", return_value=SimpleNamespace(hex="a" * 32)), self.assertRaises(RuntimeError):
            copies.create(evaluation.evaluation_definition(SOURCE, "gpt-5.4", "none", "oracle"), "oracle", "none")
        self.assertEqual(copies.cleanup()["agents"], [])
        self.assertFalse(harness.created)
        self.assertFalse(harness.deleted)
        self.assertIn(name, harness.versions)

    def execute_continuation(self, snapshot, harness=None):
        harness = harness or Harness()
        evidence = MemoryEvidence()
        with snapshot.patches(), harness.patches():
            evaluation.fetch_catalogue.return_value = snapshot.catalogue
            prior = snapshot.load()
            code = evaluation.evaluate(
                snapshot.args, CFG, snapshot.cases, snapshot.cases_sha256, evidence, prior,
            )
        return code, evidence, harness, prior

    def test_continuation_reuses_713_unchanged_and_requests_exactly_79_missing_turns(self):
        snapshot = PriorSnapshot()
        before = dict(snapshot.files)
        code, evidence, harness, prior = self.execute_continuation(snapshot)
        self.assertEqual(code, 0)
        self.assertEqual(snapshot.files, before)
        self.assertEqual(len(prior.completed), 713)
        self.assertEqual(len(harness.requests), 79)
        self.assertEqual({model: sum(call["model"] == model for call in harness.requests) for model in snapshot.args.models},
                         {"gpt-5.4": 28, "gpt-5.4-mini": 0, "gpt-5.6-luna": 26, "gpt-5.6-terra": 25})
        self.assertTrue(all(call["request"]["tool_choice"] == "auto" for call in harness.requests))
        self.assertEqual(len(harness.created), 6)
        self.assertEqual(len(harness.deleted), 6)
        self.assertEqual(len(harness.credentials), 4)
        self.assertEqual(len(FakePacer.instances), 3)
        self.assertTrue(all(body["definition"]["tools"] == SOURCE["tools"] for _, body in harness.created))
        self.assertTrue(all(body["definition"]["instructions"] == SOURCE["instructions"] for _, body in harness.created))
        self.assertTrue(all(body["metadata"]["evaluation_run_id"] != snapshot.run_id for _, body in harness.created))
        records, lines = {}, {}
        names = {f"{stage}-{effort}.jsonl" for stage in snapshot.args.stages for effort in snapshot.args.efforts}
        for path, log in evidence.logs.items():
            if Path(path).name not in names:
                continue
            for line in log.getvalue().split("\n"):
                if not line.strip():
                    continue
                record = json.loads(line)
                key = evaluation.turn_key(record)
                self.assertNotIn(key, records)
                records[key], lines[key] = record, line + "\n"
        self.assertEqual(len(records), 792)
        self.assertEqual(sum(key[1] == "oracle" for key in records), 384)
        self.assertTrue(all(record["status"] == "completed" for record in records.values()))
        for key, raw in prior.completed.items():
            self.assertEqual(lines[key].encode(), raw.encode())
        for key, record in records.items():
            scheduled = next(item for item in snapshot.schedules[key[1]] if item["sequence"] == record["sequence"])
            self.assertTrue(all(record[name] == value for name, value in scheduled.items()))
        for key, old in prior.failed.items():
            recovered = records[key]
            self.assertEqual(recovered["attempts"], old.get("attempts", 0) + 1)
            self.assertEqual(recovered["attempts_this_run"], 1)
            self.assertEqual(recovered["attempt_errors"], old["attempt_errors"])
            self.assertEqual(recovered["recovery"]["prior_status"], old["status"])
            self.assertEqual(recovered["recovery"]["source_run_id"], snapshot.run_id)
            self.assertEqual(recovered["recovery"]["prior_error_retryable_under_current_classifier"],
                             True if old["status"] == "error" else None)
        errors = [json.loads(line) for line in evidence.documents["prior-errors.jsonl"].split("\n") if line]
        self.assertCountEqual([wrapper["record"]["status"] for wrapper in errors], ["error", "cancelled"])
        self.assertEqual(sum(len(wrapper["record"]["attempt_errors"]) for wrapper in errors), 1)
        self.assertFalse(next(wrapper["record"] for wrapper in errors if wrapper["record"]["status"] == "error")["attempt_errors"][0]["retryable"])
        summary = evidence.json("summary.json")
        self.assertEqual((summary["planned_turns"], summary["completed_turns"], summary["reused_completed_turns"], summary["new_completed_turns"]),
                         (792, 792, 713, 79))
        self.assertEqual((summary["request_attempts"], summary["api_error_attempts"]), (793, 1))
        self.assertEqual(summary["workers"]["gpt-5.4-mini"]["new_request_attempts"], 0)
        self.assertEqual(summary["workers"]["gpt-5.4-mini"]["cleanup"]["agents"], [])
        provenance = evidence.json("manifest.json")["continuation"]
        self.assertEqual(provenance["source_run_id"], snapshot.run_id)
        self.assertEqual(provenance["reused_completed_turns"], 713)
        self.assertEqual(provenance["remaining_turns"], 79)
        self.assertEqual(provenance["prior_api_error_attempts"], 1)
        self.assertEqual(provenance["prior_cancelled_records"], 1)
        self.assertEqual(evidence.json("manifest.json")["schedule"], snapshot.schedules)
        self.assertEqual(evidence.json("continuation.json")["pending_schedule"]["gpt-5.4-mini"], {"oracle": [], "agent": []})
        self.assertTrue(evidence.json("continuation-source-unchanged.json")["unchanged"])

    def test_continuation_requires_disjoint_external_source_and_new_output(self):
        snapshot = PriorSnapshot()
        for output in (snapshot.root, snapshot.root / "nested", snapshot.root.parent):
            snapshot.args.output_dir = output
            with snapshot.patches(), self.subTest(output=output), self.assertRaises(ValueError):
                snapshot.load()
        snapshot.args.output_dir = snapshot.output
        snapshot.args.continue_from = ROOT
        with snapshot.patches(), self.assertRaises(ValueError):
            snapshot.load()

    def test_continuation_rejects_all_changed_frozen_settings_before_clients(self):
        snapshot = PriorSnapshot()
        original = snapshot.json("manifest.json")
        mutations = {
            "models": ["gpt-5.4"], "efforts": ["low", "none"], "stages": ["agent"], "runs": 2,
            "workers": 3, "schedule_seed": 99, "interval_s": 9, "reserve_tokens": 31000,
            "budgets_per_61s": {**snapshot.args.budgets, "gpt-5.4": 179999}, "oracle_system_overlay": "changed",
            "cases_sha256": "changed", "selected_cases_sha256": "changed", "selected_case_ids": ["Q1"],
            "source_agent": "production", "source_index": "production", "project_endpoint": "https://other.invalid",
            "fresh_sessions": False, "sdk_retries": 1, "max_attempts": 7, "schema_version": 2,
            "schedule": {**snapshot.schedules, "agent": list(reversed(snapshot.schedules["agent"]))},
        }
        with snapshot.patches(), patch.object(evaluation, "AzureCliCredential") as credential:
            for field, value in mutations.items():
                snapshot.put_json("manifest.json", {**original, field: value})
                with self.subTest(field=field), self.assertRaises(ValueError):
                    snapshot.load()
            credential.assert_not_called()

    def test_continuation_rejects_corrupt_snapshots_and_unfinished_workers(self):
        snapshot = PriorSnapshot()
        original = dict(snapshot.files)
        for change in ("cases", "definition", "catalogue", "source_version", "source_unchanged", "cleanup", "worker_settings", "counts", "running"):
            snapshot.files = dict(original)
            if change == "cases":
                snapshot.put_json("cases.json", [{**snapshot.cases[0], "question": "changed"}, *snapshot.cases[1:]])
            elif change == "definition":
                snapshot.put_json("source-definition.json", {**SOURCE, "instructions": "changed"})
            elif change == "catalogue":
                snapshot.files[str(snapshot.root / "catalog.txt")] += b"changed"
            elif change in ("source_version", "source_unchanged"):
                value = snapshot.json("source-unchanged.json")
                value["current_version" if change == "source_version" else "unchanged"] = "8" if change == "source_version" else False
                snapshot.put_json("source-unchanged.json", value)
            elif change == "cleanup":
                value = snapshot.json("cleanup.json")
                value["workers"]["gpt-5.4"]["status"] = "error"
                snapshot.put_json("cleanup.json", value)
            elif change == "worker_settings":
                path = Path("gpt-5.4") / "manifest.json"
                snapshot.put_json(path, {**snapshot.json(path), "client_token_budget_per_61s": 1})
            else:
                value = snapshot.json("summary.json")
                if change == "counts":
                    value["workers"]["gpt-5.4"]["completed_turns"] += 1
                else:
                    value["workers"]["gpt-5.4"]["status"] = "running"
                snapshot.put_json("summary.json", value)
            with snapshot.patches(), self.subTest(change=change), self.assertRaises(ValueError):
                snapshot.load()

    def test_continuation_rejects_duplicate_truncated_or_mismatched_result_records(self):
        snapshot = PriorSnapshot()
        original = dict(snapshot.files)
        relative = Path("gpt-5.4") / "oracle-none.jsonl"
        file = str(snapshot.root / relative)
        first = copy.deepcopy(snapshot.rows[str(relative)][0])
        for change in ("duplicate", "truncated", "missing_file", "sequence", "case_id", "question", "effort", "attempts", "completion", "actual_model", "usage"):
            snapshot.files = dict(original)
            record = copy.deepcopy(first)
            if change == "duplicate":
                snapshot.files[file] += json.dumps(first).encode() + b"\n"
            elif change == "truncated":
                snapshot.files[file] = snapshot.files[file][:-1]
            elif change == "missing_file":
                snapshot.files.pop(file)
            else:
                if change == "completion":
                    record["response"]["status"] = "incomplete"
                elif change == "usage":
                    record["usage"] = None
                else:
                    record[change] = {"sequence": 999, "case_id": "unknown", "question": "changed", "effort": "low",
                                      "attempts": 2, "actual_model": "different-model"}[change]
                remainder = snapshot.files[file].split(b"\n", 1)[1]
                snapshot.files[file] = json.dumps(record).encode() + b"\n" + remainder
            with snapshot.patches(), self.subTest(change=change), self.assertRaises(ValueError):
                snapshot.load()

    def test_continuation_rejects_permanent_and_exhausted_prior_errors(self):
        snapshot = PriorSnapshot()
        relative = next(path for path, rows in snapshot.rows.items() if any(row["status"] == "error" for row in rows))
        original = copy.deepcopy(snapshot.rows[relative])
        for change in ("permanent", "exhausted"):
            snapshot.rows[relative] = copy.deepcopy(original)
            record = next(row for row in snapshot.rows[relative] if row["status"] == "error")
            if change == "permanent":
                record["attempt_errors"][0]["error"] = {"status": 400, "body": {"error": "invalid_request_error"}}
            else:
                record["attempts"] = 6
                record["attempt_errors"] = [{**copy.deepcopy(record["attempt_errors"][0]), "attempt": i} for i in range(1, 7)]
            snapshot.write_rows(relative)
            with snapshot.patches(), self.subTest(change=change), self.assertRaises(ValueError):
                snapshot.load()

    def test_continuation_attempt_cap_and_prior_failure_evidence_are_cumulative(self):
        error = {
            "error": {"type": "APIError", "message": evaluation.STREAMED_PROVIDER_MESSAGE},
            "retryable": False, "partial": {"text": ""},
        }
        prior = {"status": "error", "attempts": 5, "finished_at": "2020-01-01T00:00:00+00:00",
                 "attempt_errors": [{**copy.deepcopy(error), "attempt": i} for i in range(1, 6)]}
        before = copy.deepcopy(prior)
        client = SimpleNamespace(responses=SimpleNamespace(create=Mock(side_effect=status_error(503))))
        log, _, _ = self.invoke_turn(client, prior=prior)
        record = log.records()[0]
        self.assertEqual(client.responses.create.call_count, 1)
        self.assertEqual((record["prior_attempts"], record["attempts_this_run"], record["attempts"]), (5, 1, 6))
        self.assertEqual(record["attempt_errors"][:5], before["attempt_errors"])
        self.assertEqual(record["status"], "error")
        self.assertEqual(prior, before)

    def test_live_source_or_catalogue_drift_blocks_recovery_before_inference(self):
        for kind in ("source", "catalogue"):
            snapshot, harness = PriorSnapshot(), Harness()
            if kind == "source":
                harness.versions[evaluation.SOURCE_AGENT].data["instructions"] = "Concurrent changed instructions"
            with snapshot.patches(), harness.patches():
                evaluation.fetch_catalogue.return_value = snapshot.catalogue + (" changed" if kind == "catalogue" else "")
                evidence = MemoryEvidence()
                code = evaluation.evaluate(snapshot.args, CFG, snapshot.cases, snapshot.cases_sha256, evidence, snapshot.load())
            self.assertEqual(code, 1)
            self.assertEqual(harness.requests, [])
            self.assertEqual(harness.created, [])

    def test_changed_original_during_recovery_is_detected(self):
        snapshot, harness = PriorSnapshot(), Harness()

        def mutate_original(name, request):
            if not getattr(harness, "mutated", False):
                harness.mutated = True
                manifest = snapshot.json("manifest.json")
                snapshot.put_json("manifest.json", {**manifest, "external_edit": True})

        harness.on_request = mutate_original
        code, evidence, _, _ = self.execute_continuation(snapshot, harness)
        self.assertEqual(code, 1)
        self.assertFalse(evidence.json("continuation-source-unchanged.json")["unchanged"])
        self.assertEqual(evidence.json("cleanup.json")["status"], "completed")

    def test_continuation_output_can_be_reused_again_without_any_new_requests(self):
        snapshot = PriorSnapshot()
        code, first, _, _ = self.execute_continuation(snapshot)
        self.assertEqual(code, 0)
        first_manifest = first.json("manifest.json")
        snapshot.from_output(first)
        code, second, harness, prior = self.execute_continuation(snapshot)
        self.assertEqual(code, 0)
        self.assertEqual(harness.requests, [])
        self.assertEqual(harness.created, [])
        self.assertEqual(len(harness.credentials), 1)
        self.assertEqual(len(prior.completed), 792)
        self.assertEqual(second.json("summary.json")["reused_completed_turns"], 792)
        self.assertEqual(second.json("summary.json")["request_attempts"], 793)
        self.assertEqual(second.json("summary.json")["api_error_attempts"], 1)
        self.assertEqual(second.json("manifest.json")["continuation"]["source_run_id"], first_manifest["run_id"])
        self.assertEqual(second.documents["prior-errors.jsonl"], first.documents["prior-errors.jsonl"])
        self.assertEqual(second.json("cleanup.json")["status"], "completed")

    def test_interrupted_continuation_keeps_unretried_failure_attempts_in_history(self):
        snapshot, harness = PriorSnapshot(), Harness()

        def interrupt_before_paid_call(name, live):
            if live.data["model"] == "gpt-5.4":
                raise RuntimeError("Offline interruption before a paid call")

        harness.on_create = interrupt_before_paid_call
        code, first, _, original = self.execute_continuation(snapshot, harness)
        self.assertEqual(code, 1)
        failed_key = next(key for key, record in original.failed.items() if record["status"] == "error")
        self.assertEqual(sum(call["model"] == "gpt-5.4" for call in harness.requests), 0)
        first_records = [
            row for path, log in first.logs.items()
            if Path(path).name in ("agent-none.jsonl", "agent-low.jsonl")
            for row in log.records()
        ]
        self.assertFalse(any(evaluation.turn_key(record) == failed_key for record in first_records))
        snapshot.from_output(first)
        future = datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp()
        with patch.object(evaluation.time, "time", return_value=future):
            code, second, _, prior = self.execute_continuation(snapshot)
        self.assertEqual(code, 0)
        self.assertEqual(prior.failed[failed_key]["attempts"], 1)
        self.assertEqual(prior.failed_source_run_ids[failed_key], "original-offline-run")
        self.assertEqual(second.json("summary.json")["completed_turns"], 792)
        self.assertEqual(second.json("summary.json")["request_attempts"], 793)
        self.assertEqual(second.json("summary.json")["api_error_attempts"], 1)
        recovered = next(
            record for path, log in second.logs.items()
            if Path(path).name in ("agent-none.jsonl", "agent-low.jsonl")
            for record in log.records() if evaluation.turn_key(record) == failed_key
        )
        self.assertEqual(recovered["attempts"], 2)
        self.assertEqual(len(recovered["attempt_errors"]), 1)

    def test_continuation_inherits_adaptive_reserve_and_quota_cooldown(self):
        snapshot = PriorSnapshot()
        path = str(Path("gpt-5.4") / "oracle-none.jsonl")
        snapshot.rows[path][0]["usage"].update(input_tokens=42000, output_tokens=3000, total_tokens=45000)
        snapshot.write_rows(path)
        with snapshot.patches():
            prior = snapshot.load()
        self.assertEqual(prior.reserves["gpt-5.4"], 45000)
        original_finish = datetime(2020, 1, 1, 0, 0, 3, tzinfo=timezone.utc).timestamp()
        self.assertEqual(prior.not_before["gpt-5.4"], original_finish + 65)
        harness, stop, evidence = Harness(), threading.Event(), MemoryEvidence()
        with harness.patches(), patch.object(stop, "wait", return_value=False) as wait, \
                patch.object(evaluation.time, "time", return_value=original_finish + 10):
            result = evaluation.run_model(
                "gpt-5.4", snapshot.args, CFG, snapshot.cases, prior.source, snapshot.catalogue,
                snapshot.schedules, "new-run", evidence, stop, prior,
            )
        self.assertEqual(result["status"], "completed")
        wait.assert_called_once_with(55)
        self.assertEqual(FakePacer.instances[0].reserve, 45000)


if __name__ == "__main__":
    unittest.main()
