"""ACS HTTP + WebSocket endpoints (channel C, issue #27).

All endpoints are additive and gated on ``ACS_ENABLED``. When ACS is not
configured every endpoint returns 503 and the rest of the app is unaffected.

Endpoints:
  GET  /api/acs/config    -> {enabled, endpoint} for the browser joiner page
  POST /api/acs/token     -> mint an ACS VoIP token for the browser joiner
  WS   /ws/acs/audio      -> the .NET Graph media bot <-> Voice Live (hears the room)
  WS   /ws/acs/browser    -> the browser joiner <-> Voice Live (hears the operator only)

  POST /api/acs/call      -> attach media to a joined call (ServerCallId -> connect_call)
  POST /api/acs/callback  -> ACS Call Automation event webhook (CloudEvents)
      NOT used by either meeting leg above. Server-side media streaming was
      measured not to carry a Teams *meeting*'s audio, so both legs bridge audio
      themselves. These remain for ACS-native / Teams-user calls, which do
      support it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse

from ..config import (
    ACS_AVATAR_VIDEO_ENABLED,
    ACS_CALLBACK_BASE_URL,
    ACS_ENABLED,
    BROWSER_JOIN_VIDEO_ENABLED,
    ACS_ENDPOINT,
    AVATAR_DISPLAY_NAME,
    DEFAULT_ENDPOINT,
    MEETING_BOT_VIDEO_ENABLED,
    get_ui_defaults,
)
from ..voice import VoiceSessionHandler
from ..voice.auth import create_credential
from . import client as acs_client
from .bridge import AcsVoiceBridge, BrowserVoiceBridge

logger = logging.getLogger(__name__)

# Truth source for "is Nuru live in a call right now": the set of active media
# sessions (one per /ws/acs/audio connection). The Companion control panel polls
# /api/acs/status to surface this. Module-level + single-process (the ACA app runs
# one replica for the avatar session affinity), mirroring how the rest of the app
# keeps per-process session state.
_ACTIVE_CALLS: set[str] = set()

# Config for the in-call Voice Live session. Audio-only on the Voice Live socket
# (the avatar's picture is decoded separately and pushed as video frames),
# no proactive greeting (she must not announce herself over the room on connect),
# semantic VAD + barge-in so she yields to humans.
#
# Turn-taking latency: without `eouDetectionType` the VAD can only end a turn by
# waiting out `turnDetectionSilenceMs` of silence on EVERY question. Semantic
# end-of-utterance detection lets it commit as soon as the sentence is complete,
# which is the difference between a natural reply and one that lands a beat late.
# The silence window is only the FALLBACK for speech that trails off without a
# clean sentence boundary, so it is deliberately NOT overridden here — the call
# inherits TURN_DETECTION_SILENCE_MS along with every other channel. Shaving it
# buys ~100ms on the minority of turns where semantics do not fire, at the cost
# of cutting off anyone who pauses mid-sentence. In a boardroom, people pause.
_IN_CALL_CONFIG = {
    "avatarEnabled": False,
    "enableProactive": False,
    "turnDetectionType": "azure_semantic_vad",
    "eouDetectionType": "semantic_detection_v1",
    "enableBargeIn": True,
    "useEC": True,
    "useNS": True,
}


def _in_call_config(avatar_video: bool) -> dict:
    """Voice Live session config for one in-call media session.

    With ``avatar_video`` the session switches to avatar/``websocket`` output, so
    Voice Live streams a fragmented MP4 carrying BOTH the rendered face and the
    answer audio (it stops sending ``response.audio.delta`` in this mode). The
    bridge decodes that stream back into NV12 + PCM16. The avatar identity is read
    from the app's own UI defaults so the meeting face is the same avatar the web
    app shows — one source of truth, no separate knob to drift.
    """
    defaults = get_ui_defaults()
    config = dict(_IN_CALL_CONFIG)
    # Read per-session, not at import, so TURN_DETECTION_SILENCE_MS stays a live
    # knob and the in-call turn boundary can never silently diverge from the web
    # and Teams-tab channels.
    config["turnDetectionSilenceMs"] = defaults.get("turnDetectionSilenceMs", 500)
    if not avatar_video:
        return config

    config.update(
        {
            "avatarEnabled": True,
            "avatarOutputMode": "websocket",
            "isPhotoAvatar": defaults.get("isPhotoAvatar", False),
            "isCustomAvatar": defaults.get("isCustomAvatar", False),
            "avatarName": defaults.get("avatarName", "Lisa-casual-sitting"),
            "customAvatarName": defaults.get("customAvatarName", ""),
            "photoAvatarName": defaults.get("photoAvatarName", "Anika"),
            "avatarBackgroundImageUrl": defaults.get("avatarBackgroundImageUrl", ""),
        }
    )
    return config


def _strip_realtime_suffix(endpoint: str) -> str:
    endpoint = (endpoint or "").strip().rstrip("/")
    for suffix in ("/voice-live/realtime", "/voice-agent/realtime"):
        if endpoint.endswith(suffix):
            return endpoint[: -len(suffix)]
    return endpoint


def _public_base(request: Request) -> str:
    """HTTPS base URL ACS should call back on (explicit override or request host)."""
    if ACS_CALLBACK_BASE_URL:
        return ACS_CALLBACK_BASE_URL.rstrip("/")
    # Honour the proxy headers ACA sets so we advertise the external ingress.
    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return f"{proto}://{host}".rstrip("/")


def _joiner_build_id() -> str:
    """Short fingerprint of the joiner script currently on disk."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "frontend",
        "acs-join.js",
    )
    try:
        st = os.stat(path)
        return hashlib.sha1(f"{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:12]
    except OSError:
        return "unknown"


def build_acs_router() -> APIRouter:
    """Return an APIRouter exposing the ACS in-call media endpoints."""
    router = APIRouter()

    @router.get("/api/acs/config")
    async def acs_config():
        """Tell the joiner page whether channel C is enabled."""
        return {
            "enabled": ACS_ENABLED,
            "endpoint": ACS_ENDPOINT,
            # Single branding knob (AVATAR_DISPLAY_NAME) so the browser joiner's
            # participant name is never hardcoded.
            "avatarDisplayName": AVATAR_DISPLAY_NAME,
            # Avatar face: when true the joiner sends a branded video tile.
            "avatarVideoEnabled": ACS_AVATAR_VIDEO_ENABLED,
            # ...and when THIS is true that tile carries the live lip-synced
            # avatar (streamed over the media socket) instead of the placard.
            "avatarLiveVideo": ACS_AVATAR_VIDEO_ENABLED and BROWSER_JOIN_VIDEO_ENABLED,
            # Fingerprint of the joiner script actually on disk. The page records
            # this at load and re-checks it; if it changes, the open tab is running
            # code from before the last deploy. That silently invalidated several
            # live test rounds — a tab kept open across a deploy keeps its old JS
            # in memory no matter what the cache headers say, and the telemetry
            # then describes a build that is no longer deployed.
            "buildId": _joiner_build_id(),
        }

    @router.get("/api/acs/status")
    async def acs_status():
        """Live state for the Companion control panel: is Nuru in a call?"""
        return {
            "enabled": ACS_ENABLED,
            "active": len(_ACTIVE_CALLS) > 0,
            "count": len(_ACTIVE_CALLS),
        }

    @router.post("/api/acs/token")
    async def acs_token():
        """Mint a short-lived ACS VoIP identity+token for the browser joiner."""
        if not ACS_ENABLED:
            return JSONResponse({"error": "ACS not configured"}, status_code=503)
        try:
            token = await asyncio.to_thread(acs_client.mint_voip_token)
            return token
        except Exception as e:  # noqa: BLE001
            logger.exception(f"ACS token mint failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @router.post("/api/acs/call")
    async def acs_call(request: Request):
        """Attach Call Automation + media streaming to a call the browser joined.

        Body: ``{"serverCallId": "<id from the browser ACS Calling SDK>"}``.
        """
        if not ACS_ENABLED:
            return JSONResponse({"error": "ACS not configured"}, status_code=503)
        body = await request.json()
        server_call_id = (body.get("serverCallId") or "").strip()
        if not server_call_id:
            return JSONResponse({"error": "serverCallId required"}, status_code=400)

        base = _public_base(request)
        callback_url = f"{base}/api/acs/callback"
        transport_url = f"{base.replace('https://', 'wss://', 1)}/ws/acs/audio"
        try:
            props = await asyncio.to_thread(
                acs_client.connect_to_call, server_call_id, callback_url, transport_url
            )
            call_conn_id = getattr(props, "call_connection_id", None)
            logger.info(
                f"ACS connect_call ok: server_call_id={server_call_id} "
                f"call_connection_id={call_conn_id}"
            )
            return {"callConnectionId": call_conn_id, "status": "connecting"}
        except Exception as e:  # noqa: BLE001
            logger.exception(f"ACS connect_call failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @router.post("/api/acs/callback")
    async def acs_callback(request: Request):
        """ACS Call Automation event webhook (CloudEvents array)."""
        if not ACS_ENABLED:
            return JSONResponse({"error": "ACS not configured"}, status_code=503)
        try:
            events = await request.json()
        except Exception:  # noqa: BLE001
            events = []
        if isinstance(events, dict):
            events = [events]
        for ev in events:
            etype = ev.get("type") or ev.get("eventType") or "unknown"
            logger.info(f"[ACS callback] {etype}")
        return JSONResponse({"status": "ok"})

    @router.websocket("/ws/acs/audio")
    async def acs_audio(websocket: WebSocket):
        """ACS connects here for bidirectional media; bridge it to Voice Live."""
        await websocket.accept()
        if not ACS_ENABLED:
            await websocket.close(code=1011)
            return

        client_id = f"acs-{id(websocket)}"
        logger.info(f"[ACS {client_id}] media socket connected")

        bridge = AcsVoiceBridge(
            websocket, client_id, avatar_video=MEETING_BOT_VIDEO_ENABLED
        )
        endpoint = _strip_realtime_suffix(DEFAULT_ENDPOINT)
        if not endpoint:
            logger.error("AZURE_VOICELIVE_ENDPOINT not set; closing ACS media socket")
            await websocket.close(code=1011)
            return

        handler = VoiceSessionHandler(
            client_id=client_id,
            endpoint=endpoint,
            credential=create_credential(""),
            send_message=bridge.send_message,
            send_binary=bridge.send_binary,
            config=_in_call_config(MEETING_BOT_VIDEO_ENABLED),
        )
        bridge.handler = handler
        _ACTIVE_CALLS.add(client_id)
        handler_task = asyncio.create_task(handler.start())
        try:
            await bridge.pump()
        finally:
            _ACTIVE_CALLS.discard(client_id)
            await handler.stop()
            if not handler_task.done():
                handler_task.cancel()
                try:
                    await handler_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            logger.info(f"[ACS {client_id}] media session ended")

    @router.websocket("/ws/acs/browser")
    async def acs_browser_audio(websocket: WebSocket):
        """Client-side media path: the browser captures the Teams meeting audio
        and streams raw PCM16 here; we bridge it to Voice Live and stream Nuru's
        spoken response back for the browser to play as its outgoing call audio.

        This is the working media path for Teams *meetings* (server-side Call
        Automation media streaming does not deliver Teams-meeting audio).
        """
        await websocket.accept()
        if not ACS_ENABLED:
            await websocket.close(code=1011)
            return

        client_id = f"browser-{id(websocket)}"
        logger.info(f"[browser {client_id}] media socket connected")

        bridge = BrowserVoiceBridge(
            websocket, client_id, avatar_video=BROWSER_JOIN_VIDEO_ENABLED
        )
        endpoint = _strip_realtime_suffix(DEFAULT_ENDPOINT)
        if not endpoint:
            logger.error("AZURE_VOICELIVE_ENDPOINT not set; closing browser media socket")
            await websocket.close(code=1011)
            return

        handler = VoiceSessionHandler(
            client_id=client_id,
            endpoint=endpoint,
            credential=create_credential(""),
            send_message=bridge.send_message,
            send_binary=bridge.send_binary,
            config=_in_call_config(BROWSER_JOIN_VIDEO_ENABLED),
        )
        bridge.handler = handler
        _ACTIVE_CALLS.add(client_id)
        handler_task = asyncio.create_task(handler.start())
        try:
            await bridge.pump()
        finally:
            _ACTIVE_CALLS.discard(client_id)
            await handler.stop()
            if not handler_task.done():
                handler_task.cancel()
                try:
                    await handler_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            logger.info(f"[browser {client_id}] media session ended")

    return router
