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
# The correlation handle. Present in the document from day one — even though
# nothing populates it yet — so that shipping telemetry later is an additive
# change rather than a second revision of a schema already holding real records.
check("correlation field is always in the document shape",
      "operationId" in doc["meta"], True)
check("correlation is null until telemetry ships", doc["meta"]["operationId"], None)
correlated = TurnRecord(session_id="sess-1", turn_index=3)
correlated.operation_id = "4bf92f3577b34da6a3ce929d0e0e4736"
check("correlation id round-trips when set",
      correlated.to_document()["meta"]["operationId"],
      "4bf92f3577b34da6a3ce929d0e0e4736")


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
# ``lossy`` is present even here. /health reads it unconditionally, so a shape
# that only sometimes carries the key would make every consumer handle a None
# that means "no idea" the same as a False that means "nothing was lost".
check("stats report disabled", audit.stats(),
      {"enabled": False, "sink": None, "degraded": None, "lossy": False})


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
print("turn duration is measured, and survives a carried exchange")

# `latencyMs` has been in the document shape since the feature shipped, but
# nothing ever assigned it — every record written so far carried a null. It is
# measured on the monotonic clock rather than as `endedAt - startedAt`, because
# a wall-clock step under NTP correction can otherwise yield a negative
# duration, and a turn that appears to have taken -400ms is worse than no
# number at all.
open_turn = TurnRecord(session_id="s", turn_index=0)
check("duration is null while the turn is still open",
      open_turn.to_document()["latencyMs"], None)

timed = TurnRecord(session_id="s", turn_index=0)
timed._monotonic_start -= 1.5
timed.mark_ended()
check_true("duration is populated once the turn closes",
           1500 <= timed.to_document()["latencyMs"] < 1600)
check_true("closing stamps the end time as well", timed.to_document()["endedAt"])

instant = TurnRecord(session_id="s", turn_index=0)
instant.mark_ended()
check_true("duration is never negative", instant.to_document()["latencyMs"] >= 0)


class _CapturingQueue:
    """Keeps what was submitted, so the emitted record can be inspected."""

    def __init__(self):
        self.records = []

    def submit(self, record) -> None:
        self.records.append(record)


# A model-binding tool call ends one response before the tool has run, and the
# spoken answer arrives in a second. The record is carried rather than
# re-created, so the tool's own execution time must stay inside the measured
# turn instead of vanishing in the gap between two responses.
captured = _CapturingQueue()
audit._queue = captured
try:
    carried_handler = FakeHandler()
    audit.start_turn(carried_handler)
    carried_handler._audit_record._monotonic_start -= 2.0
    audit.finish_turn(carried_handler, status="completed",
                      output_types=["ItemType.FUNCTION_CALL"])
    check("the tool-call response is carried, not written", len(captured.records), 0)

    audit.start_turn(carried_handler)
    carried_handler._audit_record._monotonic_start -= 1.0
    audit.record_assistant_text(carried_handler, "the budget was approved")
    audit.finish_turn(carried_handler, status="completed",
                      output_types=["ItemType.MESSAGE"])
    check("the carried exchange lands as a single record", len(captured.records), 1)
    check_true("and is timed across both responses",
               captured.records[0].to_document()["latencyMs"] >= 3000)
finally:
    audit._queue = None


print()
print("trace correlation is resolved once, and degrades to null")

# OpenTelemetry is deliberately not a dependency yet, so resolution must return
# None rather than raise. Resolving once at startup also matters for latency: a
# *failing* import is not cached by Python and re-walks sys.path every attempt,
# so doing this per turn would put real cost on the hot path.
check("resolution is safe when OpenTelemetry is absent",
      audit._resolve_trace_id() is None or callable(audit._resolve_trace_id()), True)

audit._queue = _StubQueue()
try:
    h2 = FakeHandler()
    audit.start_turn(h2)
    check("no correlation captured when nothing resolved",
          h2._audit_record.operation_id, None)

    audit._trace_id = lambda: "4bf92f3577b34da6a3ce929d0e0e4736"
    traced = FakeHandler()
    audit.start_turn(traced)
    check("correlation captured onto the turn when a tracer is present",
          traced._audit_record.operation_id, "4bf92f3577b34da6a3ce929d0e0e4736")

    # Correlation is the least valuable field on the record. A tracer that
    # throws must cost exactly that field — never the question, the tool
    # results and the answer, which is what happens if the capture is
    # positioned before the record is attached to the handler.
    def _boom():
        raise RuntimeError("tracer exploded")

    audit._trace_id = _boom
    survived = FakeHandler()
    audit.start_turn(survived)
    check("a failing tracer still yields a record",
          getattr(survived, "_audit_record", None) is not None, True)
    check("a failing tracer costs only the correlation field",
          survived._audit_record.operation_id, None)
    audit.record_user_text(survived, "what did we decide?")
    check("a failing tracer leaves the turn fully usable",
          survived._audit_record.user_text, "what did we decide?")
finally:
    audit._queue = None
    audit._trace_id = None


print()
print("sink selection and the fallback policy (#104)")

