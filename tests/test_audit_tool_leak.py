"""Regression test for tool-call accumulation across turns.

Reproduces what arm 4 exposed in a live ten-turn session: Foundry's
conversations API returns every item in the session, so each turn's reconcile
re-reported all preceding tool calls. Turn 9 carried nine, eight of them copies.

Run: uv run --no-sync python tests\\test_audit_tool_leak.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.audit import foundry  # noqa: E402
from backend.audit.records import TurnRecord  # noqa: E402


class FakeHandler:
    """Just enough of the voice handler for start_turn's attribute stashing."""

    audit_channel = "web"
    model_binding = False
    voice_session_id = "sess_test"


def conversation_items(n_calls: int) -> list:
    """The whole session's items, newest first — exactly how Foundry returns them."""
    items = []
    for i in reversed(range(n_calls)):
        items.append(
            {
                "type": "remote_function_call_output",
                "call_id": f"call_{i}",
                "name": "azure_ai_search",
                "output": {"documents": ["d"] * 8},
            }
        )
        items.append(
            {
                "type": "remote_function_call",
                "call_id": f"call_{i}",
                "name": "azure_ai_search",
                "arguments": '{"query": "q"}',
            }
        )
    return items


def make_record(turn_index: int, ledger: set) -> TurnRecord:
    record = TurnRecord(session_id="sess_test", turn_index=turn_index)
    record.conversation_id = "conv_test"
    record.seen_call_ids = ledger
    return record


async def run() -> int:
    failures = []

    # Stub the Foundry fetch: every turn sees the full, growing conversation.
    async def fake_client():
        return object()

    foundry._get_client = fake_client  # type: ignore[assignment]

    ledger: set = set()
    counts = []
    for turn in range(10):
        record = make_record(turn, ledger)
        items = conversation_items(turn + 1)

        original = foundry.asyncio.wait_for

        async def fake_wait_for(coro, timeout):  # noqa: ARG001
            coro.close()
            return items

        foundry.asyncio.wait_for = fake_wait_for  # type: ignore[assignment]
        try:
            await foundry.reconcile(record)
        finally:
            foundry.asyncio.wait_for = original

        counts.append(len(record.tools))

    print(f"tools recorded per turn: {counts}")

    if counts != [1] * 10:
        failures.append(
            f"each turn should record exactly its own tool call, got {counts}"
        )

    if len(ledger) != 10:
        failures.append(f"ledger should hold 10 call ids, holds {len(ledger)}")

    # Items that arrive with no call_id at all must still be deduped. Foundry has
    # always supplied one in practice, but anything unkeyed bypasses the ledger
    # entirely and reproduces the original bug, so the invariant must be total.
    def anonymous_items(n: int) -> list:
        items = []
        for i in reversed(range(n)):
            items.append(
                {
                    "type": "remote_function_call",
                    "name": "azure_ai_search",
                    "arguments": '{"query": "q%d"}' % i,
                }
            )
        return items

    anon_ledger: set = set()
    anon_counts = []
    for turn in range(6):
        record = make_record(turn, anon_ledger)
        anon = anonymous_items(turn + 1)
        original = foundry.asyncio.wait_for

        async def fake_anon(coro, timeout, _items=anon):  # noqa: ARG001
            coro.close()
            return _items

        foundry.asyncio.wait_for = fake_anon  # type: ignore[assignment]
        try:
            await foundry.reconcile(record)
        finally:
            foundry.asyncio.wait_for = original
        anon_counts.append(len(record.tools))

    print(f"tools recorded per turn (no call_id): {anon_counts}")
    if anon_counts != [1] * 6:
        failures.append(
            f"items without a call_id must not accumulate, got {anon_counts}"
        )

    # The same question asked repeatedly is several real calls, not one. Keying
    # anonymous calls on content alone collapsed them and wrote turns that did
    # search as `tools: []` — the failure the trail must never produce silently.
    repeat_ledger: set = set()
    repeat_counts = []
    for turn in range(6):
        record = make_record(turn, repeat_ledger)
        same = [
            {
                "type": "remote_function_call",
                "name": "azure_ai_search",
                "arguments": '{"query": "same question"}',
            }
            for _ in range(turn + 1)
        ]
        original = foundry.asyncio.wait_for

        async def fake_repeat(coro, timeout, _items=same):  # noqa: ARG001
            coro.close()
            return _items

        foundry.asyncio.wait_for = fake_repeat  # type: ignore[assignment]
        try:
            await foundry.reconcile(record)
        finally:
            foundry.asyncio.wait_for = original
        repeat_counts.append(len(record.tools))

    print(f"tools recorded per turn (repeated identical call): {repeat_counts}")
    if repeat_counts != [1] * 6:
        failures.append(
            "a repeated identical anonymous call is a distinct call each time, "
            f"got {repeat_counts}"
        )

    # The ledger must never reach the stored document.
    doc = make_record(0, ledger).to_document()
    if "seen_call_ids" in doc or "seenCallIds" in doc:
        failures.append("seen_call_ids leaked into the stored document")

    # A fresh session must not inherit another session's ledger.
    from backend import audit

    audit._queue = object()  # enable start_turn's early return guard
    h1, h2 = FakeHandler(), FakeHandler()
    audit.start_turn(h1)
    audit.start_turn(h2)
    if h1._audit_seen_calls is h2._audit_seen_calls:
        failures.append("two sessions share one ledger")
    if h1._audit_record.seen_call_ids is not h1._audit_seen_calls:
        failures.append("record ledger is not the handler's ledger")

    # Same session, second turn: the ledger must be reused, not replaced.
    first = h1._audit_seen_calls
    audit.start_turn(h1)
    if h1._audit_seen_calls is not first:
        failures.append("ledger replaced between turns of one session")

    for f in failures:
        print(f"FAIL  {f}")
    if failures:
        return 1
    print("PASS  tool calls are attributed to exactly one turn each")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
