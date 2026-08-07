"""Shape of an audit record, and the CPU-bound work of preparing one.

One document per **turn** — the natural audit unit, and the unit a compliance
question is actually asked in ("what did she say, what did she look up, what did
she answer?").

Everything in this module that costs real CPU — redaction, truncation,
serialisation — is called from the background writer, never from the capture
sites on the event loop. See ``backend/audit/queue.py``.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Patterns masked before persistence. Deliberately conservative: an audit trail
# that has been over-redacted is useless, so this targets only things that are
# unambiguously secrets or strong identifiers, not names or general content.
_REDACTIONS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"), "[email]"),
    # Bearer/JWT and common key material.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"), "[jwt]"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|pwd|token)\s*[:=]\s*\S+"), r"\1=[redacted]"),
    # 13-19 digits with optional separators - payment card shaped. Last, so it
    # cannot eat digits out of a token that an earlier rule would have masked.
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[card]"),
)


def redact(text: Optional[str]) -> Optional[str]:
    """Mask secrets and strong identifiers in free text.

    Runs in the writer thread. Returns the input unchanged when it is empty or
    not a string, so callers never have to guard.
    """
    if not text or not isinstance(text, str):
        return text
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _redact_deep(value: Any, _depth: int = 0) -> Any:
    """Apply :func:`redact` to every string inside a nested structure."""
    if _depth > 12:
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_deep(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_deep(v, _depth + 1) for v in value]
    return value


def _truncate(value: Any, max_bytes: int) -> tuple[Any, bool]:
    """Bound a serialised value to ``max_bytes``.

    Returns ``(value, was_truncated)``. Large tool results are the reason this
    exists: a broad AI Search hit can carry tens of KB of passages, and storing
    them unbounded costs both writer CPU and ingest.
    """
    if max_bytes <= 0 or value is None:
        return value, False
    try:
        encoded = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        encoded = str(value)
    if len(encoded.encode("utf-8")) <= max_bytes:
        return value, False
    clipped = encoded.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    return {"_truncated": True, "preview": clipped}, True


@dataclass
class ToolCall:
    """One tool invocation within a turn.

    ``source`` records *how* we learned about this call, which is the crux of
    the fidelity question in ``docs/audit.md``:

    - ``in-process`` — model binding; we executed the tool ourselves and hold
      the exact arguments and return value.
    - ``foundry-conversation-item`` — agent binding; reconstructed after the
      turn from the Foundry conversations API, because the agent runtime relays
      no tool events to us at all.
    """

    name: str
    args: Any = None
    results: Any = None
    hit_count: Optional[int] = None
    elapsed_ms: Optional[float] = None
    error: Optional[str] = None
    source: str = "in-process"
    call_id: Optional[str] = None

    def to_dict(self, *, max_payload_bytes: int, do_redact: bool) -> dict:
        args = self.args
        results = self.results
        if do_redact:
            args = _redact_deep(args)
            results = _redact_deep(results)
        results, truncated = _truncate(results, max_payload_bytes)
        args, _ = _truncate(args, max_payload_bytes)
        return {
            "name": self.name,
            "callId": self.call_id,
            "args": args,
            "results": results,
            "hitCount": self.hit_count,
            "elapsedMs": self.elapsed_ms,
            "error": self.error,
            "source": self.source,
            "resultsTruncated": truncated,
        }


@dataclass
class TurnRecord:
    """One conversation turn, accumulated on the handler and emitted once.

    Built incrementally on the hot path — each capture site sets one or two
    plain fields, which is why this is a mutable dataclass and not a validated
    model. The expensive conversion to a storable document happens later, in
    :meth:`to_document`, off the event loop.
    """

    session_id: str
    turn_index: int
    channel: str = "web"
    binding: str = "agent"

    started_at: str = field(default_factory=utc_now_iso)
    ended_at: Optional[str] = None
    latency_ms: Optional[float] = None

    user_text: Optional[str] = None
    user_item_id: Optional[str] = None
    user_at: Optional[str] = None

    assistant_text: Optional[str] = None
    status: Optional[str] = None
    truncated: bool = False

    tools: list[ToolCall] = field(default_factory=list)

    # Agent binding: the handle that makes this turn's tool I/O recoverable.
    # There is no `list` operation on Foundry conversations, so if this is not
    # captured live from `response.created` the tool detail is lost forever.
    conversation_id: Optional[str] = None
    response_id: Optional[str] = None
    tools_pending: bool = False

    agent_name: Optional[str] = None
    model: Optional[str] = None
    app_version: Optional[str] = None

    # App Insights surfaces the OpenTelemetry trace id as ``operation_Id``.
    # Carrying it here is what lets a slow turn found in App Insights be joined
    # to its full content in Cosmos. Null until telemetry ships; the field
    # exists now so that arrival is an additive change, not a schema revision.
    operation_id: Optional[str] = None

    user_id: Optional[str] = None
    display_name: Optional[str] = None
    tenant_id: Optional[str] = None

    def to_document(
        self,
        *,
        retention_days: int = 365,
        do_redact: bool = True,
        max_payload_bytes: int = 32 * 1024,
    ) -> dict:
        """Render the storable document. Called in the writer, never inline."""
        doc = {
            "id": f"{self.session_id}:{self.turn_index}",
            "sessionId": self.session_id,
            "turnIndex": self.turn_index,
            "channel": self.channel,
            "binding": self.binding,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "latencyMs": self.latency_ms,
            "user": {
                "text": redact(self.user_text) if do_redact else self.user_text,
                "itemId": self.user_item_id,
                "at": self.user_at,
            },
            "tools": [
                t.to_dict(max_payload_bytes=max_payload_bytes, do_redact=do_redact)
                for t in self.tools
            ],
            "assistant": {
                "text": redact(self.assistant_text) if do_redact else self.assistant_text,
                "status": self.status,
                "truncated": self.truncated,
            },
            "identity": {
                "userId": self.user_id,
                "displayName": self.display_name,
                "tenantId": self.tenant_id,
            },
            "meta": {
                "agentName": self.agent_name,
                "model": self.model,
                "appVersion": self.app_version,
                "conversationId": self.conversation_id,
                "responseId": self.response_id,
                "toolsPending": self.tools_pending,
                "operationId": self.operation_id,
            },
        }
        if retention_days > 0:
            doc["ttl"] = retention_days * 86400
        return doc
