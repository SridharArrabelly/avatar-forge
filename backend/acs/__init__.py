"""Azure Communication Services in-call media participant (channel D, issue #27).

Additive and opt-in: nothing in this package runs unless ACS is configured
(``ACS_ENABLED``). The standalone web app, the channel B personal tab, and the
channel C chat bot are unaffected when ACS is disabled.

Two ways the avatar gets into a meeting, both ending at the same Voice Live
session — see ``docs/channels/d-in-call-media-bot.md`` for the full design:

    1. MEDIA BOT (hears everyone).  A .NET service on a Windows VM uses the Graph
       Real-Time Media SDK to join as a proper calling bot, and streams the
       meeting's mixed audio here:

           .NET media bot  <--wss-->  /ws/acs/audio  <-->  AcsVoiceBridge
                                                             <--> VoiceSessionHandler

    2. BROWSER JOINER (hears only the operator).  ACS Call Automation has no
       "join a Teams meeting by URL" API, so /acs-join.html loads the ACS Calling
       Web SDK from a CDN (no Node toolchain on the server) and joins as an
       anonymous interop guest, governed by the meeting lobby — which is why this
       leg needs no Teams-admin action:

           browser joins -> emits ServerCallId -> POST /api/acs/call
                         -> connect_call() -> /ws/acs/browser -> BrowserVoiceBridge

See ``backend/acs/bridge.py`` for the media bridges and ``backend/acs/routes.py``
for the HTTP/WebSocket endpoints.
"""

from .bridge import AcsVoiceBridge
from .routes import build_acs_router

__all__ = ["AcsVoiceBridge", "build_acs_router"]
