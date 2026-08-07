"""Event handlers for the Voice Live API session.

Module-level functions that take the VoiceSessionHandler instance as their first
argument. Kept separate from handler.py to keep the class focused on session
lifecycle and I/O.
"""

import asyncio
import base64
import json
import logging
import time

from azure.ai.voicelive.models import (
    FunctionCallOutputItem,
    ItemType,
    ServerEventType,
)

from .. import audit
from ..logsafe import fingerprint, keys_only
from .functions import execute_function

logger = logging.getLogger(__name__)


def _now_ms() -> float:
    return time.monotonic() * 1000.0


def _log_first_text_delta(handler, kind: str) -> None:
    """Log the agent thinking + tool-call time (response_created -> first token).

    This is a useful proxy for "how long did the Foundry agent take to call
    tools (AI Search / Web Search) and produce its first text token", separate
    from the TTS warm-up time that dominates `user_done -> first_audio`. If
    this number is large, the bottleneck is the agent / tools; if it's small
    but `first_audio` is still large, the bottleneck is TTS.
    """
    if getattr(handler, "_first_text_logged", False):
        return
    handler._first_text_logged = True
    t_resp = getattr(handler, "_t_response_created_ms", None)
    t_user = getattr(handler, "_t_user_done_ms", None)
    if t_resp is None:
        return
    now = _now_ms()
    msg = f"[LATENCY] first {kind} delta: response_created->first_token={now - t_resp:.0f}ms"
    if t_user is not None:
        msg += f", user_done->first_token={now - t_user:.0f}ms"
    logger.info(msg)


# Which cue a tool corresponds to. Matched on substrings because the same
# retrieval shows up under different names depending on binding: as our own
# function names in model binding (search_minutes / search_web), and as managed
# item types in agent binding (file_search_call, bing_custom_search_call, ...).
# Order matters — "web_search" also contains "search", so web is tested first.
_WEB_HINTS = ("web_search", "websearch", "bing", "search_web", "browser")
_RECORDS_HINTS = (
    "file_search", "filesearch", "ai_search", "aisearch", "azure_ai_search",
    "search_minutes", "vector_store", "knowledge",
)


def _retrieval_cue_name(raw) -> str | None:
    """Map a tool/item name to the cue the browser should show, or None."""
    if not raw:
        return None
    # SDK enums stringify as "ItemType.FILE_SEARCH_CALL"; .value gives the wire
    # name. Take the value when present, and lowercase either way.
    s = str(getattr(raw, "value", raw)).lower()
    if any(h in s for h in _WEB_HINTS):
        return "search_web"
    if any(h in s for h in _RECORDS_HINTS):
        return "search_minutes"
    # Anything else that is clearly a lookup still beats a bare "working on it".
    if "search" in s or "retriev" in s:
        return "search_minutes"
    return None


async def _send_retrieval_cue(handler, raw) -> None:
    """Tell the browser a retrieval started, so the cue can name the wait.

    Deduped per response: the same search surfaces through several events
    (output_item.added, *.in_progress, *.searching) and re-sending would restart
    the "taking longer" escalation each time.
    """
    name = _retrieval_cue_name(raw)
    if not name or getattr(handler, "_retrieval_cue_sent", None) == name:
        return
    handler._retrieval_cue_sent = name
    # The truth has arrived, so retire the guess. In model binding a tool turn
    # produces TWO responses — the tool call, then the answer — and a surviving
    # prediction would re-announce a search on the second one, after it finished.
    handler._expected_tool = None
    logger.info(f"[TOOL] retrieval cue -> {name} [{fingerprint(raw)}]")
    await handler.send_message({
        "type": "function_call_started",
        "functionName": name,
    })


