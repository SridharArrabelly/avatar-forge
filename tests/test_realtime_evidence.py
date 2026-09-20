"""Offline checks for bounded, redacted event capture outside measured turns."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_realtime_evaluation as fixtures

evaluation = fixtures.evaluation


class EvidenceTests(unittest.TestCase):
    def test_event_snapshots_are_redacted_and_flushed_at_attempt_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            evidence = evaluation.Evidence(root, evaluation.Redactor(["synthetic-secret"]))
            try:
                with patch.object(evaluation.os, "fsync", wraps=evaluation.os.fsync) as sync:
                    event = {"status": "running", "data": "synthetic-secret"}
                    evidence.emit("events", event)
                    event["status"] = "completed"
                    self.assertFalse((root / "events.jsonl").exists())
                    sync.assert_not_called()
                    evidence.emit("attempts", {"status": "completed"})
                    self.assertEqual(sync.call_count, 2)
                saved = json.loads((root / "events.jsonl").read_text(encoding="utf-8"))
                self.assertEqual(saved, {"status": "running", "data": "[REDACTED]"})
                self.assertEqual(evidence.pending_events, [])
                self.assertTrue((root / "attempts.jsonl").exists())
            finally:
                evidence.close()

    def test_close_flushes_pending_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            evidence = evaluation.Evidence(root, evaluation.Redactor())
            evidence.emit("events", {"kind": "setup"})
            evidence.close()
            self.assertEqual(json.loads((root / "events.jsonl").read_text()), {"kind": "setup"})
            self.assertTrue(all(stream.closed for stream in evidence.logs.values()))
            with self.assertRaisesRegex(RuntimeError, "closed"):
                evidence.emit("events", {"kind": "must-not-disappear"})

    def test_buffer_overflow_fails_the_turn_instead_of_dropping_successfully(self):
        async def exercise(root):
            evidence = evaluation.Evidence(root, evaluation.Redactor())
            runtime = fixtures.runtime([fixtures.FakeConnection(fixtures.answer_events())])
            pacer = fixtures.ImmediatePacer(0)
            await pacer.wait()
            try:
                with patch.object(evaluation, "MAX_BUFFERED_EVENTS", 1):
                    record = await evaluation.run_turn(
                        runtime, fixtures.SETTINGS, evaluation.parse_args(fixtures.CLI),
                        fixtures.CASES[0], fixtures.scheduled(), 1, evidence.emit, pacer,
                    )
                self.assertEqual(record["status"], "failed")
                self.assertIn("incomplete evidence", record["error"]["message"])
                self.assertFalse(record["error"]["retryable"])
            finally:
                evidence.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(exercise(Path(directory) / "evidence"))

    def test_flush_failure_is_not_hidden_or_retried_as_duplicate_events(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = evaluation.Evidence(Path(directory) / "evidence", evaluation.Redactor())
            evidence.emit("events", {"kind": "one"})
            with patch.object(evaluation.os, "fsync", side_effect=OSError("synthetic disk failure")):
                with self.assertRaisesRegex(OSError, "synthetic disk failure"):
                    evidence.emit("attempts", {"status": "completed"})
            self.assertNotIn("attempts", evidence.logs)
            self.assertEqual(evidence.pending_events, [])
            evidence.close()


if __name__ == "__main__":
    unittest.main()
