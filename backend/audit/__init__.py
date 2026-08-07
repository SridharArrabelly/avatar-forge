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
    AUDIT_TOOL_PAYLOAD_MAX_KB,
    ENABLE_AUDIT,
)
from .records import ToolCall, TurnRecord, utc_now_iso

logger = logging.getLogger(__name__)

# The single global. None means "audit is off", and that check is the entire
# cost of this feature in a deployment that does not use it.
_queue = None


def is_enabled() -> bool:
    return _queue is not None


def stats() -> dict:
    return _queue.stats() if _queue is not None else {"enabled": False}


async def _build_sink():
    """Construct the configured sink, falling back to file on any failure."""
    from .sinks import FileSink, NullSink

    kind = AUDIT_SINK
    if kind == "none":
        return NullSink()
    if kind == "file":
        return FileSink()
    if kind == "cosmos":
        if not AUDIT_COSMOS_ENDPOINT:
            logger.warning(
                "[AUDIT] AUDIT_SINK=cosmos but AUDIT_COSMOS_ENDPOINT is unset — "
                "falling back to the local file sink"
            )
            return FileSink()
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
            logger.error(f"[AUDIT] Cosmos sink unavailable ({e}) — using file sink")
            return FileSink()
    logger.warning(f"[AUDIT] unknown AUDIT_SINK={kind!r} — using file sink")
    return FileSink()


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

        sink = await _build_sink()
        _queue = AuditQueue(
            sink,
            max_size=AUDIT_QUEUE_MAX,
            retention_days=AUDIT_RETENTION_DAYS,
            redact=AUDIT_REDACT,
            max_payload_bytes=AUDIT_TOOL_PAYLOAD_MAX_KB * 1024,
        )
        _queue.start()
        logger.info(f"[AUDIT] enabled — sink={AUDIT_SINK}")
    except Exception as e:
        # A broken audit configuration must not stop the app from serving.
        _queue = None
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
        # A user transcript arrives *before* response.created, so carry across
        # whatever was stashed for this turn.
        pending = getattr(handler, "_audit_pending_user", None)
        if pending:
            record.user_text, record.user_item_id, record.user_at = pending
            handler._audit_pending_user = None
        handler._audit_record = record
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
        record.ended_at = utc_now_iso()
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
