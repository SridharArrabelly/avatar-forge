"""HTTP endpoints (health + frontend config bootstrap)."""

from fastapi import APIRouter

from ..config import (
    DEFAULT_VOICE,
    DEVELOPER_MODE,
    VOICE_BINDING,
    VOICELIVE_MODEL,
    get_ui_defaults,
)

router = APIRouter()


@router.get("/health")
async def health_check():
    """Liveness probe."""
    return {"status": "healthy", "service": "avatar-forge"}


@router.get("/api/config")
async def get_config():
    """Return default configuration to the frontend."""
    return {
        "voice": DEFAULT_VOICE,
        "developerMode": DEVELOPER_MODE,
        "defaults": get_ui_defaults(),
        # Which brain is answering. Surfaced because the two bindings differ in
        # capability as well as latency — model mode answers from the minutes
        # only — so anyone reading a timing number needs to know which one
        # produced it.
        "voiceBinding": VOICE_BINDING,
        "voiceLiveModel": VOICELIVE_MODEL if VOICE_BINDING == "model" else "",
    }
