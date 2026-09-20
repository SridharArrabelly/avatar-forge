"""Offline timing-boundary checks; real pacing logic, fake clock and SDK."""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock

import test_realtime_evaluation as fixtures

evaluation = fixtures.evaluation


class LatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_stages_omit_token_wait_without_changing_tool_or_requests(self):
        with fixtures.fake_clock() as clock:
            class Connection(fixtures.FakeConnection):
                async def recv(self):
                    clock.now += 0.05
                    return await super().recv()

                async def create_item(self, **kwargs):
                    clock.now += 0.02
                    await super().create_item(**kwargs)

                async def create_response(self):
                    clock.now += 0.03
                    await super().create_response()

            class Evidence(fixtures.MemoryEvidence):
                def emit(self, kind, value):
                    clock.now += 0.005
                    super().emit(kind, value)

            result = {"passages": [{"extract": "Exact evidence caf\u00e9", "truncated": False}]}

            async def execute(*_args):
                clock.now += 0.4
                return result

            conn = Connection(fixtures.tool_events() + fixtures.answer_events(text=None))
            runtime = fixtures.runtime([conn], result)
            runtime.execute_function = AsyncMock(side_effect=execute)
            evidence = Evidence()
            pacer = evaluation.Pacer(8)
            await pacer.wait("live")
            record = await evaluation.run_turn(
                runtime, fixtures.SETTINGS, evaluation.parse_args(fixtures.CLI),
                fixtures.CASES[0], fixtures.scheduled(), 1, evidence.emit, pacer,
            )
            self.assertEqual(record["status"], "completed")
            path = record["latency_trace"]
            tool = record["tool_calls"][0]["timing_stages"]
            stages = [path["T0_ms"], tool["T1_ms"], tool["T2_ms"], tool["T3_ms"],
                      path["T4_ms"], path["T5_ms"]]
            self.assertEqual(stages, sorted(stages))
            self.assertAlmostEqual(tool["T2_ms"] - tool["T1_ms"], 400)
            self.assertAlmostEqual(
                tool["T3_ms"] - tool["result_send_started_ms"], 20,
            )
            self.assertLess(tool["function_announced_ms"], tool["arguments_ready_ms"])
            self.assertLess(tool["arguments_ready_ms"], tool["call_response_done_ms"])
            self.assertLess(tool["call_response_done_ms"], tool["T1_ms"])
            followup = path["response_requests"][1]
            actual_wait = followup["wait_completed_ms"] - followup["wait_started_ms"]
            self.assertEqual(actual_wait, 0)
            self.assertEqual(followup["planned_wait_ms"], 0)
            self.assertGreater(followup["token_due_ms"], 0)
            self.assertEqual(followup["service_defer_due_ms"], 0)
            self.assertFalse(path["service_wait_observed"])
            self.assertGreater(path["T4_ms"] - tool["T3_ms"], actual_wait)
            self.assertAlmostEqual(followup["send_completed_ms"] - followup["send_started_ms"], 30)
            self.assertEqual(path["T4_ms"], record["timings"]["final_answer_first_received_ms"]["AUDIO_TRANSCRIPT"])
            self.assertEqual(path["T5_ms"], record["timings"]["final_answer_first_received_ms"]["PCM"])
            # Two receive intervals and their preceding 5ms observer writes.
            self.assertAlmostEqual(path["T5_ms"] - path["T4_ms"], 110)
            self.assertTrue(path["trace_capture_spans"])
            for span in path["trace_capture_spans"]:
                self.assertAlmostEqual(span["end_ms"] - span["start_ms"], 5)
            self.assertEqual(conn.response_creates, 2)
            runtime.execute_function.assert_awaited_once()
            self.assertEqual(json.loads(conn.items[-1]["output"]), result)
            self.assertEqual(record["context_sizes"]["prior_user_turns"], 0)
            self.assertEqual(
                record["context_sizes"]["tool_calls"][0]["result_json"],
                evaluation.text_size(json.dumps(result, ensure_ascii=False)),
            )

    async def test_no_tool_turn_keeps_absent_tool_stages_and_real_audio_channel(self):
        conn = fixtures.FakeConnection(fixtures.answer_events(text=None))
        runtime = fixtures.runtime([conn])
        evidence = fixtures.MemoryEvidence()
        pacer = fixtures.ImmediatePacer(0)
        await pacer.wait("oracle")
        record = await evaluation.run_turn(
            runtime, fixtures.SETTINGS, evaluation.parse_args(fixtures.CLI),
            fixtures.CASES[0], fixtures.scheduled(stage="oracle"), 1, evidence.emit, pacer,
        )
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["tool_calls"], [])
        self.assertEqual(record["latency_trace"]["function_events"], [])
        self.assertEqual(len(record["latency_trace"]["response_requests"]), 1)
        self.assertIsNotNone(record["latency_trace"]["T5_ms"])
        self.assertIsNone(record["timings"]["final_answer_first_received_ms"]["TEXT"])

    async def test_genuine_service_wait_is_honored_and_labelled(self):
        with fixtures.fake_clock() as clock:
            events = fixtures.tool_events()
            events.insert(-1, {
                "type": "rate_limits.updated",
                "rate_limits": [{"name": "tokens", "remaining": 0, "reset_seconds": 5}],
            })
            conn = fixtures.FakeConnection(events + fixtures.answer_events(text=None))
            runtime = fixtures.runtime([conn])
            evidence = fixtures.MemoryEvidence()
            pacer = evaluation.Pacer(0)
            await pacer.wait()
            record = await evaluation.run_turn(
                runtime, fixtures.SETTINGS, evaluation.parse_args(fixtures.CLI),
                fixtures.CASES[0], fixtures.scheduled(), 1, evidence.emit, pacer,
            )
            self.assertEqual(record["status"], "completed")
            self.assertTrue(record["latency_trace"]["service_wait_observed"])
            request = record["latency_trace"]["response_requests"][1]
            self.assertEqual(request["planned_wait_ms"], 5000)
            self.assertEqual(request["wait_completed_ms"] - request["wait_started_ms"], 5000)
            self.assertEqual(clock.sleeps, [5])
            waits = [event for event in evidence.events["events"] if event["kind"] == "client_pacing"]
            self.assertEqual(len(waits), 1)
            self.assertEqual(waits[0]["data"]["reason"], "service_directed")

    def test_sizes_do_not_pretend_to_be_token_counts_or_invent_missing_values(self):
        self.assertEqual(evaluation.text_size("\u00e9"), {"characters": 1, "utf8_bytes": 2})
        self.assertEqual(evaluation.text_size(None), {"characters": None, "utf8_bytes": None})


if __name__ == "__main__":
    unittest.main()
