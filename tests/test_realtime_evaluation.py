"""Offline tests; no Azure/network calls, private env reads, or evidence disk writes.

    $env:PYTHON_DOTENV_DISABLED='1'
    uv run --offline --no-sync python tests\\test_realtime_evaluation.py
"""
from __future__ import annotations

import asyncio
import base64
import copy
import inspect
import json
import os
import sys
import unittest
from collections import deque
from contextlib import ExitStack, asynccontextmanager, contextmanager, redirect_stderr
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bench_realtime_evaluation as evaluation
from azure.ai.voicelive import models
from azure.ai.voicelive.aio import connect

CLI = ["--env-file", "never-read.env", "--cases", "never-read.json", "--output-dir", "never-written"]
SETTINGS = {
    **evaluation.ENV_DEFAULTS, "AZURE_VOICELIVE_ENDPOINT": "https://voice.invalid",
    "AZURE_SEARCH_ENDPOINT": "https://search.invalid", "SEARCH_INDEX_NAME": "eval-model-sections-v1-dc53",
}
CASES = [
    {"id": "Q1", "question": "What was decided?", "group": "minutes", "cohort": "holdout",
     "expected": "internal", "answerable": True, "oracle_context": "  EXACT SOURCE PASSAGES\n"},
    {"id": "W2", "question": "What is the latest news?", "group": "web", "cohort": "negative",
     "expected": "external", "answerable": False},
]
TOOLS = [{"type": "function", "name": name, "parameters": {
    "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
}} for name in ("search_minutes", "search_web")]
PCM = b"\x00\xff\x01\x00\x02\x00"


class MemoryEvidence:
    def __init__(self, redact=None):
        self.redact, self.events, self.documents = redact or evaluation.Redactor(), {}, {}
        self.closed = False

    def emit(self, kind, value):
        self.events.setdefault(kind, []).append(copy.deepcopy(self.redact(value)))

    def write_json(self, name, value):
        self.documents[name] = copy.deepcopy(self.redact(value))

    def write_text(self, name, value):
        self.documents[name] = self.redact(value)

    def close(self):
        self.closed = True


class ImmediatePacer(evaluation.Pacer):
    """Real reservation accounting, but no wall-clock waits in stream/matrix tests."""

    def __init__(self, *args):
        super().__init__(*args)
        self.delays, self.waited_stages, self.response_waits = [], [], 0

    def defer(self, seconds):
        self.delays.append(seconds)

    def response_delay(self):
        return 0.0

    async def wait_response(self):
        self.response_waits += 1

    async def wait(self, stage="live"):
        if self.active is not None:
            raise RuntimeError("Unfinished reservation")
        self.waited_stages.append(stage)
        reserve = self.reserves[stage]
        self.active = evaluation.TokenReservation(stage, reserve, reserve, 0)
        self.calls.append(self.active)
        return self.reservation_snapshot()


class FakeConnection:
    def __init__(self, events=(), *, enter_error=None, echo=None):
        self.events, self.enter_error, self.echo = deque(events), enter_error, echo
        self.items, self.updates, self.response_creates = [], [], 0
        self.entered, self.closed, self.recv_cancelled = False, False, False
        self.session = SimpleNamespace(update=self.update)
        self.conversation = SimpleNamespace(item=SimpleNamespace(create=self.create_item))
        self.response = SimpleNamespace(create=self.create_response)

    async def __aenter__(self):
        if self.enter_error:
            raise self.enter_error
        self.entered = True
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def update(self, *, session):
        data = evaluation.plain(session)
        self.updates.append(data)
        self.events.appendleft({"type": "session.updated", "session": self.echo if self.echo is not None else data})
        self.events.appendleft({"type": "session.created", "session": {"id": "fresh"}})

    async def create_item(self, *, item):
        self.items.append(evaluation.plain(item))

    async def create_response(self):
        self.response_creates += 1

    async def recv(self):
        if not self.events:
            raise EOFError("offline socket end")
        event = self.events.popleft()
        if event == "stall":
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.recv_cancelled = True
                raise
        if isinstance(event, Exception):
            raise event
        return event(self) if callable(event) else event


class FakeConnector:
    def __init__(self, connections):
        self.connections, self.calls = deque(connections), []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.connections.popleft()


def runtime(connections, result=None):
    return SimpleNamespace(
        connect=FakeConnector(connections), models=models, credential=object(),
        prompt="Frozen production persona.", catalogue="FROZEN CATALOGUE", tools=copy.deepcopy(TOOLS),
        execute_function=AsyncMock(return_value=result if result is not None else {
            "passages": [{"id": "section1", "source": "original.docx", "extract": "Full passage", "truncated": False}],
        }),
    )


def scheduled(model=None, stage="live"):
    return dict(case_id="Q1", model=model or evaluation.MODELS[0], stage=stage, run=1)


def answer_events(text="Grounded answer.", transcript="Grounded answer.", audio=PCM, rid="answer"):
    base = {"response_id": rid, "item_id": rid + "-item", "output_index": 0, "content_index": 0}
    events = [{"type": "response.created", "response": {"id": rid}}]
    if text is not None:
        events += [{"type": "response.text.delta", **base, "delta": ""},
                   {"type": "response.text.delta", **base, "delta": text},
                   {"type": "response.text.done", **base, "text": text}]
    if transcript is not None:
        events += [{"type": "response.audio_transcript.delta", **base, "delta": transcript},
                   {"type": "response.audio_transcript.done", **base, "transcript": transcript}]
    if audio is not None:
        events.append({"type": "response.audio.delta", **base, "delta": audio})
    part = {"type": "audio" if text is None else "text"}
    if text is not None:
        part["text"] = text
    if transcript is not None:
        part["transcript"] = transcript
    events.append({"type": "response.done", "response": {
        "id": rid, "status": "completed", "output": [{
            "id": rid + "-item", "type": "message", "role": "assistant", "status": "completed", "content": [part],
        }], "usage": {"total_tokens": 27, "input_tokens": 10, "output_tokens": 17,
                     "output_token_details": {"text_tokens": 5, "audio_tokens": 12}},
    }})
    return events


