"""audit trail: does it record the turn faithfully without ever risking latency?

Issue #30. Two things are being guarded here, and the second matters more than
the first.

1. **Fidelity** — a turn record must hold the question, the tool calls with
   their arguments and results, and the answer. Agent-binding tool I/O has to be
   reconstructed from Foundry conversation items, so the join of a
   ``remote_function_call`` to its ``remote_function_call_output`` by ``call_id``
   is tested directly against the item shape the live spike observed.

2. **Latency safety** — the capture path shares an event loop with the audio
   being streamed to the user. So this asserts the properties that keep it safe:
   submitting never blocks or raises even when the queue is full, capture is a
   no-op when audit is disabled, and a sink that throws cannot escape into the
   caller. These are the checks that would catch a well-meaning change of
   ``put_nowait`` to ``await put`` — the single most dangerous line that could be
   written in this feature.

No Azure, no network.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.audit import foundry  # noqa: E402
from backend.audit.queue import AuditQueue  # noqa: E402
from backend.audit.records import ToolCall, TurnRecord, redact  # noqa: E402
from backend.audit.sinks import FileSink, NullSink  # noqa: E402

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    ok = got == want
    if not ok:
        FAILED += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         got:  {got!r}")
        print(f"         want: {want!r}")


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


print("record shape")

record = TurnRecord(session_id="sess-1", turn_index=2, channel="web", binding="model")
record.user_text = "What was decided at the February board meeting?"
record.assistant_text = "The board approved the budget."
record.status = "completed"
record.tools.append(
    ToolCall(
        name="search_minutes",
        args={"query": "February board meeting"},
        results={"results": [{"id": "doc1"}, {"id": "doc2"}]},
        hit_count=2,
        elapsed_ms=340.0,
        source="in-process",
    )
)
doc = record.to_document(retention_days=365)

check("id is session:turn", doc["id"], "sess-1:2")
check("partition key is sessionId", doc["sessionId"], "sess-1")
check("binding recorded", doc["binding"], "model")
check("channel recorded", doc["channel"], "web")
check("question preserved", doc["user"]["text"], record.user_text)
check("answer preserved", doc["assistant"]["text"], record.assistant_text)
check("outcome preserved", doc["assistant"]["status"], "completed")
check("one tool captured", len(doc["tools"]), 1)
check("tool name", doc["tools"][0]["name"], "search_minutes")
check("tool args", doc["tools"][0]["args"], {"query": "February board meeting"})
check("tool hit count", doc["tools"][0]["hitCount"], 2)
check("tool provenance", doc["tools"][0]["source"], "in-process")
# Retention is a Cosmos-native per-item field, so expiry needs no cleanup job.
check("ttl is retention in seconds", doc["ttl"], 365 * 86400)
check("ttl omitted when retention disabled",
      "ttl" not in record.to_document(retention_days=0), True)


print()
print("redaction")

check("email masked", redact("mail me at a.person@contoso.com"),
      "mail me at [email]")
check("secret masked", redact("api_key=abc123secret"), "api_key=[redacted]")
check("ordinary text untouched", redact("the budget was approved"),
      "the budget was approved")
check("none passes through", redact(None), None)
# Redaction has to reach inside tool results, which is where retrieved passages
# (and therefore any PII in the corpus) actually live.
nested = ToolCall(
    name="search_minutes",
    results={"documents": [{"content": "contact jo@contoso.com"}]},
).to_dict(max_payload_bytes=32768, do_redact=True)
check("redaction reaches nested tool results",
      nested["results"]["documents"][0]["content"], "contact [email]")

# An unbounded tool result would cost writer CPU and storage on every turn.
big = ToolCall(name="t", results={"content": "x" * 100_000}).to_dict(
    max_payload_bytes=1024, do_redact=False
)
check_true("oversized results truncated", big["resultsTruncated"])


print()
print("foundry reconstruction (agent binding)")

# The exact item shape observed from the live deployment during the spike:
# a call and its output arrive as separate items joined by call_id.
items = [
    {"type": "message", "role": "user", "content": "What did the board decide?"},
    {
        "type": "remote_function_call",
        "id": "fc_1",
        "call_id": "call_9031724b",
        "name": "azure_ai_search",
        "arguments": '{"query":"Board Meeting 15 February 2026 budget"}',
    },
    {
        "type": "remote_function_call_output",
        "id": "fco_1",
        "call_id": "call_9031724b",
        "status": "completed",
        "output": {"documents": [
            {"id": "d1", "content": "The board approved the budget."},
            {"id": "d2", "content": "Capex was deferred."},
        ]},
    },
    {"type": "message", "role": "assistant", "content": "The board approved it."},
]

tools = foundry.extract_tools(items)
check("one tool reconstructed", len(tools), 1)
check("tool name recovered", tools[0].name, "azure_ai_search")
# The query is the whole point: it proves what was actually asked of the index.
check("query arguments parsed from JSON",
      tools[0].args, {"query": "Board Meeting 15 February 2026 budget"})
check_true("retrieved passages recovered", tools[0].results)
check("hit count derived from documents", tools[0].hit_count, 2)
check("provenance marked as foundry",
      tools[0].source, "foundry-conversation-item")
check("call joined to output by call_id", tools[0].call_id, "call_9031724b")

# A turn where the agent answered without searching must produce no tools at
# all — this is exactly the false negative that nearly sank the spike, so it is
# pinned here as an expected outcome rather than a bug.
check("no tools when the agent never searched",
      len(foundry.extract_tools([
          {"type": "message", "role": "user", "content": "hello"},
          {"type": "message", "role": "assistant", "content": "hi"},
      ])), 0)

# An output whose call went missing is still worth keeping.
orphan = foundry.extract_tools([
    {"type": "remote_function_call_output", "call_id": "x",
     "name": "azure_ai_search", "output": {"documents": [{"id": "d"}]}},
])
check("orphaned output still recorded", len(orphan), 1)


print()
print("latency safety")


async def latency_checks() -> None:
    # A full queue must drop, not wait. `await queue.put()` here would suspend
    # the event loop that is streaming audio to the user.
    q = AuditQueue(NullSink(), max_size=2)
    accepted = [q.submit(TurnRecord(session_id="s", turn_index=i)) for i in range(5)]
    check("accepts up to capacity", accepted[:2], [True, True])
    check("drops beyond capacity rather than blocking", accepted[2:], [False, False, False])
    check("drops are counted", q.dropped, 3)
    check("submitted counted", q.submitted, 2)

    # Submitting must never raise, whatever it is handed.
    class Hostile:
        @property
        def session_id(self):
            raise RuntimeError("boom")

    try:
        q.submit(Hostile())
        raised = False
    except Exception:
        raised = True
    check("submit never raises", raised, False)

    # A sink that throws must be contained by the writer, not surfaced.
    class BrokenSink:
        async def write(self, documents):
            raise RuntimeError("sink is down")

        async def close(self):
            return None

    broken = AuditQueue(BrokenSink(), max_size=10)
    broken.start()
    broken.submit(TurnRecord(session_id="s", turn_index=0))
    await asyncio.sleep(0.3)
    check("sink failure does not stop the writer", broken.written, 0)
    await broken.drain(timeout=0.2)

    # End to end through the real file sink.
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "audit.jsonl")
        fq = AuditQueue(FileSink(path), max_size=10)
        fq.start()
        rec = TurnRecord(session_id="sess-9", turn_index=0, binding="agent")
        rec.user_text = "what was decided?"
        rec.assistant_text = "the budget was approved"
        fq.submit(rec)
        await asyncio.sleep(0.5)
        await fq.drain(timeout=2.0)

        lines = [ln for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln]
        check("one line written per turn", len(lines), 1)
        written = json.loads(lines[0])
        check("written record keeps the question",
              written["user"]["text"], "what was decided?")
        check("written record keeps the answer",
              written["assistant"]["text"], "the budget was approved")
        check("writer reports what it wrote", fq.written, 1)


asyncio.run(latency_checks())


print()
print("disabled by default")

import backend.audit as audit  # noqa: E402


class FakeHandler:
    model_binding = False
    client_id = "c1"


# With ENABLE_AUDIT unset, every capture entry point must be inert — no record
# created, nothing enqueued, and crucially no exception raised into handle_event.
h = FakeHandler()
audit.start_turn(h)
audit.record_user_text(h, "hello")
audit.set_conversation(h, "conv_x", "resp_x")
audit.record_assistant_text(h, "hi")
audit.record_tool(h, "search_minutes", {"query": "x"}, {"results": []})
audit.finish_turn(h, status="completed")

check("audit is off unless enabled", audit.is_enabled(), False)
check("no record created when disabled", getattr(h, "_audit_record", None), None)
check("stats report disabled", audit.stats(), {"enabled": False})


print()
print("tool-call turns are not split (model binding)")

# In model binding a tool call ends one response before the tool has run, and
# the spoken answer arrives in a second. Recording those separately would file
# the question and its answer apart, with the tool detail attached to neither.
check("tool-call turn is carried, not emitted",
      audit._is_tool_call_turn(TurnRecord(session_id="s", turn_index=0),
                               ["ItemType.FUNCTION_CALL"]), True)
spoken = TurnRecord(session_id="s", turn_index=0)
spoken.assistant_text = "the budget was approved"
check("a turn that spoke is emitted",
      audit._is_tool_call_turn(spoken, ["ItemType.FUNCTION_CALL"]), False)
check("a normal turn is emitted",
      audit._is_tool_call_turn(TurnRecord(session_id="s", turn_index=0),
                               ["ItemType.MESSAGE"]), False)


print()
print("user transcript lands on the right turn, whichever order it arrives in")


class _StubQueue:
    """Just enough to make the capture API live — capture never touches a sink."""

    def submit(self, record) -> None:
        return None


# Input-audio transcription is a separate asynchronous model, so it completes
# either before response.created or after the response has already started.
# Both happen. A transcript written to the wrong record attributes one user's
# question to a different turn, which is silent and unfalsifiable after the
# fact — so both orderings are pinned here.
audit._queue = _StubQueue()
try:
    early = FakeHandler()
    audit.record_user_text(early, "what did we decide in February?")
    audit.start_turn(early)
    check("transcript arriving before response.created is carried onto the turn",
          early._audit_record.user_text, "what did we decide in February?")

    late = FakeHandler()
    audit.start_turn(late)
    audit.record_user_text(late, "what did we decide in March?")
    check("transcript arriving after response.created lands on the open turn",
          late._audit_record.user_text, "what did we decide in March?")
    check("late transcript is not left stashed for the next turn",
          getattr(late, "_audit_pending_user", None), None)

    # A second transcript while a turn is already answered belongs to the next
    # turn, not this one — it must not overwrite the question being answered.
    audit.record_user_text(late, "and in April?")
    check("a further transcript does not overwrite the open turn",
          late._audit_record.user_text, "what did we decide in March?")
    check("it is stashed for the turn that follows",
          late._audit_pending_user[0], "and in April?")
finally:
    audit._queue = None


print()
if FAILED:
    print(f"{FAILED} check(s) FAILED")
    sys.exit(1)
print("All checks passed.")
