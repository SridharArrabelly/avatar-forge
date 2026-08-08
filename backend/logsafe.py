"""Keeping conversation content out of operational telemetry.

Container stdout is collected into Log Analytics, and will additionally reach
Application Insights once telemetry ships. Those are *operational* stores:
broad team access, dashboards, export, and a retention window chosen for
debugging convenience rather than for compliance.

Conversation content does not belong there. It belongs in the audit trail,
which is opt-in, access-controlled, redacted and retained deliberately. A
question logged to stdout bypasses every one of those controls — including
``ENABLE_AUDIT=false``, which a deployment may well have chosen precisely
because it does not want conversations recorded.

This module exists so that taking content *out* of a log line does not also
take the debuggability out of it. The two things an operator actually needs
from these lines are "how big was it" and "is this the same one again", and
both survive without the text.
"""

import hashlib
from typing import Any, Optional

_FP_CHARS = 8


def fingerprint(value: Optional[str]) -> str:
    """A stable, content-free handle for a piece of user text.

    Renders as ``len=37 fp=9c1a4be2``. Identical inputs fingerprint alike, so
    "this same query failed six times" and "the user repeated themselves"
    remain visible in logs while the words do not.

    **This is not a security boundary.** Conversational text is short and
    low-entropy, so a fingerprint is guessable by anyone willing to hash a
    candidate list. It is not pretending otherwise. What it defends against is
    the real and current exposure: content sitting in ops telemetry by default,
    read by everyone with workspace access and carried into every export.
    Anything needing an actual confidentiality guarantee belongs in the audit
    trail, not in a log line.
    """
    if value is None:
        return "len=0 fp=none"
    text = value if isinstance(value, str) else str(value)
    if not text:
        return "len=0 fp=empty"
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    return f"len={len(text)} fp={digest[:_FP_CHARS]}"


def keys_only(value: Any) -> str:
    """Describe a tool-call argument payload by its shape, never its values.

    Argument *names* come from our own tool schema, so they carry no user
    content and are the part with diagnostic value — a missing or misspelled
    key is the usual wiring bug. The values are the user's question.
    """
    payload = value
    if isinstance(payload, (str, bytes)):
        try:
            import json

            payload = json.loads(payload)
        except Exception:
            return fingerprint(value if isinstance(value, str) else None)
    if isinstance(payload, dict):
        if not payload:
            return "keys=[]"
        return f"keys={sorted(str(k) for k in payload)}"
    return f"type={type(payload).__name__}"
