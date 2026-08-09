"""Conversation audit trail (issue #30).

A durable record of every turn — the user's question, what the tools returned,
and the answer the model gave — across all four channels and both bindings.

**The design constraint that shapes everything here:** the capture points share
an event loop with the audio being streamed to the user. Audit work is therefore
never allowed to be slow, blocking, or fallible on that path. Concretely:

* Capture attaches only to discrete lifecycle events (~4 per turn), never to
  audio or transcript deltas, so per-frame cost is exactly zero.
* Submitting is ``put_nowait`` on a bounded queue — it drops rather than waits.
* Redaction, truncation and serialisation happen in a worker thread.
* Every entry point swallows its own exceptions.

When ``ENABLE_AUDIT`` is false the queue is never created and every function
here returns immediately on a single ``is None`` check, so a deployment that
does not want audit pays nothing for its existence.

Usage from the hot path::

    audit.start_turn(handler)          # RESPONSE_CREATED
    audit.set_conversation(handler, cid)
    audit.record_user_text(handler, text, item_id)
    audit.record_assistant_text(handler, text)
    audit.finish_turn(handler, status) # RESPONSE_DONE
"""

import logging
from typing import Any, Optional

from ..config import (
    AUDIT_COSMOS_CONTAINER,
    AUDIT_COSMOS_DATABASE,
    AUDIT_COSMOS_ENDPOINT,
    AUDIT_QUEUE_MAX,
    AUDIT_RECONCILE_AGENT_TOOLS,
    AUDIT_REDACT,
    AUDIT_RETENTION_DAYS,
    AUDIT_SINK,
    AUDIT_SINK_FALLBACK,
    AUDIT_TOOL_PAYLOAD_MAX_KB,
    ENABLE_AUDIT,
)
from .records import ToolCall, TurnRecord, utc_now_iso

logger = logging.getLogger(__name__)

# The single global. None means "audit is off", and that check is the entire
# cost of this feature in a deployment that does not use it.
_queue = None

# Resolved once at startup, never on the hot path. A *failing* import is not
# cached by Python — it re-walks sys.path on every attempt — so trying this
# per turn would be exactly the kind of cost this module refuses to incur.
_trace_id = None

# The sink actually in use, and why it isn't the configured one. Both are needed
# because the two can differ: without them the startup log reports what was
# *asked for* rather than what is happening, which is how a deployment writing
# to an ephemeral file can report itself as writing to Cosmos.
_sink_name = None
_degraded = None


class AuditSinkUnavailable(RuntimeError):
    """The configured sink could not be built and no fallback was permitted.

    Raised out of :func:`init_audit` and deliberately not caught there, so the
    FastAPI lifespan fails and the deployment stops. That is the point: an
    operator who set ``ENABLE_AUDIT=true`` asked for a record of every
    conversation, and a process that serves conversations it cannot record is
    the exact failure the feature exists to prevent.
    """


def _resolve_trace_id():
    """Return a callable giving the current W3C trace id, or None.

    App Insights records the OpenTelemetry trace id as ``operation_Id``, so
    capturing it on the turn is what later allows a slow request found in App
    Insights to be joined to that turn's full content in Cosmos — without
    putting any content into App Insights itself.

    OpenTelemetry is deliberately not a dependency yet. Until it is, this
    returns None and the field stays null; when it arrives, capture starts
    working with no further change here.
    """
    try:
        from opentelemetry import trace
    except Exception:
        return None

    def _current() -> Optional[str]:
        ctx = trace.get_current_span().get_span_context()
        if ctx is None or not ctx.is_valid:
            return None
        return format(ctx.trace_id, "032x")

    return _current


def _capture_operation_id() -> Optional[str]:
    """Best-effort correlation id. Returns None rather than raising, ever."""
    if _trace_id is None:
        return None
    try:
        return _trace_id()
    except Exception:
        return None


def is_enabled() -> bool:
    return _queue is not None


def stats() -> dict:
    """Counters plus the two facts that say whether they can be trusted.

    ``sink`` is the resolved sink, not the configured one, and ``degraded``
    names the reason they differ. A caller reading ``written`` needs both to
    know whether those records went anywhere durable.
    """
    if _queue is None:
        return {"enabled": False, "sink": None, "degraded": _degraded}
    return {**_queue.stats(), "enabled": True, "sink": _sink_name,
            "degraded": _degraded}