# ── Predicting the retrieval cue when the tool is invisible ──────────────────
#
# In model binding the tools are ours, so a real function call tells us exactly
# what is running. In agent binding — the shipped default — the Foundry agent
# runs AI Search / Web Search inside its own thread and Voice Live relays none
# of it: no function call, no output item, no *.in_progress event. Verified
# against a live session; the only observable is how long the turn takes.
#
# So for that binding the cue is predicted from the user's own question, and the
# browser only promotes the prediction to a caption if the answer is STILL
# pending well past the point a no-retrieval turn would have finished. A
# chit-chat turn answers before the promotion fires, so it never gets a
# retrieval claim — which is the bug this whole cue exists to avoid.
#
# Anything unrecognised stays None: dots, no claim. Silence is the safe default.
_RECORDS_MARKERS = (
    "meeting", "minutes", "discuss", "agenda", "action item", "decision",
    "attendee", "board", "committee", "resolution", "quorum", "minuted",
    "last time", "record", "transcript", "who said", "was raised", "agreed",
)
_WEB_MARKERS = (
    "today", "latest", "current", "news", "share price", "stock", "market",
    "weather", "right now", "this week", "headline", "exchange rate",
    "who won", "recently announced", "price of", "at the moment",
)
# Short utterances rarely repeat their subject ("I mean February 2026."), so a
# marker-less follow-up inherits the previous turn's prediction rather than
# dropping to dots.
_FOLLOW_UP_MAX_WORDS = 7


def _classify_question(text: str, previous: str | None = None) -> str | None:
    """Guess which retrieval a question will trigger, or None if unclear."""
    s = (text or "").lower()
    if not s.strip():
        return None
    # Records first: a question can mention both ("the share price we discussed
    # last meeting") and the meeting corpus is the more specific claim.
    if any(m in s for m in _RECORDS_MARKERS):
        return "search_minutes"
    if any(m in s for m in _WEB_MARKERS):
        return "search_web"
    if previous and len(s.split()) <= _FOLLOW_UP_MAX_WORDS:
        return previous
    return None


