"""Offline checks for starter questions and their environment overrides."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

with patch.dict(os.environ, {"PYTHON_DOTENV_DISABLED": "1"}, clear=True):
    from backend.config import get_ui_defaults


DEFAULT_PROMPTS = [
    "Summarise the latest board meeting",
    "What actions were agreed at the latest meeting?",
    "What is MTN's latest share price?",
]


def main() -> int:
    for binding in ("agent", "model"):
        for raw in (None, "", " | | "):
            env = {"PYTHON_DOTENV_DISABLED": "1", "VOICE_BINDING": binding}
            if raw is not None:
                env["SUGGESTED_PROMPTS"] = raw
            with patch.dict(os.environ, env, clear=True):
                defaults = get_ui_defaults()
                assert defaults["suggestedPrompts"] == DEFAULT_PROMPTS
                assert defaults["enableSuggestedPrompts"] is True
        with patch.dict(os.environ, {
            "PYTHON_DOTENV_DISABLED": "1",
            "VOICE_BINDING": binding,
            "SUGGESTED_PROMPTS": "  Ask about a meeting | | Ask about revenue  ",
            "ENABLE_SUGGESTED_PROMPTS": "false",
        }, clear=True):
            defaults = get_ui_defaults()
            assert defaults["suggestedPrompts"] == [
                "Ask about a meeting", "Ask about revenue",
            ]
            assert defaults["enableSuggestedPrompts"] is False
    print("PASS starter defaults, overrides, and opt-out in both voice bindings")

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "# SUGGESTED_PROMPTS=" + "|".join(DEFAULT_PROMPTS) in example
    docs = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    assert r"\|".join(DEFAULT_PROMPTS) in docs
    print("PASS documented starter questions match runtime defaults")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
