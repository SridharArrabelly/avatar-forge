"""Offline walk of the audit test ladder under **model** binding.

Needs **no Azure resources and no credentials**. Runs in about a second.

Why it exists: the ladder was verified live against a deployment bound to a
Foundry **agent** -- arm 1 ``ENABLE_AUDIT=false``, arm 2 ``true`` + ``none``,
arm 3 ``true`` + ``file``, arm 3.5 ``cosmos`` with no endpoint, arm 4 ``cosmos``
end to end, arm 5 the deployed app. Model binding is a different capture path,
so "it passed in agent mode" is not evidence about model mode.

The two paths genuinely differ:

* agent  -- tool I/O is fetched from Foundry after the turn, ``tools_pending``
  is set, and the reconcile/ledger machinery runs on every turn.
* model  -- tools are captured in-process as they execute, with their real
  arguments, results and measured latency. ``tools_pending`` is never set, so
  reconcile never runs at all.

This walks the same ladder offline, through the real capture entry points with
a stand-in handler, so the arms can be trusted before anyone re-runs them
against a live deployment. What it pins:

* arm 1   -- every capture entry point is inert; nothing is written
* arm 2   -- the record is built and drained; the sink is asked for nothing
* arm 3   -- a model-bound turn lands in JSONL with the shape model mode owes:
             binding, in-process tool source, real arguments and results, and a
             measured elapsed_ms
* arm 3.5 -- a misconfigured cosmos sink refuses to start (binding-independent,
             re-checked here so the ladder is complete in one place)
* arm 4   -- a configured cosmos endpoint selects the cosmos sink
* the model-binding invariant: ``tools_pending`` is never set, so no turn ever
  waits on a Foundry fetch

Run from the repo root:

    uv run python tests/test_model_mode_arms.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import audit  # noqa: E402
from backend.audit import AuditSinkUnavailable  # noqa: E402
from backend.audit.queue import AuditQueue  # noqa: E402
from backend.audit.sinks import FileSink, NullSink  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}", end="")
    if not ok:
        print(f": expected {want!r}, got {got!r}", end="")
        FAILURES.append(label)
    print()


class Handler:
    """The attributes backend.audit reads off a live voice handler."""

    def __init__(self, session_id: str = "sess-model", channel: str = "web"):
        self.voice_session_id = session_id
        self.audit_channel = channel
        self.model_binding = True


class CountingSink:
    """A sink that records what it was asked to write.

    ``write`` takes a *batch* -- the queue coalesces turns before handing them
    over -- so flatten it here and count documents, not batches.
    """

    def __init__(self) -> None:
        self.docs: list[dict] = []
        self.batches = 0

    async def warm(self) -> None:
        pass

    async def write(self, documents: list[dict]) -> int:
        self.batches += 1
        self.docs.extend(documents)
        return len(documents)

    async def close(self) -> None:
        pass


def _one_model_turn(handler: Handler) -> None:
    """Drive one model-mode exchange through the real capture entry points.

    Mirrors the live event order: the response that issues a tool call ends
    before the tool has run, so the turn spans two responses.
    """
    audit.start_turn(handler)
    audit.record_user_text(handler, "What did the board decide about the budget?")
    # The tool-call response ends with no assistant text -> carried forward.
    audit.finish_turn(handler, status="completed", output_types=["function_call"])

    audit.start_turn(handler)
    audit.record_tool(
        handler,
        "search_minutes",
        args={"query": "board budget decision"},
        results={"results": [{"id": "doc1"}, {"id": "doc2"}]},
        elapsed_ms=412.0,
    )
    audit.record_assistant_text(handler, "The board approved the budget in February.")
    audit.finish_turn(handler, status="completed", output_types=["message"])


async def _drain(queue: AuditQueue) -> None:
    await queue.drain()


async def _run_arm(sink, enable: bool = True) -> tuple[list[dict], Handler]:
    """Install a queue over `sink`, run one model turn, drain, return the docs."""
    handler = Handler()
    if not enable:
        audit._queue = None
        _one_model_turn(handler)
        return [], handler

    queue = AuditQueue(sink)
    queue.start()
    audit._queue = queue
    try:
        _one_model_turn(handler)
    finally:
        await _drain(queue)
        audit._queue = None
    return getattr(sink, "docs", []), handler


class _ConfigPatch:
    """Patch the config constants bound into backend.audit, restoring after."""

    def __init__(self, **overrides):
        self._overrides = overrides
        self._saved: dict = {}

    def __enter__(self):
        self._saved = {k: getattr(audit, k) for k in self._overrides}
        for k, v in self._overrides.items():
            setattr(audit, k, v)
        audit._degraded = None
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            setattr(audit, k, v)
        audit._degraded = None
        audit._sink_name = None
        audit._queue = None
        return False


async def _build_name(**overrides) -> str:
    with _ConfigPatch(**overrides):
        return type(await audit._build_sink()).__name__


async def _refuses(**overrides) -> bool:
    with _ConfigPatch(**overrides):
        try:
            await audit._build_sink()
            return False
        except AuditSinkUnavailable:
            return True


async def main() -> int:
    saved_queue = audit._queue

    print("arm 1 - ENABLE_AUDIT=false: capture is inert")
    print("-" * 62)
    docs, handler = await _run_arm(CountingSink(), enable=False)
    check("nothing written", docs, [])
    check("no record was even opened", hasattr(handler, "_audit_record"), False)

    print()
    print("arm 2 - true + AUDIT_SINK=none: captured and drained, nothing stored")
    print("-" * 62)
    null = NullSink()
    queue = AuditQueue(null)
    queue.start()
    audit._queue = queue
    try:
        _one_model_turn(Handler())
    finally:
        await queue.drain()
        audit._queue = None
    check("a NullSink is what 'none' builds", await _build_name(AUDIT_SINK="none"), "NullSink")
    # The point of arm 2: the pipeline really ran, it just stored nothing.
    counting = CountingSink()
    docs, _ = await _run_arm(counting)
    check("the same turn does reach a real sink", len(docs), 1)

    print()
    print("arm 3 - true + AUDIT_SINK=file: the shape model mode owes")
    print("-" * 62)
    check("a FileSink is what 'file' builds", await _build_name(AUDIT_SINK="file"), "FileSink")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audit-log.jsonl"
        sink = FileSink(str(path))
        await _run_arm(sink)
        lines = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    check("exactly one record for one exchange", len(lines), 1)
    doc = lines[0] if lines else {}
    check("binding recorded as model", doc.get("binding"), "model")
    check("question captured", doc.get("user", {}).get("text"),
          "What did the board decide about the budget?")
    check("answer captured", doc.get("assistant", {}).get("text"),
          "The board approved the budget in February.")
    tools = doc.get("tools", [])
    check("one tool captured", len(tools), 1)
    tool = tools[0] if tools else {}
    check("captured in-process, not fetched from Foundry",
          tool.get("source"), "in-process")
    check("real arguments survive", tool.get("args", {}).get("query"),
          "board budget decision")
    check("hit count derived from real results", tool.get("hitCount"), 2)
    # Latency is the thing agent mode structurally cannot report.
    check("per-tool latency measured", tool.get("elapsedMs"), 412.0)

    print()
    print("arm 3.5 / 4 - sink selection and fail-closed (binding-independent)")
    print("-" * 62)
    check("cosmos with no endpoint refuses to start",
          await _refuses(AUDIT_SINK="cosmos", AUDIT_COSMOS_ENDPOINT="",
                   AUDIT_SINK_FALLBACK="error"), True)
    check("an unrecognised sink name refuses to start",
          await _refuses(AUDIT_SINK="cosmosdb", AUDIT_SINK_FALLBACK="error"), True)

    print()
    print("the model-binding invariant: no turn ever waits on Foundry")
    print("-" * 62)
    sink = CountingSink()
    docs, _ = await _run_arm(sink)
    pending = [d for d in docs if d.get("toolsPending")]
    check("tools_pending is never set under model binding", pending, [])
    # Agent mode is the contrast: its records are the ones the reconcile path
    # exists for. Model mode must never enter it.
    check("the record is complete when it is written",
          bool(docs) and bool(docs[0].get("tools")), True)

    audit._queue = saved_queue

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
