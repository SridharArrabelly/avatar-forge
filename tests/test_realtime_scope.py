"""Offline checks for minutes/web scope and legacy document formatting.

Run from the repository root:

    uv run python tests/test_realtime_scope.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backend.voice.instructions as instructions  # noqa: E402
import backend.voice.tools as tools  # noqa: E402
from backend.document_titles import display_document_title  # noqa: E402


failures: list[str] = []
checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


class FakeResults:
    def __init__(self, rows: list[dict]):
        self._rows = iter(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._rows)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeSearchClient:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def search(self, **kwargs):
        self.kwargs = kwargs
        return FakeResults(
            [
                {
                    "title": (
                        "G004 Group Gift, Hospitality & Entertainment "
                        "Policy final_signed"
                    ),
                    "documentType": "Policy",
                    "meeting_date": None,
                    "content": (
                        "Type: Policy\nGift acceptance above USD50 is prohibited."
                    ),
                }
            ]
        )


async def exercise_search() -> tuple[dict, FakeSearchClient]:
    fake = FakeSearchClient()
    original = tools.get_search_client
    try:
        tools.get_search_client = lambda: fake
        result = await tools.search_minutes("supplier gift acceptance limit")
    finally:
        tools.get_search_client = original
    return result, fake


def main() -> int:
    print("1. Realtime prompt describes only available sources")
    saved_name = os.environ.get("AVATAR_DISPLAY_NAME")
    try:
        os.environ["AVATAR_DISPLAY_NAME"] = "Nuru"
        instructions._load_body.cache_clear()
        prompt = instructions.load_realtime_instructions()
    finally:
        instructions._load_body.cache_clear()
        if saved_name is None:
            os.environ.pop("AVATAR_DISPLAY_NAME", None)
        else:
            os.environ["AVATAR_DISPLAY_NAME"] = saved_name
    flat_prompt = " ".join(prompt.split())

    check("persona is substituted", prompt.startswith("You are Nuru,"))
    check("all placeholders are resolved", "{{" not in prompt)
    check("internal tool name is real", tools.SEARCH_MINUTES_TOOL["name"] in prompt)
    check("web tool name is real", tools.SEARCH_WEB_TOOL["name"] in prompt)
    check("first spoken words must be grounded",
          "first spoken words must be the grounded answer" in flat_prompt
          and '"I\'ll check"' in flat_prompt)
    check("minutes are the internal source", "Type: MeetingMinutes" in prompt)
    check("no policy corpus or embedded gift rules",
          not re.search(r"\bpolic(?:y|ies)\b|USD(?:50|200|750)", prompt, re.I))
    check("unavailable internal sources refuse invention",
          "Other internal sources are unavailable" in flat_prompt
          and "Do not search the public web as a substitute or invent requirements" in flat_prompt)
    check("getting started explains the interface",
          "Tap the microphone and speak" in prompt and "type your question" in prompt)

    print("\n2. Registered tool advertises meeting minutes only")
    description = tools.SEARCH_MINUTES_TOOL["description"].lower()
    check("tool description excludes policies", "polic" not in description)
    check("tool description includes minutes", "minutes" in description)
    check("tool description excludes rule intents",
          not any(term in description for term in ("eligibility", "compliance")))

    print("\n3. Legacy document-title formatting remains compatible")
    check(
        "gift policy title",
        display_document_title(
            "G004 Group Gift, Hospitality & Entertainment Policy", "Policy"
        ) == "Group Gift, Hospitality & Entertainment Policy",
    )
    check(
        "IP policy version suffix",
        display_document_title(
            "MTN Group IP Policy November 2025 final_signed", "Policy"
        ) == "MTN Group IP Policy",
    )
    check(
        "bursary separators",
        display_document_title(
            "MTN-MANCO-Bursary-Policy-final_signed", "Policy"
        ) == "MTN MANCO Bursary Policy",
    )
    check(
        "meeting title is untouched",
        display_document_title("Board Meeting - 15 February 2026", "MeetingMinutes")
        == "Board Meeting - 15 February 2026",
    )

    print("\n4. Tool results preserve document type metadata")
    result, fake = asyncio.run(exercise_search())
    passages = result.get("passages", [])
    check("search requested documentType",
          "documentType" in fake.kwargs.get("select", []),
          repr(fake.kwargs.get("select")))
    check("one passage returned", len(passages) == 1, repr(passages))
    passage = passages[0] if passages else {}
    check("policy type reaches the model", passage.get("type") == "Policy", repr(passage))
    check("human document title reaches the model",
          passage.get("title") == "Group Gift, Hospitality & Entertainment Policy",
          repr(passage))
    check("legacy meeting-only result key removed", "meeting" not in passage, repr(passage))

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