async def _fallback_or_raise(reason: str):
    """Apply ``AUDIT_SINK_FALLBACK`` to a sink that could not be built.

    Every route to a sink other than the configured one passes through here, so
    there is one place that decides whether a broken audit configuration stops
    the deployment, and one place that records that audit is degraded.
    """
    from .sinks import FileSink, NullSink

    if AUDIT_SINK_FALLBACK not in ("file", "none"):
        # Covers the default 'error' and any typo. A misspelled fallback must
        # not be read as permission to fall back.
        raise AuditSinkUnavailable(reason)

    global _degraded
    _degraded = reason
    logger.error(
        f"[AUDIT] DEGRADED — {reason}. AUDIT_SINK_FALLBACK={AUDIT_SINK_FALLBACK!r}, "
        f"so the trail is NOT going to the configured destination. Both fallbacks "
        f"are ephemeral on Container Apps and are lost on the next revision."
    )
    return NullSink() if AUDIT_SINK_FALLBACK == "none" else FileSink()


async def _build_sink():
    """Construct the configured sink, or apply the fallback policy."""
    from .sinks import FileSink, NullSink

    kind = AUDIT_SINK
    if kind == "none":
        return NullSink()
    if kind == "file":
        return FileSink()
    if kind == "cosmos":
        if not AUDIT_COSMOS_ENDPOINT:
            return await _fallback_or_raise(
                "AUDIT_SINK=cosmos but AUDIT_COSMOS_ENDPOINT is unset"
            )
        try:
            from .cosmos import CosmosSink

            sink = CosmosSink(
                endpoint=AUDIT_COSMOS_ENDPOINT,
                database=AUDIT_COSMOS_DATABASE,
                container=AUDIT_COSMOS_CONTAINER,
            )
            await sink.warm()
            return sink
        except Exception as e:
            # warm() is where a missing data-plane role, a firewall rule or a
            # private-only account first shows up, and all three are permanent
            # rather than transient. See scripts/smoke_audit_cosmos.py.
            return await _fallback_or_raise(f"Cosmos sink unavailable ({e})")
    return await _fallback_or_raise(f"unknown AUDIT_SINK={kind!r}")


async def init_audit() -> None:
    """Start the writer. Called once from the FastAPI lifespan.

    Everything expensive — client construction, TLS, the first managed-identity
    token — happens here, at startup, so no conversation ever pays for it.
    """
    global _queue
    if not ENABLE_AUDIT:
        logger.info("[AUDIT] disabled (ENABLE_AUDIT=false)")
        return
    if _queue is not None:
        return
    try:
        from .queue import AuditQueue

        global _trace_id, _sink_name
        _trace_id = _resolve_trace_id()
        sink = await _build_sink()
        _queue = AuditQueue(
            sink,
            max_size=AUDIT_QUEUE_MAX,
            retention_days=AUDIT_RETENTION_DAYS,
            redact=AUDIT_REDACT,
            max_payload_bytes=AUDIT_TOOL_PAYLOAD_MAX_KB * 1024,
        )
        _queue.start()
        _sink_name = type(sink).__name__
        # Report what is actually in use. Logging the configured value instead
        # is what let a degraded deployment announce "sink=cosmos" while
        # appending to a file nobody would ever read.
        suffix = f" (configured: {AUDIT_SINK}, DEGRADED)" if _degraded else ""
        logger.info(f"[AUDIT] enabled — sink={_sink_name}{suffix}")
    except AuditSinkUnavailable:
        # Not absorbed, unlike everything else here. This is the one audit
        # failure allowed to stop the app, because continuing would mean
        # serving conversations that are silently not recorded.
        _queue = None
        _sink_name = None
        logger.critical(
            "[AUDIT] refusing to start: audit was requested but no sink could be "
            "built, and AUDIT_SINK_FALLBACK does not permit a fallback. Fix the "
            "configuration, or set AUDIT_SINK_FALLBACK=file|none to accept an "
            "ephemeral trail, or set ENABLE_AUDIT=false to run without audit."
        )
        raise
    except Exception as e:
        # A broken audit configuration must not stop the app from serving.
        _queue = None
        _sink_name = None
        logger.error(f"[AUDIT] failed to start, continuing without audit: {e}")


