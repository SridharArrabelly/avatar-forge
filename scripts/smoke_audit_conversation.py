"""Prove the agent-mode audit path against a live Foundry conversation (#30).

Agent binding relays no tool events to us: when Voice Live is bound to a Foundry
agent, the retrieval happens server-side and the app sees only the question and
the answer. The audit trail closes that gap by capturing ``conversation_id`` from
``response.created`` and reading the conversation back afterwards.

This script exercises **the production reconciler itself**
(:mod:`backend.audit.foundry`) rather than a parallel reimplementation, so a
green run is evidence about the code that actually ships.

Get a conversation id from the app log — with audit enabled, every agent-mode
turn logs the one it captured. Then::

    uv run python scripts/smoke_audit_conversation.py <conversation-id>

Expect to see the user's question, the tool the agent chose, the exact query it
issued, the passages it retrieved, and the answer. If tools come back empty,
check the turn actually triggered retrieval: a vague question makes the agent ask
for clarification instead of searching, which looks identical to a capture
failure but is not one.

Reads only. Costs nothing beyond a Foundry data-plane call.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402


def preview(value, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} chars)"


async def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    conversation_id = sys.argv[1]

    from backend.audit import foundry
    from backend.audit.records import TurnRecord

    print(f"conversation: {conversation_id}\n")

    # Reconcile against a real TurnRecord, exactly as the writer does.
    record = TurnRecord(session_id="smoke", turn_index=0, binding="agent")
    record.conversation_id = conversation_id

    try:
        ok = await foundry.reconcile(record)
    except Exception as e:
        print(f"FAILED  {type(e).__name__}: {e}")
        return 1
    finally:
        await foundry.close()

    if not ok:
        print("Reconciliation failed — the conversation could not be read.")
        print("Check PROJECT_ENDPOINT, that you are signed in (az login), and")
        print("that the id came from an agent-binding turn.")
        return 1

    if not record.tools:
        print("Conversation read, but no tool calls in it.")
        print("Either the turn genuinely used no tools, or the question was too")
        print("vague and the agent asked for clarification instead of searching.")
        print("Re-run against a turn you know performed a retrieval.")
        return 1

    print(f"Recovered {len(record.tools)} tool call(s):\n")
    for i, tool in enumerate(record.tools, 1):
        print(f"  {i}. {tool.name}")
        print(f"     args      {preview(tool.args)}")
        print(f"     hits      {tool.hit_count}")
        print(f"     results   {preview(tool.results)}")
        print(f"     source    {tool.source}")
        print()

    print("PASS — agent-mode tool detail is recoverable for this conversation.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
