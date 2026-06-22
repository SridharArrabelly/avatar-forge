"""AcsVoiceBridge — bridges an ACS media WebSocket to a Voice Live session.

This is the heart of Phase 2b. It is an **adapter**, not a transport swap: it
reuses the existing ``VoiceSessionHandler`` unchanged by feeding it the two
callbacks it already expects (``send_message`` for control JSON, ``send_binary``
for PCM16 output) and driving its input via ``send_audio_bytes``.

    ACS meeting audio (MIXED, base64 PCM16)
        --> _on_acs_text() --> handler.send_audio_bytes(pcm)   [inbound]
    Voice Live RESPONSE_AUDIO_DELTA (PCM16)
        --> send_binary(pcm) --> ACS AudioData frame            [outbound]

Format: 16-bit PCM mono. Voice Live expects 24 kHz PCM16. The Teams meeting bot
delivers 16 kHz, so inbound audio is **resampled up to 24 kHz** here (see
``_resample_to_target``) — without it Voice Live interprets the audio ~1.5x too
fast and STT returns empty transcripts. Browser clients already send 24 kHz and
bypass the resampler. Output is forwarded as-is.

Turn-taking (so she never talks over the room): outbound speech is gated on a
**wake phrase** appearing in the triggering user utterance (``ACS_REQUIRE_WAKE_
PHRASE``). When an utterance is not addressed to her, the bridge cancels the
in-flight Voice Live response and drops its audio. This is a first, tunable slice
of half-duplex turn-taking; finer barge-in tuning over live room audio is 2b
follow-up work (the bridge owns this policy so the handler stays generic).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
import time
from array import array
from typing import Optional

from ..config import (
    ACS_IDLE_TIMEOUT_S,
    ACS_REQUIRE_WAKE_PHRASE,
    ACS_WAKE_PHRASES,
)

logger = logging.getLogger(__name__)


class AcsVoiceBridge:
    """Glue between one ACS media WebSocket and one VoiceSessionHandler.

    The handler is created and started by ``routes.py`` with this bridge's
    ``send_message``/``send_binary`` as its output callbacks. The bridge then
    pumps ACS inbound frames into ``handler.send_audio_bytes``.
    """

    def __init__(self, acs_ws, client_id: str):
        self._ws = acs_ws
        self.client_id = client_id
        self.handler = None  # set by routes after construction

        # Turn-taking state.
        self._answer_armed = not ACS_REQUIRE_WAKE_PHRASE
        self._suppress_current_response = False
        self._last_activity_ms = time.monotonic() * 1000.0

        # ACS inbound audio metadata (filled from the AudioMetadata frame).
        self._inbound_sample_rate: Optional[int] = None
        # Voice Live expects PCM16 @ 24 kHz. The meeting bot delivers 16 kHz, so
        # we resample inbound audio up to 24 kHz here; otherwise Voice Live
        # interprets it at the wrong speed and STT returns empty transcripts.
        self._target_sample_rate = 24000
        self._resample_carry: Optional[int] = None
        self._frames_in = 0
        self._frames_out = 0
        self._silent_in = 0
        self._closed = False

    # ───────── Voice Live -> ACS (outbound) ─────────

    async def send_binary(self, pcm_bytes: bytes) -> None:
        """Voice Live PCM16 output -> ACS AudioData frame.

        Dropped while the current response is suppressed (utterance not addressed
        to the avatar), which is what keeps her from speaking over the room.
        """
        if self._closed or self._suppress_current_response:
            return
        try:
            data_b64 = base64.b64encode(pcm_bytes).decode("ascii")
            frame = {"Kind": "AudioData", "AudioData": {"Data": data_b64}}
            await self._ws.send_text(json.dumps(frame))
            self._frames_out += 1
        except Exception as e:  # noqa: BLE001 — one bad frame must not kill the call
            logger.debug(f"[ACS {self.client_id}] outbound audio send failed: {e}")

    async def send_message(self, msg: dict) -> None:
        """Handle Voice Live control events relayed by the session handler.

        We don't have a browser client here; instead we use these events to drive
        turn-taking and to stop ACS playback on interrupt.
        """
        mtype = msg.get("type")

        if mtype == "transcript_done" and msg.get("role") == "user":
            self._on_user_utterance((msg.get("transcript") or "").strip())

        elif mtype == "response_created":
            # Decide whether this response should be heard by the room.
            self._suppress_current_response = not self._answer_armed
            if self._suppress_current_response:
                logger.info(
                    f"[ACS {self.client_id}] response suppressed "
                    f"(no wake phrase in last utterance)"
                )
                # Stop generation early to save tokens/latency; best-effort.
                if self.handler is not None:
                    await self.handler.interrupt()

        elif mtype in ("response_done", "audio_done"):
            # Re-arm gate for the next turn when a wake phrase is required.
            if ACS_REQUIRE_WAKE_PHRASE:
                self._answer_armed = False

        elif mtype == "stop_playback":
            await self._send_stop_audio()

    # ───────── ACS -> Voice Live (inbound) ─────────

    async def pump(self) -> None:
        """Read ACS media frames until the socket closes.

        ACS connects to *our* WebSocket and streams JSON text frames:
        first an ``AudioMetadata`` frame, then a stream of ``AudioData`` frames.
        """
        idle_task = (
            asyncio.create_task(self._idle_watchdog())
            if ACS_IDLE_TIMEOUT_S > 0
            else None
        )
        try:
            while True:
                raw = await self._ws.receive_text()
                await self._on_acs_text(raw)
        except Exception as e:  # noqa: BLE001 — normal on disconnect
            logger.info(f"[ACS {self.client_id}] media socket closed: {e}")
        finally:
            self._closed = True
            if idle_task is not None:
                idle_task.cancel()

    async def _on_acs_text(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        kind = msg.get("kind") or msg.get("Kind")

        if kind == "AudioMetadata":
            meta = msg.get("audioMetadata") or msg.get("AudioMetadata") or {}
            self._inbound_sample_rate = meta.get("sampleRate")
            logger.info(
                f"[ACS {self.client_id}] audio metadata: "
                f"rate={self._inbound_sample_rate} "
                f"channels={meta.get('channels')} encoding={meta.get('encoding')}"
            )
            return

        if kind == "AudioData":
            audio = msg.get("audioData") or msg.get("AudioData") or {}
            if audio.get("silent"):
                self._silent_in += 1
                if self._silent_in in (1, 50) or self._silent_in % 500 == 0:
                    logger.info(
                        f"[ACS {self.client_id}] inbound AudioData (silent) "
                        f"count={self._silent_in} (no voice yet; non-silent={self._frames_in})"
                    )
                return
            data_b64 = audio.get("data") or audio.get("Data")
            if not data_b64 or self.handler is None:
                return
            try:
                pcm = base64.b64decode(data_b64)
            except Exception:  # noqa: BLE001
                return
            pcm = self._resample_to_target(pcm)
            self._frames_in += 1
            if self._frames_in == 1 or self._frames_in % 100 == 0:
                logger.info(
                    f"[ACS {self.client_id}] inbound voice AudioData "
                    f"non-silent={self._frames_in} silent={self._silent_in} "
                    f"in_rate={self._inbound_sample_rate} -> "
                    f"{self._target_sample_rate} Voice Live"
                )
            self._last_activity_ms = time.monotonic() * 1000.0
            await self.handler.send_audio_bytes(pcm)

    def _resample_to_target(self, pcm: bytes) -> bytes:
        """Resample mono PCM16 from the inbound rate up to Voice Live's 24 kHz.

        The Teams media bot delivers 16 kHz; Voice Live expects 24 kHz. Without
        this, audio is interpreted ~1.5x too fast and STT returns empty
        transcripts even though VAD detects speech. Uses dependency-free linear
        interpolation, carrying the last sample across frames for continuity.
        """
        in_rate = self._inbound_sample_rate
        if not in_rate or in_rate == self._target_sample_rate or len(pcm) < 2:
            return pcm
        out_rate = self._target_sample_rate
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder == "big":
            samples.byteswap()
        prev = self._resample_carry
        if prev is None:
            prev = samples[0]
        src = [prev] + list(samples)
        n_in = len(samples)
        n_out = (n_in * out_rate) // in_rate
        out = array("h", bytes(2 * n_out))
        step = in_rate / out_rate
        for i in range(n_out):
            pos = i * step
            idx = int(pos)
            frac = pos - idx
            a = src[idx]
            b = src[idx + 1] if idx + 1 < len(src) else src[idx]
            out[i] = int(a + (b - a) * frac)
        self._resample_carry = samples[-1]
        if sys.byteorder == "big":
            out.byteswap()
        return out.tobytes()

    # ───────── turn-taking ─────────

    def _on_user_utterance(self, transcript: str) -> None:
        """Arm the answer gate when an utterance is addressed to the avatar."""
        self._last_activity_ms = time.monotonic() * 1000.0
        logger.info(f"[ACS {self.client_id}] heard utterance: {transcript!r}")
        if not ACS_REQUIRE_WAKE_PHRASE:
            self._answer_armed = True
            return
        lowered = transcript.lower()
        armed = any(p in lowered for p in ACS_WAKE_PHRASES)
        self._answer_armed = armed
        if armed:
            logger.info(
                f"[ACS {self.client_id}] wake phrase detected — answering: "
                f"{transcript!r}"
            )
        else:
            logger.info(
                f"[ACS {self.client_id}] no wake phrase {ACS_WAKE_PHRASES} in "
                f"utterance — staying silent"
            )

    async def _send_stop_audio(self) -> None:
        """Tell ACS to flush any buffered outbound audio (barge-in)."""
        if self._closed:
            return
        try:
            await self._ws.send_text(json.dumps({"Kind": "StopAudio", "StopAudio": {}}))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[ACS {self.client_id}] StopAudio send failed: {e}")

    async def _idle_watchdog(self) -> None:
        """Leave the call after ACS_IDLE_TIMEOUT_S of no inbound speech."""
        while not self._closed:
            await asyncio.sleep(5)
            idle_s = (time.monotonic() * 1000.0 - self._last_activity_ms) / 1000.0
            if idle_s >= ACS_IDLE_TIMEOUT_S:
                logger.info(
                    f"[ACS {self.client_id}] idle {idle_s:.0f}s >= "
                    f"{ACS_IDLE_TIMEOUT_S:.0f}s — closing media socket"
                )
                try:
                    await self._ws.close()
                except Exception:  # noqa: BLE001
                    pass
                return


class BrowserVoiceBridge:
    """Bridges a *browser* media WebSocket (raw PCM16) to a Voice Live session.

    This is the client-side media path (issue #27, D5 option A1). Microsoft's
    server-side Call Automation media streaming does **not** deliver real-time
    audio from a Teams *meeting* (only from ACS/PSTN/Teams-*user* calls), so the
    meeting audio is captured in the browser instead — the ACS Calling SDK
    participant leg already carries it both ways. The browser:

        meeting remote audio  --(Web Audio -> PCM16)-->  this WS  --> Voice Live
        Voice Live PCM16 out  --> this WS --> browser plays it as the leg's
                                              outgoing call audio (Nuru speaks)

    Transport is raw binary PCM16 mono at ``ACS_AUDIO_SAMPLE_RATE`` (24 kHz),
    matching Voice Live, so there is no base64/JSON envelope (unlike the ACS
    Call Automation socket). Turn-taking is identical to ``AcsVoiceBridge``.
    """

    def __init__(self, ws, client_id: str):
        self._ws = ws
        self.client_id = client_id
        self.handler = None  # set by routes after construction

        # Turn-taking state (mirrors AcsVoiceBridge).
        self._answer_armed = not ACS_REQUIRE_WAKE_PHRASE
        self._suppress_current_response = False
        self._hard_muted = False  # host pressed "Mute Nuru" — suppress all output
        self._last_activity_ms = time.monotonic() * 1000.0

        self._frames_in = 0
        self._frames_out = 0
        self._closed = False

    # ───────── Voice Live -> browser (outbound) ─────────

    async def send_binary(self, pcm_bytes: bytes) -> None:
        """Voice Live PCM16 output -> raw binary frame to the browser.

        Dropped while the current response is suppressed (utterance not addressed
        to the avatar), which is what keeps her from speaking over the room.
        """
        if self._closed or self._suppress_current_response or self._hard_muted:
            return
        try:
            await self._ws.send_bytes(pcm_bytes)
            self._frames_out += 1
        except Exception as e:  # noqa: BLE001 — one bad frame must not kill the call
            logger.debug(f"[browser {self.client_id}] outbound audio send failed: {e}")

    async def send_message(self, msg: dict) -> None:
        """Drive turn-taking and barge-in from Voice Live control events."""
        mtype = msg.get("type")

        if mtype == "transcript_done" and msg.get("role") == "user":
            self._on_user_utterance((msg.get("transcript") or "").strip())

        elif mtype == "response_created":
            self._suppress_current_response = not self._answer_armed
            if self._suppress_current_response:
                logger.info(
                    f"[browser {self.client_id}] response suppressed "
                    f"(no wake phrase in last utterance)"
                )
                if self.handler is not None:
                    await self.handler.interrupt()

        elif mtype in ("response_done", "audio_done"):
            if ACS_REQUIRE_WAKE_PHRASE:
                self._answer_armed = False

        elif mtype == "stop_playback":
            await self._send_stop_audio()

    async def _send_stop_audio(self) -> None:
        """Tell the browser to flush its outbound playback queue (barge-in)."""
        if self._closed:
            return
        try:
            await self._ws.send_text(json.dumps({"type": "stop_playback"}))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[browser {self.client_id}] stop_playback send failed: {e}")

    # ───────── browser -> Voice Live (inbound) ─────────

    async def pump(self) -> None:
        """Read raw PCM16 frames from the browser until the socket closes."""
        try:
            while True:
                message = await self._ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data is None:
                    # Text control / diagnostics frames from the browser.
                    text = message.get("text")
                    if text:
                        try:
                            ctrl = json.loads(text)
                        except Exception:  # noqa: BLE001
                            ctrl = None
                        if isinstance(ctrl, dict):
                            ct = ctrl.get("type")
                            if ct == "capture_stats":
                                logger.info(
                                    f"[browser {self.client_id}] capture stats: "
                                    f"frames={ctrl.get('frames')} maxRms={ctrl.get('maxRms')} "
                                    f"ctxRate={ctrl.get('ctxRate')} "
                                    f"selfTalking={ctrl.get('selfTalking')} "
                                    f"humanMuted={ctrl.get('humanMuted')} "
                                    f"remoteStreams={ctrl.get('remoteStreams')} "
                                    f"wiredTracks={ctrl.get('wiredTracks')}"
                                )
                            elif ct == "remote_wired":
                                logger.info(
                                    f"[browser {self.client_id}] browser wired remote "
                                    f"audio track {ctrl.get('trackId')}"
                                )
                            elif ct == "mic_wired":
                                logger.info(
                                    f"[browser {self.client_id}] browser wired mic capture "
                                    f"({ctrl.get('tracks')} track(s))"
                                )
                            elif ct == "incoming_muted":
                                logger.info(
                                    f"[browser {self.client_id}] browser muted incoming "
                                    f"audio (echo guard)"
                                )
                            elif ct == "interrupt":
                                logger.info(
                                    f"[browser {self.client_id}] interrupt requested "
                                    f"(muted by others) — stopping current response"
                                )
                                self._suppress_current_response = True
                                if self.handler is not None:
                                    await self.handler.interrupt()
                            elif ct == "hard_mute":
                                logger.info(
                                    f"[browser {self.client_id}] host muted Nuru — "
                                    f"suppressing all output until unmuted"
                                )
                                self._hard_muted = True
                                if self.handler is not None:
                                    await self.handler.interrupt()
                            elif ct == "hard_unmute":
                                logger.info(
                                    f"[browser {self.client_id}] host unmuted Nuru — "
                                    f"output re-enabled"
                                )
                                self._hard_muted = False
                            elif ct == "farside_wired":
                                logger.info(
                                    f"[browser {self.client_id}] browser wired far-side "
                                    f"(display) audio ({ctrl.get('tracks')} track(s))"
                                )
                    continue
                if self.handler is None:
                    continue
                self._frames_in += 1
                if self._frames_in == 1 or self._frames_in % 200 == 0:
                    logger.info(
                        f"[browser {self.client_id}] inbound voice frames="
                        f"{self._frames_in} -> Voice Live"
                    )
                self._last_activity_ms = time.monotonic() * 1000.0
                await self.handler.send_audio_bytes(data)
        except Exception as e:  # noqa: BLE001 — normal on disconnect
            logger.info(f"[browser {self.client_id}] media socket closed: {e}")
        finally:
            self._closed = True

    def _on_user_utterance(self, transcript: str) -> None:
        """Arm the answer gate when an utterance is addressed to the avatar."""
        self._last_activity_ms = time.monotonic() * 1000.0
        logger.info(f"[browser {self.client_id}] heard utterance: {transcript!r}")
        if not ACS_REQUIRE_WAKE_PHRASE:
            self._answer_armed = True
            return
        lowered = transcript.lower()
        armed = any(p in lowered for p in ACS_WAKE_PHRASES)
        self._answer_armed = armed
        if armed:
            logger.info(
                f"[browser {self.client_id}] wake phrase detected — answering: "
                f"{transcript!r}"
            )
        else:
            logger.info(
                f"[browser {self.client_id}] no wake phrase {ACS_WAKE_PHRASES} in "
                f"utterance — staying silent"
            )