async def shutdown_audit() -> None:
    global _queue
    if _queue is None:
        return
    try:
        await _queue.drain()
    except Exception as e:
        logger.debug(f"[AUDIT] drain failed: {e}")
    finally:
        _queue = None
        global _trace_id, _sink_name, _degraded
        _trace_id = None
        _sink_name = None
        _degraded = None
    try:
        from . import foundry

        await foundry.close()
    except Exception:
        pass


# --- capture API -------------------------------------------------------------
# Each function is a no-op when audit is off, and none of them can raise.


def start_turn(handler) -> None:
    """Open a record for a new turn. Called on RESPONSE_CREATED.

    Resumes the carried record when the previous response was an internal
    tool-call turn — see :func:`finish_turn` for why one exchange can span two
    responses.
    """
    if _queue is None:
        return
    try:
        carried = getattr(handler, "_audit_carry", None)
        if carried is None and getattr(handler, "model_binding", False):
            # Model binding only. A tool-call response never reaches finish_turn:
            # handle_conversation_item consumes its RESPONSE_DONE with
            # _wait_for_event, which *returns* the event rather than dispatching
            # it, so the carry in finish_turn never runs. The record holding the
            # tool call is therefore still in flight here, and building a new one
            # would drop the tools we just captured.
            #
            # Agent binding cannot reach this: its tools are filled in by
            # foundry.reconcile() in the writer task, long after the record has
            # left the handler, so an in-flight agent record never has any.
            # The guard is explicit anyway, so the isolation is a stated
            # property rather than a coincidence of ordering.
            inflight = getattr(handler, "_audit_record", None)
            if inflight is not None and inflight.tools and not inflight.assistant_text:
                carried = inflight
        if carried is not None:
            handler._audit_carry = None
            handler._audit_record = carried
            return
        index = getattr(handler, "_audit_turn_index", 0)
        handler._audit_turn_index = index + 1
        record = TurnRecord(
            session_id=_session_id(handler),
            turn_index=index,
            channel=getattr(handler, "audit_channel", "web"),
            binding="model" if getattr(handler, "model_binding", False) else "agent",
        )
        # One ledger per session, shared by reference with every record in it,
        # so tool reconciliation can tell this turn's calls from earlier ones.
        # Lives on the handler and dies with it.
        seen = getattr(handler, "_audit_seen_calls", None)
        if seen is None:
            seen = set()
            handler._audit_seen_calls = seen
        record.seen_call_ids = seen
        # A user transcript arrives *before* response.created, so carry across
        # whatever was stashed for this turn.
        pending = getattr(handler, "_audit_pending_user", None)
        if pending:
            record.user_text, record.user_item_id, record.user_at = pending
            handler._audit_pending_user = None
        # Attach before correlating. Correlation is the least valuable field on
        # the record, so it must never be positioned where its failure could
        # cost us the question, the tool results and the answer.
        handler._audit_record = record
        record.operation_id = _capture_operation_id()
    except Exception as e:
        logger.debug(f"[AUDIT] start_turn failed: {e}")


def _session_id(handler) -> str:
    return (
        getattr(handler, "voice_session_id", None)
        or getattr(handler, "client_id", None)
        or "unknown"
    )


def record_user_text(handler, text: str, item_id: str = "") -> None:
    """Capture the user's final transcript.

    Input-audio transcription is a separate, asynchronous model, so it can
    complete either *before* ``response.created`` opens the turn or *after* the
    response has already started. Both orderings happen in practice, so handle
    both: fill the open record if it is still waiting for a question, otherwise
    stash the text for the turn that is about to open.

    Getting this wrong is not a cosmetic bug — a transcript written to the wrong
    record attributes one user's question to another turn.
    """
    if _queue is None:
        return
    try:
        stamped = (text, item_id or None, utc_now_iso())
        record = getattr(handler, "_audit_record", None)
        if record is not None and not record.user_text:
            record.user_text, record.user_item_id, record.user_at = stamped
            return
        handler._audit_pending_user = stamped
    except Exception as e:
        logger.debug(f"[AUDIT] record_user_text failed: {e}")


