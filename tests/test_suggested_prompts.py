"""Offline checks for starter questions and their environment overrides."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

with patch.dict(os.environ, {"PYTHON_DOTENV_DISABLED": "1"}, clear=True):
    from backend.config import get_ui_defaults
    from backend.onboarding import ONBOARDING_ANSWERS, ONBOARDING_PLACEHOLDER, expand_onboarding


DEFAULT_PROMPTS = [
    "What can you help me with?",
    "Tell me about your services",
    "How do I get started?",
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

    sys.path.insert(0, str(ROOT / "scripts"))
    import setup_foundry_agent as agent
    from backend.voice import instructions as realtime

    with patch.dict(os.environ, {"AVATAR_DISPLAY_NAME": "Nuru"}, clear=True):
        realtime._load_body.cache_clear()
        try:
            prompts = [
                agent._load_prompt("agent", "instructions.md"),
                realtime.load_realtime_instructions(),
            ]
            with patch.object(realtime, "_load_body", return_value=realtime.FALLBACK):
                prompts.append(realtime.load_realtime_instructions())
        finally:
            realtime._load_body.cache_clear()
    assert list(ONBOARDING_ANSWERS) == DEFAULT_PROMPTS
    for prompt in prompts:
        assert prompt.startswith("You are Nuru,")
        assert "{{" not in prompt
        assert not re.search(r"\bpolic(?:y|ies)\b|USD(?:50|200|750)", prompt, re.I)
        for question, answer in ONBOARDING_ANSWERS.items():
            assert question in prompt and answer in prompt
        assert "without calling a tool" in prompt
        assert "Do not ask what the user wants to get started with" in prompt
    assert expand_onboarding("No shared marker.") == "No shared marker."
    assert ONBOARDING_PLACEHOLDER not in expand_onboarding(ONBOARDING_PLACEHOLDER)
    print("PASS shared spoken replies, scope and substitutions in both prompts and fallback")

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "# SUGGESTED_PROMPTS=" + "|".join(DEFAULT_PROMPTS) in example
    docs = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    assert r"\|".join(DEFAULT_PROMPTS) in docs
    print("PASS documented starter questions match runtime defaults")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
