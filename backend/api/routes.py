"""HTTP endpoints (health + frontend config bootstrap)."""

from fastapi import APIRouter

from .. import audit
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
    """Liveness probe, plus whether the audit trail can be trusted.

    Audit state is reported here because a degraded trail is otherwise visible
    only in a single startup log line, and the whole failure mode is that the
    system looks healthy while discarding records.

    Two deliberate choices. It does not change ``status``: this is the probe
    Container Apps restarts on, and restarting fixes neither a missing role
    assignment nor a firewall rule — it would just loop. And ``degraded`` is a
    boolean rather than the reason, because the reason carries endpoint names
    and Azure error text, which do not belong on an unauthenticated endpoint.
    The reason is logged.
    """
    body = {"status": "healthy", "service": "avatar-forge"}
    state = audit.stats()
    if state.get("enabled") or state.get("degraded"):
        body["audit"] = {
            "sink": state.get("sink"),
            "degraded": bool(state.get("degraded")),
            # Distinct from `degraded`, which is about the sink we ended up
            # with. This is about records that were accepted and then lost —
            # the failure the counters used to report as a clean zero.
            "lossy": bool(state.get("lossy")),
        }
    return body


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