# This path had no coverage at all, which is how a silent fallback shipped. The
# three conditions below each used to return FileSink while the app reported
# audit as working — on Container Apps that means an ephemeral file, lost on the
# next revision, with `written` still counting up.
import backend.audit.cosmos as cosmos_mod  # noqa: E402
from backend.audit import AuditSinkUnavailable  # noqa: E402


class _ConfigPatch:
    """Patch the config constants bound into backend.audit, restoring after.

    They are attributes of the module because __init__ does
    ``from ..config import ...``, and _build_sink() resolves them from module
    globals at call time, so setting them here is what the running app sees.
    """

    def __init__(self, **overrides):
        self._overrides = overrides
        self._saved = {}

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


def _build(**overrides):
    """Build a sink under the given config. Returns (class name, degraded reason)."""
    with _ConfigPatch(**overrides):
        sink = asyncio.run(audit._build_sink())
        return type(sink).__name__, audit._degraded


def _refuses(**overrides) -> bool:
    with _ConfigPatch(**overrides):
        try:
            asyncio.run(audit._build_sink())
            return False
        except AuditSinkUnavailable:
            return True


class _BrokenCosmosSink:
    """Stands in for the real sink failing in warm(), where RBAC and firewall
    problems both surface. No network, so this stays a unit test."""

    def __init__(self, **kwargs):
        pass

    async def warm(self):
        raise RuntimeError("403 Forbidden — blocked by firewall settings")


check("a configured 'none' sink is built as asked", _build(AUDIT_SINK="none")[0],
      "NullSink")
check("a configured 'file' sink is built as asked", _build(AUDIT_SINK="file")[0],
      "FileSink")

check_true("cosmos without an endpoint refuses to start",
           _refuses(AUDIT_SINK="cosmos", AUDIT_COSMOS_ENDPOINT="",
                    AUDIT_SINK_FALLBACK="error"))
check_true("an unrecognised sink name refuses to start",
           _refuses(AUDIT_SINK="cosmosdb", AUDIT_SINK_FALLBACK="error"))

_real_cosmos_sink = cosmos_mod.CosmosSink
cosmos_mod.CosmosSink = _BrokenCosmosSink
try:
    check_true("a cosmos sink that cannot connect refuses to start",
               _refuses(AUDIT_SINK="cosmos",
                        AUDIT_COSMOS_ENDPOINT="https://x.documents.azure.com:443/",
                        AUDIT_SINK_FALLBACK="error"))
    name, reason = _build(AUDIT_SINK="cosmos",
                          AUDIT_COSMOS_ENDPOINT="https://x.documents.azure.com:443/",
                          AUDIT_SINK_FALLBACK="file")
    check("an opted-in fallback still yields a working sink", name, "FileSink")
    check_true("a failed connection is recorded as the degraded reason", reason)
finally:
    cosmos_mod.CosmosSink = _real_cosmos_sink

name, reason = _build(AUDIT_SINK="cosmos", AUDIT_COSMOS_ENDPOINT="",
                      AUDIT_SINK_FALLBACK="file")
check("fallback=file yields the file sink", name, "FileSink")
check_true("fallback=file says why audit is degraded", reason)

name, reason = _build(AUDIT_SINK="cosmos", AUDIT_COSMOS_ENDPOINT="",
                      AUDIT_SINK_FALLBACK="none")
check("fallback=none yields the null sink", name, "NullSink")
check_true("fallback=none says why audit is degraded", reason)

# A typo in the fallback must not read as permission to fall back — that would
# turn a second configuration mistake into the silent behaviour being removed.
check_true("a misspelled fallback is not permission to fall back",
           _refuses(AUDIT_SINK="cosmos", AUDIT_COSMOS_ENDPOINT="",
                    AUDIT_SINK_FALLBACK="flie"))

# init_audit absorbs almost every failure on purpose, so that a broken audit
# config cannot take the app down. This is the one exception, and it only works
# if the catch-all does not swallow it.
with _ConfigPatch(ENABLE_AUDIT=True, AUDIT_SINK="cosmos",
                  AUDIT_COSMOS_ENDPOINT="", AUDIT_SINK_FALLBACK="error"):
    refused = False
    try:
        asyncio.run(audit.init_audit())
    except AuditSinkUnavailable:
        refused = True
    check_true("init_audit lets the refusal reach the lifespan", refused)
    check("a refused start leaves audit off", audit.is_enabled(), False)


async def _init_stats_shutdown():
    # One event loop for all three: the writer task is bound to the loop it was
    # started on, so draining it from a second asyncio.run would not work.
    await audit.init_audit()
    reported = audit.stats()
    await audit.shutdown_audit()
    return reported, audit._degraded


with _ConfigPatch(ENABLE_AUDIT=True, AUDIT_SINK="cosmos",
                  AUDIT_COSMOS_ENDPOINT="", AUDIT_SINK_FALLBACK="file"):
    reported, after_shutdown = asyncio.run(_init_stats_shutdown())
    check("stats name the resolved sink, not the configured one",
          reported["sink"], "FileSink")
    check_true("stats expose the degraded reason", reported["degraded"])
    check_true("stats still report audit as enabled while degraded",
               reported["enabled"])
    check("shutdown clears the degraded flag", after_shutdown, None)


print()
if FAILED:
    print(f"{FAILED} check(s) FAILED")
    sys.exit(1)
print("All checks passed.")
