"""Offline authoring contracts for the active concise agent prompt."""

import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import setup_foundry_agent as setup
from backend.onboarding import ONBOARDING_ANSWERS

PROMPT = ROOT / "prompts" / "agent" / "instructions.md"
PLACEHOLDERS = {
    "{{AVATAR_NAME}}", "{{SEARCH_TOOL}}", "{{WEB_TOOL}}", "{{ONBOARDING_GUIDANCE}}",
}


class AgentPromptTests(unittest.TestCase):
    def setUp(self):
        self.raw = PROMPT.read_text(encoding="utf-8").strip()

    def render(self, name="Nuru"):
        with patch.dict(os.environ, {"AVATAR_DISPLAY_NAME": name}):
            return setup._apply_brand(self.raw)

    def test_exact_supported_placeholders_and_one_onboarding_expansion(self):
        self.assertEqual(set(re.findall(r"\{\{[^}]+\}\}", self.raw)), PLACEHOLDERS)
        self.assertEqual(self.raw.count("{{ONBOARDING_GUIDANCE}}"), 1)
        self.assertNotIn(r"\_", self.raw)
        rendered = self.render()
        self.assertNotIn("{{", rendered)
        self.assertIsNone(re.search(r"\b(?:SEARCH_TOOL|WEB_TOOL|ONBOARDING_GUIDANCE)\b", rendered))
        self.assertIn(setup.AGENT_SEARCH_TOOL_NAME, rendered)
        self.assertIn(setup.AGENT_WEB_TOOL_NAME, rendered)
        for answer in ONBOARDING_ANSWERS.values():
            self.assertEqual(rendered.count(answer), 1)

    def test_persona_is_dynamic_and_first(self):
        for name in ("Nuru", "Test Assistant"):
            self.assertTrue(self.render(name).startswith(f"You are {name},"))
        self.assertNotIn("You are Nuru", self.raw)

    def test_active_loader_uses_the_bounded_prompt(self):
        with patch.dict(os.environ, {"AVATAR_DISPLAY_NAME": "Nuru"}):
            active = setup._load_prompt("agent", "instructions.md")
        rendered = self.render()
        self.assertLessEqual(len(rendered), 6000)
        self.assertEqual(rendered, active)

    def test_no_policy_specific_wording_or_unsupported_capabilities(self):
        self.assertIsNone(re.search(
            r"\bpolic(?:y|ies)\b|staff rules|eligibility|approval requirements|standing rule|USD(?:50|200|750)",
            self.render(), re.IGNORECASE,
        ))
        self.assertIn("Only meeting minutes and public information are available", self.raw)
        self.assertIn("You cannot access email, calendars", self.raw)

    def test_routing_output_and_accuracy_guards_are_present(self):
        flat = " ".join(self.raw.split())
        for rule in (
            "headline first in a complete sentence of about twelve words",
            "three short sentences and no more than 70 spoken words",
            "before producing any spoken text",
            "never copy or read them aloud",
            "questions about MTN's business do not",
            "dates and titles, not meeting content",
            "Use both tools only when the user explicitly requests",
            "at most once per turn",
            "If results are empty, irrelevant or insufficient",
            "If a required tool is unavailable",
            "session's TODAY date",
            "latest reported completed financial year",
            "Never reconstruct or guess a missing total",
            "financial statements or income statement",
            'If total revenue cannot be verified, begin: "I could not verify total revenue',
            "Only then may you give supported service revenue, clearly labelled",
            "Convert cents to rand exactly once",
            "Never infer units from number formatting",
            "dated or delayed quote from a live price",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, flat)
        self.assertIn("\u3010source-id\u2020source\u3011", self.raw)


if __name__ == "__main__":
    unittest.main()
