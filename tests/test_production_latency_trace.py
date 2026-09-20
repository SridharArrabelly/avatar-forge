"""Offline production telemetry tests. Run: python tests\\test_production_latency_trace.py."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

from azure.ai.voicelive.models import ItemType, Response, ServerEventType, TokenUsage
from backend.voice import event_handlers, handler as handler_module
from backend.voice.handler import VoiceSessionHandler
from backend.voice.latency_trace import LatencyTrace, create_response

PRIVATE = "PRIVATE_QUESTION_PROMPT_TRANSCRIPT_ARGUMENT_RESULT_https://secret.invalid_key"


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms / 1000

    def at(self, ms):
        self.ms = ms


def created(rid="resp_1"):
    return NS(type=ServerEventType.RESPONSE_CREATED, response=NS(id=rid))


def delta(kind, rid="resp_1", value=PRIVATE):
    return NS(type=kind, response_id=rid, delta=value)


def done(rid="resp_1", *, tool=False, status="completed", usage=None):
    return NS(
        type=ServerEventType.RESPONSE_DONE,
        response=NS(
            id=rid, status=status, usage=usage,
            status_details={"error": PRIVATE, "reason": PRIVATE},
            output=[NS(type="function_call" if tool else "message", content=PRIVATE)],
        ),
    )


def usage(inputs=100, outputs=20, cached=30):
    return {
        "input_tokens": inputs, "output_tokens": outputs,
        "total_tokens": inputs + outputs,
        "input_token_details": {
            "cached_tokens": cached,
            "cached_tokens_details": {"text_tokens": cached, "audio_tokens": 0},
        },
        "private_extra": PRIVATE,
    }


def expected_usage(inputs, outputs, cached, *, cached_audio=0):
    return {
        "total_tokens": inputs + outputs,
        "input_tokens": inputs, "output_tokens": outputs, "cached_input_tokens": cached,
        "input_text_tokens": None, "input_audio_tokens": None, "input_image_tokens": None,
        "output_text_tokens": None, "output_audio_tokens": None, "output_reasoning_tokens": None,
        "cached_input_text_tokens": cached, "cached_input_audio_tokens": cached_audio,
        "cached_input_image_tokens": None,
    }


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.trace = LatencyTrace(True, self.clock)
        self.log_patch = patch("backend.voice.latency_trace.logger.info")
        self.log = self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        warning_patch = patch("backend.voice.latency_trace.logger.warning")
        self.warning = warning_patch.start()
        self.addCleanup(warning_patch.stop)

    def observe(self, at, event, trace=None):
        self.clock.at(at)
        (trace or self.trace).observe(event)

    def records(self):
        return [json.loads(call.args[1]) for call in self.log.call_args_list]

    def test_exact_boundaries_preamble_usage_and_content_free_summary(self):
        turn = self.trace.begin("text_submission_received")
        self.observe(1, created())
        self.observe(2, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA))
        self.observe(3, delta(ServerEventType.RESPONSE_AUDIO_DELTA, value=b"\0\1"))
        self.observe(4, NS(
            type=ServerEventType.RESPONSE_OUTPUT_ITEM_ADDED, response_id="resp_1",
            item=NS(type=ItemType.FUNCTION_CALL, name=PRIVATE),
        ))
        self.clock.at(5)
        token = self.trace.function_announced(PRIVATE)
        self.clock.at(6)
        self.trace.tool_mark(token, "arguments_ready_ms", count=52)
        self.clock.at(7)
        self.trace.tool_mark(token, "execution_start_ms")
        self.clock.at(9)
        self.trace.tool_mark(token, "execution_end_ms", status="completed")
        self.observe(20, done(tool=True, usage=usage()))
        self.observe(21, done(tool=True, usage=usage()))  # duplicate is not billable twice
        self.log.assert_not_called()
        self.clock.at(22)
        self.trace.tool_mark(token, "output_send_start_ms", count=105)
        self.clock.at(23)
        self.trace.tool_mark(token, "output_send_done_ms")
        self.clock.at(24)
        send = self.trace.response_create_start(turn, "tool")
        self.clock.at(25)
        self.trace.response_create_end(send)
        self.observe(26, created("resp_answer"))
        self.observe(27, delta(ServerEventType.RESPONSE_TEXT_DELTA, "resp_answer"))
        self.observe(28, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA, "resp_answer"))
        self.observe(29, delta(ServerEventType.RESPONSE_AUDIO_DELTA, "resp_answer", b"\0\1\2\3"))
        self.observe(30, delta(ServerEventType.RESPONSE_AUDIO_DELTA, "resp_answer", b"\0\1"))
        self.log.assert_not_called()
        self.observe(40, done("resp_answer", usage=usage(200, 40, 50)))
        self.observe(41, done("resp_answer", usage=usage(200, 40, 50)))
        record, = self.records()
        self.assertEqual(record["milestones_ms"], dict(t0=0, t1=7, t2=9, t3=23, t4=28, t5=29))
        self.assertEqual(record["first_any_ms"], dict(audio_transcript_ms=2, pcm_ms=3, text_ms=27))
        self.assertEqual(record["tools"][0]["announced_ms"], 4)
        self.assertEqual(record["tools"][0]["arguments_ready_ms"], 6)
        self.assertEqual(record["tools"][0]["name"], "other")
        self.assertEqual(record["tools"][0]["argument_chars"], 52)
        self.assertEqual(record["tools"][0]["output_chars"], 105)
        self.assertEqual(record["response_create_sends"], [dict(reason="tool", start_ms=24, done_ms=25)])
        self.assertEqual(record["responses"][0]["done_ms"], 20)
        self.assertEqual(record["responses"][1]["first_pcm_bytes"], 4)
        self.assertEqual(record["usage_totals"], expected_usage(300, 60, 80))
        self.assertTrue(record["trace_valid"])
        self.assertEqual(record["instrumentation_errors"], 0)
        self.assertEqual(record["terminal_response_id"], "resp_answer")
        self.assertEqual(record["terminal_pcm"], "observed")
        self.assertNotIn(PRIVATE, json.dumps(record))
        self.assertRegex(record["turn_id"], r"^[0-9a-f]{32}$")

    def test_missing_pcm_and_late_user_transcription_do_not_fake_t5_or_reset_t0(self):
        self.observe(100, NS(type=ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED, audio_end_ms=PRIVATE))
        self.observe(110, created())
        self.observe(120, NS(type=ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED, transcript=PRIVATE))
        self.observe(130, delta(ServerEventType.RESPONSE_TEXT_DELTA))
        self.observe(140, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA))
        self.observe(145, delta("response.video.delta"))
        self.observe(150, done())
        record, = self.records()
        self.assertEqual(record["origin"], "audio_speech_stopped_received")
        self.assertEqual(record["milestones_ms"]["t4"], 40)
        self.assertIsNone(record["milestones_ms"]["t5"])
        self.assertEqual(record["terminal_pcm"], "unobserved")
        self.assertTrue(all(value is None for value in record["usage_totals"].values()))

    def test_agent_tool_cues_do_not_claim_actual_execution(self):
        self.trace = LatencyTrace(False, self.clock)
        self.trace.begin("text_submission_received")
        self.observe(1, created())
        self.observe(2, NS(type=ServerEventType.RESPONSE_OUTPUT_ITEM_ADDED, response_id="resp_1",
                           item=NS(type="file_search_call", name="search_minutes")))
        self.observe(3, NS(type="function_call_started", functionName="search_minutes"))
        self.assertIsNone(self.trace.function_announced("search_minutes"))
        self.observe(4, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA))
        self.observe(5, done())
        record, = self.records()
        self.assertEqual(record["tool_phases"], "unavailable_managed")
        self.assertEqual(record["tools"], [])
        self.assertEqual([record["milestones_ms"][key] for key in ("t1", "t2", "t3")], [None] * 3)
        self.assertIsNone(record["milestones_ms"]["t4"])
        self.assertEqual(record["first_any_ms"]["audio_transcript_ms"], 4)
        self.assertEqual(record["terminal_boundary"], "unavailable_managed")

    def test_cancelled_or_failed_response_does_not_claim_terminal_answer(self):
        for index, status in enumerate(("cancelled", "failed", "incomplete")):
            self.trace.begin("text_submission_received")
            rid = f"resp_{index}"
            self.observe(10, created(rid))
            self.observe(11, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA, rid))
            self.observe(12, delta(ServerEventType.RESPONSE_AUDIO_DELTA, rid, b"\1"))
            self.observe(13, done(rid, status=status, usage=usage()))
        self.assertEqual(len(self.records()), 3)
        for record in self.records():
            self.assertIsNone(record["milestones_ms"]["t4"])
            self.assertIsNone(record["milestones_ms"]["t5"])
            self.assertIsNotNone(record["first_any_ms"]["pcm_ms"])
            self.assertNotIn(PRIVATE, json.dumps(record))

    def test_superseded_turn_ignores_late_events_and_tool_completions(self):
        old = self.trace.begin("text_submission_received")
        self.observe(1, created("resp_old"))
        token = self.trace.function_announced("search_web")
        self.clock.at(2)
        new = self.trace.begin("text_submission_received")
        self.observe(3, created("resp_new"))
        self.trace.tool_mark(token, "execution_end_ms", status="completed")
        self.observe(4, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA, "resp_old"))
        self.observe(5, done("resp_old", usage=usage()))
        self.trace.finish(old, "error", "tool_error")
        self.assertIs(self.trace.current, new)
        self.observe(6, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA, "resp_new"))
        self.observe(7, done("resp_new", usage=usage(5, 2, 1)))
        first, second = self.records()
        self.assertEqual(first["reason"], "superseded")
        self.assertEqual(second["milestones_ms"]["t4"], 4)
        self.assertEqual(second["usage_totals"]["input_tokens"], 5)
        self.assertEqual(second["tools"], [])
        self.assertNotEqual(first["turn_id"], second["turn_id"])

    def test_interleaved_sessions_with_same_response_id_are_isolated(self):
        other = LatencyTrace(True, self.clock)
        self.trace.begin("text_submission_received")
        other.begin("audio_speech_stopped_received")
        self.observe(1, created())
        self.observe(2, created(), other)
        self.observe(3, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA))
        self.observe(4, delta(ServerEventType.RESPONSE_AUDIO_DELTA, value=b"\1"), other)
        self.observe(5, done(usage=usage(10, 5, 2)), other)
        self.observe(6, done(usage=usage(20, 10, 3)))
        second_session, first_session = self.records()
        self.assertIsNone(second_session["milestones_ms"]["t4"])
        self.assertEqual(second_session["milestones_ms"]["t5"], 4)
        self.assertEqual(first_session["milestones_ms"]["t4"], 3)
        self.assertIsNone(first_session["milestones_ms"]["t5"])

    def test_provider_error_is_content_free_and_already_stopped_error_is_ignored(self):
        turn = self.trace.begin("text_submission_received")
        self.observe(1, created())
        self.observe(2, NS(type=ServerEventType.ERROR, error={"code": "response_cancel_not_active"}))
        self.log.assert_not_called()
        self.observe(3, NS(type=ServerEventType.ERROR, error={"code": PRIVATE, "message": PRIVATE}))
        self.trace.finish(turn, "error", "handler_error")
        record, = self.records()
        self.assertEqual(record["reason"], "provider_error")
        self.assertNotIn(PRIVATE, json.dumps(record))

    def test_real_sdk_usage_missing_and_invalid_counts(self):
        self.trace.begin("text_submission_received")
        self.observe(1, created())
        response = Response(
            id="resp_1", status="completed", output=[],
            usage=TokenUsage(
                input_tokens=12, output_tokens=5, total_tokens=17,
                input_token_details={"cached_tokens": 3, "cached_tokens_details": {"text_tokens": 3}},
                output_token_details={},
            ),
        )
        self.observe(2, NS(type=ServerEventType.RESPONSE_DONE, response=response))
        self.assertEqual(self.records()[0]["usage_totals"],
                         expected_usage(12, 5, 3, cached_audio=None))
        self.trace.begin("text_submission_received")
        self.observe(3, created("resp_2"))
        self.observe(4, done("resp_2", usage={"input_tokens": PRIVATE, "output_tokens": True,
                                           "input_token_details": {"cached_tokens": -1}}))
        self.assertTrue(all(value is None for value in self.records()[1]["usage_totals"].values()))

    def test_multiple_tool_rounds_and_bounded_metadata(self):
        turn = self.trace.begin("text_submission_received")
        for index in range(36):
            rid = f"resp_{index}"
            self.observe(index * 10 + 1, created(rid))
            token = self.trace.function_announced("search_minutes")
            self.trace.tool_mark(token, "execution_start_ms")
            self.trace.tool_mark(token, "execution_end_ms", status="completed")
            self.observe(index * 10 + 2, done(rid, tool=True, usage=usage(1, 1, 0)))
        self.log.assert_not_called()
        self.observe(370, created("resp_answer"))
        self.observe(371, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA, "resp_answer"))
        self.observe(380, done("resp_answer", usage=usage(1, 1, 0)))
        record, = self.records()
        self.assertTrue(record["metadata_truncated"])
        self.assertLessEqual(len(record["responses"]), 32)
        self.assertLessEqual(len(record["tools"]), 32)
        self.assertTrue(all(value is None for value in record["usage_totals"].values()))
        self.assertEqual(record["milestones_ms"]["t4"], 371)
        self.assertTrue(turn.closed)

    def test_logging_failure_is_best_effort_and_not_retried(self):
        turn = self.trace.begin("text_submission_received")
        self.log.side_effect = RuntimeError(PRIVATE)
        self.trace.finish(turn, "cancelled", "manual_interrupt")
        self.trace.finish(turn, "cancelled", "manual_interrupt")
        self.log.assert_called_once()
        self.assertIsNone(self.trace.current)
        self.assertFalse(turn.valid)
        self.assertEqual(turn.instrumentation_errors, 1)
        self.warning.assert_called_once_with(
            "%s", "[LATENCY_TRACE_ERROR] method=finish exception_class=RuntimeError",
        )

    def test_argument_ready_uses_received_event_not_waiter_resume_time(self):
        self.trace.begin("text_submission_received")
        self.observe(1, created())
        token = self.trace.function_announced("search_minutes")
        self.observe(2, NS(type=ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE,
                           response_id="resp_1", arguments=PRIVATE))
        self.clock.at(20)
        self.trace.tool_mark(token, "arguments_ready_ms", count=50)
        self.assertEqual(token[1]["arguments_ready_ms"], 2)

    def test_two_tool_rounds_sum_actual_usage_and_keep_first_and_last_boundaries(self):
        turn = self.trace.begin("text_submission_received")
        for index in range(2):
            rid = f"resp_tool{index}"
            self.observe(index * 10 + 1, created(rid))
            token = self.trace.function_announced("search_minutes")
            self.clock.at(index * 10 + 2)
            self.trace.tool_mark(token, "execution_start_ms")
            self.clock.at(index * 10 + 3)
            self.trace.tool_mark(token, "execution_end_ms", status="completed")
            self.observe(index * 10 + 4, done(rid, tool=True, usage=usage(100, 20, 30)))
            self.clock.at(index * 10 + 5)
            self.trace.tool_mark(token, "output_send_done_ms")
            send = self.trace.response_create_start(turn, "tool")
            self.trace.response_create_end(send)
        self.observe(21, created("resp_answer"))
        self.observe(22, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA, "resp_answer"))
        self.observe(23, done("resp_answer", usage=usage(100, 20, 30)))
        record, = self.records()
        self.assertEqual(record["milestones_ms"], dict(t0=0, t1=2, t2=13, t3=15, t4=22, t5=None))
        self.assertEqual(record["usage_totals"], expected_usage(300, 60, 90))
        self.assertEqual(len(record["responses"]), 3)

    def test_completed_response_waits_for_inflight_create_send_boundary(self):
        turn = self.trace.begin("text_submission_received")
        send = self.trace.response_create_start(turn, "text")
        self.observe(1, created())
        self.observe(2, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA))
        self.observe(3, delta(ServerEventType.RESPONSE_AUDIO_DELTA, value=b"\1"))
        self.observe(4, done(usage=usage()))
        self.log.assert_not_called()
        self.clock.at(5)
        self.trace.response_create_end(send)
        record, = self.records()
        self.assertEqual(record["response_create_sends"][0]["done_ms"], 5)
        self.assertEqual(record["responses"][0]["done_ms"], 4)
        self.assertEqual(record["milestones_ms"]["t5"], 3)

    def test_failed_cancelled_send_does_not_swallow_the_next_response(self):
        old = self.trace.begin("text_submission_received")
        send = self.trace.response_create_start(old, "text")
        self.trace.finish(old, "cancelled", "manual_interrupt")
        self.trace.response_create_end(send, failed=True)
        self.trace.begin("text_submission_received")
        self.observe(1, created("resp_next"))
        self.observe(2, done("resp_next"))
        self.assertEqual(self.records()[1]["terminal_response_id"], "resp_next")

    def test_private_ids_status_and_non_pcm_are_not_serialized(self):
        turn = self.trace.begin("text_submission_received")
        self.observe(1, created(PRIVATE))
        self.observe(2, delta(ServerEventType.RESPONSE_AUDIO_DELTA, value=PRIVATE))
        self.trace.finish(turn, PRIVATE, PRIVATE)
        record, = self.records()
        self.assertEqual(record["status"], "unknown")
        self.assertIsNone(record["responses"][0]["id"])
        self.assertIsNone(record["milestones_ms"]["t5"])
        self.assertNotIn(PRIVATE, json.dumps(record))

    def test_capture_failure_is_explicit_invalid_bounded_and_content_free(self):
        turn = self.trace.begin("text_submission_received")
        self.observe(1, created())
        token = self.trace.function_announced("search_minutes")
        private_class = type(PRIVATE, (ValueError,), {})
        with patch.object(self.trace, "clock", side_effect=private_class(PRIVATE)):
            for _ in range(300):
                self.trace.tool_mark(token, "execution_start_ms")
        self.observe(2, done(tool=True, usage=usage()))
        self.observe(3, created("resp_answer"))
        self.observe(4, done("resp_answer", usage=usage()))
        record, = self.records()
        self.assertEqual(record["status"], "completed")
        self.assertFalse(record["trace_valid"])
        self.assertEqual(record["instrumentation_errors"], 255)
        self.assertEqual(record["instrumentation_errors_session"], 255)
        self.assertTrue(all(record["milestones_ms"][f"t{i}"] is None for i in range(1, 6)))
        self.assertTrue(all(value is None for value in record["usage_totals"].values()))
        self.assertEqual(record["terminal_pcm"], "invalid")
        self.warning.assert_called_once_with(
            "%s", "[LATENCY_TRACE_ERROR] method=tool_mark exception_class=ValueError",
        )
        self.assertNotIn(PRIVATE, str(self.warning.call_args_list))
        self.assertTrue(turn.closed)

    def test_summary_logging_failure_has_safe_stderr_and_descriptor_fallbacks(self):
        for stderr_fails in (False, True):
            turn = self.trace.begin("text_submission_received")
            self.log.side_effect = RuntimeError(PRIVATE)
            self.warning.side_effect = OSError(PRIVATE)
            with patch("backend.voice.latency_trace.sys.stderr.write") as stderr, \
                    patch("backend.voice.latency_trace.os.write") as descriptor:
                if stderr_fails:
                    stderr.side_effect = OSError(PRIVATE)
                self.trace.finish(turn, "cancelled", "manual_interrupt")
                diagnostic = "[LATENCY_TRACE_ERROR] method=finish exception_class=RuntimeError\n"
                stderr.assert_called_once_with(diagnostic)
                if stderr_fails:
                    descriptor.assert_called_once_with(2, diagnostic.encode("ascii"))
                else:
                    descriptor.assert_not_called()
                self.assertFalse(turn.valid)
                self.assertEqual(turn.instrumentation_errors, 1)
                self.assertNotIn(PRIVATE, str(stderr.call_args_list))
        self.assertEqual(self.warning.call_count, 2)

    def test_pending_overflow_disables_correlation_instead_of_reassigning_owners(self):
        turn = self.trace.begin("text_submission_received")
        for _ in range(128):
            self.assertIsNotNone(self.trace.response_create_start(turn, "text"))
        self.assertEqual(len(self.trace._pending), 128)
        self.assertIsNone(self.trace.response_create_start(turn, "text"))
        self.assertFalse(self.trace.correlation_valid)
        self.assertFalse(turn.valid)
        self.assertEqual(len(self.trace._pending), 0)
        self.warning.assert_called_once_with(
            "%s", "[LATENCY_TRACE_ERROR] method=pending_overflow exception_class=OverflowError",
        )
        self.assertIsNone(self.trace.begin("text_submission_received"))
        for index in range(200):
            self.assertIsNone(self.trace.response_create_start(None, "text"))
            self.observe(index + 1, created(f"resp_late{index}"))
            self.observe(index + 1, done(f"resp_late{index}", usage=usage()))
        record, = self.records()
        self.assertFalse(record["trace_valid"])
        self.assertFalse(record["correlation_valid"])
        self.assertEqual(record["instrumentation_errors"], 1)
        self.assertEqual(record["responses"], [])
        self.assertEqual(len(self.trace._responses), 0)
        self.assertEqual(len(self.trace._pending), 0)

    def test_response_id_bound_never_evicts_a_tombstone_for_reassignment(self):
        for index in range(128):
            self.observe(index, created(f"resp_unowned{index}"))
        turn = self.trace.begin("text_submission_received")
        self.observe(130, created("resp_new"))
        self.assertFalse(self.trace.correlation_valid)
        self.assertFalse(turn.valid)
        self.observe(131, created("resp_unowned0"))
        self.observe(132, done("resp_unowned0", usage=usage()))
        self.assertEqual(turn.responses, [])
        self.assertEqual(len(self.trace._responses), 0)
        self.warning.assert_called_once_with(
            "%s", "[LATENCY_TRACE_ERROR] method=response_id_overflow exception_class=OverflowError",
        )
        self.trace.finish(turn, "cancelled", "session_closed")
        self.assertFalse(self.records()[0]["trace_valid"])

    def test_provider_details_are_numeric_reported_values_not_extra_charges(self):
        self.trace.begin("text_submission_received")
        self.observe(1, created())
        detail = {
            "total_tokens": 120, "input_tokens": 100, "output_tokens": 20,
            "input_token_details": {
                "cached_tokens": 30, "text_tokens": 80, "audio_tokens": 20,
                "cached_tokens_details": {"text_tokens": 25, "audio_tokens": 5, "image_tokens": 0},
            },
            "output_token_details": {
                "text_tokens": 10, "audio_tokens": 5, "reasoning_tokens": 5, "content": PRIVATE,
            },
            "credentials": PRIVATE,
        }
        self.observe(2, NS(
            type=ServerEventType.RESPONSE_DONE,
            response=Response(id="resp_1", status="completed", output=[], usage=detail),
        ))
        record, = self.records()
        counts = record["usage_totals"]
        self.assertEqual(counts["total_tokens"], 120)
        self.assertEqual(counts["input_tokens"], 100)
        self.assertEqual(counts["cached_input_tokens"], 30)
        self.assertEqual(counts["input_text_tokens"], 80)
        self.assertEqual(counts["input_audio_tokens"], 20)
        self.assertEqual(counts["output_text_tokens"], 10)
        self.assertEqual(counts["output_audio_tokens"], 5)
        self.assertEqual(counts["output_reasoning_tokens"], 5)
        self.assertEqual(counts["cached_input_text_tokens"], 25)
        self.assertEqual(counts["cached_input_audio_tokens"], 5)
        self.assertEqual(counts["cached_input_image_tokens"], 0)
        self.assertIsNone(counts["input_image_tokens"])
        self.assertNotIn(PRIVATE, json.dumps(record))
        self.trace.begin("text_submission_received")
        self.observe(3, created("resp_missing_total"))
        self.observe(4, done("resp_missing_total", usage={"input_tokens": 100, "output_tokens": 20}))
        self.assertIsNone(self.records()[1]["usage_totals"]["total_tokens"])

    def test_session_metadata_failure_cannot_silently_appear_valid(self):
        self.trace.session_config(NS(instructions=PRIVATE, tools=[object()]))
        turn = self.trace.begin("text_submission_received")
        self.trace.finish(turn, "cancelled", "manual_interrupt")
        record, = self.records()
        self.assertFalse(record["trace_valid"])
        self.assertEqual(record["instrumentation_errors_session"], 1)
        self.assertNotIn(PRIVATE, json.dumps(record))
        self.warning.assert_called_once_with(
            "%s", "[LATENCY_TRACE_ERROR] method=session_config exception_class=TypeError",
        )


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.log_patch = patch("backend.voice.latency_trace.logger.info")
        self.log = self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        warning_patch = patch.object(event_handlers.logger, "warning")
        warning_patch.start()
        self.addCleanup(warning_patch.stop)

    def make_handler(self, enabled=True):
        with patch.object(handler_module, "ENABLE_LATENCY_TRACE", enabled), \
                patch.object(handler_module, "MODEL_BINDING", True):
            handler = VoiceSessionHandler(
                PRIVATE, PRIVATE, Mock(), AsyncMock(),
                {"avatarEnabled": True, "avatarOutputMode": "webrtc"}, send_binary=AsyncMock(),
            )
        if enabled:
            handler._latency_trace = LatencyTrace(True, self.clock)
        handler.connection = NS(
            conversation=NS(item=NS(create=AsyncMock())),
            response=NS(create=AsyncMock(), cancel=AsyncMock()),
            output_audio_buffer=NS(clear=AsyncMock()), close=AsyncMock(),
        )
        return handler

    def records(self):
        return [json.loads(call.args[1]) for call in self.log.call_args_list]

    async def test_tool_overlap_consumed_response_usage_and_unchanged_sdk_payloads(self):
        handler = self.make_handler()
        handler._latency_trace.begin("text_submission_received")
        connection = handler.connection
        await event_handlers.handle_event(handler, created(), connection)
        self.clock.at(2)
        await event_handlers.handle_event(handler, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA), connection)
        self.clock.at(5)
        await event_handlers.handle_event(handler, NS(
            type=ServerEventType.RESPONSE_OUTPUT_ITEM_ADDED, response_id="resp_1",
            item=NS(type=ItemType.FUNCTION_CALL, name="search_minutes"),
        ), connection)
        args = json.dumps({"query": PRIVATE})
        result = {"passages": [PRIVATE], "source": PRIVATE}
        completed = asyncio.Event()
        execution_order = []
        clock = self.clock

        async def execute(name, arguments):
            execution_order.append("tool")
            self.assertEqual(clock.ms, 20)
            self.assertEqual((name, arguments), ("search_minutes", args))
            clock.at(35)
            completed.set()
            return result

        async def send_message(message):
            if message["type"] == "function_call_result":
                clock.at(110)

        async def output_send(**kwargs):
            execution_order.append("output_send")
            self.assertEqual(kwargs["previous_item_id"], "item_1")
            self.assertEqual(kwargs["item"].call_id, "call_1")
            self.assertEqual(json.loads(kwargs["item"].output), result)
            clock.at(120)

        async def response_send(**kwargs):
            self.assertEqual(kwargs, {})
            clock.at(130)

        class Events:
            def __init__(self):
                self.index = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                self.index += 1
                if self.index == 1:
                    clock.at(20)
                    return NS(type=ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE,
                              response_id="resp_1", call_id="call_1", arguments=args)
                if self.index == 2:
                    await completed.wait()
                    execution_order.append("response_done")
                    clock.at(100)
                    return done(tool=True, usage=usage(100, 10, 20))
                raise StopAsyncIteration

        events = Events()
        events.conversation = connection.conversation
        events.response = connection.response
        handler.send_message.side_effect = send_message
        connection.conversation.item.create.side_effect = output_send
        connection.response.create.side_effect = response_send
        self.clock.at(10)
        with patch.object(event_handlers, "execute_function", side_effect=execute) as execute_mock, \
                patch.object(event_handlers.audit, "record_tool") as audit_tool:
            await event_handlers.handle_conversation_item(handler, NS(
                item=NS(type=ItemType.FUNCTION_CALL, name="search_minutes",
                        call_id="call_1", id="item_1"),
            ), events)
        execute_mock.assert_awaited_once_with("search_minutes", args)
        self.assertEqual(audit_tool.call_args.kwargs["args"], args)
        self.assertEqual(audit_tool.call_args.kwargs["results"], result)
        self.assertEqual(execution_order, ["tool", "response_done", "output_send"])
        self.log.assert_not_called()
        self.clock.at(140)
        await event_handlers.handle_event(handler, created("resp_answer"), connection)
        self.clock.at(150)
        await event_handlers.handle_event(handler, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA, "resp_answer"), connection)
        self.clock.at(160)
        await event_handlers.handle_event(handler, delta(ServerEventType.RESPONSE_AUDIO_DELTA, "resp_answer", b"\0\1"), connection)
        self.log.assert_not_called()
        self.clock.at(180)
        await event_handlers.handle_event(handler, done("resp_answer", usage=usage(200, 30, 40)), connection)
        record, = self.records()
        self.assertEqual(record["milestones_ms"], dict(t0=0, t1=20, t2=35, t3=120, t4=150, t5=160))
        self.assertEqual(record["tools"][0]["announced_ms"], 5)
        self.assertEqual(record["tools"][0]["arguments_ready_ms"], 20)
        self.assertEqual(record["tools"][0]["output_send_start_ms"], 110)
        self.assertEqual(record["responses"][0]["done_ms"], 100)
        self.assertEqual(record["response_create_sends"], [dict(reason="tool", start_ms=120, done_ms=130)])
        self.assertEqual(record["usage_totals"], expected_usage(300, 40, 60))
        self.assertNotIn(PRIVATE, json.dumps(record))
        handler.send_binary.assert_awaited_once_with(b"\0\1")

    async def test_text_origin_and_response_create_sdk_boundaries(self):
        handler = self.make_handler()

        async def item_send(**kwargs):
            self.assertEqual(kwargs["item"].content[0].text, PRIVATE)
            self.clock.at(10)

        async def response_send(**kwargs):
            self.assertEqual(kwargs, {})
            self.clock.at(20)

        handler.connection.conversation.item.create.side_effect = item_send
        handler.connection.response.create.side_effect = response_send
        await handler.send_text_message(PRIVATE)
        self.clock.at(30)
        await event_handlers.handle_event(handler, created(), handler.connection)
        self.clock.at(40)
        await event_handlers.handle_event(handler, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA), handler.connection)
        self.clock.at(50)
        await event_handlers.handle_event(handler, done(), handler.connection)
        record, = self.records()
        self.assertEqual(record["origin"], "text_submission_received")
        self.assertEqual(record["milestones_ms"]["t4"], 40)
        self.assertEqual(record["response_create_sends"], [dict(reason="text", start_ms=10, done_ms=20)])

    async def test_flag_off_has_no_new_trace_and_preserves_legacy_latency_logs(self):
        with patch.object(handler_module, "LatencyTrace", side_effect=AssertionError("disabled constructor")):
            handler = self.make_handler(enabled=False)
        self.assertIsNone(handler._latency_trace)
        with patch.object(event_handlers, "_now_ms", side_effect=[10, 20, 30, 40, 50]), \
                patch.object(event_handlers.logger, "info") as event_log:
            await handler.send_text_message(PRIVATE)
            await event_handlers.handle_event(handler, NS(
                type=ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
                transcript=PRIVATE, item_id="item_audio",
            ), handler.connection)
            await event_handlers.handle_event(handler, created(), handler.connection)
            await event_handlers.handle_event(handler, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA), handler.connection)
            await event_handlers.handle_event(handler, delta(ServerEventType.RESPONSE_AUDIO_DELTA, value=b"\0"), handler.connection)
            video = {"type": "response.video.delta", "delta": PRIVATE}

            class Video(dict):
                type = "response.video.delta"

            await event_handlers.handle_event(handler, Video(video), handler.connection)
            await event_handlers.handle_event(handler, done(), handler.connection)
            await handler.interrupt()
            await handler.stop()
        self.log.assert_not_called()
        legacy = [call.args[0] for call in event_log.call_args_list if "[LATENCY]" in call.args[0]]
        self.assertEqual(legacy, [
            "[LATENCY] user_done->response_created=10ms",
            "[LATENCY] first audio_transcript delta: response_created->first_token=10ms, user_done->first_token=20ms",
            "[LATENCY] first audio: user_done->audio=30ms, response_created->audio=20ms",
            "[LATENCY] first avatar video: user_done->video=40ms",
        ])
        self.assertEqual(handler._t_user_done_ms, 10)
        self.assertEqual(handler._t_response_created_ms, 20)
        handler.send_binary.assert_awaited_once_with(b"\0")

    async def test_flag_on_suppresses_legacy_per_delta_logs(self):
        handler = self.make_handler()
        await handler.send_text_message(PRIVATE)
        with patch.object(event_handlers, "_now_ms", side_effect=AssertionError("legacy clock")), \
                patch.object(event_handlers.logger, "info") as event_log:
            await event_handlers.handle_event(handler, NS(
                type=ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
                transcript=PRIVATE,
            ), handler.connection)
            await event_handlers.handle_event(handler, created(), handler.connection)
            await event_handlers.handle_event(handler, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA), handler.connection)
            await event_handlers.handle_event(handler, delta(ServerEventType.RESPONSE_AUDIO_DELTA, value=b"\0"), handler.connection)
            await event_handlers.handle_event(handler, done(), handler.connection)
        self.assertFalse(any("[LATENCY]" in str(call) for call in event_log.call_args_list))
        record, = self.records()
        self.assertTrue(record["trace_valid"])
        self.assertFalse(hasattr(handler, "_t_user_done_ms"))
        handler.send_binary.assert_awaited_once_with(b"\0")

    async def test_instrumentation_failure_never_prevents_existing_sdk_sends(self):
        handler = self.make_handler()
        trace = handler._latency_trace
        with patch.object(trace, "clock", side_effect=RuntimeError(PRIVATE)), \
                patch("backend.voice.latency_trace.logger.warning") as warning:
            await handler.send_text_message(PRIVATE)
        handler.connection.conversation.item.create.assert_awaited_once()
        self.assertEqual(
            handler.connection.conversation.item.create.call_args.kwargs["item"].content[0].text,
            PRIVATE,
        )
        handler.connection.response.create.assert_awaited_once_with()
        self.assertFalse(trace.correlation_valid)
        self.assertEqual(trace.instrumentation_errors, 1)
        self.log.assert_not_called()
        warning.assert_called_once_with(
            "%s", "[LATENCY_TRACE_ERROR] method=begin exception_class=RuntimeError",
        )
        # Once correlation is unsafe, further inference still proceeds without
        # silently reassigning a late response to the next user's trace.
        await handler.send_text_message(PRIVATE)
        self.assertEqual(handler.connection.response.create.await_count, 2)
        await event_handlers.handle_event(handler, created(), handler.connection)
        await event_handlers.handle_event(handler, done(), handler.connection)
        self.log.assert_not_called()

    async def test_static_session_character_counts_preserve_payloads_and_drop_content(self):
        handler = self.make_handler()
        handler.config["enableProactive"] = False
        handler.connection.session = NS(update=AsyncMock())
        handler._wait_for_event = AsyncMock(return_value=NS(session=NS(
            id="session_fixture", input_audio_transcription=NS(model="mai-transcribe"),
        )))
        instructions = PRIVATE + "é"
        catalog = PRIVATE + "éé"
        definitions = [{
            "type": "function", "name": "search_minutes", "description": PRIVATE,
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": PRIVATE + "é"},
            }},
        }]
        with patch.object(handler_module, "get_meeting_catalog", new=AsyncMock(return_value=catalog)) as get_catalog, \
                patch.object(handler_module, "load_realtime_instructions", return_value=instructions) as get_instructions, \
                patch.object(handler_module, "build_realtime_tools", new=AsyncMock(return_value=definitions)) as get_tools, \
                patch.object(handler_module, "build_voice_config", return_value=None), \
                patch.object(handler_module, "build_avatar_config", return_value=None), \
                patch.object(handler_module, "build_turn_detection", return_value=None), \
                patch.object(handler_module, "build_interim_response", return_value=None):
            await handler._setup_session(handler.connection)
        get_catalog.assert_awaited_once_with()
        get_instructions.assert_called_once_with()
        get_tools.assert_awaited_once_with()
        session = handler.connection.session.update.call_args.kwargs["session"]
        self.assertEqual(session.instructions, instructions)
        self.assertEqual(session.as_dict()["tools"], definitions)
        self.assertEqual(
            handler.connection.conversation.item.create.call_args.kwargs["item"].content[0].text,
            catalog,
        )
        expected = {
            "instructions_chars": len(instructions),
            "catalogue_chars": len(catalog),
            "tool_schema_compact_json_chars": len(json.dumps(
                definitions, ensure_ascii=False, separators=(",", ":"),
            )),
        }
        self.assertEqual(handler._latency_trace.session_char_counts, expected)
        self.assertNotIn(PRIVATE, repr(vars(handler._latency_trace)))
        await handler.send_text_message(PRIVATE)
        await event_handlers.handle_event(handler, created(), handler.connection)
        await event_handlers.handle_event(handler, done(), handler.connection)
        record, = self.records()
        self.assertEqual(record["session_char_counts"], expected)
        self.assertTrue(record["trace_valid"])
        self.assertNotIn(PRIVATE, json.dumps(record))

    async def test_manual_cancel_and_late_response_before_new_turn(self):
        handler = self.make_handler()
        await handler.send_text_message(PRIVATE)
        self.clock.at(1)
        await handler.interrupt()
        await handler.send_text_message(PRIVATE)
        await event_handlers.handle_event(handler, created("resp_old"), handler.connection)
        await event_handlers.handle_event(handler, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA, "resp_old"), handler.connection)
        await event_handlers.handle_event(handler, done("resp_old", status="cancelled"), handler.connection)
        await event_handlers.handle_event(handler, created("resp_new"), handler.connection)
        self.clock.at(3)
        await event_handlers.handle_event(handler, delta(ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA, "resp_new"), handler.connection)
        await event_handlers.handle_event(handler, done("resp_new"), handler.connection)
        await handler.stop()
        old, new = self.records()
        self.assertEqual(old["reason"], "manual_interrupt")
        self.assertEqual(new["terminal_response_id"], "resp_new")
        self.assertEqual(new["milestones_ms"]["t4"], 2)
        self.assertEqual([r["id"] for r in new["responses"]], ["resp_new"])

    async def test_sdk_send_error_and_task_cancellation_are_reported_once(self):
        handler = self.make_handler()
        handler.connection.response.create.side_effect = RuntimeError(PRIVATE)
        with patch.object(handler_module.logger, "error"):
            await handler.send_text_message(PRIVATE)
        await handler.stop()
        record, = self.records()
        self.assertEqual(record["reason"], "response_create_error")
        self.assertIsNone(record["response_create_sends"][0]["done_ms"])
        self.assertNotIn(PRIVATE, json.dumps(record))
        handler = self.make_handler()
        handler.connection.conversation.item.create.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await handler.send_text_message(PRIVATE)
        self.assertEqual(self.records()[1]["status"], "cancelled")
        handler = self.make_handler()
        handler.connection.response.create.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await handler.send_text_message(PRIVATE)
        self.assertEqual(self.records()[2]["status"], "cancelled")

    async def test_actual_tool_exception_and_disabled_tool_path_preserve_error_behavior(self):
        for enabled in (False, True):
            handler = self.make_handler(enabled)
            if enabled:
                handler._latency_trace.begin("text_submission_received")
            await event_handlers.handle_event(handler, created(), handler.connection)
            args = json.dumps({"query": PRIVATE})
            events = [
                NS(type=ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE,
                   response_id="resp_1", call_id="call_1", arguments=args),
                done(tool=True, usage=usage()),
            ]
            finished = asyncio.Event()

            class Events:
                conversation = handler.connection.conversation
                response = handler.connection.response

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if not events:
                        raise StopAsyncIteration
                    if len(events) == 1:
                        await finished.wait()
                    return events.pop(0)

            async def fail_tool(name, arguments):
                self.assertEqual((name, arguments), ("search_web", args))
                finished.set()
                raise RuntimeError(PRIVATE)

            with patch.object(event_handlers, "execute_function", side_effect=fail_tool) as execute_mock, \
                    patch.object(event_handlers.logger, "error"):
                await event_handlers.handle_conversation_item(handler, NS(
                    item=NS(type=ItemType.FUNCTION_CALL, name="search_web",
                            call_id="call_1", id="item_1"),
                ), Events())
            execute_mock.assert_awaited_once_with("search_web", args)
            handler.connection.conversation.item.create.assert_not_awaited()
            handler.connection.response.create.assert_not_awaited()
            self.assertEqual(handler.send_message.call_args.args[0]["type"], "function_call_error")
            if not enabled:
                self.log.assert_not_called()
        record, = self.records()
        self.assertEqual(record["reason"], "tool_error")
        self.assertEqual(record["tools"][0]["execution_status"], "error")
        self.assertIsNone(record["milestones_ms"]["t3"])
        self.assertEqual(record["usage_totals"]["input_tokens"], 100)
        self.assertNotIn(PRIVATE, json.dumps(record))

    async def test_proactive_response_is_not_attributed_to_user_submission(self):
        handler = self.make_handler()
        await create_response(handler, handler.connection, additional_instructions=PRIVATE)
        await handler.send_text_message(PRIVATE)
        await event_handlers.handle_event(handler, created("resp_greeting"), handler.connection)
        await event_handlers.handle_event(handler, done("resp_greeting"), handler.connection)
        self.log.assert_not_called()
        await event_handlers.handle_event(handler, created(), handler.connection)
        await event_handlers.handle_event(handler, done(), handler.connection)
        record, = self.records()
        self.assertEqual([r["id"] for r in record["responses"]], ["resp_1"])


class ConfigTests(unittest.TestCase):
    def test_flag_defaults_off_and_uses_established_bool_parser(self):
        env = dict(os.environ, PYTHON_DOTENV_DISABLED="1", PYTHONPATH=str(ROOT))
        env.pop("ENABLE_LATENCY_TRACE", None)
        code = "from backend.config import ENABLE_LATENCY_TRACE; print(ENABLE_LATENCY_TRACE)"
        for value, expected in ((None, "False"), ("false", "False"), ("true", "True")):
            if value is not None:
                env["ENABLE_LATENCY_TRACE"] = value
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                                    check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
