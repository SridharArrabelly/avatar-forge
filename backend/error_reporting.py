"""User-facing error details for Voice Live session failures."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def describe_error(value: Any, fallback: str) -> str:
    """Extract the useful message and code from SDK exceptions or error events."""
    details = _field(value, "error") or value
    message = (
        _field(details, "message")
        or _field(details, "detail")
        or _field(details, "reason")
    )
    if not message:
        message = str(details).strip() or str(value).strip()
    if not message:
        return fallback

    code = _field(details, "code")
    if code and str(code).lower() not in str(message).lower():
        return f"{message} (code: {code})"
    return str(message)


def describe_session_start_error(error: Any, config: Mapping[str, Any]) -> str:
    """Describe startup failures, including actionable custom-avatar guidance."""
    generic = "Voice Live could not start the session."
    message = describe_error(error, "")
    if message:
        if (
            "session.input_audio_transcription.model" in message
            and "mai-transcribe-2" in message
        ):
            return (
                "Speech recognition model 'mai-transcribe-2' is not supported by "
                "Voice Live. Set SR_MODEL=mai-transcribe."
            )
        return message

    if config.get("avatarEnabled"):
        avatar_name = str(config.get("avatarName") or "").strip() or "the configured avatar"
        modality = "photo" if config.get("isPhotoAvatar") else "video"
        expected_type = f"custom-{modality}"
        return (
            f"Voice Live could not start avatar '{avatar_name}'. If this is a custom "
            f"avatar, set AVATAR_TYPE={expected_type} and verify that the model exists "
            "in the same Speech resource used by AZURE_VOICELIVE_ENDPOINT."
        )
    return generic