def tool_events(call_id="call-1", name="search_minutes", arguments='{"query":"meeting approval"}'):
    item = {"id": call_id + "-item", "type": "function_call", "call_id": call_id,
            "name": name, "arguments": arguments, "status": "completed"}
    return [
        {"type": "response.created", "response": {"id": call_id + "-response"}},
        {"type": "response.output_item.added", "item": {**item, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": item["id"], "delta": arguments},
        {"type": "response.function_call_arguments.done", "item_id": item["id"],
         "call_id": call_id, "name": name, "arguments": arguments},
        {"type": "response.output_item.done", "item": item},
        {"type": "response.done", "response": {"id": call_id + "-response", "status": "completed",
                                             "output": [item], "usage": {"total_tokens": 1000}}},
    ]


@contextmanager
def fake_clock():
    clock = SimpleNamespace(now=100.0, sleeps=[])
    async def sleep(seconds):
        if seconds < 0 or seconds > 1000:
            raise AssertionError("Unbounded/invalid delay")
        clock.sleeps.append(seconds)
        clock.now += seconds
    with patch.object(evaluation.time, "monotonic", side_effect=lambda: clock.now), \
            patch.object(evaluation.asyncio, "sleep", side_effect=sleep):
        yield clock


class ConfigurationTests(unittest.TestCase):
    def test_defaults_and_exact_198_turn_matrix(self):
        args = evaluation.parse_args(CLI)
        self.assertEqual(args.models, ["gpt-realtime-2.1", "gpt-realtime-2.1-mini"])
        self.assertEqual((args.api_version, args.voice), ("2026-04-10", "en-ZA-LeahNeural"))
        self.assertEqual((args.interval, args.token_budget, args.reserve_tokens), (8, 90000, 15000))
        cases = [{**CASES[0], "id": f"Q{i}"} for i in range(16)] + [CASES[1]]
        plan = evaluation.build_schedule(cases, args)
        self.assertEqual(len(plan), 198)
        self.assertEqual(sum(row["stage"] == "oracle" for row in plan), 96)
        self.assertEqual(sum(row["stage"] == "live" for row in plan), 102)
        self.assertEqual(plan, evaluation.build_schedule(cases, args))
        self.assertEqual({row["model"] for row in plan}, set(args.models))
        for offset in range(0, len(plan), 2):
            self.assertEqual({row["model"] for row in plan[offset:offset + 2]}, set(args.models))

    def test_pilot_subset_and_exact_optional_model_ids(self):
        args = evaluation.parse_args(CLI + ["--case-ids", "Q1", "--runs", "1", "--stages", "live"])
        with patch.object(Path, "read_bytes", return_value=json.dumps(CASES).encode()):
            cases, _ = evaluation.load_cases(Path("unread.json"), args.case_ids)
        self.assertEqual(len(evaluation.build_schedule(cases, args)), 2)
        for model in ("mai-mm-realtime", "phi4-mm-realtime"):
            selected = evaluation.parse_args(CLI + ["--models", model])
            self.assertEqual({row["model"] for row in evaluation.build_schedule(CASES, selected)}, {model})
        for report in evaluation.PRIOR_CAPABILITY_REPORTS:
            self.assertEqual(report["source"], "parent_reported")
            self.assertFalse(report["quality_evaluated"])
            self.assertIsNone(report["quality_score"])

    def test_cases_gold_exclusion_and_verbatim_oracle_context(self):
        dirty = [{**CASES[0], "gold_answer": "NEVER SEND GOLD", "scoring_rules": "NEVER SEND RUBRIC"}]
        with patch.object(Path, "read_bytes", return_value=json.dumps(dirty).encode()):
            cases, digest = evaluation.load_cases(Path("unread.json"))
        self.assertEqual(cases[0]["oracle_context"], CASES[0]["oracle_context"])
        self.assertNotIn("gold_answer", cases[0])
        self.assertEqual(len(digest), 64)
        for stage in evaluation.STAGES:
            payload = json.dumps(evaluation.input_messages("CATALOGUE", dirty[0], stage))
            self.assertNotIn("NEVER SEND", payload)
            self.assertNotIn("expected", payload)
        with patch.object(Path, "read_bytes", return_value=json.dumps(dirty * 2).encode()):
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                evaluation.load_cases(Path("unread.json"))

    def test_explicit_env_and_process_only_restoration(self):
        ambient = {"AGENT_NAME": "old-agent", "AGENT_PROJECT_NAME": "old-project",
                   "AZURE_SEARCH_API_KEY": "old-search-key", "AZURE_CLIENT_ID": "managed-identity",
                   "AZURE_CLIENT_SECRET": "old-secret", "VOICELIVE_MODEL": "wrong-model",
                   "VOICE_BINDING": "agent", "WEBIQ_ALLOWED_DOMAINS": "wrong.invalid", "UNRELATED": "keep"}
        values = {**SETTINGS, "WEBIQ_API_KEY": "fixture-web-secret", "AGENT_NAME": "file-agent",
                  "AZURE_SEARCH_API_KEY": "file-search-key"}
        with patch.dict(os.environ, ambient, clear=True):
            with patch("dotenv.dotenv_values", return_value=values) as reader, \
                    patch.object(Path, "is_file", return_value=True):
                cfg, redact = evaluation.load_settings(Path("synthetic-unread.env"))
            reader.assert_called_once_with(Path("synthetic-unread.env"), interpolate=False)
            with evaluation.isolated_environment(cfg):
                for key in ("AGENT_NAME", "AGENT_PROJECT_NAME", "AZURE_SEARCH_API_KEY",
                            "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "VOICELIVE_MODEL"):
                    self.assertNotIn(key, os.environ)
                self.assertEqual(os.environ["VOICE_BINDING"], "model")
                self.assertEqual(os.environ["AZURE_TOKEN_CREDENTIALS"], "AzureCliCredential")
                self.assertEqual(os.environ["PYTHON_DOTENV_DISABLED"], "1")
                self.assertEqual(os.environ["WEBIQ_ALLOWED_DOMAINS"], "")
                self.assertEqual(os.environ["UNRELATED"], "keep")
            self.assertEqual(dict(os.environ), ambient)
            self.assertNotIn("fixture-web-secret", redact("error fixture-web-secret"))
        with patch("dotenv.dotenv_values", return_value={**SETTINGS, "AI_SEARCH_TOP_K": "4"}), \
                patch.object(Path, "is_file", return_value=True):
            with self.assertRaisesRegex(ValueError, "fixed"):
                evaluation.load_settings(Path("synthetic-unread.env"))

    def test_safe_budget_validation(self):
        for extra in (["--token-budget", "0"], ["--token-budget", "120001"],
                      ["--reserve-tokens", "90001"], ["--reserve-tokens", "0"], ["--interval", "nan"]):
            with self.subTest(extra=extra), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                evaluation.parse_args(CLI + extra)
        args = evaluation.parse_args(CLI + ["--token-budget", "60000", "--reserve-tokens", "8000"])
        self.assertEqual((args.token_budget, args.reserve_tokens), (60000, 8000))

    def test_manifest_secret_redaction_source_proof_and_no_live_helper_imports(self):
        redact = evaluation.Redactor(["fixture-sensitive-key"])
        evidence, args, rt = MemoryEvidence(redact), evaluation.parse_args(CLI), runtime([])
        evaluation.freeze(evidence, {**SETTINGS, "WEBIQ_API_KEY": "fixture-sensitive-key"},
                          args, CASES, "case-source-hash", evaluation.build_schedule(CASES, args), rt)
        manifest = evidence.documents["manifest.json"]
        self.assertNotIn("WEBIQ_API_KEY", manifest["config"])
        self.assertNotIn("fixture-sensitive-key", json.dumps(evidence.documents))
        self.assertEqual(manifest["client_token_policy"]["global_budget"], 90000)
        self.assertEqual(manifest["client_token_policy"]["initial_reservation"], 15000)
        self.assertEqual(manifest["timing"]["primary_latency_field"], "timings.final_answer_first_received_ms")
        self.assertEqual(manifest["oracle_system_overlay"], evaluation.ORACLE_SYSTEM_OVERLAY)
        self.assertIn("NOT an observed quota", manifest["documented_limits_reference"]["source"])
        self.assertEqual(manifest["execution_location"], "local")
        self.assertEqual(manifest["tool_execution_location"], "local")
        self.assertIn("not the deployed application", manifest["measurement_scope"])
        self.assertIn("deployed-app end-to-end latency", manifest["timing"]["not_measured"])
        source = inspect.getsource(evaluation)
        for forbidden in ("import bench_routing_model", "import bench_routing_matrix", "AIProjectClient"):
            self.assertNotIn(forbidden, source)
        value = redact({"headers": {"Authorization": "unlisted-value"}, "access_token": "token-value",
                        "text": "fixture-sensitive-key Bearer abc.def.sig ?api_key=other&sig=signature"})
        for secret in ("unlisted-value", "token-value", "fixture-sensitive-key", "abc.def.sig", "signature"):
            self.assertNotIn(secret, json.dumps(value))

    def test_private_output_rejection_and_checkpoint_fsync(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            evaluation.Evidence(ROOT / "not-created", evaluation.Redactor())
        with patch.object(Path, "exists", autospec=True, side_effect=lambda p: p.name != ".git"), \
                patch.object(Path, "is_dir", return_value=True), \
                patch.object(Path, "iterdir", return_value=iter([Path("existing")])), \
                patch.object(Path, "mkdir") as mkdir:
            with self.assertRaisesRegex(ValueError, "new or empty"):
                evaluation.Evidence(ROOT.parent / "private-existing", evaluation.Redactor())
            mkdir.assert_not_called()
        stream = StringIO()
        evidence = evaluation.Evidence.__new__(evaluation.Evidence)
        evidence.logs, evidence.redact = {"attempts": stream}, evaluation.Redactor(["test-secret"])
        with patch.object(stream, "fileno", return_value=71), patch.object(stream, "flush") as flush, \
                patch.object(evaluation.os, "fsync") as fsync:
            evidence.emit("attempts", {"error": "test-secret"})
            flush.assert_called_once()
            fsync.assert_called_once_with(71)
        self.assertEqual(json.loads(stream.getvalue())["error"], "[REDACTED]")

    def test_reported_retry_after_and_reset_parsing(self):
        self.assertEqual(evaluation.retry_delay({"headers": {"Retry-After": "12"}}), 12)
        self.assertEqual(evaluation.retry_delay({"x-ms-retry-after-ms": "1250"}), 1.25)
        self.assertEqual(evaluation.retry_delay({"rate_limits": [
            {"name": "tokens", "remaining": 0, "reset_seconds": 35},
            {"name": "requests", "remaining": 29, "reset_seconds": 60},
        ]}), 35)
        self.assertEqual(evaluation.retry_delay({
            "x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "1m2.5s",
        }), 62.5)
        date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60))
        self.assertTrue(58 <= evaluation.retry_delay({"Retry-After": date}) <= 60)
        self.assertEqual(evaluation.retry_delay({"retry_after": "nan"}), 0)
        failure = evaluation.service_failure({"error": {"code": "unsupported_parameter", "status": 500}})
        self.assertFalse(failure.retryable)

    def test_short_secret_does_not_corrupt_serialized_json_numbers(self):
        evidence = evaluation.Evidence.__new__(evaluation.Evidence)
        evidence.redact = evaluation.Redactor(["1"])
        with patch.object(evidence, "_write") as write:
            evidence.write_json("manifest.json", {"schema_version": 1, "detail": "1", "usage": 123})
        value = json.loads(write.call_args.args[1])
        self.assertEqual(value, {"schema_version": 1, "detail": "[REDACTED]", "usage": 123})


class PacerTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_spacing_and_reported_reset(self):
        with fake_clock() as clock:
            pacer = evaluation.Pacer(8)
            await pacer.wait()
            pacer.finish_turn()
            await pacer.wait()
            pacer.finish_turn()
            pacer.defer(25)
            await pacer.wait()
            pacer.finish_turn()
        self.assertEqual(clock.sleeps, [8, 25])

    async def test_reservations_prevent_next_turn_exceeding_client_window(self):
        with fake_clock() as clock:
            pacer = evaluation.Pacer(0, 30000, 15000)
            for _ in range(2):
                await pacer.wait()
                pacer.finish_turn()
            await pacer.wait()
            self.assertEqual(clock.now, 161)
            self.assertEqual(sum(call.charged for call in pacer.calls), 15000)
            pacer.finish_turn()
        self.assertEqual(clock.sleeps, [61])

    async def test_sum_all_responses_audio_usage_and_adaptive_stage_reservation(self):
        with fake_clock() as clock:
            pacer = evaluation.Pacer(0)
            await pacer.wait("live")
            for tokens in (10000, 20000):
                pacer.response_started()
                pacer.account_usage({"total_tokens": tokens, "output_token_details": {"audio_tokens": 5000}})
            note = pacer.finish_turn()
            self.assertEqual(note["reported_tokens_sum"], 30000)
            self.assertEqual(note["responses_done"], 2)
            self.assertTrue(note["usage_complete"])
            self.assertEqual(note["client_charged_tokens"], 30000)
            self.assertEqual(note["next_stage_reservation"], 30000)
            self.assertEqual(pacer.reserves["oracle"], 15000)
            await pacer.wait("oracle")
            self.assertAlmostEqual(clock.now, 120)
            pacer.finish_turn()
            await pacer.wait("live")
            self.assertEqual(pacer.reservation_snapshot()["reserved_tokens"], 30000)
            pacer.finish_turn()

    async def test_missing_usage_and_unfinished_response_keep_reserves_not_fake_observations(self):
        with fake_clock():
            pacer = evaluation.Pacer(0)
            await pacer.wait()
            pacer.response_started()
            self.assertIsNone(pacer.account_usage(None))
            pacer.response_started()
            pacer.account_usage({"total_tokens": 7000})
            pacer.response_started()  # No response.done: retain this reservation too.
            note = pacer.finish_turn()
            self.assertEqual(note["reported_tokens_sum"], 7000)
            self.assertFalse(note["usage_complete"])
            self.assertEqual(note["client_charged_tokens"], 37000)
            await pacer.wait("oracle")
            note = pacer.finish_turn()
            self.assertIsNone(note["reported_tokens_sum"])
            self.assertEqual(note["client_charged_tokens"], 15000)

    async def test_usage_overrun_is_charged_and_expires_without_infinite_wait(self):
        with fake_clock() as clock:
            pacer = evaluation.Pacer(0)
            await pacer.wait()
            pacer.response_started()
            pacer.account_usage({"total_tokens": 100000})
            note = pacer.finish_turn()
            self.assertEqual(note["client_charged_tokens"], 100000)
            self.assertEqual(note["next_stage_reservation"], 90000)
            await pacer.wait()
            self.assertGreaterEqual(clock.now, 161)
            self.assertLess(clock.now, 168)
            self.assertEqual(pacer.reservation_snapshot()["reserved_tokens"], 90000)
            pacer.finish_turn()

    async def test_reported_reset_gates_continuation_without_new_session(self):
        with fake_clock() as clock:
            pacer = evaluation.Pacer(8)
            await pacer.wait()
            pacer.defer(35)
            await pacer.wait_response()
            self.assertEqual(clock.now, 135)
            self.assertEqual(pacer.last_start, 100)
            pacer.finish_turn()


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def run_one(self, events, *, stage="live", model=None, result=None, args=None, connection=None, rt=None):
        args = args or evaluation.parse_args(CLI)
        connection = connection or FakeConnection(events)
        rt = rt or runtime([connection], result)
        evidence = MemoryEvidence()
        pacer = ImmediatePacer(args.interval, args.token_budget, args.reserve_tokens)
        await pacer.wait(stage)
        record = await evaluation.run_turn(rt, SETTINGS, args, CASES[0], scheduled(model, stage),
                                           1, evidence.emit, pacer)
        self.assertEqual(len(evidence.events["attempts"]), 1)
        return record, connection, rt, evidence, pacer

    async def test_model_specific_args_fresh_connections_actual_app_profile(self):
        connections = [FakeConnection(answer_events()) for _ in evaluation.MODELS]
        rt, evidence, args = runtime(connections), MemoryEvidence(), evaluation.parse_args(CLI)
        pacer = ImmediatePacer(8)
        for model in evaluation.MODELS:
            await pacer.wait()
            record = await evaluation.run_turn(rt, SETTINGS, args, CASES[0], scheduled(model), 1, evidence.emit, pacer)
            self.assertEqual(record["status"], "completed")
        self.assertEqual([call["model"] for call in rt.connect.calls], list(evaluation.MODELS))
        for call in rt.connect.calls:
            self.assertEqual(set(call), {"endpoint", "credential", "api_version", "model"})
            self.assertLessEqual(set(call), set(inspect.signature(connect).parameters))
            self.assertEqual(call["api_version"], "2026-04-10")
        for connection in connections:
            self.assertTrue(connection.closed)
            self.assertEqual(connection.response_creates, 1)
            config = connection.updates[0]
            self.assertEqual(config["voice"]["name"], "en-ZA-LeahNeural")
            self.assertEqual(config["modalities"], ["text", "audio"])
            self.assertFalse(config["parallel_tool_calls"])
            self.assertIsNone(config["input_audio_transcription"])
            self.assertNotIn("avatar", config)

    async def test_explicit_preview_is_not_an_implicit_fallback(self):
        args = evaluation.parse_args(CLI + ["--api-version", "2026-06-01-preview"])
        record, _, rt, _, _ = await self.run_one(answer_events(), args=args)
        self.assertEqual(record["connect_args"]["api_version"], "2026-06-01-preview")
        self.assertEqual(rt.connect.calls[0]["api_version"], "2026-06-01-preview")

    async def test_oracle_roles_and_disabled_tools(self):
        record, connection, rt, _, _ = await self.run_one(answer_events(), stage="oracle")
        self.assertEqual(record["status"], "completed")
        self.assertEqual([item["role"] for item in connection.items], ["system", "system", "system", "user"])
        self.assertEqual([item["content"][0]["text"] for item in connection.items], [
            rt.catalogue, CASES[0]["oracle_context"], evaluation.ORACLE_SYSTEM_OVERLAY, CASES[0]["question"],
        ])
        self.assertEqual(connection.updates[0]["tools"], [])
        self.assertEqual(connection.updates[0]["tool_choice"], "none")
        rt.execute_function.assert_not_awaited()

    async def test_native_roundtrip_call_id_full_results_and_all_response_usage(self):
        result = {"passages": [{"id": "block-60", "source": "original.docx", "extract": "x" * 12000,
                                "original_chars": 12000, "truncated": False}], "note": "Production note."}
        def require_function_output(connection):
            self.assertEqual(connection.response_creates, 2)
            self.assertEqual(connection.items[-1]["type"], "function_call_output")
            self.assertEqual(connection.items[-1]["call_id"], "call-1")
            self.assertEqual(json.loads(connection.items[-1]["output"]), result)
            return {"type": "rate_limits.updated", "rate_limits": [
                {"name": "tokens", "remaining": 0, "reset_seconds": 35},
            ]}
        record, connection, rt, evidence, pacer = await self.run_one(
            tool_events() + [require_function_output] + answer_events(), result=result
        )
        self.assertEqual(record["status"], "completed")
        rt.execute_function.assert_awaited_once_with("search_minutes", '{"query":"meeting approval"}')
        self.assertEqual(record["tool_calls"][0]["result"], result)
        self.assertEqual(record["responses"][0]["tool_calls"], record["tool_calls"])
        self.assertEqual(record["token_accounting"]["reported_tokens_sum"], 1027)
        self.assertEqual(record["token_accounting"]["responses_done"], 2)
        self.assertTrue(record["token_accounting"]["usage_complete"])
        self.assertEqual(record["usage"][-1]["usage"]["output_token_details"]["audio_tokens"], 12)
        self.assertIn(35, pacer.delays)
        self.assertEqual(pacer.response_waits, 2)
        self.assertNotIn(CASES[0]["oracle_context"], json.dumps(connection.items))
        self.assertEqual(evidence.events["attempts"][0]["tool_calls"][0]["result"], result)

    async def test_web_function_uses_actual_dispatch_not_routing_stub(self):
        record, _, rt, _, _ = await self.run_one(tool_events(name="search_web") + answer_events(),
                                                result={"results": [{"url": "https://public.invalid", "text": "News"}]})
        self.assertEqual(record["status"], "completed")
        rt.execute_function.assert_awaited_once_with("search_web", '{"query":"meeting approval"}')

    async def test_web_publication_update_crawl_and_other_result_fields_remain_unchanged(self):
        result = {
            "results": [
                {"url": "https://public.invalid/dated", "title": "Dated fixture", "text": "A source quote.",
                 "published": "2020-01-02", "last_updated": "2026-09-18T07:08:09.123Z",
                 "crawled_at": "2026-09-19T10:11:12.456Z", "source_identifier": "fixture-row-1"},
                {"url": "https://public.invalid/undated", "text": "Undated source.",
                 "last_updated": "", "crawled_at": "2026-09-19T10:11:12.456Z"},
            ],
            "note": "Publication, update and crawl metadata is not a live-quote timestamp.",
        }
        record, connection, _, evidence, _ = await self.run_one(
            tool_events(name="search_web") + answer_events(), result=result
        )
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["execution_location"], "local")
        self.assertEqual(record["tool_calls"][0]["execution_location"], "local")
        self.assertEqual(record["tool_calls"][0]["result"], result)
        self.assertEqual(json.loads(connection.items[-1]["output"]), result)
        self.assertEqual(evidence.events["attempts"][0]["tool_calls"][0]["result"], result)
        tool_event = next(event for event in evidence.events["events"] if event["kind"] == "tool_result")
        self.assertEqual(tool_event["data"]["result"], result)
        self.assertNotIn("published", record["tool_calls"][0]["result"]["results"][1])

    async def test_sdk_bytes_base64_empty_chunks_and_channel_dedup(self):
        sdk = models.ServerEventResponseAudioDelta(response_id="answer", item_id="answer-item",
                                                  output_index=0, content_index=0, delta=PCM)
        for audio in (PCM, base64.b64encode(PCM).decode(), sdk):
            with self.subTest(kind=type(audio).__name__):
                events = answer_events(audio=None)
                events.insert(-1, {"type": "response.audio.delta", "delta": b""})
                events.insert(-1, audio if hasattr(audio, "as_dict") else {"type": "response.audio.delta", "delta": audio})
                record, _, _, evidence, _ = await self.run_one(events)
                self.assertEqual(record["status"], "completed")
                self.assertEqual(record["audio_bytes"], len(PCM))
                self.assertEqual(record["answer_text"], "Grounded answer.")
                self.assertEqual(record["channels"], {"TEXT": "Grounded answer.", "AUDIO_TRANSCRIPT": "Grounded answer."})
                first = next(event for event in evidence.events["events"] if event["kind"] == "server_event"
                             and event["data"]["type"] == "response.text.delta" and event["data"]["delta"])
                self.assertEqual(record["timings"]["first_any_received_ms"]["TEXT"], first["turn_elapsed_ms"])

    async def test_mini_preamble_never_wins_final_answer_latency(self):
        events, preamble = tool_events(), "Calling the readiness probe now."
        base = {"response_id": "call-1-response", "item_id": "preamble", "output_index": 0}
        events[1:1] = [
            {"type": "response.text.delta", **base, "content_index": 0, "delta": preamble},
            {"type": "response.audio_transcript.delta", **base, "content_index": 1, "delta": preamble},
            {"type": "response.audio.delta", **base, "content_index": 1, "delta": PCM},
        ]
        events[-1]["response"]["output"].insert(0, {
            "id": "preamble", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "text", "text": preamble}, {"type": "audio", "transcript": preamble}],
        })
        record, _, _, _, _ = await self.run_one(
            events + answer_events(text="Readiness: Ready", transcript="Readiness: Ready"), model="gpt-realtime-2.1-mini"
        )
        self.assertEqual(record["answer_text"], "Readiness: Ready")
        self.assertEqual(record["preamble_response_ids"], ["call-1-response"])
        self.assertEqual(record["responses"][0]["channels"], {"TEXT": preamble, "AUDIO_TRANSCRIPT": preamble})
        self.assertFalse(record["responses"][0]["is_final_answer"])
        self.assertTrue(record["responses"][1]["is_final_answer"])
        self.assertEqual(record["audio_bytes"], 2 * len(PCM))
        self.assertEqual(record["final_answer_audio_bytes"], len(PCM))
        for channel in ("TEXT", "AUDIO_TRANSCRIPT", "PCM"):
            self.assertLess(record["timings"]["first_any_received_ms"][channel],
                            record["timings"]["final_answer_first_received_ms"][channel])
            self.assertEqual(record["timings"]["final_answer_first_received_ms"][channel],
                             record["responses"][1]["first_received_ms"][channel])

    async def test_spoken_json_is_never_parsed_into_function_calls(self):
        spoken = '[{"name":"search_minutes","arguments":{"query":"something"}}]'
        record, _, rt, _, _ = await self.run_one(answer_events(text=None, transcript=spoken))
        self.assertEqual(record["tool_calls"], [])
        self.assertEqual(record["responses"][0]["function_calls"], [])
        rt.execute_function.assert_not_awaited()
        self.assertEqual(record["answer_text"], spoken)

    async def test_late_preamble_cannot_contaminate_final_response(self):
        events = answer_events()
        events.insert(1, {"type": "response.audio.delta", "response_id": "call-1-response", "delta": PCM})
        record, _, _, _, _ = await self.run_one(tool_events() + events)
        self.assertEqual(record["error"]["code"], "response_id_mismatch")
        self.assertEqual(record["answer_text"], "")
        self.assertIsNone(record["timings"]["final_answer_first_received_ms"]["PCM"])

    async def test_completed_empty_response_is_failure_without_retry_or_zero_score(self):
        args = evaluation.parse_args(CLI + ["--runs", "1", "--stages", "live"])
        events = [{"type": "response.done", "response": {
            "id": "empty", "status": "completed", "output": [], "usage": {"total_tokens": 1, "output_tokens": 1},
        }}]
        rt, evidence, summary = runtime([FakeConnection(events)]), MemoryEvidence(), {}
        plan = evaluation.build_schedule([CASES[0]], args)
        with patch.object(evaluation, "Pacer", ImmediatePacer):
            await evaluation.run_matrix(rt, SETTINGS, args, [CASES[0]], plan, evidence, summary)
        self.assertEqual(len(rt.connect.calls), 1)
        self.assertEqual(summary["status"], "partial")
        record = evidence.events["attempts"][0]
        self.assertEqual(record["error"]["code"], "empty_response")
        self.assertFalse(record["error"]["retryable"])
        self.assertEqual(record["response_done"][0]["output"], [])
        self.assertEqual(record["token_accounting"]["reported_tokens_sum"], 1)
        self.assertIsNone(record["timings"]["final_answer_first_received_ms"]["PCM"])
        self.assertNotIn("quality_score", record)
        self.assertTrue(evidence.events["events"])

    async def test_tool_errors_unknown_calls_truncation_and_bad_arguments_never_full_success(self):
        variants = [
            ("search_minutes", '{"query":"x"}', {"error": "Search failed"}, None),
            ("invented", '{"query":"x"}', None, None),
            ("search_minutes", '{"query":"x"}', {"passages": [{"truncated": True}]}, None),
            ("search_web", '{"query":"x"}', None, RuntimeError("upstream failure")),
            ("search_minutes", "not-json", None, None),
        ]
        for name, arguments, result, error in variants:
            with self.subTest(name=name, args=arguments):
                connection = FakeConnection(tool_events(name=name, arguments=arguments) + answer_events())
                rt = runtime([connection], result)
                if error:
                    rt.execute_function.side_effect = error
                record, connection, _, _, _ = await self.run_one([], rt=rt, connection=connection)
                self.assertEqual(record["status"], "completed_with_tool_error")
                self.assertEqual(record["tool_calls"][0]["status"], "error")
                self.assertEqual(connection.items[-1]["type"], "function_call_output")

    async def test_incomplete_error_truncation_missing_audio_and_unsupported_parameters(self):
        variants = [
            ([{"type": "error", "error": {"code": "unsupported_parameter", "message": "not supported"}}], "unsupported_parameter"),
            ([{"type": "response.done", "response": {"status": "incomplete"}}], "incomplete_response"),
            ([{"type": "response.done", "response": {"status": "failed", "status_details": {"error": {"code": "bad_request"}}}}], "bad_request"),
            (answer_events()[:-1], "truncated_stream"),
            (answer_events(audio=None), "missing_answer_channel"),
            (answer_events(audio="not-base64"), "invalid_audio"),
        ]
        for events, code in variants:
            with self.subTest(code=code):
                record, connection, _, evidence, _ = await self.run_one(events)
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["error"]["code"], code)
                self.assertFalse(record["error"]["retryable"])
                self.assertTrue(connection.closed)
                self.assertEqual(evidence.events["attempts"][0]["status"], "failed")

    async def test_actual_async_timeout_retains_partial_channels_and_token_reservation(self):
        args = evaluation.parse_args(CLI + ["--turn-timeout", "0.015"])
        record, connection, _, _, _ = await self.run_one(answer_events()[:-1] + ["stall"], args=args)
        self.assertEqual(record["error"]["code"], "timeout")
        self.assertTrue(connection.recv_cancelled and connection.closed)
        self.assertEqual(record["responses"][0]["channels"]["TEXT"], "Grounded answer.")
        self.assertEqual(record["audio_bytes"], len(PCM))
        self.assertEqual(record["token_accounting"]["client_charged_tokens"], 15000)
        self.assertIsNone(record["token_accounting"]["reported_tokens_sum"])

    async def test_tool_timeout_returns_explicit_output_and_not_success(self):
        args = evaluation.parse_args(CLI + ["--tool-timeout", "0.01"])
        connection = FakeConnection(tool_events() + answer_events())
        rt = runtime([connection])
        async def stall(*args):
            await asyncio.Future()
        rt.execute_function.side_effect = stall
        record, connection, _, _, _ = await self.run_one([], rt=rt, connection=connection, args=args)
        self.assertEqual(record["status"], "completed_with_tool_error")
        self.assertIn("TimeoutError", connection.items[-1]["output"])

    async def test_bounded_calls_oracle_tool_rejection_and_echo_rejection(self):
        record, _, rt, _, _ = await self.run_one(sum((tool_events(f"call-{i}") for i in range(4)), []))
        self.assertEqual(record["error"]["code"], "tool_limit")
        self.assertEqual(rt.execute_function.await_count, 3)
        record, _, rt, _, _ = await self.run_one(tool_events() + answer_events(), stage="oracle")
        self.assertEqual(record["error"]["code"], "oracle_tool_call")
        rt.execute_function.assert_not_awaited()
        connection = FakeConnection(answer_events(), echo={"modalities": ["text"]})
        record, _, _, _, _ = await self.run_one([], connection=connection)
        self.assertEqual(record["error"]["code"], "session_profile_mismatch")
        self.assertEqual(connection.response_creates, 0)

    async def test_sdk_configuration_rejection_is_persisted_without_connecting(self):
        rt = runtime([])
        rt.models = SimpleNamespace(RequestSession=Mock(side_effect=ValueError("unsupported parameter")))
        record, _, _, evidence, _ = await self.run_one([], rt=rt)
        self.assertEqual(record["error"]["phase"], "session_config")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(rt.connect.calls, [])
        self.assertEqual(evidence.events["attempts"][0]["token_accounting"]["client_charged_tokens"], 15000)

    async def test_absent_usage_and_rate_limits_remain_absent(self):
        events = answer_events()
        events[-1]["response"].pop("usage")
        record, _, _, _, _ = await self.run_one(events)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["usage"], [])
        self.assertEqual(record["rate_limit_events"], [])
        self.assertIsNone(record["token_accounting"]["reported_tokens_sum"])
        self.assertFalse(record["token_accounting"]["usage_complete"])

    async def test_cancellation_checkpoints_and_closes(self):
        connection, evidence, args, pacer = FakeConnection(["stall"]), MemoryEvidence(), evaluation.parse_args(CLI), ImmediatePacer(8)
        rt = runtime([connection])
        await pacer.wait()
        task = asyncio.create_task(evaluation.run_turn(rt, SETTINGS, args, CASES[0], scheduled(), 1, evidence.emit, pacer))
        while not connection.response_creates:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(connection.closed)
        self.assertEqual(evidence.events["attempts"][0]["status"], "cancelled")
        self.assertIsNone(pacer.active)

    async def test_four_transient_attempts_only_no_model_fallback(self):
        args = evaluation.parse_args(CLI + ["--runs", "1", "--stages", "live"])
        plan = evaluation.build_schedule([CASES[0]], args)
        error = {"type": "error", "error": {"code": "rate_limit_exceeded", "retry_after": 17}}
        rt, evidence, summary = runtime([FakeConnection([error]) for _ in range(4)]), MemoryEvidence(), {}
        with patch.object(evaluation, "Pacer", ImmediatePacer):
            await evaluation.run_matrix(rt, SETTINGS, args, [CASES[0]], plan, evidence, summary)
        self.assertEqual(len(rt.connect.calls), 4)
        self.assertEqual({call["model"] for call in rt.connect.calls}, {plan[0]["model"]})
        self.assertEqual((summary["failed"], summary["skipped"]), (1, 1))
        self.assertEqual(len(evidence.events["attempts"]), 4)
        self.assertEqual(evidence.events["attempts"][0]["error"]["retry_after_seconds"], 17)

    async def test_retry_after_handshake_headers_are_safe_and_retry_can_succeed(self):
        class HandshakeError(RuntimeError):
            status = 429
            headers = {"Retry-After": "40", "Authorization": "must-not-persist"}
        args = evaluation.parse_args(CLI + ["--runs", "1", "--stages", "live", "--models", evaluation.MODELS[0]])
        rt, evidence, summary = runtime([FakeConnection(enter_error=HandshakeError("slow down")),
                                         FakeConnection(answer_events())]), MemoryEvidence(), {}
        with patch.object(evaluation, "Pacer", ImmediatePacer):
            await evaluation.run_matrix(rt, SETTINGS, args, [CASES[0]], evaluation.build_schedule([CASES[0]], args), evidence, summary)
        self.assertEqual((summary["status"], summary["attempts"]), ("completed", 2))
        self.assertEqual(evidence.events["attempts"][0]["error"]["retry_after_seconds"], 40)
        self.assertNotIn("must-not-persist", json.dumps(evidence.events))

    async def test_unavailability_separate_from_quality_and_explicit_partial_policy(self):
        args = evaluation.parse_args(CLI + ["--runs", "1", "--stages", "live",
                                           "--models", "mai-mm-realtime", evaluation.MODELS[0],
                                           "--failure-policy", "continue-other-models"])
        plan = [scheduled("mai-mm-realtime"), scheduled(evaluation.MODELS[0])]
        rt, evidence, summary = runtime([
            FakeConnection([{"type": "error", "error": {"code": "invalid_model", "message": "Not supported in region"}}]),
            FakeConnection(answer_events()),
        ]), MemoryEvidence(), {}
        with patch.object(evaluation, "Pacer", ImmediatePacer):
            await evaluation.run_matrix(rt, SETTINGS, args, [CASES[0]], plan, evidence, summary)
        self.assertEqual((summary["status"], summary["unavailable"], summary["failed"], summary["completed"]),
                         ("partial", 1, 0, 1))
        self.assertEqual([call["model"] for call in rt.connect.calls], ["mai-mm-realtime", evaluation.MODELS[0]])
        report = evidence.events["availability"][0]
        self.assertIsNone(report["quality_score"])
        self.assertEqual(report["source"], "run_observed")

    async def test_execute_artifacts_keep_prior_capabilities_outside_scored_matrix(self):
        args = evaluation.parse_args(CLI + ["--runs", "1", "--stages", "live"])
        rt = runtime([FakeConnection(answer_events()) for _ in evaluation.MODELS])
        evidence = MemoryEvidence()
        @asynccontextmanager
        async def fake_runtime():
            yield rt
        with patch.object(evaluation, "Evidence", return_value=evidence), \
                patch.object(evaluation, "production_runtime", fake_runtime), \
                patch.object(evaluation, "Pacer", ImmediatePacer):
            summary = await evaluation.execute(args, SETTINGS, evaluation.Redactor(), [CASES[0]], "hash",
                                               evaluation.build_schedule([CASES[0]], args))
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["execution_location"], "local")
        self.assertTrue(evidence.closed)
        self.assertEqual(len(evidence.documents["availability.json"]["reports"]), 2)
        self.assertEqual(evidence.documents["manifest.json"]["models"], list(evaluation.MODELS))
        self.assertTrue(all(row["model"] in evaluation.MODELS for row in evidence.events["turns"]))
        self.assertIn("summary.json", evidence.documents)

    async def test_production_runtime_uses_cli_real_dispatch_and_closes_clients(self):
        with evaluation.isolated_environment(SETTINGS):
            from backend.voice import auth, catalog, functions, instructions, tools
            credentials = []
            def make_credential(**kwargs):
                credential = SimpleNamespace(close=AsyncMock(), **kwargs)
                credentials.append(credential)
                return credential
            async def schemas():
                self.assertIs(auth.create_credential(""), auth._default_credential)
                self.assertEqual(auth._default_credential._inner.process_timeout, 120)
                self.assertEqual(os.environ["VOICE_BINDING"], "model")
                return copy.deepcopy(TOOLS)
            with ExitStack() as stack:
                stack.enter_context(patch("azure.identity.aio.AzureCliCredential", side_effect=make_credential))
                stack.enter_context(patch.object(tools, "build_realtime_tools", side_effect=schemas))
                stack.enter_context(patch.object(catalog, "get_meeting_catalog", AsyncMock(return_value="CATALOGUE")))
                closed_search = stack.enter_context(patch.object(catalog, "close_search_client", AsyncMock()))
                stack.enter_context(patch.object(instructions, "load_realtime_instructions", return_value="PROMPT"))
                async with evaluation.production_runtime() as rt:
                    self.assertIs(rt.execute_function, functions.execute_function)
                    self.assertEqual(rt.catalogue, "CATALOGUE")
                closed_search.assert_awaited_once()
            for credential in credentials:
                credential.close.assert_awaited_once()
            self.assertEqual(len(credentials), 2)
            self.assertIsNone(auth._default_credential)
            self.assertIsNone(tools._aad_credential)

    async def test_missing_webiq_refuses_degraded_profile_and_cleans_credentials(self):
        with evaluation.isolated_environment(SETTINGS):
            from backend.voice import auth, catalog, tools
            with patch("azure.identity.aio.AzureCliCredential", side_effect=lambda **kw: SimpleNamespace(close=AsyncMock())), \
                    patch.object(tools, "build_realtime_tools", AsyncMock(return_value=[TOOLS[0]])), \
                    patch.object(catalog, "get_meeting_catalog", AsyncMock()) as catalogue:
                with self.assertRaisesRegex(ValueError, "Both production"):
                    async with evaluation.production_runtime():
                        self.fail("No WebIQ stub/degraded profile allowed")
                catalogue.assert_not_awaited()
            self.assertIsNone(auth._default_credential)
            self.assertIsNone(tools._aad_credential)


if __name__ == "__main__":
    unittest.main()