async def handle_event(handler, event, connection):
    """Handle individual events from Voice Live API."""
    try:
        event_type = event.type

        # Diagnostic probe. Which tool events a session actually receives depends
        # on the binding: model binding raises our own function calls, while in
        # agent binding the Foundry agent runs its tools server-side and only
        # some of that is mirrored back. These types are rare, so logging every
        # one costs nothing and makes "why is there no retrieval cue?" a
        # one-log-line question instead of a guessing game.
        _et = str(getattr(event_type, "value", event_type)).lower()
        if "search" in _et or "tool" in _et or "mcp" in _et:
            logger.info(f"[TOOL] event: {_et}")

        # Audio delta - relay to browser as raw binary frame when supported.
        # Falls back to base64-in-JSON for older clients (no send_binary callback).
        if event_type == ServerEventType.RESPONSE_AUDIO_DELTA:
            if hasattr(event, "delta") and event.delta:
                # Latency milestone: first TTS audio chunk for this response.
                if not getattr(handler, "_first_audio_logged", False):
                    handler._first_audio_logged = True
                    t_user = getattr(handler, "_t_user_done_ms", None)
                    t_resp = getattr(handler, "_t_response_created_ms", None)
                    now = _now_ms()
                    if t_user is not None:
                        logger.info(
                            f"[LATENCY] first audio: user_done->audio={now - t_user:.0f}ms"
                            + (f", response_created->audio={now - t_resp:.0f}ms" if t_resp else "")
                        )
                if getattr(handler, "send_binary", None):
                    await handler.send_binary(event.delta)
                else:
                    audio_b64 = base64.b64encode(event.delta).decode("utf-8")
                    await handler.send_message({
                        "type": "audio_data",
                        "data": audio_b64,
                        "format": "pcm16",
                        "sampleRate": 24000,
                    })

        elif event_type == ServerEventType.RESPONSE_AUDIO_DONE:
            await handler.send_message({"type": "audio_done"})

        # Audio transcript (assistant speaking text)
        elif event_type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA:
            if hasattr(event, "delta") and event.delta:
                _log_first_text_delta(handler, "audio_transcript")
                await handler.send_message({
                    "type": "transcript_delta",
                    "role": "assistant",
                    "delta": event.delta,
                })

        elif event_type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            transcript = getattr(event, "transcript", "")
            # The *_DONE event carries the complete transcript, so audit never
            # has to accumulate deltas and the streaming path stays untouched.
            audit.record_assistant_text(handler, transcript)
            await handler.send_message({
                "type": "transcript_done",
                "role": "assistant",
                "transcript": transcript,
            })

        # Text delta (for text responses)
        elif event_type == ServerEventType.RESPONSE_TEXT_DELTA:
            if hasattr(event, "delta") and event.delta:
                _log_first_text_delta(handler, "text")
                await handler.send_message({
                    "type": "text_delta",
                    "delta": event.delta,
                })

        elif event_type == ServerEventType.RESPONSE_TEXT_DONE:
            text = getattr(event, "text", "")
            audit.record_assistant_text(handler, text)
            await handler.send_message({
                "type": "text_done",
                "text": text,
            })

        # Response lifecycle
        elif event_type == ServerEventType.RESPONSE_CREATED:
            handler._response_active = True
            handler._t_response_created_ms = _now_ms()
            handler._first_audio_logged = False
            handler._first_video_logged = False
            handler._first_text_logged = False
            # New turn: allow the retrieval cue to fire again.
            handler._retrieval_cue_sent = None
            t_user = getattr(handler, "_t_user_done_ms", None)
            if t_user is not None:
                logger.info(
                    f"[LATENCY] user_done->response_created={handler._t_response_created_ms - t_user:.0f}ms"
                )
            response_id = getattr(event, "response", None)
            rid = response_id.id if response_id and hasattr(response_id, "id") else ""
            # Open the audit record for this turn, and capture the Foundry
            # conversation id while it is available. In agent binding this is
            # the ONLY moment it is offered: there is no way to enumerate
            # conversations afterwards, so a turn whose id is missed here can
            # never have its tool calls recovered. Costs two attribute reads.
            audit.start_turn(handler)
            cid = getattr(response_id, "conversation_id", None) if response_id else None
            audit.set_conversation(handler, cid, rid)
            expected = getattr(handler, "_expected_tool", None)
            if expected:
                logger.info(f"[TOOL] predicted retrieval for this turn: {expected}")
            await handler.send_message({
                "type": "response_created",
                "responseId": rid,
                # Prediction only. The browser must not present it as fact until
                # the turn has run long enough that a search is the only
                # explanation. See _classify_question.
                "expectedTool": expected,
            })

        elif event_type == ServerEventType.RESPONSE_DONE:
            handler._response_active = False
            # Surface WHY a response ended — critical for diagnosing empty/cut-off
            # turns (the "awkward silence"). The realtime response carries a
            # status ("completed"/"cancelled"/"failed"/"incomplete") and, when it
            # is not "completed", a status_details object with the reason. We also
            # enumerate the output item types so we can tell a normal internal
            # tool-call turn (output contains a function_call, no audio) apart
            # from a barge-in cancellation (status=cancelled, empty output) or an
            # agent error (status=failed).
            resp = getattr(event, "response", None)
            status = getattr(resp, "status", None) if resp else None
            details = getattr(resp, "status_details", None) if resp else None
            rid = getattr(resp, "id", "") if resp else ""

            output = getattr(resp, "output", None) if resp else None
            out_types = []
            if output:
                for it in output:
                    t = getattr(it, "type", None)
                    out_types.append(str(t) if t is not None else "?")

            produced_audio = getattr(handler, "_first_audio_logged", False)
            produced_text = getattr(handler, "_first_text_logged", False)
            empty_turn = not produced_audio and not produced_text

            # Pull a human-readable reason out of status_details, which may be a
            # model object or a dict depending on the event.
            reason = getattr(details, "reason", None)
            err = getattr(details, "error", None)
            if reason is None and isinstance(details, dict):
                reason = details.get("reason")
                err = details.get("error")

            if status and status != "completed":
                logger.warning(
                    f"[RESPONSE_DONE] non-completed status='{status}' "
                    f"reason='{reason}' error='{err}' empty_turn={empty_turn} "
                    f"output={out_types} id={rid}"
                )
            elif empty_turn:
                logger.warning(
                    f"[RESPONSE_DONE] completed but EMPTY (no audio, no text) — "
                    f"output={out_types} status='{status}' id={rid}"
                )

            await handler.send_message({"type": "response_done"})
            # Hand the completed turn to the background writer. This is the only
            # audit call that does real work, and all of it happens after the
            # queue: submit() is put_nowait and returns immediately.
            audit.finish_turn(
                handler, status=str(status) if status else None, output_types=out_types
            )

        # Speech detection
        elif event_type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            item_id = getattr(event, "item_id", "") or getattr(event, "itemId", "")
            logger.info(f"User speech STARTED (item={item_id})")
            await handler.send_message({
                "type": "speech_started",
                "itemId": item_id,
            })

        elif event_type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            logger.info("User speech STOPPED")
            await handler.send_message({
                "type": "speech_stopped",
            })

        # User transcription
        elif event_type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            handler._t_user_done_ms = _now_ms()
            transcript = getattr(event, "transcript", "") or ""
            item_id = getattr(event, "item_id", "") or getattr(event, "itemId", "")
            if transcript.strip():
                logger.info(f"User transcript (item={item_id}): [{fingerprint(transcript)}]")
                audit.record_user_text(handler, transcript, item_id)
                # _last_topic is sticky so a short follow-up can inherit it;
                # _expected_tool is this turn's prediction and gets retired as
                # soon as a real tool event supersedes it.
                handler._last_topic = _classify_question(
                    transcript, getattr(handler, "_last_topic", None)
                )
                handler._expected_tool = handler._last_topic
                await handler.send_message({
                    "type": "transcript_done",
                    "role": "user",
                    "transcript": transcript,
                    "itemId": item_id,
                })
            else:
                # Segment produced no recognized words (silence / noise / clipped
                # audio). The server will NOT generate a response, so tell the
                # browser to drop the dangling "..." placeholder instead of
                # leaving it hanging as if the avatar went silent.
                logger.warning(
                    f"User transcript EMPTY (item={item_id}) — no recognized "
                    f"speech; no response will be generated"
                )
                await handler.send_message({
                    "type": "transcript_empty",
                    "role": "user",
                    "itemId": item_id,
                })

        # Avatar WebRTC signaling
        elif event_type == ServerEventType.SESSION_AVATAR_CONNECTING:
            server_sdp = getattr(event, "server_sdp", "")
            if server_sdp:
                await handler.send_message({
                    "type": "avatar_sdp_answer",
                    "serverSdp": server_sdp,
                })
                logger.info("Relayed avatar SDP answer to browser")

                # Avatar connection succeeded — now send proactive greeting if pending
                if getattr(handler, "_pending_proactive", False):
                    handler._pending_proactive = False
                    try:
                        logger.info("[SEND] response.create (proactive greeting, after avatar connect)")
                        from .handler import PROACTIVE_GREETING_INSTRUCTIONS
                        await connection.response.create(
                            additional_instructions=PROACTIVE_GREETING_INSTRUCTIONS
                        )
                        logger.info("Proactive greeting sent after avatar connect")
                    except Exception as e:
                        logger.error(f"Failed to send proactive greeting: {e}")

        # Function calls
        elif event_type == ServerEventType.CONVERSATION_ITEM_CREATED:
            await handle_conversation_item(handler, event, connection)

        # Managed Foundry tools (agent binding). The agent runs AI Search / Web
        # Search server-side, so there is no custom function call to hook. What
        # does come back is the output item being opened, plus (sometimes) the
        # dedicated in-progress events. Handle all of them and let
        # _send_retrieval_cue dedupe, because which ones arrive varies by tool.
        elif event_type == ServerEventType.RESPONSE_OUTPUT_ITEM_ADDED:
            item = getattr(event, "item", None)
            item_type = getattr(item, "type", None) if item is not None else None
            item_name = getattr(item, "name", None) if item is not None else None
            _it = str(getattr(item_type, "value", item_type)).lower()
            if item_type is not None and _it != "message":
                logger.info(f"[TOOL] output item added: type={_it} name={item_name}")
            await _send_retrieval_cue(handler, item_name or item_type)

        elif event_type in (
            ServerEventType.RESPONSE_FILE_SEARCH_CALL_IN_PROGRESS,
            ServerEventType.RESPONSE_FILE_SEARCH_CALL_SEARCHING,
        ):
            await _send_retrieval_cue(handler, "file_search")

        elif event_type in (
            ServerEventType.RESPONSE_WEB_SEARCH_CALL_IN_PROGRESS,
            ServerEventType.RESPONSE_WEB_SEARCH_CALL_SEARCHING,
        ):
            await _send_retrieval_cue(handler, "web_search")

        elif event_type == ServerEventType.RESPONSE_MCP_CALL_IN_PROGRESS:
            await _send_retrieval_cue(handler, getattr(event, "name", None) or "search")

        # Errors
        elif event_type == ServerEventType.ERROR:
            error = getattr(event, "error", None)
            error_code = (
                error.get("code")
                if isinstance(error, dict)
                else getattr(error, "code", None)
            )
            if error_code == "response_cancel_not_active":
                logger.debug("Voice Live response was already stopped before cancellation")
                return
            error_msg = str(event)
            logger.error(f"Voice Live error: {error_msg}")
            await handler.send_message({
                "type": "error",
                "error": error_msg,
            })

        # Session updated (may contain additional info)
        elif event_type == ServerEventType.SESSION_UPDATED:
            logger.debug("[SESSION_UPDATED] received")

        # Avatar video via WebSocket mode (response.video.delta)
        # SDK parses this as a generic ServerEvent with string type
        elif event_type == "response.video.delta":
            delta = event.get("delta", "")
            if delta:
                handler._video_sent_count = getattr(handler, '_video_sent_count', 0) + 1
                if handler._video_sent_count == 1:
                    logger.info("[SEND] first video_data forwarded to browser")
                if not getattr(handler, "_first_video_logged", False):
                    handler._first_video_logged = True
                    t_user = getattr(handler, "_t_user_done_ms", None)
                    if t_user is not None:
                        logger.info(
                            f"[LATENCY] first avatar video: user_done->video={_now_ms() - t_user:.0f}ms"
                        )
                await handler.send_message({
                    "type": "video_data",
                    "delta": delta,
                })

    except Exception as e:
        logger.error(f"Error handling event {getattr(event, 'type', 'unknown')}: {e}")

