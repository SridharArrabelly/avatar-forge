"""Offline checks for canonical avatar selection and persona naming."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.avatar_identity import (  # noqa: E402
    active_avatar_model,
    avatar_model,
    avatar_type,
    resolve_avatar_display_name,
)

_ENV_KEYS = ("AVATAR_DISPLAY_NAME", "AVATAR_TYPE", "AVATAR_MODEL", "ACS_WAKE_PHRASES")
_failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {label}: {got!r}")
    else:
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
        _failures.append(label)


def main() -> int:
    print("1. Canonical selection")
    check("standard photo type", avatar_type({"AVATAR_TYPE": "standard-photo"}), "standard-photo")
    check("canonical model", avatar_model({"AVATAR_TYPE": "standard-photo", "AVATAR_MODEL": "Simone"}), "Simone")
    check("custom model", avatar_model({"AVATAR_TYPE": "custom-photo", "AVATAR_MODEL": "Nuru"}), "Nuru")
    check("invalid type uses default", avatar_type({"AVATAR_TYPE": "old"}), "standard-video")
    check("missing standard photo model uses default",
          avatar_model({"AVATAR_TYPE": "standard-photo"}), "Anika")

    print("\n2. Display name")
    check("explicit branding wins",
          resolve_avatar_display_name({"AVATAR_DISPLAY_NAME": "Nuru",
                                        "AVATAR_TYPE": "standard-photo",
                                        "AVATAR_MODEL": "Simone"}), "Nuru")
    check("model name is friendly",
          resolve_avatar_display_name({"AVATAR_TYPE": "standard-video",
                                       "AVATAR_MODEL": "Lisa-casual-sitting"}), "Lisa")
    check("empty custom model falls back safely",
          resolve_avatar_display_name({"AVATAR_TYPE": "custom-photo"}), "Avatar")

    print("\n3. Prompt and config parity")
    saved = {key: os.environ.pop(key, None) for key in _ENV_KEYS}
    try:
        os.environ.update({
            "AVATAR_TYPE": "standard-photo",
            "AVATAR_MODEL": "Simone",
        })
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import setup_foundry_agent as sfa

        prompt = sfa._load_prompt("agent", "instructions.md")
        check("prompt has resolved name", prompt.splitlines()[0].startswith("You are Simone,"), True)
        check("prompt has no placeholder", "{{AVATAR_NAME}}" in prompt, False)

        for mod in [m for m in sys.modules if m.startswith("backend.config")]:
            del sys.modules[mod]
        from backend import config as cfg

        check("config name matches resolver",
              cfg.AVATAR_DISPLAY_NAME, resolve_avatar_display_name())
        defaults = cfg.get_ui_defaults()
        check("UI type is canonical", defaults["avatarType"], "standard-photo")
        check("UI model is canonical", defaults["avatarModel"], "Simone")
        check("active model agrees", active_avatar_model(), "Simone")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
