"""Opt-in, content-free server latency observations (no SDK/network ownership).

Enable with ENABLE_LATENCY_TRACE=true before backend startup (INFO logging).
Default-off handlers do not construct a trace; legacy [LATENCY] logs remain.
All times are monotonic milliseconds relative to a server-side turn origin:
text submission received, or audio speech_stopped received (NOT microphone
capture time). Only completed, non-function-call responses qualify as terminal
answers; a preamble in a tool-call response remains first-any, never T4/T5.
T1 is the first local execution start; T2/T3 are the last tool's execution/send
completion. The tools array disambiguates multi-tool turns. Cached input is a
subset of input, never an additional token charge.

No transcript, arguments, results, request identifiers, exception text, or
client configuration is retained. Only fixed labels, generated turn IDs,
validated provider response IDs, timings and counts cross the logging boundary.
One bounded JSON record is emitted at completion/cancellation, not per delta.
Each metadata list is capped at 32 entries; totals become null if truncated.
Capture failures invalidate measurements and emit a fixed-label warning, never
exception text. Correlation failure/overflow disables correlation for this
session rather than assigning late responses to another turn (128 pending sends
or remembered response IDs). Error counters saturate at 255; warnings are
deduplicated by fixed method/exception category, except each failed summary.
Session character counts describe strings/compact tool-schema JSON, NOT tokens
or exact wire bytes. Provider token details are overlapping breakdowns, not
additional charges to add to the provider's reported total.
WebRTC avatar playback and managed-agent tool execution are not observable here.
Agent-mode output may mix a preamble and answer inside one opaque response, so
only first-any/per-response output times are reported there, not terminal T4/T5.
"""

import asyncio
from collections import deque
from collections.abc import Mapping
from functools import wraps
import json
import logging
import os
import re
import sys
import time
from uuid import uuid4

logger = logging.getLogger(__name__)
_MAX_RECORDS = 32
_MAX_RESPONSE_IDS = 128
_MAX_ERRORS = 255
_RESPONSE_ID = re.compile(r"(?:resp|response)[_-][A-Za-z0-9_-]{1,120}\Z")
_TOOLS = frozenset(("search_minutes", "search_web"))
_STATUSES = frozenset(("completed", "cancelled", "failed", "incomplete"))
_REASONS = frozenset((
    "response_done", "superseded", "manual_interrupt", "session_closed",
    "session_cancelled", "connection_error", "provider_error", "handler_error",
    "text_send_error", "tool_error", "tool_cancelled", "argument_mismatch",
    "response_create_error",
))
_METHODS = frozenset((
    "begin", "observe", "function_announced", "tool_mark", "response_create_start",
    "response_create_end", "finish", "session_config", "catalogue_chars",
    "pending_overflow", "response_id_overflow", "error_reporting",
))
_CORRELATION_METHODS = frozenset((
    "begin", "observe", "response_create_start", "response_create_end",
    "pending_overflow", "response_id_overflow",
))
_EXCEPTION_CLASSES = (
    AttributeError, TypeError, ValueError, OverflowError, LookupError,
    RuntimeError, OSError, ArithmeticError, AssertionError, Exception,
)
_USAGE_PATHS = {
    "total_tokens": ("total_tokens",),
    "input_tokens": ("input_tokens",),
    "output_tokens": ("output_tokens",),
    "cached_input_tokens": ("input_token_details", "cached_tokens"),
    "input_text_tokens": ("input_token_details", "text_tokens"),
    "input_audio_tokens": ("input_token_details", "audio_tokens"),
    "input_image_tokens": ("input_token_details", "image_tokens"),
    "output_text_tokens": ("output_token_details", "text_tokens"),
    "output_audio_tokens": ("output_token_details", "audio_tokens"),
    "output_reasoning_tokens": ("output_token_details", "reasoning_tokens"),
    "cached_input_text_tokens": ("input_token_details", "cached_tokens_details", "text_tokens"),
    "cached_input_audio_tokens": ("input_token_details", "cached_tokens_details", "audio_tokens"),
    "cached_input_image_tokens": ("input_token_details", "cached_tokens_details", "image_tokens"),
}


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _wire(value):
    return getattr(value, "value", value)


