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
* AGENT_WEB_TOOL=webiq                         -> an OpenAPI tool to the app's own
  route, in key or managed-identity mode; anything missing degrades, and a
  leftover Bing connection is never substituted for it

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
    instructions: str = ""
    description: str = ""

    def create_version(self, **kwargs):
        _Agents.tools = kwargs["definition"].tools
        _Agents.instructions = kwargs["definition"].instructions
        _Agents.description = kwargs.get("description") or ""
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

    # ── Web IQ through the app (AGENT_WEB_TOOL=webiq) ────────────────────────
    APP = "https://ca-test.example.azurecontainerapps.io"
    WEBIQ = dict(
        BASE,
        agent_web_tool="webiq",
        agent_web_tool_url=APP,
        # Left behind in an env that used to run Bing: must be ignored.
        bing_connection_name="bing-conn",
        bing_custom_config_name="cfg",
    )
    both = {"aisearch-connection", "bing-conn", "agent-web-tool-key"}

    def web_tool() -> dict:
        extra = [t.as_dict() for t in _Agents.tools[1:]]
        return extra[0] if len(extra) == 1 else {"count": len(extra)}

    print("\n6. Web IQ, key mode -> OpenAPI tool with the key connection, Bing ignored")
    settings = dict(WEBIQ, agent_web_tool_auth="key", agent_web_tool_connection_name="agent-web-tool-key")
    _agent, web = sfa.create_agent(_Project(both), settings)
    tool = web_tool()
    check("web_tool_enabled", web, True)
    check("tool count (search + webiq, no bing)", len(_Agents.tools), 2)
    check("tool type", tool.get("type"), "openapi")
    check("tool name", tool["openapi"]["name"], "webiq")
    check("auth", tool["openapi"]["auth"], {
        "type": "project_connection",
        "security_scheme": {"project_connection_id": "/connections/agent-web-tool-key"},
    })
    spec = tool["openapi"]["spec"]
    check("spec targets the app", spec["servers"], [{"url": APP}])
    check("spec path is the app route", list(spec["paths"]), [sfa.WEBIQ_TOOL_PATH])
    check("key sent in x-tool-key",
          spec["components"]["securitySchemes"]["apiKeyHeader"],
          {"type": "apiKey", "name": "x-tool-key", "in": "header"})
    check("scheme is required", spec["security"], [{"apiKeyHeader": []}])
    body = spec["paths"][sfa.WEBIQ_TOOL_PATH]["post"]["requestBody"]["content"]["application/json"]["schema"]
    check("body is just the query", (body["required"], list(body["properties"])), (["query"], ["query"]))
    check("the model is told not to add site: itself", "site:" in body["properties"]["query"]["description"], True)
    check("instructions name the Web IQ tool", sfa.AGENT_WEBIQ_TOOL_NAME in _Agents.instructions, True)
    check("instructions no longer name Bing", sfa.AGENT_WEB_TOOL_NAME in _Agents.instructions, False)

    print("\n7. Web IQ, managed identity -> Entra audience, no key scheme")
    settings = dict(WEBIQ, agent_web_tool_auth="entra", agent_web_tool_audience="api://app-1")
    _agent, web = sfa.create_agent(_Project(both), settings)
    tool = web_tool()
    check("web_tool_enabled", web, True)
    check("auth", tool["openapi"]["auth"], {"type": "managed_identity", "security_scheme": {"audience": "api://app-1"}})
    check("no apiKey scheme in the spec", ("components" in tool["openapi"]["spec"], "security" in tool["openapi"]["spec"]), (False, False))

    print("\n8. Web IQ degrades, never fails, when what it needs is missing")
    for label, overrides in (
        ("key connection absent", {"agent_web_tool_auth": "key", "agent_web_tool_connection_name": "gone"}),
        ("key mode without a connection name", {"agent_web_tool_auth": "key"}),
        ("managed identity without an audience", {"agent_web_tool_auth": "entra"}),
        ("no caller auth configured", {"agent_web_tool_auth": None}),
        ("no app URL", {"agent_web_tool_auth": "entra", "agent_web_tool_audience": "a", "agent_web_tool_url": None}),
        ("plain-http app URL", {"agent_web_tool_auth": "entra", "agent_web_tool_audience": "a",
                                "agent_web_tool_url": "http://localhost:3000"}),
    ):
        _agent, web = sfa.create_agent(_Project(both), dict(WEBIQ, **overrides))
        check(f"{label}: degraded, search only, and Bing NOT substituted",
              (web, len(_Agents.tools)), (False, 1))

    print("\n9. Settings: choice, inference and URL")
    import os
    from unittest.mock import patch

    def settings_for(env: dict) -> dict:
        with patch.dict(os.environ, env, clear=True):
            return sfa._web_tool_settings()

    check("default is webiq", settings_for({})["agent_web_tool"], "webiq")
    # Run by hand on an environment deployed before Web IQ became the default,
    # the Bing connection it has is the tool it was using.
    check("unset with a Bing connection -> bing",
          settings_for({"BING_CONNECTION_NAME": "b"})["agent_web_tool"], "bing")
    check("an explicit choice beats the Bing connection",
          settings_for({"BING_CONNECTION_NAME": "b", "AGENT_WEB_TOOL": "webiq"})["agent_web_tool"], "webiq")
    check("case-insensitive", settings_for({"AGENT_WEB_TOOL": " WebIQ "})["agent_web_tool"], "webiq")
    try:
        settings_for({"AGENT_WEB_TOOL": "google"})
        check("an invalid choice is rejected", False, True)
    except ValueError:
        check("an invalid choice is rejected", True, True)
    check("explicit auth wins", settings_for({"AGENT_WEB_TOOL_AUTH": "entra",
                                              "AGENT_WEB_TOOL_CONNECTION_NAME": "c"})["agent_web_tool_auth"], "entra")
    check("inferred: connection -> key", settings_for({"AGENT_WEB_TOOL_CONNECTION_NAME": "c",
                                                       "AGENT_WEB_TOOL_AUDIENCE": "a"})["agent_web_tool_auth"], "key")
    check("inferred: audience -> entra", settings_for({"AGENT_WEB_TOOL_AUDIENCE": "a"})["agent_web_tool_auth"], "entra")
    check("inferred: nothing -> none", settings_for({})["agent_web_tool_auth"], None)
    check("URL from the azd output, slash trimmed",
          settings_for({"SERVICE_APP_URI": APP + "/"})["agent_web_tool_url"], APP)
    check("AGENT_WEB_TOOL_URL overrides it",
          settings_for({"SERVICE_APP_URI": APP, "AGENT_WEB_TOOL_URL": "https://other"})["agent_web_tool_url"],
          "https://other")
    try:
        sfa.build_webiq_tool(APP, connection_id="c", audience="a")
        check("build_webiq_tool refuses two auth modes", False, True)
    except ValueError:
        check("build_webiq_tool refuses two auth modes", True, True)

    print("\n10. An instruction-only update keeps naming the tool the agent has")
    check("webiq definition", sfa._definition_web_tool_name(
        {"tools": [{"type": "azure_ai_search"}, {"type": "openapi", "openapi": {"name": "webiq"}}]}),
        sfa.AGENT_WEBIQ_TOOL_NAME)
    check("bing definition", sfa._definition_web_tool_name(
        {"tools": [{"type": "bing_custom_search_preview"}]}), sfa.AGENT_WEB_TOOL_NAME)
    check("some other openapi tool is not Web IQ", sfa._definition_web_tool_name(
        {"tools": [{"type": "openapi", "openapi": {"name": "weather"}}]}), sfa.AGENT_WEB_TOOL_NAME)

    print("\n11. The spec matches the route the app serves")
    sys.path.insert(0, str(_SCRIPTS.parent))
    from backend.api import agent_tools

    check("path", sfa.WEBIQ_TOOL_PATH, agent_tools.SEARCH_WEB_PATH)
    check("key header", sfa.WEBIQ_TOOL_KEY_HEADER, agent_tools.TOOL_KEY_HEADER)

    print()
    if _failures:
        print(f"FAILED: {_failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
