"""The assistant's persona name — ONE rule, shared by every surface.

The name the avatar calls itself ("I'm Simone") has to match the name printed on
the stage, in the Teams roster, in the bot's welcome message and in the wake
phrase. Each of those used to resolve the name independently, and they
disagreed: the web stage derived a friendly name from the *selected avatar
model*, while the Foundry agent prompt, the bot, the wake phrase and the meeting
roster all fell back to the literal ``"Avatar"``. A deployment showing "Simone"
on screen therefore introduced itself as "Avatar".

Resolution order:

1. ``AVATAR_DISPLAY_NAME`` — the explicit branding knob. Used verbatim.
2. The leading segment of the *active* avatar model: ``Lisa-casual-sitting`` ->
   ``Lisa``, ``Simone`` -> ``Simone``. Which variable is active follows the same
   ``IS_*`` gates the UI applies, and that gating is what makes deriving from the
   model safe: ``CUSTOM_AVATAR_NAME`` is a Speech custom-avatar *model id* that is
   only meaningful when ``IS_CUSTOM_AVATAR=true`` and is empty or stale
   otherwise, so it is never consulted unless that gate is on.
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

# Last-resort persona name. Only reached when the branding knob is unset AND the
# active avatar model is empty (i.e. IS_CUSTOM_AVATAR=true with no model id set).
DEFAULT_AVATAR_NAME = "Avatar"

# Defaults for the avatar model variables. These live here rather than in
# config.get_ui_defaults() because the resolver has to agree with what the UI
# actually renders — two copies of a default is exactly how the name drifted
# apart in the first place.
DEFAULT_STANDARD_AVATAR = "Lisa-casual-sitting"
DEFAULT_PHOTO_AVATAR = "Anika"

# Same truthy spellings backend.config._bool accepts, so a value that switches the
# avatar model on also switches the name derivation.
_TRUE_VALUES = ("1", "true", "yes", "on")


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _flag(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in _TRUE_VALUES


def active_avatar_model(env: Mapping[str, str] | None = None) -> str:
    """Return the avatar model actually in use, per the ``IS_*`` gates.

    Mirrors the precedence the frontend applies when it picks which model field
    to read (custom > photo > standard).
    """
    env = _env(env)
    if _flag(env, "IS_CUSTOM_AVATAR"):
        # No default: a custom avatar with no model id is a misconfiguration, and
        # the caller falls back to DEFAULT_AVATAR_NAME rather than inventing one.
        return (env.get("CUSTOM_AVATAR_NAME") or "").strip()
    # Strip before testing for empty so a blank value means "unset" here exactly as
    # it does everywhere else — treating "" and "   " differently would be an
    # accident, not a decision.
    if _flag(env, "IS_PHOTO_AVATAR"):
        return (env.get("PHOTO_AVATAR_NAME") or "").strip() or DEFAULT_PHOTO_AVATAR
    return (env.get("AVATAR_NAME") or "").strip() or DEFAULT_STANDARD_AVATAR


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