def _response_id(value):
    return value if isinstance(value, str) and _RESPONSE_ID.fullmatch(value) else None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _diagnostic_label(method, error):
    label = method if method in _METHODS else "error_reporting"
    # A custom exception's class name can itself contain arbitrary strings.
    # Report only its nearest allowlisted built-in category.
    category = next(cls.__name__ for cls in _EXCEPTION_CLASSES if isinstance(error, cls))
    return label, category


def _diagnostic(label, category, *, fallback=False):
    line = f"[LATENCY_TRACE_ERROR] method={label} exception_class={category}"
    if not fallback:
        try:
            logger.warning("%s", line)
            return
        except Exception:
            pass
    try:
        sys.stderr.write(line + "\n")
    except Exception:
        try:
            os.write(2, (line + "\n").encode("ascii"))
        except Exception:
            # No working diagnostic transport remains. The in-memory invalid
            # flag and saturated counter still survive; inference must continue.
            pass


def _safe(method):
    @wraps(method)
    def capture(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception as error:
            trace = args[0]
            candidate = args[1] if len(args) > 1 else kwargs.get("turn", kwargs.get("token"))
            if type(candidate) is tuple and candidate:
                candidate = candidate[0]
            elif type(candidate) is dict:
                candidate = candidate.get("turn")
            owner = candidate if isinstance(candidate, _Turn) else trace.current
            try:
                trace._instrumentation_error(method.__name__, error, owner)
            except Exception as reporting_error:
                _diagnostic(*_diagnostic_label("error_reporting", reporting_error), fallback=True)
            return None
    return capture


def _usage(response):
    usage = _get(response, "usage")
    result = {}
    for field, path in _USAGE_PATHS.items():
        value = usage
        for key in path:
            value = _get(value, key)
        result[field] = _count(value)
    return result


class _Turn:
    def __init__(self, origin, now):
        self.id = uuid4().hex
        self.origin = origin
        self.started = now
        self.closed = False
        self.valid = True
        self.instrumentation_errors = 0
        self.responses = []
        self.tools = []
        self.last_tool = None
        self.sends = []
        self.inflight_sends = 0
        self.pending_finish = None
        self.truncated = False
        self.first_any = {"audio_transcript_ms": None, "pcm_ms": None, "text_ms": None}
        self.terminal = None

    def append(self, collection, value):
        if len(collection) < _MAX_RECORDS:
            collection.append(value)
        else:
            self.truncated = True


class LatencyTrace:
    """One instance per handler; instantiate only when ENABLE_LATENCY_TRACE."""

    def __init__(self, model_binding, clock=time.monotonic):
        self.model_binding = model_binding
        self.clock = clock
        self.current = None
        self.instrumentation_errors = 0
        self.correlation_valid = True
        self._session_metadata_valid = True
        self._warned_errors = set()
        self.session_char_counts = {
            "instructions_chars": None, "catalogue_chars": None,
            "tool_schema_compact_json_chars": None,
        }
        self._responses = {}
        self._latest = None
        # A cancelled send can still yield response.created. Keep its place so
        # that late events cannot become the next user's response.
        self._pending = deque()

    def _instrumentation_error(self, method, error, owner=None):
        self.instrumentation_errors = min(self.instrumentation_errors + 1, _MAX_ERRORS)
        affected = {turn for turn in (owner, self.current) if turn is not None}
        if method in _CORRELATION_METHODS:
            self.correlation_valid = False
            affected.update(token["turn"] for token in self._pending if token["turn"] is not None)
            affected.update(pair[0] for pair in self._responses.values() if pair[0] is not None)
            self._pending.clear()
            self._responses.clear()
            self._latest = None
        if method in ("session_config", "catalogue_chars"):
            self._session_metadata_valid = False
        for turn in affected:
            turn.valid = False
            turn.instrumentation_errors = min(turn.instrumentation_errors + 1, _MAX_ERRORS)
        signature = _diagnostic_label(method, error)
        if method == "finish" or signature not in self._warned_errors:
            self._warned_errors.add(signature)
            _diagnostic(*signature)

    @_safe
    def session_config(self, session):
        if not self.model_binding:
            return
        instructions = _get(session, "instructions")
        self.session_char_counts["instructions_chars"] = (
            len(instructions) if isinstance(instructions, str) else None
        )
        tools = _get(session, "tools")
        if tools is not None:
            definitions = [tool.as_dict() if hasattr(tool, "as_dict") else tool for tool in tools]
            self.session_char_counts["tool_schema_compact_json_chars"] = len(
                json.dumps(definitions, ensure_ascii=False, separators=(",", ":"))
            )

    @_safe
    def catalogue_chars(self, count):
        self.session_char_counts["catalogue_chars"] = _count(count)

    def _ms(self, turn):
        return round((self.clock() - turn.started) * 1000, 3)

    @_safe
    def begin(self, origin):
        if origin not in ("text_submission_received", "audio_speech_stopped_received"):
            raise ValueError("Unsupported trace origin")
        self.finish(self.current, "cancelled", "superseded")
        if not self.correlation_valid:
            return None
        self.current = _Turn(origin, self.clock())
        self.current.valid = self._session_metadata_valid
        self._latest = None
        return self.current

    def _find(self, rid=None):
        if not self.correlation_valid:
            return None, None
        pair = self._responses.get(rid) if rid is not None else self._latest
        if pair and pair[0] is not None and not pair[0].closed:
            return pair
        return None, None

    @_safe
    def observe(self, event):
        kind = _wire(_get(event, "type"))
        if kind == "input_audio_buffer.speech_stopped":
            self.begin("audio_speech_stopped_received")
            return
        if kind == "error":
            if _get(_get(event, "error"), "code") != "response_cancel_not_active":
                self.finish(self.current, "error", "provider_error")
            return
        if not self.correlation_valid:
            return
        if kind == "response.created":
            rid = _response_id(_get(_get(event, "response"), "id"))
            if rid is not None and rid in self._responses:
                return
            if rid is not None and len(self._responses) >= _MAX_RESPONSE_IDS:
                self._instrumentation_error("response_id_overflow", OverflowError())
                return
            pending = self._pending.popleft() if self._pending else None
            turn = pending["turn"] if pending is not None else self.current
            if turn is None or turn.closed:
                pair = (None, None)
            else:
                response = {
                    "id": rid, "created_ms": self._ms(turn), "done_ms": None,
                    "status": None, "tool_response": False, "function_announced_ms": None,
                    "function_arguments_ready_ms": None,
                    "first_audio_transcript_ms": None, "first_pcm_ms": None,
                    "first_text_ms": None, "first_pcm_bytes": None,
                    "usage": _usage(None),
                }
                turn.append(turn.responses, response)
                pair = (turn, response)
            self._latest = pair
            if rid is not None:
                self._responses[rid] = pair
            return

        raw_rid = _get(_get(event, "response"), "id") if kind == "response.done" else _get(event, "response_id")
        # Do not fall back to the active response for an explicit unknown ID.
        turn, response = self._find(raw_rid)
        if turn is None:
            return
        if kind in ("response.audio.delta", "response.audio_transcript.delta", "response.text.delta"):
            if response["done_ms"] is not None:
                return
            delta = _get(event, "delta")
            if not delta:
                return
            field, any_field = {
                "response.audio.delta": ("first_pcm_ms", "pcm_ms"),
                "response.audio_transcript.delta": ("first_audio_transcript_ms", "audio_transcript_ms"),
                "response.text.delta": ("first_text_ms", "text_ms"),
            }[kind]
            if kind == "response.audio.delta" and not isinstance(delta, (bytes, bytearray, memoryview)):
                return
            if response[field] is None:
                response[field] = self._ms(turn)
                if any_field == "pcm_ms":
                    response["first_pcm_bytes"] = delta.nbytes if isinstance(delta, memoryview) else len(delta)
                if turn.first_any[any_field] is None:
                    turn.first_any[any_field] = response[field]
        elif kind == "response.function_call_arguments.done" and self.model_binding:
            response["function_arguments_ready_ms"] = self._ms(turn)
        elif kind == "response.output_item.added" and self.model_binding:
            if _wire(_get(_get(event, "item"), "type")) == "function_call":
                response["tool_response"] = True
                if response["function_announced_ms"] is None:
                    response["function_announced_ms"] = self._ms(turn)
        elif kind == "response.done":
            if response["done_ms"] is not None:
                return
            provider_response = _get(event, "response")
            status = _wire(_get(provider_response, "status"))
            response["status"] = status if status in _STATUSES else "unknown"
            response["done_ms"] = self._ms(turn)
            response["usage"] = _usage(provider_response)
            if self.model_binding and any(
                _wire(_get(item, "type")) == "function_call"
                for item in (_get(provider_response, "output") or [])
            ):
                response["tool_response"] = True
            if status == "completed" and response["tool_response"]:
                return
            if status == "completed" and self.model_binding:
                turn.terminal = response
            self.finish(turn, response["status"], "response_done")

    @_safe
    def function_announced(self, name):
        if not self.model_binding:
            return None
        turn, response = self._find()
        if turn is None:
            return None
        response["tool_response"] = True
        if response["function_announced_ms"] is None:
            response["function_announced_ms"] = self._ms(turn)
        tool = {
            "name": name if name in _TOOLS else "other",
            "response_id": response["id"],
            "announced_ms": response["function_announced_ms"],
            "arguments_ready_ms": None, "argument_chars": None,
            "execution_start_ms": None, "execution_end_ms": None,
            "execution_status": None, "output_send_start_ms": None,
            "output_send_done_ms": None, "output_chars": None,
        }
        turn.append(turn.tools, tool)
        turn.last_tool = tool
        return turn, tool

    @_safe
    def tool_mark(self, token, phase, count=None, status=None):
        if token is None or token[0].closed:
            return
        turn, tool = token
        if phase not in (
            "arguments_ready_ms", "execution_start_ms", "execution_end_ms",
            "output_send_start_ms", "output_send_done_ms",
        ):
            raise ValueError("Unsupported trace phase")
        if tool[phase] is None:
            tool[phase] = self._ms(turn)
        if phase == "arguments_ready_ms":
            owner, response = self._find(tool["response_id"])
            if owner is turn and response["function_arguments_ready_ms"] is not None:
                tool[phase] = response["function_arguments_ready_ms"]
            tool["argument_chars"] = _count(count)
        elif phase == "execution_end_ms":
            tool["execution_status"] = status if status in ("completed", "error", "cancelled") else "unknown"
        elif phase == "output_send_start_ms":
            tool["output_chars"] = _count(count)

    @_safe
    def response_create_start(self, turn, reason):
        if not self.correlation_valid:
            return None
        if len(self._pending) >= _MAX_RESPONSE_IDS:
            self._instrumentation_error("pending_overflow", OverflowError(), turn)
            return None
        if turn is not None and turn.closed:
            turn = None
        data = None
        if turn is not None:
            data = {
                "reason": reason if reason in ("text", "tool") else "other",
                "start_ms": self._ms(turn), "done_ms": None,
            }
            turn.append(turn.sends, data)
            turn.inflight_sends += 1
        token = {"turn": turn, "data": data}
        self._pending.append(token)
        return token

    @_safe
    def response_create_end(self, token, failed=False, cancelled=False):
        if token is None:
            return
        turn, data = token["turn"], token["data"]
        if turn is not None and not turn.closed:
            turn.inflight_sends -= 1
        if failed:
            self._pending = deque(p for p in self._pending if p is not token)
            self.finish(
                turn, "cancelled" if cancelled else "error",
                "session_cancelled" if cancelled else "response_create_error",
            )
        elif turn is not None and not turn.closed and data is not None:
            data["done_ms"] = self._ms(turn)
            if turn.pending_finish is not None:
                self.finish(turn, *turn.pending_finish)

    @_safe
    def finish(self, turn, status, reason):
        if turn is None or turn.closed:
            return
        # A fast server can deliver response.done while the SDK send is still
        # awaited in another task. Let that send supply its actual end boundary.
        if reason == "response_done" and turn.inflight_sends:
            turn.pending_finish = (status, reason)
            return
        turn.closed = True
        if self.current is turn:
            self.current = None
        for rid, pair in self._responses.items():
            if pair[0] is turn:
                self._responses[rid] = (None, None)
        if self._latest and self._latest[0] is turn:
            self._latest = None
        # Keep only tombstones, not entire closed records, for outstanding sends.
        for token in self._pending:
            if token["turn"] is turn:
                token["turn"] = token["data"] = None
        first = turn.tools[0] if turn.tools else {}
        last = turn.last_tool or {}
        terminal = turn.terminal or {}
        totals = {}
        valid = turn.valid and self.correlation_valid
        for field in _USAGE_PATHS:
            values = [response["usage"][field] for response in turn.responses]
            totals[field] = (
                sum(values) if values and all(v is not None for v in values)
                and not turn.truncated and valid else None
            )
        summary = {
            "event": "latency_trace", "version": 1, "turn_id": turn.id,
            "binding": "model" if self.model_binding else "agent",
            "origin": turn.origin, "status": status if status in _STATUSES | {"error"} else "unknown",
            "reason": reason if reason in _REASONS else "handler_error",
            "elapsed_ms": self._ms(turn), "metadata_truncated": turn.truncated,
            "trace_valid": valid, "correlation_valid": self.correlation_valid,
            "instrumentation_errors": turn.instrumentation_errors,
            "instrumentation_errors_session": self.instrumentation_errors,
            "session_char_counts": dict(self.session_char_counts),
            "milestones_ms": {
                "t0": 0.0, "t1": first.get("execution_start_ms") if valid else None,
                "t2": last.get("execution_end_ms") if valid else None,
                "t3": last.get("output_send_done_ms") if valid else None,
                "t4": terminal.get("first_audio_transcript_ms") if valid else None,
                "t5": terminal.get("first_pcm_ms") if valid else None,
            },
            "first_any_ms": turn.first_any,
            "terminal_response_id": terminal.get("id"),
            "terminal_boundary": (
                "completed_non_function_response" if self.model_binding else "unavailable_managed"
            ),
            "terminal_pcm": (
                "invalid" if not valid else
                "observed" if terminal.get("first_pcm_ms") is not None else "unobserved"
            ),
            "tool_phases": "in_process_model" if self.model_binding else "unavailable_managed",
            "usage_totals": totals, "responses": turn.responses,
            "tools": turn.tools, "response_create_sends": turn.sends,
        }
        logger.info("%s", json.dumps(summary, separators=(",", ":"), allow_nan=False))


async def create_response(handler, connection, *, turn=None, reason="other", **kwargs):
    """Observe the existing awaited SDK send, without pacing or payload changes."""
    trace = getattr(handler, "_latency_trace", None)
    token = trace.response_create_start(turn, reason) if trace is not None else None
    try:
        result = await connection.response.create(**kwargs)
    except asyncio.CancelledError:
        if trace is not None:
            trace.response_create_end(token, failed=True, cancelled=True)
        raise
    except BaseException:
        if trace is not None:
            trace.response_create_end(token, failed=True)
        raise
    if trace is not None:
        trace.response_create_end(token)
    return result
