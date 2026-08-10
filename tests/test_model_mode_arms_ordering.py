"""The tool-call turn, in the order Voice Live actually delivers it.

This is the test that should have existed before the deployment of 9 August
2026, when model mode recorded eight turns and not one tool call, despite the
answers demonstrably coming from tools.

``tests/test_model_mode_arms.py`` already asserted that ``record_tool`` captures
a call. It passed. What it never reproduced was the *sequence*: a tool call ends
its response before the tool has run, and the spoken answer arrives in a second
response. Between the two, ``start_turn`` is called again.

The subtlety is that ``handle_conversation_item`` waits for that first
RESPONSE_DONE with ``handler._wait_for_event``, which **returns** the event
instead of dispatching it (``backend/voice/handler.py``). So ``finish_turn``,
and with it the carry-forward, never runs for a tool-call response. The record
holding the tool is still in ``_audit_record`` when the next RESPONSE_CREATED
arrives -- and a naive ``start_turn`` overwrites it.

Captured, then thrown away one step later. The written record carried the
question and the answer, and silently omitted the retrieval that produced it,
which is the single thing model binding exists to record.

Run: uv run --no-sync python tests/test_model_mode_arms_ordering.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["ENABLE_AUDIT"] = "true"
os.environ["AUDIT_SINK"] = "none"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)
    return ok


class FakeHandler:
    """Only the attributes the audit module reads off a real handler."""

    def __init__(self):
        self.voice_session_id = "sess_ordering_test"
        self.model_binding = True
        self.audit_channel = "web"
        self.audit_agent_name = "Nuru"
        self.audit_model = "gpt-realtime"


class CollectingSink:
    """Keeps every batch the writer hands over."""

    def __init__(self):
        self.documents: list[dict] = []

    async def write(self, documents: list[dict]) -> int:
        self.documents.extend(documents)
        return len(documents)

    async def close(self) -> None:
        pass


async def main() -> int:
    import backend.audit as audit
    from backend.audit.queue import AuditQueue

    sink = CollectingSink()
    queue = AuditQueue(sink, max_size=100, retention_days=1, redact=True)
    queue.start()
    audit._queue = queue

    handler = FakeHandler()

    print("\nthe exact sequence Voice Live delivers for a tool-backed answer")
    print("-" * 70)

    # response A: the user asks, the model decides to call a tool.
    audit.start_turn(handler)
    audit.record_user_text(handler, "What were MTN's latest results?", "item_1")

    # The tool runs in our process and is captured. This much already worked.
    audit.record_tool(
        handler,
        name="search_web",
        args={"query": "MTN latest financial results"},
        results={"results": [{"title": "MTN Group FY25", "url": "https://www.mtn.com/x"}]},
        elapsed_ms=412.0,
        error=None,
    )

    inflight = getattr(handler, "_audit_record", None)
    check(
        "the tool is captured on the in-flight record",
        inflight is not None and len(inflight.tools) == 1,
        f"tools={len(inflight.tools) if inflight else 'no record'}",
    )

    # response A's RESPONSE_DONE is consumed by _wait_for_event and never
    # dispatched, so finish_turn does NOT run here. That is the whole point:
    # this line is deliberately absent.

    # response B: the spoken answer. start_turn is called again.
    audit.start_turn(handler)

    resumed = getattr(handler, "_audit_record", None)
    check(
        "start_turn resumed the record instead of replacing it",
        resumed is inflight,
        "a new record here silently drops the tool call",
    )

    audit.record_assistant_text(handler, "MTN grew service revenue by 2.0%.")
    audit.finish_turn(handler, status="completed", output_types=["message"])

    await queue.drain()

    print("\nwhat actually reached the sink")
    print("-" * 70)
    check("exactly one record was written", len(sink.documents) == 1, f"got {len(sink.documents)}")

    if sink.documents:
        doc = sink.documents[0]
        tools = doc.get("tools") or []
        check("the written record carries the tool call", len(tools) == 1, f"got {len(tools)}")
        if tools:
            t = tools[0]
            check("tool name survived", t.get("name") == "search_web", f"got {t.get('name')!r}")
            check("source is in-process", t.get("source") == "in-process", f"got {t.get('source')!r}")
            check(
                "elapsedMs survived",
                isinstance(t.get("elapsedMs"), (int, float)),
                f"got {t.get('elapsedMs')!r}",
            )
            check(
                "the retrieved results survived",
                bool(t.get("results")),
                "agent binding cannot capture these; model binding must",
            )
        check(
            "the question and the answer are on the same record as the tool",
            bool((doc.get("user") or {}).get("text"))
            and bool((doc.get("assistant") or {}).get("text")),
            "one exchange must be one record",
        )
        check(
            "toolsPending is not set in model mode",
            (doc.get("meta") or {}).get("toolsPending") is not True,
        )

    await audit.shutdown_audit()

    # --- the same sequence, but the model speaks before it calls the tool ---
    #
    # Live trace, 10 August 2026, session sess_EBDjUZy...:
    #
    #   06:26:58  response A created
    #   06:27:00  first audio_transcript delta   <- A SPOKE first
    #   06:27:00  output item added: function_call search_minutes
    #   06:27:01  response B created
    #   06:27:05  one document upserted, tools=[]
    #
    # Cosmos held turnIndex [0, 1, 2, 3, 5]. Turn 4 was allocated and never
    # written: the carry test also demanded silence, A had spoken a preamble,
    # so A was replaced instead of resumed. The surviving record reported no
    # tools for a turn that had just searched the minutes.
    #
    # Nothing counts this. The record is dropped *before* submit(), so
    # submitted/written/dropped/failed are all still in agreement.

    print("\na spoken preamble before the tool call must not lose the turn")
    print("-" * 70)

    sink4 = CollectingSink()
    queue4 = AuditQueue(sink4, max_size=100, retention_days=1, redact=True)
    queue4.start()
    audit._queue = queue4

    speaker = FakeHandler()
    speaker.voice_session_id = "sess_preamble_test"

    audit.start_turn(speaker)
    audit.record_user_text(speaker, "What did the board decide about the dividend?", "item_p")
    first_index = getattr(speaker, "_audit_record", None).turn_index

    # A speaks, then calls the tool -- both inside response A.
    audit.record_assistant_text(speaker, "Let me check the records for you.")
    audit.record_tool(
        speaker,
        name="search_minutes",
        args={"query": "dividend"},
        results={"passages": [{"t": "a"}, {"t": "b"}, {"t": "c"}, {"t": "d"}]},
        elapsed_ms=312.0,
    )

    spoken_inflight = getattr(speaker, "_audit_record", None)

    # A's RESPONSE_DONE is swallowed by _wait_for_event, exactly as in arm 1.
    audit.start_turn(speaker)

    check(
        "a record that spoke before calling the tool is still resumed",
        getattr(speaker, "_audit_record", None) is spoken_inflight,
        "replacing it drops the tool call and leaves a hole in turnIndex",
    )
    check(
        "no turn index is burned by the second response",
        getattr(speaker, "_audit_turn_index", None) == first_index + 1,
        f"got {getattr(speaker, '_audit_turn_index', None)!r}, expected {first_index + 1}",
    )

    audit.record_assistant_text(speaker, "The board approved a final dividend of 330 cents.")
    audit.finish_turn(speaker, status="completed", output_types=["message"])
    await queue4.drain()

    check("exactly one record was written", len(sink4.documents) == 1, f"got {len(sink4.documents)}")
    if sink4.documents:
        doc = sink4.documents[0]
        tools = doc.get("tools") or []
        check(
            "the written record carries the tool call",
            len(tools) == 1 and tools[0].get("name") == "search_minutes",
            f"got {[t.get('name') for t in tools]}",
        )
        check(
            "hitCount survived the carry",
            bool(tools) and tools[0].get("hitCount") == 4,
            f"got {tools[0].get('hitCount') if tools else 'no tool'}",
        )
        check(
            "turnIndex is contiguous — the turn was not skipped",
            doc.get("turnIndex") == first_index,
            f"got {doc.get('turnIndex')!r}, expected {first_index}",
        )
        said = (doc.get("assistant") or {}).get("text") or ""
        check(
            "the preamble the user heard is kept, not overwritten",
            "check the records" in said,
            f"got {said[:60]!r}",
        )
        check(
            "the answer is kept too",
            "330 cents" in said,
            f"got {said[:60]!r}",
        )

    await audit.shutdown_audit()


    #
    # The resume branch is guarded on model_binding. Agent binding fills tools
    # in from foundry.reconcile() inside the writer, after the record has left
    # the handler, so it must keep the original behaviour: a new RESPONSE_CREATED
    # starts a new record, full stop.

    print("\nagent binding is unaffected")
    print("-" * 70)

    sink2 = CollectingSink()
    queue2 = AuditQueue(sink2, max_size=100, retention_days=1, redact=True)
    queue2.start()
    audit._queue = queue2

    agent = FakeHandler()
    agent.model_binding = False

    audit.start_turn(agent)
    audit.record_user_text(agent, "What did the board decide?", "item_a")
    first = getattr(agent, "_audit_record", None)

    # Force the exact shape the model-mode branch keys on: tools present and a
    # record still in flight. Agent mode must still not resume.
    audit.record_tool(agent, name="search_minutes", args={"query": "board"}, results={})

    audit.start_turn(agent)
    second = getattr(agent, "_audit_record", None)

    check(
        "agent binding starts a fresh record, as before",
        second is not first,
        "the model-mode resume must not leak into agent binding",
    )
    check(
        "agent binding still increments the turn index",
        getattr(second, "turn_index", None) != getattr(first, "turn_index", None),
    )
    check("the agent record is marked agent-bound", getattr(second, "binding", None) == "agent")

    audit.record_assistant_text(agent, "It approved the dividend.")
    audit.finish_turn(agent, status="completed", output_types=["message"])
    await queue2.drain()
    await audit.shutdown_audit()

    # --- hitCount must understand the shapes our tools actually return ------
    #
    # Live records showed hitCount=None on every search_minutes call while the
    # payload held four passages. The heuristic knew "results"/"documents"/"hits"
    # and search_minutes returns "passages", so the summary field the audit docs
    # tell operators to query was null for half the tool set.

    print("\nhitCount understands both tools' real return shapes")
    print("-" * 70)

    sink3 = CollectingSink()
    queue3 = AuditQueue(sink3, max_size=100, retention_days=1, redact=True)
    queue3.start()
    audit._queue = queue3

    shapes = FakeHandler()
    audit.start_turn(shapes)

    # The exact shapes backend/voice/tools.py returns.
    audit.record_tool(
        shapes,
        name="search_minutes",
        args={"query": "board"},
        results={"passages": [{"title": "a"}, {"title": "b"}], "note": "..."},
        elapsed_ms=1.0,
    )
    audit.record_tool(
        shapes,
        name="search_web",
        args={"query": "mtn"},
        results={"results": [{"title": "x"}, {"title": "y"}, {"title": "z"}]},
        elapsed_ms=1.0,
    )

    inflight3 = getattr(shapes, "_audit_record", None)
    by_name = {t.name: t for t in (inflight3.tools if inflight3 else [])}
    check(
        "search_minutes hitCount counts its passages",
        by_name.get("search_minutes") is not None
        and by_name["search_minutes"].hit_count == 2,
        f"got {getattr(by_name.get('search_minutes'), 'hit_count', 'no tool')!r}",
    )
    check(
        "search_web hitCount counts its results",
        by_name.get("search_web") is not None and by_name["search_web"].hit_count == 3,
        f"got {getattr(by_name.get('search_web'), 'hit_count', 'no tool')!r}",
    )

    audit.record_assistant_text(shapes, "done")
    audit.finish_turn(shapes, status="completed", output_types=["message"])
    await queue3.drain()
    await audit.shutdown_audit()

    print("\n" + "-" * 70)
    if failures:
        print(f"{len(failures)} FAILED: " + "; ".join(failures))
        return 1
    print("All ordering assertions passed.")
    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
