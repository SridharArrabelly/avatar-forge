"""Offline coverage for the gpt-realtime-2 opt-in candidate addition to
scripts/bench_realtime_evaluation.py's --models parsing.

This does NOT modify tests/test_realtime_evaluation.py (a hash-verified copy
supplied by the handoff) -- it is a small, additive, standalone test that
proves: (1) the default two-model roster is unchanged, (2) gpt-realtime-2 is
now an accepted explicit --models choice, and (3) --models still rejects an
arbitrary/unknown model string (choices did not become permissive).

No Azure/network calls, no private env reads, no evidence disk writes.

    uv run --offline --no-sync python tests\\test_realtime_model_choice.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bench_realtime_evaluation as evaluation  # noqa: E402

CLI = ["--env-file", "never-read.env", "--cases", "never-read.json", "--output-dir", "never-written"]


class DefaultRosterUnchangedTests(unittest.TestCase):
    def test_default_models_tuple_excludes_gpt_realtime_2(self):
        self.assertEqual(evaluation.MODELS, ("gpt-realtime-2.1", "gpt-realtime-2.1-mini"))
        self.assertNotIn("gpt-realtime-2", evaluation.MODELS)

    def test_parse_args_default_roster_is_unaffected(self):
        args = evaluation.parse_args(CLI)
        self.assertEqual(args.models, ["gpt-realtime-2.1", "gpt-realtime-2.1-mini"])


class GptRealtime2OptInTests(unittest.TestCase):
    def test_gpt_realtime_2_is_an_explicit_model_choice(self):
        self.assertIn("gpt-realtime-2", evaluation.MODEL_CHOICES)
        # Exactly one occurrence -- not accidentally duplicated.
        self.assertEqual(evaluation.MODEL_CHOICES.count("gpt-realtime-2"), 1)

    def test_gpt_realtime_2_parses_as_an_explicit_opt_in_selection(self):
        args = evaluation.parse_args(CLI + ["--models", "gpt-realtime-2"])
        self.assertEqual(args.models, ["gpt-realtime-2"])

    def test_gpt_realtime_2_can_be_combined_with_default_roster_members(self):
        args = evaluation.parse_args(CLI + ["--models", "gpt-realtime-2", "gpt-realtime-2.1"])
        self.assertEqual(args.models, ["gpt-realtime-2", "gpt-realtime-2.1"])

    def test_unknown_model_string_is_still_rejected(self):
        with self.assertRaises(SystemExit):
            evaluation.parse_args(CLI + ["--models", "not-a-real-model"])


if __name__ == "__main__":
    unittest.main()
