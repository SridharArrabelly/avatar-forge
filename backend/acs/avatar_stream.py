"""Decoder for the Voice Live **avatar** stream (fragmented MP4 -> NV12 + PCM16).

Why this module exists
----------------------
When a Voice Live session runs with ``avatarOutputMode="websocket"`` the service
stops emitting ``response.audio.delta`` entirely and instead streams a single
**fragmented MP4** over ``response.video.delta``:

    ftyp + moov (init segment), then a run of (moof + mdat) fragments
    video : H.264  (512x512, yuv420p, ~24.6 fps)
    audio : AAC    (the answer audio — this is now the ONLY copy of it)

Both facts were measured against the live service, not assumed. Two consequences
drive this design:

1. **Deltas are not fragment-aligned.** 60 deltas produced 92 fragments, so a
   per-delta parser cannot work — the bytes must be fed to a *streaming* demuxer.
2. **Audio must be demuxed too.** Enabling the avatar without recovering the AAC
   track would silence the bot, regressing the one path that already works.

So this decoder demuxes both tracks and hands them back as the plain formats the
rest of the pipeline already speaks: raw NV12 frames for the meeting bot's
``VideoSocket``, and PCM16 for the existing outbound audio path (which keeps the
anti-aliasing low-pass, wake-phrase suppression and barge-in behaviour intact).

Decoding happens here in Python rather than in the .NET bot deliberately: PyAV
ships manylinux wheels so it runs on the existing Linux Container App, and it
keeps the Windows media bot a dumb pump, which is its whole design intent.
"""

from __future__ import annotations

import asyncio
import io
import logging
import queue
import threading
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Sentinel pushed onto the feed queue to unblock the reader at shutdown.
_EOF = object()


class _QueueReader(io.RawIOBase):
    """File-like adapter turning pushed byte chunks into a blocking read stream.

    PyAV wants a synchronous, blocking file object; the deltas arrive
    asynchronously. This bridges the two: ``push`` is called from the event loop,
    ``readinto`` blocks the decoder thread until bytes (or EOF) are available.
    """

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._buf = b""
        self._eof = False

    def readable(self) -> bool:  # noqa: D102
        return True

    def push(self, data: bytes) -> None:
        self._q.put(data)

    def close_stream(self) -> None:
        self._q.put(_EOF)

    def readinto(self, b) -> int:  # noqa: D102
        while not self._buf:
            if self._eof:
                return 0
            chunk = self._q.get()
            if chunk is _EOF:
                self._eof = True
                return 0
            self._buf = chunk
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


