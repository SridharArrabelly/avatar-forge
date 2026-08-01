"""Voice Live session handler.

Owns one Voice Live SDK connection per browser client. Bridges browser
WebSocket messages into SDK calls and relays SDK events back via send_message.
"""

import asyncio
import json
import logging
from typing import Any, Callable, Optional

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AudioInputTranscriptionOptions,
    ClientEventSessionAvatarConnect,
    InputAudioFormat,
    InputTextContentPart,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    SystemMessageItem,
    UserMessageItem,
)

from ..config import (
    AGENT_NAME,
    AGENT_PROJECT_NAME,
    DEVELOPER_MODE,
    MODEL_BINDING,
    PROACTIVE_GREETING,
    REALTIME_MAX_TOKENS,
    VOICELIVE_API_VERSION,
    VOICELIVE_MODEL,
)
from .builders import (
    build_avatar_config,
    build_interim_response,
    build_turn_detection,
    build_voice_config,
)
from .catalog import get_meeting_catalog
from .event_handlers import handle_event
from .instructions import load_realtime_instructions
from .tools import build_realtime_tools

logger = logging.getLogger(__name__)


# Override applied to the proactive opening response only.
#
# Why this is so directive: immediately after SESSION_UPDATED, the most
# recent thing in model context is the MEETINGS LIST system message the
# backend just injected, AND the agent system prompt explicitly tells the
# model "read MEETINGS LIST BEFORE thinking about tools". With both of
# those pulling toward "enumerate the meetings", a soft override like
# "say a short greeting, don't mention the catalogue" is not reliable:
# the model occasionally reads the list aloud as the opening utterance.
#
# Solution: pin the opening to a verbatim string. This removes all model
# freedom on the first turn (good for a deterministic, branded opener)
# without touching the agent's behaviour on any subsequent turn.
# Applied via response.create(additional_instructions=...).
# The greeting text itself is client-configurable via the PROACTIVE_GREETING
# environment variable so this backend stays generic and reusable.
PROACTIVE_GREETING_INSTRUCTIONS = (
    "This is the opening of the session and the user has not spoken yet. "
    "Output EXACTLY the following text and absolutely nothing else — no "
    "additions, no list of meetings, no date, no internal data: "
    f'"{PROACTIVE_GREETING}"'
)


def normalize_recognition_language(sr_model: str, recognition_language: str):
    """Return the Voice Live transcription ``language`` for the chosen SR model.

    Different speech-recognition models accept different locale formats, and
    sending a value the model doesn't understand causes recognition failures
    (empty transcripts / the avatar appearing to ignore the user):

    * ``"auto"`` / empty -> ``None`` (Voice Live multilingual auto-detect).
    * ``mai-transcribe*`` -> **ISO-639-1 primary subtag only** (e.g.
      ``"en-ZA"`` -> ``"en"``). The MAI Transcribe models accept bare language
      codes, not BCP-47 regional locales; ``"en-ZA"`` is not a valid value and
      is rejected/ignored.
    * ``azure-speech`` / cascaded models -> full BCP-47 locale passed through
      (e.g. ``"en-ZA"``, ``"en-GB"``), which those models support.
    """
    lang = (recognition_language or "").strip()
    if not lang or lang.lower() == "auto":
        return None
    model = (sr_model or "").strip().lower()
    if model.startswith("mai-transcribe"):
        return lang.split("-", 1)[0].lower()
    return lang


