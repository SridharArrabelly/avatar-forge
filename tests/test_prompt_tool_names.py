"""Offline check: one authored prompt names the tools each binding really has.

Needs **no Azure resources and no credentials**. Runs in well under a second.

Why it exists: the two voice bindings register *different* tool names, and a
single prompt is authored for both.

======================  =============================================
binding                 tools the model can actually call
======================  =============================================
agent (Foundry)         ``azure_ai_search``, ``bing_custom_search``
model (``gpt-realtime``) ``search_minutes``, ``search_web`` (Web IQ)
======================  =============================================

A prompt naming either set *literally* is therefore correct in one mode and
wrong in the other — it would describe two tools that do not exist while the
two real ones go undescribed. That degrades routing for a reason unrelated to
prompt quality, so any A/B measurement taken against it scores the defect
rather than the prompt. The prompt uses ``{{SEARCH_TOOL}}``/``{{WEB_TOOL}}``
and each loader substitutes its own pair, mirroring ``{{AVATAR_NAME}}``.

What it pins:

* every ``{{PLACEHOLDER}}`` in ``prompts/`` is one a loader actually
  substitutes — a new placeholder nobody wired reaches the model verbatim
* model mode resolves to the names on the **registered tool schemas**, so a
  rename in ``tools.py`` cannot silently desynchronise the prompt
* agent mode resolves to the names its tools are really **created** with,
  read out of ``create_agent`` itself rather than restated here
* neither mode leaks the other's names into the prompt it sends

Run from the repo root::

    uv run python tests/test_prompt_tool_names.py
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import setup_foundry_agent as agent_setup  # noqa: E402
from backend.voice.instructions import load_realtime_instructions  # noqa: E402
from backend.voice.tools import SEARCH_MINUTES_TOOL, SEARCH_WEB_TOOL  # noqa: E402

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")

# Substituted by _apply_brand (agent) and load_realtime_instructions (model).
KNOWN_PLACEHOLDERS = {"{{AVATAR_NAME}}", "{{SEARCH_TOOL}}", "{{WEB_TOOL}}"}

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


def main() -> int:
    print("Placeholders are all wired to a loader")
    for md in sorted((ROOT / "prompts").rglob("*.md")):
        found = set(PLACEHOLDER_RE.findall(md.read_text(encoding="utf-8")))
        unknown = found - KNOWN_PLACEHOLDERS
        check(
            f"{md.relative_to(ROOT).as_posix()}",
            not unknown,
            f"unsubstituted: {sorted(unknown)}",
        )

    model_search = SEARCH_MINUTES_TOOL["name"]
    model_web = SEARCH_WEB_TOOL["name"]
    agent_search = agent_setup.AGENT_SEARCH_TOOL_NAME
    agent_web = agent_setup.AGENT_WEB_TOOL_NAME

    print("\nAgent constants match how the tools are actually created")
    src = inspect.getsource(agent_setup.build_tools) + inspect.getsource(
        agent_setup.build_bing_tool
    )
    # The SDK kwargs are the tool surface; the web tool's kwarg carries a
    # `_preview` suffix that the prompt has never used, hence the \w* .
    check(
        f"{agent_search!r} is a tool-construction kwarg",
        re.search(rf"\b{re.escape(agent_search)}\w*=", src) is not None,
    )
    check(
        f"{agent_web!r} prefixes a tool-construction kwarg",
        re.search(rf"\b{re.escape(agent_web)}\w*=", src) is not None,
    )

    print("\nThe two bindings register different names (the reason for this file)")
    check("agent != model search tool", agent_search != model_search)
    check("agent != model web tool", agent_web != model_web)

    print("\nModel mode substitutes its own pair")
    live_model = load_realtime_instructions()
    check("live model prompt has no unsubstituted placeholder", "{{" not in live_model)

    # Drive the REAL loader against a fixture, rather than restating the
    # substitution here: a check that re-implements the logic it is testing
    # passes happily when the logic is deleted.
    #
    # _load_body is @lru_cache'd, so the call above has already cached the live
    # body — without cache_clear() the patched path is never read and this whole
    # section silently tests nothing.
    import backend.voice.instructions as model_loader

    fixture = ROOT / "prompts" / "realtime" / ".tool_name_fixture.md"
    original_path = model_loader.PROMPT_PATH
    try:
        fixture.write_text(
            "{{AVATAR_NAME}} uses {{SEARCH_TOOL}} and {{WEB_TOOL}}.\n",
            encoding="utf-8",
        )
        model_loader.PROMPT_PATH = fixture
        model_loader._load_body.cache_clear()
        model_out = load_realtime_instructions()
    finally:
        model_loader.PROMPT_PATH = original_path
        model_loader._load_body.cache_clear()
        fixture.unlink(missing_ok=True)

    check(
        "fixture was actually read (guards against a cached no-op)",
        "uses" in model_out and len(model_out) < 200,
        f"got {model_out[:80]!r}",
    )

    check("no unsubstituted placeholder", "{{" not in model_out, f"got {model_out!r}")
    check(f"model resolves search -> {model_search!r}", model_search in model_out)
    check(f"model resolves web -> {model_web!r}", model_web in model_out)
    check(
        "model leaks no agent names",
        agent_search not in model_out and agent_web not in model_out,
    )

    print("\nAgent mode substitutes its own pair")
    agent_out = agent_setup._apply_brand(
        "{{AVATAR_NAME}} uses {{SEARCH_TOOL}} and {{WEB_TOOL}}.\n"
    )
    check("no unsubstituted placeholder", "{{" not in agent_out)
    check(f"agent resolves search -> {agent_search!r}", agent_search in agent_out)
    check(f"agent resolves web -> {agent_web!r}", agent_web in agent_out)
    check(
        "agent leaks no model names",
        model_search not in agent_out and model_web not in agent_out,
    )

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