class AvatarStreamDecoder:
    """Streaming fMP4 -> (NV12 video, PCM16 audio) demuxer for one session.

    One instance lives for the whole bridge session. It is deliberately **never
    reset on barge-in**: the fMP4 init segment (``ftyp``/``moov``) arrives only
    once, so tearing the demuxer down mid-session would leave every later
    fragment undecodable. Barge-in is handled downstream by dropping frames,
    which is also where the existing suppression logic already lives.

    Args:
        width/height: target NV12 size — must match the format the meeting bot
            negotiated (``VideoFormatFor``), otherwise the .NET side rejects the
            frame and falls back to its placeholder.
        fps: target frame rate. The source runs faster than the bot plays, so
            frames are decimated by presentation time; without this the bot's
            queue grows without bound and video drifts behind the audio.
        audio_rate: PCM16 sample rate to emit (the bridge's Voice Live target).
    """

    def __init__(
        self,
        *,
        width: int,
        height: int,
        fps: int,
        audio_rate: int,
        on_video: Callable[[bytes, int, int], Awaitable[None]],
        on_audio: Callable[[bytes], Awaitable[None]],
        decode_video: bool = True,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = max(1, fps)
        self.audio_rate = audio_rate
        self._on_video = on_video
        self._on_audio = on_audio
        # The browser joiner plays the fMP4 itself (MediaSource) and only needs
        # the audio track split back out, so it decodes audio only. Skipping the
        # H.264 decode + scale there avoids paying for frames nobody consumes.
        self._decode_video = decode_video
        self._loop = loop or asyncio.get_event_loop()

        self._reader = _QueueReader()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._stopped = False
        self._graph = None
        self._graph_src_size: Optional[tuple] = None
        self._next_video_t = 0.0
        self.video_frames = 0
        self.audio_frames = 0

    # ───────── feeding ─────────

    def feed(self, data: bytes) -> None:
        """Push one ``response.video.delta`` payload into the demuxer."""
        if self._stopped or not data:
            return
        if not self._started:
            self._started = True
            self._thread = threading.Thread(
                target=self._run, name="avatar-decoder", daemon=True
            )
            self._thread.start()
        self._reader.push(data)

    def close(self) -> None:
        """Unblock and wind down the decoder thread."""
        if self._stopped:
            return
        self._stopped = True
        self._reader.close_stream()

    # ───────── decoder thread ─────────

    def _run(self) -> None:
        try:
            import av  # imported lazily so an audio-only deploy needs no PyAV
        except ImportError:
            logger.error(
                "Avatar video enabled but PyAV is not installed; "
                "no avatar face or audio will be produced. Install the 'av' package."
            )
            return

        try:
            container = av.open(self._reader, mode="r", format="mp4")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[avatar] could not open the avatar stream: {e}")
            return

        vstream = next((s for s in container.streams if s.type == "video"), None)
        astream = next((s for s in container.streams if s.type == "audio"), None)
        logger.info(
            f"[avatar] stream opened: "
            f"video={vstream.codec_context.name if vstream else None} "
            f"audio={astream.codec_context.name if astream else None} "
            f"-> NV12 {self.width}x{self.height}@{self.fps}, PCM16 {self.audio_rate}Hz"
        )

        resampler = None
        if astream is not None:
            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=self.audio_rate
            )

        try:
            for packet in container.demux():
                if self._stopped:
                    break
                if packet.dts is None:
                    continue
                is_video = packet.stream.type == "video"
                if is_video and not self._decode_video:
                    continue
                try:
                    frames = packet.decode()
                except Exception:  # noqa: BLE001 — a bad packet must not kill the call
                    continue
                for frame in frames:
                    if self._stopped:
                        break
                    if is_video:
                        self._handle_video(av, frame)
                    elif resampler is not None:
                        self._handle_audio(resampler, frame)
        except Exception as e:  # noqa: BLE001 — normal at end of stream
            logger.info(f"[avatar] decode loop ended: {e}")
        finally:
            try:
                container.close()
            except Exception:  # noqa: BLE001
                pass
            logger.info(
                f"[avatar] decoder stopped "
                f"(video={self.video_frames} audio={self.audio_frames})"
            )

    # ───────── per-frame handling ─────────

    def _handle_video(self, av, frame) -> None:
        # Decimate by presentation time down to the bot's playout rate.
        t = float(frame.time) if frame.time is not None else None
        if t is not None:
            if t + 1e-6 < self._next_video_t:
                return
            self._next_video_t = max(self._next_video_t, t) + 1.0 / self.fps

        try:
            nv12 = self._to_nv12(av, frame)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[avatar] video convert failed: {e}")
            return

        self.video_frames += 1
        if self.video_frames == 1:
            logger.info(
                f"[avatar] first NV12 frame {self.width}x{self.height} "
                f"({len(nv12)} bytes)"
            )
        self._dispatch(self._on_video(nv12, self.width, self.height))

    def _handle_audio(self, resampler, frame) -> None:
        try:
            out = resampler.resample(frame)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[avatar] audio resample failed: {e}")
            return
        # PyAV >= 9 returns a list of frames; older returns one or None.
        for af in out if isinstance(out, list) else ([out] if out else []):
            pcm = bytes(af.planes[0])
            if not pcm:
                continue
            self.audio_frames += 1
            if self.audio_frames == 1:
                logger.info(f"[avatar] first PCM16 chunk ({len(pcm)} bytes)")
            self._dispatch(self._on_audio(pcm))

    def _to_nv12(self, av, frame) -> bytes:
        """Centre-crop to the target aspect, scale, and convert to NV12.

        The avatar renders square (512x512) but the meeting bot only accepts the
        media platform's enumerated 16:9 NV12 formats. Cropping to the target
        aspect before scaling keeps the face correctly proportioned — a plain
        rescale would visibly squash it.
        """
        graph = self._graph
        if graph is None or self._graph_src_size != (frame.width, frame.height):
            graph = self._build_graph(av, frame)
            self._graph = graph
            self._graph_src_size = (frame.width, frame.height)

        graph.push(frame)
        out = graph.pull()
        # NV12: full-res Y plane followed by a half-res interleaved UV plane.
        return b"".join(bytes(p) for p in out.planes)

    def _build_graph(self, av, frame):
        """Fit the square avatar frame into the negotiated tile WITHOUT cropping.

        The avatar renders square (512x512) while the meeting tile is 16:9. The
        first implementation centre-cropped to the target aspect, which threw away
        44% of the picture and decapitated her on screen — the head filled the
        frame, so any vertical crop cut the top of the head and the chin.

        Instead: scale the whole frame down to fit, then pad out to exactly the
        negotiated size. Emitting precisely ``VideoFormatFor``'s dimensions is a
        hard requirement of the media platform, so the pad is what guarantees it.
        """
        src_w, src_h = frame.width, frame.height
        # Scale to fit inside the target box, preserving aspect (even dimensions
        # are required by yuv/nv12 chroma subsampling).
        scale = min(self.width / src_w, self.height / src_h)
        fit_w = max(2, int(src_w * scale) & ~1)
        fit_h = max(2, int(src_h * scale) & ~1)
        pad_x = (self.width - fit_w) // 2
        pad_y = (self.height - fit_h) // 2

        graph = av.filter.Graph()
        chain = [
            graph.add_buffer(template=None, width=src_w, height=src_h,
                             format=frame.format.name, time_base=frame.time_base),
            graph.add("scale", f"{fit_w}:{fit_h}"),
            # Letterbox onto the brand's dark background (#0b1020) rather than pure
            # black, so the tile matches the placard the browser joiner paints.
            graph.add("pad", f"{self.width}:{self.height}:{pad_x}:{pad_y}:0x0B1020"),
            graph.add("format", "nv12"),
            graph.add("buffersink"),
        ]
        for a, b in zip(chain, chain[1:]):
            a.link_to(b)
        graph.configure()
        logger.info(
            f"[avatar] video filter: {src_w}x{src_h} "
            f"-> scale {fit_w}x{fit_h} -> pad {self.width}x{self.height}"
            f"+{pad_x}+{pad_y} nv12"
        )
        return graph

    def _dispatch(self, coro) -> None:
        """Hand a coroutine back to the event loop from the decoder thread."""
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[avatar] dispatch failed: {e}")
            coro.close()
