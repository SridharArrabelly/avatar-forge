"""The assistant's persona name — ONE rule, shared by every surface.

The name the avatar calls itself ("I'm Simone") has to match the name printed on
the stage, in the Teams roster, in the bot's welcome message and in the wake
phrase. Each of those used to resolve the name independently, and they
disagreed: the web stage derived a friendly name from the *selected avatar
model*, while the Foundry agent prompt, the bot, the wake phrase and the meeting
roster all fell back to the literal ``"Avatar"``. A deployment showing "Simone"
on screen therefore introduced itself as "Avatar".

Avatar selection uses the canonical ``AVATAR_TYPE`` and ``AVATAR_MODEL`` pair.
Display-name resolution then uses:

1. ``AVATAR_DISPLAY_NAME`` — the explicit branding knob. Used verbatim.
2. The leading segment of the active avatar model.
3. ``Avatar`` — last-resort literal, so a half-configured deployment still has a
   name instead of a blank one.

Deliberately free of imports and side effects (no dotenv, no logging, no network)
so the deploy-time scripts can share it with the runtime backend. In particular
it must NOT pull in ``backend.config``, whose module-level ``load_dotenv(override=True)``
would let a stale local ``.env`` override the azd environment inside a deploy hook.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# Last-resort persona name for an incomplete custom-avatar configuration.
DEFAULT_AVATAR_NAME = "Avatar"

# Defaults for the avatar model variables. These live here rather than in
# config.get_ui_defaults() because the resolver has to agree with what the UI
# actually renders — two copies of a default is exactly how the name drifted
# apart in the first place.
DEFAULT_STANDARD_AVATAR = "Lisa-casual-sitting"
DEFAULT_PHOTO_AVATAR = "Anika"
DEFAULT_AVATAR_TYPE = "standard-video"
AVATAR_TYPES = (
    "standard-video",
    "standard-photo",
    "custom-video",
    "custom-photo",
)

def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def avatar_type(env: Mapping[str, str] | None = None) -> str:
    """Return the configured avatar type, or the standard-video default."""
    env = _env(env)
    configured = (env.get("AVATAR_TYPE") or "").strip().lower()
    if configured in AVATAR_TYPES:
        return configured
    return DEFAULT_AVATAR_TYPE


def avatar_model(env: Mapping[str, str] | None = None) -> str:
    """Return the configured model, or the default for the selected type."""
    env = _env(env)
    configured = (env.get("AVATAR_MODEL") or "").strip()
    if configured:
        return configured
    kind = avatar_type(env)
    if kind.startswith("custom-"):
        return ""
    if kind == "standard-photo":
        return DEFAULT_PHOTO_AVATAR
    return DEFAULT_STANDARD_AVATAR


def active_avatar_model(env: Mapping[str, str] | None = None) -> str:
    """Return the avatar model actually in use."""
    return avatar_model(env)


def resolve_avatar_display_name(env: Mapping[str, str] | None = None) -> str:
    """Return the persona name every surface should use. Never empty.

    Reads the environment on every call: callers in the deploy scripts run before
    ``load_dotenv()``, so an import-time snapshot would silently ignore ``.env``.
    """
    env = _env(env)
    explicit = (env.get("AVATAR_DISPLAY_NAME") or "").strip()
    if explicit:
        return explicit
    # Strip model suffixes ("-casual-sitting", "-business") for a friendly name.
    derived = active_avatar_model(env).split("-")[0].strip()
    return derived or DEFAULT_AVATAR_NAME
