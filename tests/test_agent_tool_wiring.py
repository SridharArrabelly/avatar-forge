"""Offline check: the agent's REQUIRED vs OPTIONAL tools degrade correctly.

Unlike the other ``scripts/test_*.py`` smoke tests, this one needs **no Azure
resources and no credentials** — it drives ``setup_foundry_agent.create_agent``
against a fake project client. It runs in about a second.

Why it exists: "Bing is optional" used to mean *"is the variable empty?"* rather
than *"does the connection exist?"*. Naming a connection that wasn't in the
project raised straight out of the SDK, so no agent was created — and because the
postprovision hook downgraded that to a warning, the deploy still reported
success. The result was a fully provisioned app that could not answer. Copying a
``.env`` between environments was enough to trigger it.

What it pins:

* a Bing connection that is named but absent  -> degrade, agent still created
* a Bing connection that resolves             -> web tool wired
* Bing vars unset                             -> degrade
* the AI Search connection absent             -> fatal, because it is the corpus

Run from the repo root:

    uv run python tests/test_agent_tool_wiring.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from azure.core.exceptions import ResourceNotFoundError

# setup_foundry_agent.py is a script, not a package module, so load it by path.
# It imports its sibling `rbac_propagation` by bare name, which only resolves when
# scripts/ is on sys.path -- true when azd runs it from there, not when we load it
# from tests/. Put scripts/ on the path first or the exec below fails on the import.
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

_SCRIPT = _SCRIPTS / "setup_foundry_agent.py"
_spec = importlib.util.spec_from_file_location("setup_foundry_agent", _SCRIPT)
sfa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sfa)


class _Conn:
    def __init__(self, name: str) -> None:
        self.id = f"/connections/{name}"
        self.name = name  # the real SDK object carries this; _find_connection matches on it


class _Agent:
    id, name, version = "agent-fake", "TestAgent", "1"


class _Agents:
    tools: list = []

    def create_version(self, **kwargs):
        _Agents.tools = kwargs["definition"].tools
        return _Agent()


class _Connections:
    """Resolves only the connections the caller says exist.

    Models BOTH surfaces the code uses. `_find_connection()` falls back to
    `list()` because azure-ai-projects 2.4.0 can raise ResourceNotFoundError
    from `get()` for a connection `list()` returns from the same client - a
    real defect hit during deployment. A fake with only `get()` would let that
    fallback path go untested and pass regardless of what it does.
    """

    def __init__(self, present: set[str]) -> None:
        self._present = present

    def get(self, name: str):
        if name in self._present:
            return _Conn(name)
        raise ResourceNotFoundError(f"connection {name!r} not found")

    def list(self):
        return [_Conn(n) for n in sorted(self._present)]


class _Project:
    def __init__(self, present: set[str]) -> None:
        self.connections = _Connections(present)
        self.agents = _Agents()


BASE = {
    "project_endpoint": "https://example.services.ai.azure.com/api/projects/p",
    "search_connection_name": "aisearch-connection",
    "search_index_name": "idx",
    "agent_name": "TestAgent",
    "agent_model": "gpt-5.4",
    "agent_reasoning_effort": None,
    "bing_connection_name": None,
    "bing_custom_config_name": None,
}

_failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        _failures.append(label)


def main() -> int:
    print("1. Bing connection NAMED but ABSENT -> degrade, agent still created")
    settings = dict(
        BASE,
        bing_connection_name="stale-name-from-another-environment",
        bing_custom_config_name="cfg",
    )
    agent, web = sfa.create_agent(_Project({"aisearch-connection"}), settings)
    check("web_tool_enabled", web, False)
    check("agent still created", agent.id, "agent-fake")
    check("tool count (search only)", len(_Agents.tools), 1)

    print("\n2. Bing connection PRESENT -> web tool wired")
    settings = dict(BASE, bing_connection_name="bing-conn", bing_custom_config_name="cfg")
    _agent, web = sfa.create_agent(
        _Project({"aisearch-connection", "bing-conn"}), settings
    )
    check("web_tool_enabled", web, True)
    check("tool count (search + bing)", len(_Agents.tools), 2)

    print("\n3. Bing vars UNSET -> degrade")
    _agent, web = sfa.create_agent(_Project({"aisearch-connection"}), dict(BASE))
    check("web_tool_enabled", web, False)

    print("\n4. AI Search connection ABSENT -> fatal (it is the corpus)")
    try:
        sfa.create_agent(_Project(set()), dict(BASE))
        check("raised SystemExit", False, True)
    except SystemExit as exc:
        check("raised SystemExit", True, True)
        check("message says REQUIRED", "REQUIRED" in str(exc), True)

    print("\n5. Degraded exit code is distinct from success and failure")
    check("EXIT_DEGRADED", sfa.EXIT_DEGRADED, 3)

    print()
    if _failures:
        print(f"FAILED: {_failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
