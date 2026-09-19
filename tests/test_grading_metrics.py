"""Offline regression tests for scripts/grading_metrics.py.

These guard the exact bug caught in review: conflating the
"AUDIO_TRANSCRIPT" channel timing with true "PCM" audio timing, and reading
first-any timings from responses[0] instead of the whole-turn
first_any_received_ms block. No network access, no credentials.

Run from the repository root:

    uv run python tests/test_grading_metrics.py
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import grading_metrics as gm  # noqa: E402


def _turn(**overrides):
    base = {
        "timings": {
            "final_answer_first_received_ms": {
                "TEXT": None,
                "AUDIO_TRANSCRIPT": 850.0,
                "PCM": 1340.0,
            },
            "first_any_received_ms": {
                "TEXT": None,
                "AUDIO_TRANSCRIPT": 500.0,
                "PCM": 700.0,
            },
        },
        "responses": [
            {"usage_pacing": {"reported_total_tokens": 999}},
        ],
        "token_accounting": {"reported_tokens_sum": 2401},
        "preamble_response_ids": [],
        "tool_calls": [],
    }
    merged = copy.deepcopy(base)
    merged.update(overrides)
    return merged


class ChannelSelectionTests(unittest.TestCase):
    def test_transcript_and_audio_channels_are_distinct_values(self):
        turn = _turn()
        transcript_ms = gm.final_answer_transcript_ms(turn)
        audio_ms = gm.final_answer_audio_ms(turn)
        self.assertEqual(transcript_ms, 850.0)
        self.assertEqual(audio_ms, 1340.0)
        # The whole point of the bug fix: these must never be equal by
        # accident of a wrong channel key being read.
        self.assertNotEqual(transcript_ms, audio_ms)

    def test_final_answer_audio_uses_pcm_not_audio_transcript(self):
        turn = _turn()
        expected = turn["timings"]["final_answer_first_received_ms"]["PCM"]
        self.assertEqual(gm.final_answer_audio_ms(turn), expected)

    def test_final_answer_transcript_uses_audio_transcript_key(self):
        turn = _turn()
        expected = turn["timings"]["final_answer_first_received_ms"]["AUDIO_TRANSCRIPT"]
        self.assertEqual(gm.final_answer_transcript_ms(turn), expected)

    def test_text_channel_missing_returns_none(self):
        turn = _turn()
        timings = turn["timings"]["final_answer_first_received_ms"]
        self.assertIsNone(gm.select_timing(timings, gm.CHANNEL_TEXT))

    def test_first_any_uses_whole_turn_block_not_first_response(self):
        # responses[0] deliberately carries a DIFFERENT value than the
        # turn-level first_any_received_ms block, to prove
        # first_any_audio_ms reads the turn-level block and ignores
        # responses[0].
        turn = _turn(
            responses=[
                {
                    "first_received_ms": {
                        "TEXT": None,
                        "AUDIO_TRANSCRIPT": 999.0,
                        "PCM": 999.0,
                    },
                    "usage_pacing": {"reported_total_tokens": 999},
                }
            ]
        )
        self.assertEqual(gm.first_any_audio_ms(turn), 700.0)
        self.assertNotEqual(gm.first_any_audio_ms(turn), 999.0)


class PreambleTests(unittest.TestCase):
    def test_has_preamble_true_when_preamble_response_ids_nonempty(self):
        turn = _turn(preamble_response_ids=["resp_abc"])
        self.assertTrue(gm.has_preamble(turn))
        turn_no_preamble = _turn(preamble_response_ids=[])
        self.assertFalse(gm.has_preamble(turn_no_preamble))


class TokenAccountingTests(unittest.TestCase):
    def test_reported_tokens_sum_uses_token_accounting_not_terminal_usage(self):
        turn = _turn()
        # token_accounting.reported_tokens_sum (2401) must be used, not the
        # terminal response's own usage_pacing.reported_total_tokens (999),
        # which only reflects the last round.
        self.assertEqual(gm.reported_tokens_sum(turn), 2401)
        self.assertEqual(gm.terminal_round_tokens(turn), 999)
        self.assertNotEqual(gm.reported_tokens_sum(turn), gm.terminal_round_tokens(turn))


class ToolRoutingComplianceTests(unittest.TestCase):
    def test_oracle_tools_disabled_compliance_is_not_routing_quality(self):
        compliant_turn = _turn(tool_calls=[])
        noncompliant_turn = _turn(tool_calls=[{"name": "search_minutes"}])
        self.assertTrue(gm.oracle_tools_disabled_compliant(compliant_turn))
        self.assertFalse(gm.oracle_tools_disabled_compliant(noncompliant_turn))


class StatisticsTests(unittest.TestCase):
    def test_nearest_rank_percentile_matches_known_values(self):
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        # p95 of 10 samples (nearest-rank) -> ceil(0.95*10)=10th value = 10.
        self.assertEqual(gm.nearest_rank_percentile(values, 95), 10)
        # p50 of 10 samples -> ceil(0.5*10)=5th value = 5.
        self.assertEqual(gm.nearest_rank_percentile(values, 50), 5)
        self.assertIsNone(gm.nearest_rank_percentile([], 95))

    def test_median_even_and_odd(self):
        self.assertEqual(gm.median([1, 2, 3]), 2)
        self.assertEqual(gm.median([1, 2, 3, 4]), 2.5)
        self.assertIsNone(gm.median([]))


if __name__ == "__main__":
    unittest.main()
