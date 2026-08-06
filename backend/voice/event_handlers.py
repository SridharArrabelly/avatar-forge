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


async def handle_event(handler, event, connection):
    """Handle individual events from Voice Live API."""
    try:
        event_type = event.type

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
            t_user = getattr(handler, "_t_user_done_ms", None)
            if t_user is not None:
                logger.info(
                    f"[LATENCY] user_done->response_created={handler._t_response_created_ms - t_user:.0f}ms"
                )
            response_id = getattr(event, "response", None)
            rid = response_id.id if response_id and hasattr(response_id, "id") else ""
            await handler.send_message({
                "type": "response_created",
                "responseId": rid,
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
                logger.info(f"User transcript (item={item_id}): {transcript!r}")
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
        logger.info(f"Function args: {arguments}")

        # Kick off function execution immediately, in parallel with waiting
        # for RESPONSE_DONE. The realtime API requires the prior response to
        # finish before we can create the follow-up response, but there's no
        # reason to keep the tool idle until then.
        exec_task = asyncio.create_task(execute_function(function_name, arguments))

        await handler._wait_for_event(connection, {ServerEventType.RESPONSE_DONE})

        result = await exec_task

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