def set_conversation(handler, conversation_id: Optional[str], response_id: str = "") -> None:
    """Store the Foundry conversation id for this turn.

    The single most important line in agent binding: there is no way to
    enumerate conversations later, so an id not captured here means the turn's
    tool detail is gone permanently. Costs one attribute write.
    """
    if _queue is None:
        return
    try:
        record = getattr(handler, "_audit_record", None)
        if record is None:
            return
        if conversation_id:
            record.conversation_id = conversation_id
            if record.binding == "agent" and AUDIT_RECONCILE_AGENT_TOOLS:
                record.tools_pending = True
        if response_id:
            record.response_id = response_id
    except Exception as e:
        logger.debug(f"[AUDIT] set_conversation failed: {e}")


def record_assistant_text(handler, text: str) -> None:
    """Capture the assistant's answer.

    Attached to the ``*_DONE`` events, which already carry the complete text,
    so deltas are never accumulated and the streaming path is never touched.
    """
    if _queue is None:
        return
    try:
        record = getattr(handler, "_audit_record", None)
        if record is not None and text:
            record.assistant_text = text
    except Exception as e:
        logger.debug(f"[AUDIT] record_assistant_text failed: {e}")


def record_tool(
    handler,
    name: str,
    args: Any = None,
    results: Any = None,
    elapsed_ms: Optional[float] = None,
    error: Optional[str] = None,
) -> None:
    """Capture a tool call executed in our own process (model binding).

    Enqueues the result **by reference** — no copying, no serialisation. The
    writer owns the cost of turning it into a document.
    """
    if _queue is None:
        return
    try:
        record = getattr(handler, "_audit_record", None)
        if record is None:
            return
        hit_count = None
        if isinstance(results, dict):
            for key in ("results", "documents", "hits"):
                value = results.get(key)
                if isinstance(value, list):
                    hit_count = len(value)
                    break
        record.tools.append(
            ToolCall(
                name=name,
                args=args,
                results=results,
                hit_count=hit_count,
                elapsed_ms=elapsed_ms,
                error=error,
                source="in-process",
            )
        )
    except Exception as e:
        logger.debug(f"[AUDIT] record_tool failed: {e}")


def finish_turn(
    handler,
    status: Optional[str] = None,
    truncated: bool = False,
    output_types: Optional[list] = None,
) -> None:
    """Close the turn and hand it to the writer. Called on RESPONSE_DONE.

    One exception to "one response, one record": in model binding a tool call
    ends the current response *before* the tool has run, and the spoken answer
    arrives in a second response. Emitting that as two records would file the
    question and its answer separately, with the tool detail attached to
    neither. So a response that produced no assistant text and whose output was
    a function call is **carried forward** instead of written, and the next
    ``RESPONSE_CREATED`` resumes it. The record that finally lands holds the
    whole exchange: question, tools, answer.
    """
    if _queue is None:
        return
    try:
        record = getattr(handler, "_audit_record", None)
        if record is None:
            return

        if _is_tool_call_turn(record, output_types):
            handler._audit_record = None
            handler._audit_carry = record
            return

        handler._audit_record = None
        record.mark_ended()
        record.status = status
        record.truncated = truncated
        record.agent_name = getattr(handler, "audit_agent_name", None)
        record.model = getattr(handler, "audit_model", None)
        # Nothing below this line runs on the event loop.
        _queue.submit(record)
    except Exception as e:
        logger.debug(f"[AUDIT] finish_turn failed: {e}")


def _is_tool_call_turn(record: TurnRecord, output_types: Optional[list]) -> bool:
    """True when this response only dispatched a tool and said nothing."""
    if record.assistant_text or not output_types:
        return False
    return any("function_call" in str(t).lower() for t in output_types)


def discard_turn(handler) -> None:
    """Drop the in-flight record without writing it (e.g. an empty segment)."""
    if _queue is None:
        return
    try:
        handler._audit_record = None
    except Exception:
        pass