async def handle_conversation_item(handler, event, connection):
    """Handle function call events."""
    if not hasattr(event, "item"):
        return

    item = event.item
    if not (hasattr(item, "type") and item.type == ItemType.FUNCTION_CALL and hasattr(item, "call_id")):
        return

    function_name = item.name
    call_id = item.call_id
    previous_item_id = item.id

    logger.info(f"Function call: {function_name} (call_id: {call_id})")
    # Claim the cue so a managed event for the same retrieval can't re-send it
    # and restart the "taking longer" escalation, and retire the prediction now
    # that the real tool is known.
    handler._retrieval_cue_sent = _retrieval_cue_name(function_name)
    handler._expected_tool = None
    await handler.send_message({
        "type": "function_call_started",
        "functionName": function_name,
        "callId": call_id,
    })

    try:
        # Wait for arguments
        args_done = await handler._wait_for_event(
            connection, {ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE}
        )
        if args_done.call_id != call_id:
            logger.warning(f"Call ID mismatch: expected {call_id}, got {args_done.call_id}")
            return

        arguments = args_done.arguments
        logger.info(f"Function args: {keys_only(arguments)}")

        # Kick off function execution immediately, in parallel with waiting
        # for RESPONSE_DONE. The realtime API requires the prior response to
        # finish before we can create the follow-up response, but there's no
        # reason to keep the tool idle until then.
        _tool_started_ms = _now_ms()
        exec_task = asyncio.create_task(execute_function(function_name, arguments))

        await handler._wait_for_event(connection, {ServerEventType.RESPONSE_DONE})

        result = await exec_task
        # Model binding gives perfect tool fidelity for free: the call happened
        # in our own process, so we hold the exact arguments and return value.
        # Enqueued by reference — the writer owns the cost of serialising it.
        audit.record_tool(
            handler,
            name=function_name,
            args=arguments,
            results=result,
            elapsed_ms=_now_ms() - _tool_started_ms,
            error=result.get("error") if isinstance(result, dict) else None,
        )

        await handler.send_message({
            "type": "function_call_result",
            "functionName": function_name,
            "callId": call_id,
            "result": result,
        })

        # Send result back
        function_output = FunctionCallOutputItem(
            call_id=call_id, output=json.dumps(result)
        )
        await connection.conversation.item.create(
            previous_item_id=previous_item_id, item=function_output
        )
        await connection.response.create()

    except Exception as e:
        logger.error(f"Error handling function call {function_name}: {e}")
        await handler.send_message({
            "type": "function_call_error",
            "functionName": function_name,
            "callId": call_id,
            "error": str(e),
        })
