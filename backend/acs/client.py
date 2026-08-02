"""ACS client factories and media-streaming option builders (channel D).

The Call Automation and Identity SDKs are **synchronous** and the FastAPI app is
async, so the one-shot control calls (mint a token, ``connect_call``) are run in a
thread via ``asyncio.to_thread`` by the callers in ``routes.py``.

All imports of ``azure.communication.*`` are lazy so the module imports cleanly
(and the app boots) even if the optional ACS SDKs are not installed or ACS is
disabled — the same additive guardrail every optional channel here follows.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import (
    ACS_AUDIO_SAMPLE_RATE,
    ACS_CONNECTION_STRING,
    ACS_ENABLED,
    ACS_ENDPOINT,
)

logger = logging.getLogger(__name__)


def acs_configured() -> bool:
    """True when channel D is configured (endpoint or connection string present)."""
    return ACS_ENABLED


def get_call_automation_client() -> Any:
    """Build a ``CallAutomationClient`` from connection string or endpoint+Entra.

    Prefers ``ACS_CONNECTION_STRING`` (simplest); otherwise uses ``ACS_ENDPOINT``
    with ``DefaultAzureCredential`` (the same managed-identity chain the rest of
    the backend uses). Raises if neither is configured.
    """
    from azure.communication.callautomation import CallAutomationClient

    if ACS_CONNECTION_STRING:
        return CallAutomationClient.from_connection_string(ACS_CONNECTION_STRING)
    if ACS_ENDPOINT:
        from azure.identity import DefaultAzureCredential

        return CallAutomationClient(ACS_ENDPOINT, DefaultAzureCredential())
    raise RuntimeError(
        "ACS not configured: set ACS_CONNECTION_STRING or ACS_ENDPOINT."
    )


def get_identity_client() -> Any:
    """Build a ``CommunicationIdentityClient`` for minting browser-joiner tokens."""
    from azure.communication.identity import CommunicationIdentityClient

    if ACS_CONNECTION_STRING:
        return CommunicationIdentityClient.from_connection_string(
            ACS_CONNECTION_STRING
        )
    if ACS_ENDPOINT:
        from azure.identity import DefaultAzureCredential

        return CommunicationIdentityClient(ACS_ENDPOINT, DefaultAzureCredential())
    raise RuntimeError(
        "ACS not configured: set ACS_CONNECTION_STRING or ACS_ENDPOINT."
    )


def mint_voip_token() -> dict:
    """Create a fresh ACS identity + VoIP access token for the browser joiner.

    The browser ACS Calling Web SDK uses this token to join the Teams meeting as
    an anonymous interop guest. Returns ``{"userId", "token", "expiresOn"}``.
    Synchronous — call via ``asyncio.to_thread``.
    """
    from azure.communication.identity import CommunicationTokenScope

    client = get_identity_client()
    user = client.create_user()
    result = client.get_token(user, [CommunicationTokenScope.VOIP])
    # ``expires_on`` may be a datetime, an int epoch, or an ISO string depending on
    # the SDK version — normalise defensively (the joiner only needs ``token``).
    expires = getattr(result, "expires_on", None)
    if expires is None:
        expires_str = ""
    elif hasattr(expires, "isoformat"):
        expires_str = expires.isoformat()
    else:
        expires_str = str(expires)
    return {
        "userId": user.properties.get("id", ""),
        "token": result.token,
        "expiresOn": expires_str,
    }


def build_media_streaming_options(transport_url: str) -> Any:
    """Build ``MediaStreamingOptions`` for bidirectional MIXED audio.

    MIXED = the whole meeting mixed into one stream (what we want — the avatar
    hears the room, not per-speaker channels). 16-bit PCM mono at the configured
    rate (24 kHz by default, matching Voice Live so no resampling is needed).
    """
    from azure.communication.callautomation import (
        AudioFormat,
        MediaStreamingAudioChannelType,
        MediaStreamingContentType,
        MediaStreamingOptions,
        StreamingTransportType,
    )

    audio_format = (
        AudioFormat.PCM16_K_MONO
        if ACS_AUDIO_SAMPLE_RATE == 16000
        else AudioFormat.PCM24_K_MONO
    )
    return MediaStreamingOptions(
        transport_url=transport_url,
        transport_type=StreamingTransportType.WEBSOCKET,
        content_type=MediaStreamingContentType.AUDIO,
        audio_channel_type=MediaStreamingAudioChannelType.MIXED,
        start_media_streaming=True,
        enable_bidirectional=True,
        audio_format=audio_format,
    )


def connect_to_call(server_call_id: str, callback_url: str, transport_url: str) -> Any:
    """Attach Call Automation + bidirectional media to an existing call.

    ``server_call_id`` comes from the browser joiner once it has joined the Teams
    meeting. Synchronous — call via ``asyncio.to_thread``.
    """
    from azure.communication.callautomation import ServerCallLocator

    client = get_call_automation_client()
    media_opts = build_media_streaming_options(transport_url)
    return client.connect_call(
        callback_url=callback_url,
        call_locator=ServerCallLocator(server_call_id=server_call_id),
        media_streaming=media_opts,
    )
