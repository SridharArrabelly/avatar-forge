"""AcsVoiceBridge — bridges an ACS media WebSocket to a Voice Live session.

This is the heart of channel D. It is an **adapter**, not a transport swap: it
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
import math
import sys
import time
from array import array
from typing import Optional

from ..config import (
    ACS_FOLLOWUP_WINDOW_S,
    ACS_IDLE_TIMEOUT_S,
    ACS_REQUIRE_WAKE_PHRASE,
    ACS_WAKE_PHRASES,
    MEETING_BOT_VIDEO_FPS,
    MEETING_BOT_VIDEO_HEIGHT,
    MEETING_BOT_VIDEO_WIDTH,
)
from .avatar_stream import AvatarStreamDecoder

logger = logging.getLogger(__name__)

# Per-section Q values for a 6th-order Butterworth response (3 cascaded biquads).
_BUTTERWORTH_Q6 = (0.51763809, 0.70710678, 1.93185165)

# Voice Live's native PCM16 rate. The browser leg runs at this rate end to end
# (frontend/acs-join.js MEDIA_SAMPLE_RATE) so nothing is resampled there; the ACS
# Call Automation / meeting-bot leg resamples to whatever the far end negotiates.
_VOICE_LIVE_RATE = 24000


def _design_lowpass(fs: int, fc: float, q_list=_BUTTERWORTH_Q6):
    """RBJ-cookbook biquad coefficients for a cascaded low-pass at ``fc``."""
    sections = []
    w0 = 2 * math.pi * fc / fs
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    for q in q_list:
        alpha = sin_w0 / (2 * q)
        b0 = (1 - cos_w0) / 2
        b1 = 1 - cos_w0
        b2 = (1 - cos_w0) / 2
        a0 = 1 + alpha
        a1 = -2 * cos_w0
        a2 = 1 - alpha
        sections.append((b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0))
    return sections


class _LowPass:
    """Stateful cascaded-biquad low-pass filter (pure Python, no deps).

    Used as the anti-aliasing filter ahead of downsampling: plain linear-interp
    decimation (e.g. 24 kHz -> 16 kHz) with no low-pass folds content above the
    output Nyquist back into the audible band as hiss ("shh" on sibilants). The
    filter state (``z1``/``z2`` per section, transposed Direct-Form II) carries
    across frames so consecutive chunks join without discontinuities.
    """

    def __init__(self, fs: int, fc: float):
        self._sections = _design_lowpass(fs, fc)
        self._state = [[0.0, 0.0] for _ in self._sections]

    def process(self, samples: list) -> list:
        for si, (b0, b1, b2, a1, a2) in enumerate(self._sections):
            z1, z2 = self._state[si]
            for i, x in enumerate(samples):
                y = b0 * x + z1
                z1 = b1 * x - a1 * y + z2
                z2 = b2 * x - a2 * y
                samples[i] = y
            self._state[si] = [z1, z2]
        return samples



class AcsVoiceBridge:
    """Glue between one ACS media WebSocket and one VoiceSessionHandler.

    The handler is created and started by ``routes.py`` with this bridge's
    ``send_message``/``send_binary`` as its output callbacks. The bridge then
    pumps ACS inbound frames into ``handler.send_audio_bytes``.
    """

    def __init__(self, acs_ws, client_id: str, avatar_video: bool = False):
        self._ws = acs_ws
        self.client_id = client_id
        self.handler = None  # set by routes after construction

        # Turn-taking state.
        self._answer_armed = not ACS_REQUIRE_WAKE_PHRASE
        self._suppress_current_response = False
        self._last_activity_ms = time.monotonic() * 1000.0
        # Timestamp (monotonic ms) of the last answer we actually spoke; powers the
        # follow-up grace window so conversational follow-ups skip the wake phrase.
        self._last_answer_done_ms = 0.0

        # ACS inbound audio metadata (filled from the AudioMetadata frame).
        self._inbound_sample_rate: Optional[int] = None
        # Voice Live speaks PCM16 @ 24 kHz; the Teams meeting bot uses 16 kHz.
        # Inbound audio is resampled UP to 24 kHz (else STT returns empty
        # transcripts) and outbound answer audio is resampled DOWN to the bot's
        # rate (else playback is garbled/slow once unmuted). Each direction keeps
        # its own interpolation carry sample for continuity across frames.
        self._target_sample_rate = 24000
        self._resample_carry_in: Optional[int] = None
        self._resample_carry_out: Optional[int] = None
        # Anti-aliasing low-pass applied before outbound downsampling (built lazily
        # once the bot's sample rate is known from the AudioMetadata frame).
        self._lpf_out: Optional[_LowPass] = None
        self._frames_in = 0
        self._frames_out = 0
        self._silent_in = 0
        self._closed = False

        # ── Avatar face ──
        # In avatar/websocket mode Voice Live emits ONE fragmented-MP4 stream
        # carrying both the H.264 face and the AAC answer audio; the decoder
        # splits it back into the NV12 frames the bot's VideoSocket wants and the
        # PCM16 the outbound audio path already handles. Created lazily on the
        # first delta so an audio-only session never touches PyAV.
        self._avatar_video = avatar_video
        self._decoder: Optional[AvatarStreamDecoder] = None
        self._video_out = 0

    def _ensure_decoder(self) -> AvatarStreamDecoder:
        if self._decoder is None:
            self._decoder = AvatarStreamDecoder(
                width=MEETING_BOT_VIDEO_WIDTH,
                height=MEETING_BOT_VIDEO_HEIGHT,
                fps=MEETING_BOT_VIDEO_FPS,
                audio_rate=self._target_sample_rate,
                on_video=self.send_video_frame,
                # Route recovered audio through the normal outbound path so the
                # anti-alias low-pass, resampling and suppression gate all still
                # apply exactly as they do in audio-only mode.
                on_audio=self.send_binary,
                loop=asyncio.get_running_loop(),
            )
        return self._decoder

    # ───────── Voice Live -> ACS (outbound) ─────────

    async def send_binary(self, pcm_bytes: bytes) -> None:
        """Voice Live PCM16 output -> ACS AudioData frame.

        Dropped while the current response is suppressed (utterance not addressed
        to the avatar), which is what keeps her from speaking over the room.
        """
        if self._closed or self._suppress_current_response:
            return
        try:
            pcm_bytes = self._resample_from_target(pcm_bytes)
            data_b64 = base64.b64encode(pcm_bytes).decode("ascii")
            frame = {"Kind": "AudioData", "AudioData": {"Data": data_b64}}
            await self._ws.send_text(json.dumps(frame))
            self._frames_out += 1
            if self._frames_out == 1 or self._frames_out % 100 == 0:
                logger.info(
                    f"[ACS {self.client_id}] outbound answer AudioData "
                    f"frames_out={self._frames_out} "
                    f"{self._target_sample_rate}->{self._inbound_sample_rate} bot"
                )
        except Exception as e:  # noqa: BLE001 — one bad frame must not kill the call
            logger.debug(f"[ACS {self.client_id}] outbound audio send failed: {e}")

    async def send_video_frame(self, nv12: bytes, width: int, height: int) -> None:
        """Decoded avatar NV12 frame -> bridge ``VideoData`` frame.

        Suppressed alongside audio so a response the room is not meant to hear
        doesn't animate the tile either; the bot then falls back to its
        placeholder, which is the intended idle appearance.
        """
        if self._closed or self._suppress_current_response:
            return
        try:
            frame = {
                "Kind": "VideoData",
                "VideoData": {
                    "Data": base64.b64encode(nv12).decode("ascii"),
                    "Width": width,
                    "Height": height,
                },
            }
            await self._ws.send_text(json.dumps(frame))
            self._video_out += 1
            if self._video_out == 1 or self._video_out % 150 == 0:
                logger.info(
                    f"[ACS {self.client_id}] outbound avatar VideoData "
                    f"frames={self._video_out} {width}x{height}"
                )
        except Exception as e:  # noqa: BLE001 — one bad frame must not kill the call
            logger.debug(f"[ACS {self.client_id}] outbound video send failed: {e}")

    async def send_message(self, msg: dict) -> None:
        """Handle Voice Live control events relayed by the session handler.

        We don't have a browser client here; instead we use these events to drive
        turn-taking and to stop ACS playback on interrupt.
        """
        mtype = msg.get("type")

        if mtype == "video_data":
            # Avatar stream (fMP4 with both the face and the answer audio).
            if self._avatar_video and not self._closed:
                delta = msg.get("delta") or ""
                if delta:
                    self._ensure_decoder().feed(base64.b64decode(delta))
            return

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
            # An answer the room actually heard re-opens the follow-up window.
            if not self._suppress_current_response:
                self._last_answer_done_ms = time.monotonic() * 1000.0
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
            if self._decoder is not None:
                self._decoder.close()
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
        """Resample inbound mono PCM16 from the bot's rate up to 24 kHz."""
        in_rate = self._inbound_sample_rate
        if not in_rate or in_rate == self._target_sample_rate:
            return pcm
        out, self._resample_carry_in = self._resample_pcm16(
            pcm, in_rate, self._target_sample_rate, self._resample_carry_in
        )
        return out

    def _resample_from_target(self, pcm: bytes) -> bytes:
        """Resample outbound mono PCM16 from Voice Live's 24 kHz down to the bot's rate."""
        out_rate = self._inbound_sample_rate
        if not out_rate or out_rate == self._target_sample_rate:
            return pcm
        # Downsampling (e.g. 24 kHz -> 16 kHz) aliases without a low-pass: content
        # above the output Nyquist folds back as audible hiss ("shh" on sibilants).
        # Filter it out first, then do the rate conversion.
        if out_rate < self._target_sample_rate:
            pcm = self._lowpass_out(pcm)
        out, self._resample_carry_out = self._resample_pcm16(
            pcm, self._target_sample_rate, out_rate, self._resample_carry_out
        )
        return out

    def _lowpass_out(self, pcm: bytes) -> bytes:
        """Apply the stateful anti-aliasing low-pass to outbound 24 kHz PCM16."""
        if self._lpf_out is None:
            # Cutoff safely below the output Nyquist (out_rate / 2).
            fc = min(6800.0, 0.45 * self._inbound_sample_rate)
            self._lpf_out = _LowPass(self._target_sample_rate, fc)
            logger.info(
                f"[ACS {self.client_id}] anti-aliasing low-pass enabled "
                f"(fc={fc:.0f}Hz, fs={self._target_sample_rate})"
            )
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder == "big":
            samples.byteswap()
        filtered = self._lpf_out.process([float(s) for s in samples])
        out = array("h", bytes(2 * len(filtered)))
        for i, v in enumerate(filtered):
            out[i] = -32768 if v < -32768 else (32767 if v > 32767 else int(v))
        if sys.byteorder == "big":
            out.byteswap()
        return out.tobytes()

    @staticmethod
    def _resample_pcm16(
        pcm: bytes, in_rate: int, out_rate: int, carry: Optional[int]
    ) -> tuple[bytes, Optional[int]]:
        """Linear-interpolation resample of mono PCM16, dependency-free.

        Carries the last input sample across calls so consecutive frames join
        without clicks. Returns the resampled bytes and the new carry sample.
        """
        if len(pcm) < 2 or in_rate == out_rate:
            return pcm, carry
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder == "big":
            samples.byteswap()
        prev = carry if carry is not None else samples[0]
        src = [prev] + list(samples)
        n_out = (len(samples) * out_rate) // in_rate
        out = array("h", bytes(2 * n_out))
        step = in_rate / out_rate
        for i in range(n_out):
            pos = i * step
            idx = int(pos)
            frac = pos - idx
            a = src[idx]
            b = src[idx + 1] if idx + 1 < len(src) else src[idx]
            out[i] = max(-32768, min(32767, int(a + (b - a) * frac)))
        new_carry = samples[-1]
        if sys.byteorder == "big":
            out.byteswap()
        return out.tobytes(), new_carry

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
        if not armed and self._in_followup_window():
            armed = True
            logger.info(
                f"[ACS {self.client_id}] follow-up window "
                f"({ACS_FOLLOWUP_WINDOW_S:.0f}s) active — answering without wake "
                f"phrase: {transcript!r}"
            )
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

    def _in_followup_window(self) -> bool:
        """True if we recently answered and the follow-up grace window is open."""
        if ACS_FOLLOWUP_WINDOW_S <= 0 or not self._last_answer_done_ms:
            return False
        elapsed = time.monotonic() * 1000.0 - self._last_answer_done_ms
        return elapsed <= ACS_FOLLOWUP_WINDOW_S * 1000.0

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

    This is the client-side media path — the browser-joiner fallback. Microsoft's
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

    def __init__(self, ws, client_id: str, avatar_video: bool = False):
        self._ws = ws
        self.client_id = client_id
        self.handler = None  # set by routes after construction

        # Turn-taking state (mirrors AcsVoiceBridge).
        self._answer_armed = not ACS_REQUIRE_WAKE_PHRASE
        self._suppress_current_response = False
        self._hard_muted = False  # host pressed "Mute Nuru" — suppress all output
        self._last_activity_ms = time.monotonic() * 1000.0
        self._last_answer_done_ms = 0.0  # powers the follow-up grace window

        self._frames_in = 0
        self._frames_out = 0
        self._closed = False

        # ── Avatar face ──
        # In avatar/websocket mode Voice Live sends ONE fragmented-MP4 stream
        # carrying both the rendered face and the answer audio, and stops sending
        # response.audio.delta entirely. So we do two things with each delta:
        #
        #   1. feed it to an AUDIO-ONLY decoder, which recovers the PCM16 and
        #      pushes it through the existing send_binary path — barge-in, the
        #      wake-phrase gate and the hard mute all keep working untouched;
        #   2. forward the raw bytes to the browser, which plays them in a muted
        #      MediaSource <video> and paints that onto the outgoing video tile.
        #
        # Splitting it this way means the audio leg (the part that already works
        # in real meetings) is not on the video code path at all: if the face
        # fails, she still talks.
        self._avatar_video = avatar_video
        self._decoder: Optional[AvatarStreamDecoder] = None
        self._video_out = 0

    def _ensure_decoder(self) -> AvatarStreamDecoder:
        if self._decoder is None:
            self._decoder = AvatarStreamDecoder(
                width=MEETING_BOT_VIDEO_WIDTH,
                height=MEETING_BOT_VIDEO_HEIGHT,
                fps=MEETING_BOT_VIDEO_FPS,
                audio_rate=_VOICE_LIVE_RATE,
                on_video=self._drop_video,
                on_audio=self.send_binary,
                decode_video=False,  # the browser renders the face itself
                loop=asyncio.get_running_loop(),
            )
        return self._decoder

    async def _drop_video(self, nv12: bytes, width: int, height: int) -> None:
        """Never called (``decode_video=False``); present to satisfy the decoder."""
        return

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

        if mtype == "video_data":
            if self._avatar_video and not self._closed:
                delta = msg.get("delta") or ""
                if delta:
                    # Audio first: recovering the PCM is what keeps her audible.
                    self._ensure_decoder().feed(base64.b64decode(delta))
                    await self._send_video_delta(delta)
            return

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
            if not self._suppress_current_response:
                self._last_answer_done_ms = time.monotonic() * 1000.0
            if ACS_REQUIRE_WAKE_PHRASE:
                self._answer_armed = False

        elif mtype == "stop_playback":
            await self._send_stop_audio()

    async def _send_video_delta(self, delta_b64: str) -> None:
        """Forward one raw fMP4 chunk to the browser's MediaSource player.

        Deliberately NOT gated on ``_suppress_current_response``/``_hard_muted``.
        MediaSource needs a byte-contiguous stream — the ``ftyp``/``moov`` init
        segment arrives only once per session, and dropping fragments from the
        middle corrupts everything after them. Silencing is enforced on the audio
        path instead (that is what the room actually hears); a suppressed response
        is also interrupted immediately, so barely any video exists for it.
        """
        if self._closed:
            return
        try:
            await self._ws.send_text(
                json.dumps({"type": "video_data", "delta": delta_b64})
            )
            self._video_out += 1
            if self._video_out == 1:
                logger.info(f"[browser {self.client_id}] avatar video stream started")
        except Exception as e:  # noqa: BLE001 — video must never kill the call
            logger.debug(f"[browser {self.client_id}] video delta send failed: {e}")

    async def _send_stop_audio(self) -> None:
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
            if self._decoder is not None:
                self._decoder.close()

    def _on_user_utterance(self, transcript: str) -> None:
        """Arm the answer gate when an utterance is addressed to the avatar."""
        self._last_activity_ms = time.monotonic() * 1000.0
        logger.info(f"[browser {self.client_id}] heard utterance: {transcript!r}")
        if not ACS_REQUIRE_WAKE_PHRASE:
            self._answer_armed = True
            return
        lowered = transcript.lower()
        armed = any(p in lowered for p in ACS_WAKE_PHRASES)
        if not armed and self._in_followup_window():
            armed = True
            logger.info(
                f"[browser {self.client_id}] follow-up window "
                f"({ACS_FOLLOWUP_WINDOW_S:.0f}s) active — answering without wake "
                f"phrase: {transcript!r}"
            )
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

    def _in_followup_window(self) -> bool:
        """True if we recently answered and the follow-up grace window is open."""
        if ACS_FOLLOWUP_WINDOW_S <= 0 or not self._last_answer_done_ms:
            return False
        elapsed = time.monotonic() * 1000.0 - self._last_answer_done_ms
        return elapsed <= ACS_FOLLOWUP_WINDOW_S * 1000.0