class VoiceSessionHandler:
    """Single Voice Live session bridged to one browser WebSocket client."""

    def __init__(
        self,
        client_id: str,
        endpoint: str,
        credential: Any,
        send_message: Callable,
        config: dict,
        send_binary: Optional[Callable] = None,
    ):
        self.client_id = client_id
        self.endpoint = endpoint
        self.credential = credential
        self.send_message = send_message
        self.send_binary = send_binary
        self.config = config

        # Which brain this session binds to. Normally the deployment-wide
        # VOICE_BINDING, but DEVELOPER_MODE lets a single client override it
        # per session so agent and model mode can be compared side by side in
        # two browser tabs against one deployment. It is per-connection and
        # therefore safe: each session opens its own Voice Live connection, so
        # there is no shared state for the two bindings to collide over.
        # Gated on DEVELOPER_MODE because the binding decides which brain and
        # which tools answer, which is not a choice an end user should make.
        self.model_binding = MODEL_BINDING
        if DEVELOPER_MODE:
            requested = str(config.get("voiceBinding") or "").strip().lower()
            if requested in ("agent", "model"):
                self.model_binding = requested == "model"
                if self.model_binding != MODEL_BINDING:
                    logger.info(
                        f"[{client_id}] DEVELOPER_MODE binding override: "
                        f"{'model' if self.model_binding else 'agent'} "
                        f"(deployment default is "
                        f"{'model' if MODEL_BINDING else 'agent'})"
                    )

        self.connection = None
        self.is_running = False
        self._event_task: Optional[asyncio.Task] = None
        self._pending_proactive = False
        self._stopping = False
        self._audio_chunk_count = 0
        self._video_chunk_count = 0

    async def start(self):
        """Start the Voice Live session."""
        try:
            self.is_running = True

            connect_kwargs = {
                "endpoint": self.endpoint,
                "credential": self.credential,
                "api_version": VOICELIVE_API_VERSION,
            }
            if self.model_binding:
                if not VOICELIVE_MODEL:
                    raise ValueError("VOICELIVE_MODEL must be set when VOICE_BINDING=model")
                connect_kwargs["model"] = VOICELIVE_MODEL
                logger.info(
                    f"Connecting to Voice Live with model: {VOICELIVE_MODEL} "
                    f"(api_version={VOICELIVE_API_VERSION})"
                )
            else:
                agent_name = AGENT_NAME
                project_name = AGENT_PROJECT_NAME
                if not agent_name or not project_name:
                    raise ValueError("AGENT_NAME and AGENT_PROJECT_NAME must be set in the environment")
                connect_kwargs["agent_config"] = {
                    "agent_name": agent_name,
                    "project_name": project_name,
                }
                logger.info(
                    f"Connecting to Voice Live with agent: {agent_name} "
                    f"(project={project_name}, api_version={VOICELIVE_API_VERSION})"
                )

            async with connect(**connect_kwargs) as connection:
                self.connection = connection

                # Configure session
                await self._setup_session(connection)

                # Process events
                await self._process_events(connection)

        except asyncio.CancelledError:
            logger.info(f"Session cancelled for client {self.client_id}")
        except Exception as e:
            logger.error(f"Voice session error for {self.client_id}: {e}")
            await self.send_message({
                "type": "session_error",
                "error": str(e),
            })
        finally:
            self.is_running = False
            self.connection = None

    async def _setup_session(self, connection):
        """Configure the Voice Live session with avatar, voice, and other settings."""
        config = self.config

        # Kick off meeting catalogue fetch in parallel with session setup. The
        # Voice Live handshake itself takes ~1-2s so this fetch (~50-100ms on
        # cache miss, ~0 on hit) is almost always free.
        catalog_task = asyncio.create_task(get_meeting_catalog())

        # Build voice configuration
        voice_config = build_voice_config(config)

        # Build avatar configuration
        avatar_config = build_avatar_config(config)

        # Build turn detection
        turn_detection = build_turn_detection(config, cascaded=not self.model_binding)

        # Build modalities (avatar is NOT a modality - it's configured via the avatar field)
        modalities = [Modality.TEXT, Modality.AUDIO]

        # Build SR options. The recognition language is normalized to the
        # format the chosen SR model accepts — mai-transcribe takes bare
        # ISO-639-1 codes (en), azure-speech takes full BCP-47 (en-ZA). See
        # normalize_recognition_language for why a raw "en-ZA" on mai-transcribe
        # causes empty transcripts.
        sr_model = config.get("srModel", "mai-transcribe-1")
        recognition_language = config.get("recognitionLanguage", "auto")
        normalized_language = normalize_recognition_language(
            sr_model, recognition_language
        )
        if (
            normalized_language is not None
            and normalized_language != recognition_language
        ):
            logger.info(
                f"Normalized recognition language {recognition_language!r} -> "
                f"{normalized_language!r} for SR model {sr_model!r}"
            )
        input_audio_transcription = AudioInputTranscriptionOptions(
            model=sr_model,
            language=normalized_language,
        )

        # Build noise/echo settings
        noise_reduction = None
        echo_cancellation = None
        if config.get("useNS", False):
            noise_reduction = {"type": "azure_deep_noise_suppression"}
        if config.get("useEC", False):
            echo_cancellation = {"type": "server_echo_cancellation"}

        # Ownership of instructions and tools flips with the binding. In agent
        # mode the Foundry agent owns both, and sending them here would be
        # ignored at best. In model mode there is no agent, so the session is
        # the only place they can live.
        session_config = RequestSession(
            modalities=modalities,
            voice=voice_config,
            avatar=avatar_config,
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            input_audio_transcription=input_audio_transcription,
            turn_detection=turn_detection,
            input_audio_noise_reduction=noise_reduction,
            input_audio_echo_cancellation=echo_cancellation,
            tool_choice="auto",
            **(
                {
                    "instructions": load_realtime_instructions(),
                    "tools": build_realtime_tools(),
                    # One tool per question. The prompt asks for this, but the
                    # model is free to ignore prose; this makes it structural.
                    # Two parallel retrievals would double the silence before
                    # she speaks for a question that only ever needed one.
                    "parallel_tool_calls": False,
                    # A realtime model left uncapped answers at length: measured
                    # 27-30s of speech against a prompt asking for two or three
                    # sentences. Nobody in a meeting wants a half-minute
                    # monologue, and it delays the next question. This is the
                    # backstop for when the prompt alone does not hold.
                    "max_response_output_tokens": REALTIME_MAX_TOKENS,
                    "interim_response": build_interim_response(
                        voice_config, model_binding=self.model_binding
                    ),
                }
                if self.model_binding
                else {}
            ),
        )

        logger.debug("[SEND] session.update")
        await connection.session.update(session=session_config)

        # Wait for SESSION_UPDATED
        session_updated = await self._wait_for_event(
            connection, {ServerEventType.SESSION_UPDATED}
        )
        if session_updated is None:
            raise ValueError("SESSION_UPDATED event not received")

        logger.info(f"Session configured for client {self.client_id}")

        # Inject the live meeting catalogue as a SYSTEM message so the
        # Foundry agent can answer first/last/count/listing questions
        # without searching, and can phrase precise content searches with
        # exact meeting dates. Wrapped in try/except so a fetch failure
        # never breaks the session — the agent prompt has a fallback path.
        try:
            catalog = await catalog_task
            if catalog:
                catalog_item = SystemMessageItem(
                    content=[InputTextContentPart(text=catalog)]
                )
                await connection.conversation.item.create(item=catalog_item)
                logger.info(
                    f"Injected meeting catalogue ({len(catalog)} chars) "
                    f"for client {self.client_id}"
                )
            else:
                logger.info(
                    f"No meeting catalogue available; agent will fall back "
                    f"to direct search for client {self.client_id}"
                )
        except Exception as e:
            logger.warning(
                f"Failed to inject meeting catalogue for {self.client_id}: {e}"
            )

        avatar_output_mode = config.get("avatarOutputMode", "webrtc")

        # If avatar is enabled with WebRTC mode, relay ICE servers info to browser
        if config.get("avatarEnabled", False) and avatar_output_mode == "webrtc":
            if hasattr(session_updated, "session") and session_updated.session:
                session_data = session_updated.session
                if hasattr(session_data, "avatar") and session_data.avatar:
                    avatar_data = session_data.avatar
                    if hasattr(avatar_data, "ice_servers") and avatar_data.ice_servers:
                        ice_servers = []
                        for server in avatar_data.ice_servers:
                            ice_server = {"urls": server.urls}
                            if server.username:
                                ice_server["username"] = server.username
                            if server.credential:
                                ice_server["credential"] = server.credential
                            ice_servers.append(ice_server)

                        await self.send_message({
                            "type": "ice_servers",
                            "iceServers": ice_servers,
                        })
                        logger.info(f"Sent ICE servers to client {self.client_id}")

        # Extract session ID if available
        session_id = None
        if hasattr(session_updated, "session") and session_updated.session:
            session_id = getattr(session_updated.session, "id", None)
        logger.info(f"Session ID: {session_id}")

        # Notify client session is ready
        await self.send_message({
            "type": "session_started",
            "status": "success",
            "sessionId": session_id,
            "config": {
                "agentName": AGENT_NAME,
                "agentProjectName": AGENT_PROJECT_NAME,
                "avatarEnabled": config.get("avatarEnabled", False),
                "avatarOutputMode": avatar_output_mode,
            },
        })

        # Proactive greeting logic depends on avatar mode:
        # - No avatar: send immediately
        # - Avatar + websocket: send immediately (no WebRTC handshake needed)
        # - Avatar + webrtc: defer until SESSION_AVATAR_CONNECTING event
        if not config.get("avatarEnabled", False):
            if config.get("enableProactive", True):
                try:
                    logger.info("[SEND] response.create (proactive greeting, no avatar)")
                    await connection.response.create(
                        additional_instructions=PROACTIVE_GREETING_INSTRUCTIONS
                    )
                    logger.info("Proactive greeting sent")
                except Exception as e:
                    logger.error(f"Failed to send proactive greeting: {e}")
        elif avatar_output_mode == "websocket":
            # WebSocket avatar mode: no WebRTC handshake, send greeting immediately
            if config.get("enableProactive", True):
                try:
                    logger.info("[SEND] response.create (proactive greeting, websocket avatar)")
                    await connection.response.create(
                        additional_instructions=PROACTIVE_GREETING_INSTRUCTIONS
                    )
                    logger.info("Proactive greeting sent (websocket avatar)")
                except Exception as e:
                    logger.error(f"Failed to send proactive greeting: {e}")
        else:
            # WebRTC avatar: defer proactive greeting until avatar connect
            self._pending_proactive = config.get("enableProactive", True)

    async def _process_events(self, connection):
        """Process incoming events from Voice Live API.
        
        Uses manual recv() loop instead of 'async for' so that individual
        event parsing/handling errors don't kill the entire event loop.
        """
        while self.is_running:
            try:
                event = await connection.recv()
            except (ConnectionError, OSError) as e:
                # Parsing error from SDK — log details and continue listening
                logger.warning(f"[RECV] Event parsing error (continuing): {type(e).__name__}: {e}")
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Connection closed or fatal error
                if self._stopping:
                    logger.info(f"Voice Live connection closed for {self.client_id}")
                else:
                    logger.error(f"Connection error in event loop: {e}")
                break

            try:
                etype = getattr(event, 'type', 'unknown')
                if etype not in (ServerEventType.RESPONSE_AUDIO_DELTA,
                                 ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA,
                                 "response.video.delta"):
                    logger.debug(f"[RECV] {etype}")
                if etype == "response.video.delta":
                    self._video_chunk_count = getattr(self, '_video_chunk_count', 0) + 1
                    if self._video_chunk_count == 1:
                        logger.info(f"[RECV] first response.video.delta received")
                await handle_event(self, event, connection)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error handling event {getattr(event, 'type', 'unknown')}: {e}", exc_info=True)
                # Continue processing — don't let one bad event kill the loop

    async def send_audio(self, audio_base64: str):
        """Send base64-encoded audio data from browser to Voice Live (legacy path)."""
        if not self.connection:
            self._audio_chunk_count += 1
            if self._audio_chunk_count == 1 or self._audio_chunk_count % 500 == 0:
                logger.warning(f"[AUDIO] No connection — dropping audio chunk #{self._audio_chunk_count} (connection lost)")
            return
        if not self.is_running:
            logger.warning(f"[AUDIO] Session not running — dropping audio chunk")
            return
        try:
            self._audio_chunk_count += 1
            await self.connection.input_audio_buffer.append(audio=audio_base64)
        except Exception as e:
            logger.error(f"Error sending audio: {e}")

    async def send_audio_bytes(self, audio_bytes: bytes):
        """Send raw PCM16 audio bytes from browser to Voice Live.

        Encodes to base64 here (the SDK's append() accepts base64) but avoids
        the JSON parse + base64 encode-on-client cost on the wire.
        """
        if not self.connection:
            self._audio_chunk_count += 1
            if self._audio_chunk_count == 1 or self._audio_chunk_count % 500 == 0:
                logger.warning(f"[AUDIO] No connection — dropping audio chunk #{self._audio_chunk_count} (connection lost)")
            return
        if not self.is_running:
            return
        try:
            import base64
            self._audio_chunk_count += 1
            audio_base64 = base64.b64encode(audio_bytes).decode("ascii")
            await self.connection.input_audio_buffer.append(audio=audio_base64)
        except Exception as e:
            logger.error(f"Error sending audio bytes: {e}")

    async def send_text_message(self, text: str):
        """Send a text message to the conversation."""
        if self.connection:
            try:
                item = UserMessageItem(
                    content=[InputTextContentPart(text=text)]
                )
                await self.connection.conversation.item.create(item=item)
                await self.connection.response.create()
            except Exception as e:
                logger.error(f"Error sending text: {e}")

    async def send_avatar_sdp_offer(self, client_sdp: str):
        """Forward the browser's SDP offer to Voice Live for avatar WebRTC."""
        if self.connection:
            try:
                # Log diagnostic info about the SDP format
                sdp_preview = client_sdp[:60] if client_sdp else '(empty)'
                logger.debug(f"[SDP-CHECK] client_sdp len={len(client_sdp)} starts={sdp_preview}")

                avatar_connect = ClientEventSessionAvatarConnect(
                    client_sdp=client_sdp,
                )
                serialized = avatar_connect.as_dict() if hasattr(avatar_connect, 'as_dict') else str(avatar_connect)
                logger.debug("[SEND] session.avatar.connect")
                await self.connection.send(avatar_connect)
                logger.info("Sent avatar SDP offer to Voice Live")
            except Exception as e:
                logger.error(f"Error sending avatar SDP offer: {e}")

    async def interrupt(self):
        """Interrupt the current response — mirror what server-VAD barge-in does.

        Two steps, each best-effort:
          1. response.cancel() stops in-flight generation. On its own this is a
             no-op once a turn has finished generating but the avatar is still
             rendering buffered speech (long answers play for seconds after
             response.done), which is why a manual stop "did nothing".
          2. output_audio_buffer.clear() truncates the server-side audio output
             immediately — this is what actually stops the WebRTC avatar
             mid-sentence, matching auto_truncate barge-in behaviour.
        """
        if not self.connection:
            return
        try:
            await self.connection.response.cancel()
        except Exception as e:
            logger.debug(f"response.cancel during interrupt (often no active response): {e}")
        # output_audio_buffer.clear() is ONLY valid in avatar mode; calling it on a
        # non-avatar session (e.g. the Teams meeting bot) returns an
        # ``avatar_not_configured`` error on every barge-in/suppression. response.cancel()
        # above plus the stop_playback below already stop playback there.
        if self.config.get("avatarEnabled", False):
            try:
                await self.connection.output_audio_buffer.clear()
            except Exception as e:
                logger.warning(f"output_audio_buffer.clear during interrupt failed: {e}")
        try:
            await self.send_message({
                "type": "stop_playback",
                "reason": "manual_interrupt",
            })
        except Exception as e:
            logger.error(f"Error notifying client of interrupt: {e}")

    async def update_avatar_scene(self, avatar_data: dict):
        """Send a raw session.update with avatar scene config.
        
        Bypasses SDK serialization completely by writing raw JSON directly
        to the underlying websocket, matching the JS sample's sendRawEvent approach.
        
        Includes input/output audio format and turn detection in the update
        to prevent the server from resetting those fields to defaults.
        """
        if self.connection:
            try:
                # Build session payload with avatar + preserved audio config
                session_payload = {
                    "avatar": avatar_data,
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                }

                # Preserve turn detection config
                td = build_turn_detection(self.config)
                if hasattr(td, 'as_dict'):
                    session_payload["turn_detection"] = td.as_dict()
                elif hasattr(td, '__dict__'):
                    session_payload["turn_detection"] = {k: v for k, v in td.__dict__.items() if not k.startswith('_')}

                raw_event = {
                    "type": "session.update",
                    "session": session_payload,
                }
                raw_json = json.dumps(raw_event)
                logger.debug("[SEND] raw session.update (scene)")
                await self.connection._connection.send_str(raw_json)
            except Exception as e:
                logger.error(f"Error updating avatar scene: {e}", exc_info=True)

    async def stop(self):
        """Stop the session."""
        self._stopping = True
        self.is_running = False
        conn = self.connection
        self.connection = None
        if conn is not None:
            try:
                await conn.close()
            except Exception as e:
                logger.debug(f"Error closing Voice Live connection for {self.client_id}: {e}")

    async def _wait_for_event(self, connection, wanted_types: set, timeout_s: float = 15.0):
        """Wait for specific event types."""
        logger.debug(f"[WAIT] Waiting for event types: {wanted_types}")
        async def _next():
            async for event in connection:
                etype = getattr(event, 'type', 'unknown')
                if etype != ServerEventType.RESPONSE_AUDIO_DELTA:
                    logger.debug(f"[RECV-WAIT] {etype}")
                if event.type in wanted_types:
                    return event
                # Continue handling other events while waiting
                await handle_event(self, event, connection)
            return None

        try:
            return await asyncio.wait_for(_next(), timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.error(f"Timeout waiting for {wanted_types}")
            raise
