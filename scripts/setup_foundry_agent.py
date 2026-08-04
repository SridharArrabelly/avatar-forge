"""Provision (or update) the MTN Foundry agent used by the Voice Live backend.

This script creates a new version of a Microsoft Foundry agent (e.g.
``MtnAvatarAgent``) wired with two tools:

* **Azure AI Search** - internal index of past MTN executive meetings.
* **Grounding with Bing Custom Search** - single-shot open-web grounding
  restricted to a curated allow-list (configured server-side as a Bing Custom
  Search "configuration"). Provides hard source restriction rather than a soft
  ``site:`` hint, which makes the avatar's external answers safer to trust.

The agent's system prompt, model, and tool wiring live here; the runtime
backend (``backend/``) only references the agent by ``AGENT_NAME`` /
``AGENT_PROJECT_NAME`` and lets Foundry resolve the rest server-side.

The validated voice config is ``gpt-5.4`` (``AGENT_REASONING_EFFORT=none``) +
Grounding-with-Bing-Custom-Search: a single grounded round-trip, no web_search
fan-out. The model is chosen via ``AGENT_MODEL``; ``gpt-5.4-mini`` and the
original ``gpt-4.1-mini`` baseline are also supported.

Run ``scripts/smoke_foundry_agent.py`` after provisioning to smoke-test the
agent end-to-end.

Required environment variables (see ``.env.example``):
    PROJECT_ENDPOINT          Foundry project endpoint
                              (https://<resource>.services.ai.azure.com/api/projects/<project>)
    SEARCH_CONNECTION_NAME    Name of the Azure AI Search connection in the project
    SEARCH_INDEX_NAME         Azure AI Search index to expose to the agent
    AGENT_NAME                Name of the Foundry agent to create / version (e.g. ``MtnAvatarAgent``)
    AGENT_MODEL               Model deployment name to bind to the agent (e.g. ``gpt-5.4``)
    BING_CONNECTION_NAME      OPTIONAL. Grounding-with-Bing-Custom-Search connection in the project.
                              Leave unset to build a search-only agent; add it later and re-run.
    BING_CUSTOM_CONFIG_NAME   OPTIONAL. Bing Custom Search configuration (instance) name — the curated
                              allow-list of sites that the tool is restricted to.

Auth: uses ``DefaultAzureCredential`` - run ``az login`` first. The signed-in
identity needs "Foundry User" on the Foundry **account** (subscription
Owner/Contributor grant no ``Microsoft.CognitiveServices`` data actions, so they
are not sufficient). ``azd up`` assigns it; a new assignment can take several
minutes to take effect, which this script waits out.

Usage:
    uv run python scripts/setup_foundry_agent.py

Exit codes:
    0  agent created with every configured tool
    3  agent created, but the OPTIONAL web/news tool was left out (degraded, not failed)
    1  nothing usable was created — see the error text
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AISearchIndexResource,
    AzureAISearchQueryType,
    AzureAISearchTool,
    AzureAISearchToolResource,
    BingCustomSearchConfiguration,
    BingCustomSearchPreviewTool,
    BingCustomSearchToolParameters,
    PromptAgentDefinition,
    Reasoning,
)
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from rbac_propagation import wait_for_data_plane

# Repo root on sys.path so this deploy-time script and the runtime backend share
# ONE persona-name rule instead of each keeping its own copy — which is exactly
# how the agent ended up introducing itself as "Avatar" while the stage showed
# "Simone". Redundant under `uv run` (the project is installed editable) but makes
# a plain `python scripts/setup_foundry_agent.py` work too.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.avatar_identity import resolve_avatar_display_name  # noqa: E402

# Exit code meaning "the agent exists and works, but an OPTIONAL tool was left
# out". Distinct from 0 (fully wired) and from 1 (nothing usable was created) so
# the azd postprovision hook can report DEGRADED without claiming failure.
EXIT_DEGRADED = 3

# Prompt content lives under <repo>/prompts/. See prompts/README.md for layout
# and editing conventions. The design rationale comments below explain WHY the
# prompt is shaped the way it is — they stay here (next to the load) so they
# travel with the code that depends on the prompt's structure.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# The avatar's persona name. Prompt files use the {{AVATAR_NAME}} placeholder so
# the persona is never hardcoded; it is substituted at load time from the shared
# rule in backend/avatar_identity.py — AVATAR_DISPLAY_NAME, else the friendly name
# of the ACTIVE avatar model (so a "Simone" avatar says "I'm Simone"), else
# "Avatar".
#
# Resolved on every call rather than snapshotted at import: this module is
# imported before load_settings() runs load_dotenv(), so an import-time constant
# would read the process environment only and silently ignore .env — the
# documented way to re-run this script by hand after changing the avatar.

# The tool names as the AGENT sees them, and the values {{SEARCH_TOOL}} and
# {{WEB_TOOL}} resolve to here. The web tool's SDK kwarg is
# `bing_custom_search_preview`, but the name the prompt refers to — and the one
# every prompt in this repo has always used — is the unsuffixed form. Model mode
# substitutes its own pair; see backend/voice/instructions.py.
AGENT_SEARCH_TOOL_NAME = "azure_ai_search"
AGENT_WEB_TOOL_NAME = "bing_custom_search"


def _apply_brand(text: str) -> str:
    """Substitute brand and tool placeholders in a loaded prompt.

    {{SEARCH_TOOL}}/{{WEB_TOOL}} exist because one authored prompt serves both
    voice bindings, and the two register different tool names — model mode has
    search_minutes / search_web (see backend/voice/tools.py). Naming either set
    literally would leave the other mode describing tools that do not exist.
    These must resolve to the names the tools are actually created with below.
    """
    return (
        text.replace("{{AVATAR_NAME}}", resolve_avatar_display_name())
        .replace("{{SEARCH_TOOL}}", AGENT_SEARCH_TOOL_NAME)
        .replace("{{WEB_TOOL}}", AGENT_WEB_TOOL_NAME)
    )


def _load_prompt(*relative: str) -> str:
    """Load a prompt file from prompts/ as UTF-8 plain text."""
    return _apply_brand(
        _PROMPTS_DIR.joinpath(*relative).read_text(encoding="utf-8").strip()
    )


def agent_description() -> str:
    """Agent description, brand-substituted at call time (see _apply_brand)."""
    return _load_prompt("agent", "description.md")

# Agent instructions — one prompt, loaded for every model.
#
# prompts/agent/instructions.md is the only agent prompt, and it is loaded
# unconditionally: no per-model selection, no variants, no fallback. It carries
# the voice-first output rules (no URLs / no markdown / ≤70 words), the silent
# meeting catalogue contract, and the bing_custom_search query style by intent
# (MTN corporate / telecom industry / share price).
#
# It is tuned against the validated production config — gpt-5.4 with
# reasoning.effort="none" — and scores 30/30 on the BOUNDARY routing harness
# there. That effort is "none" by design, for conversational latency, so read
# this as "the agent prompt", not "the prompt for when reasoning is on".
#
# A second file tuned for gpt-4.x / gpt-4o used to live here, selected by model
# family. No deployment ever loaded it, so it drifted untested while every
# measurement was taken against this one — the selector made an unmaintained
# path look supported, which is worse than having a single prompt and re-tuning
# it if the model ever changes.
#
# The external tool is `bing_custom_search` (a grounded round-trip
# restricted to a curated, server-side domain allow-list) rather than
# `web_search` — the latter fans out into many calls and bloats context.


def load_settings() -> dict:
    """Read required and optional settings from the environment."""
    load_dotenv()
    settings = {
        "project_endpoint": os.getenv("PROJECT_ENDPOINT"),
        "search_connection_name": os.getenv("SEARCH_CONNECTION_NAME"),
        "search_index_name": os.getenv("SEARCH_INDEX_NAME"),
        "agent_name": os.getenv("AGENT_NAME"),
        "agent_model": os.getenv("AGENT_MODEL"),
        # Optional. Only set for reasoning models (o-series, gpt-5 family).
        # gpt-4.x / gpt-4o reject `reasoning.effort` at /responses time.
        "agent_reasoning_effort": (os.getenv("AGENT_REASONING_EFFORT") or "").strip() or None,
        # Grounding-with-Bing-Custom-Search connection name (the agent's only web tool).
        "bing_connection_name": (os.getenv("BING_CONNECTION_NAME") or "").strip() or None,
        # Bing Custom Search configuration (instance) name — the curated
        # allow-list of sites the web tool is restricted to.
        "bing_custom_config_name": (os.getenv("BING_CUSTOM_CONFIG_NAME") or "").strip() or None,
    }
    # Bing is OPTIONAL, in both of the ways it can be absent: the vars may be
    # unset (a greenfield deploy that provisioned Foundry + AI Search but no
    # Grounding-with-Bing-Custom-Search resource, which is configured out of
    # band in the Bing Custom Search portal), OR they may name a connection that
    # does not exist in this project — which is what happens whenever a .env is
    # copied between environments. Either way the agent is still created with
    # the AI Search (board/meeting minutes) tool alone, and the script exits
    # EXIT_DEGRADED so callers can say "degraded" instead of "failed". The
    # web/news tool is added once Bing is configured and the script is re-run.
    required = (
        "project_endpoint",
        "search_connection_name",
        "search_index_name",
        "agent_name",
        "agent_model",
    )
    missing = [k for k in required if not settings[k]]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(m.upper() for m in missing)}. "
            "See .env.example."
        )
    return settings


def build_bing_tool(
    bing_connection_id: str,
    bing_custom_config_name: str,
) -> BingCustomSearchPreviewTool:
    """Grounding-with-Bing-Custom-Search tool — single grounded round-trip per turn.

    A reasoning agent + WebSearchTool fans out into many web_search calls
    (measured: 121+ extra calls across the harness); even gpt-4.1-mini +
    WebSearchTool fans out and bloats tokens. Grounding-with-Bing-Custom-Search returns
    curated snippets in one shot, which is why it is the agent's only web tool.

    Custom Search vs. classic Grounding: the Custom Search variant pins the
    tool to a server-side "configuration" (instance) that lists exactly which
    domains are searchable. This is a HARD allow-list enforced by Bing — not
    a soft ``site:`` hint in the query — so external answers cite only the
    curated sources. The configuration is provisioned out of band (Bing Custom
    Search portal); we reference it by name here via ``instance_name``.

    count defaults to 8 (env: ``BING_COUNT``) — the validated production value;
    enough snippet budget to answer completely while staying tight for voice.
    market/set_lang pin South-Africa-first English. freshness is intentionally
    left unset —
    forcing recency would drop legitimate non-news lookups.

    Compliance: the formulated query leaves the Azure compliance/Geo boundary
    (per the Bing tool docs). Internal minutes never do — they stay in AI Search.
    """
    return BingCustomSearchPreviewTool(
        bing_custom_search_preview=BingCustomSearchToolParameters(
            search_configurations=[
                BingCustomSearchConfiguration(
                    project_connection_id=bing_connection_id,
                    instance_name=bing_custom_config_name,
                    market="en-ZA",
                    set_lang="en",
                    count=int(os.getenv("BING_COUNT", "8") or "8"),
                ),
            ]
        )
    )


def build_tools(
    search_connection_id: str,
    search_index_name: str,
    bing_connection_id: str | None = None,
    bing_custom_config_name: str | None = None,
) -> list:
    """Build the tool list for the agent: AI Search + (optional) Bing Custom Search.

    AI Search uses VECTOR_SIMPLE_HYBRID — vector ANN + BM25 keyword.
    The semantic re-ranker (VECTOR_SEMANTIC_HYBRID) would lift recall on
    summary queries, but the current azure-ai-projects SDK's
    AISearchIndexResource has no `semantic_configuration` field, so the
    server rejects that query type for this tool. Stick with SIMPLE_HYBRID
    until the SDK exposes the field; recall on this small corpus is strong.

    top_k defaults to 8 (env: ``AI_SEARCH_TOP_K``) — the validated production
    value. It pulls enough chunks to summarise from when several meetings are
    relevant, widening completeness on summary questions without hurting
    single-meeting scoping.
    """
    # Tool ORDER matters: smaller / non-reasoning models (e.g. the gpt-4.1-mini
    # baseline) bias hard toward the first tool, and even gpt-5.x benefits from
    # the hint. Put azure_ai_search first so MTN-meeting questions ground in the
    # index instead of falling through to the web tool.
    ai_search = AzureAISearchTool(
        azure_ai_search=AzureAISearchToolResource(
            indexes=[
                AISearchIndexResource(
                    project_connection_id=search_connection_id,
                    index_name=search_index_name,
                    query_type=AzureAISearchQueryType.VECTOR_SIMPLE_HYBRID,
                    top_k=int(os.getenv("AI_SEARCH_TOP_K", "8") or "8"),
                ),
            ]
        )
    )
    tools: list = [ai_search]
    if bing_connection_id and bing_custom_config_name:
        tools.append(build_bing_tool(bing_connection_id, bing_custom_config_name))
    return tools


def _model_supports_reasoning(model: str) -> bool:
    """Whether a model deployment accepts the ``reasoning.effort`` parameter.

    Reasoning models (o-series, gpt-5 family) accept it. The gpt-4.x / gpt-4o
    families reject it at /responses time with a 400 ``unsupported_parameter``
    — and because the agent bakes the parameter into its definition, that 400
    fires on EVERY turn, leaving the Voice Live avatar silent with no
    backend-visible error. Guard against that footgun here.
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    # o1 / o3 / o4(-mini) and the gpt-5 family are reasoning-capable.
    if re.match(r"^o[134](-|\d|$)", m):
        return True
    if m.startswith("gpt-5"):
        return True
    # Everything else (gpt-4.1, gpt-4o, gpt-4, …) does not.
    return False


def _find_connection(project: AIProjectClient, name: str):
    """Resolve a project connection by name, tolerating a broken ``get()``.

    ``connections.get(name)`` in azure-ai-projects 2.4.0 can raise
    ``ResourceNotFoundError: (NotFound) Project not found`` for a connection that
    ``connections.list()`` returns from the *same* client and endpoint moments
    later. The message blames the project rather than the connection, so the
    caller's "connection not found" diagnostic pointed at the wrong thing and
    sent you looking for a config error that does not exist.

    Try ``get()`` first — one call, and correct when it works — then fall back to
    scanning ``list()``. Only if the name is genuinely absent does this re-raise,
    so a real missing connection still fails fast with the original message.
    """
    try:
        return project.connections.get(name)
    except ResourceNotFoundError:
        for conn in project.connections.list():
            if getattr(conn, "name", None) == name:
                print(f"  (resolved connection {name!r} via list(); get() returned NotFound)")
                return conn
        raise


def create_agent(project: AIProjectClient, settings: dict) -> tuple[object, bool]:
    """Create a new version of the Foundry agent.

    Returns ``(agent, web_tool_enabled)``. ``web_tool_enabled`` is False when the
    optional Grounding-with-Bing-Custom-Search tool was left out — either because
    it was not configured or because the named connection does not exist. The
    agent is still fully usable in that case; it just answers from the indexed
    documents alone.

    Reasoning effort (`AGENT_REASONING_EFFORT`) is OPTIONAL. Behavior by model:

      * gpt-4.x / gpt-4o  — reject reasoning.effort. A set value is ignored
                            (with a warning); leave it unset.
      * gpt-5 family      — if unset, defaults to "none" for low conversational
                            latency (an unset value would otherwise let the model
                            use its server-side default "medium", adding 4-5s to
                            first-token). Set explicitly to override.
      * o-series          — must set a supported value explicitly (low/medium/
                            high); they do NOT accept "none".

    The validated voice configs are:

      * gpt-5.4 + AGENT_REASONING_EFFORT=none      — RECOMMENDED (production).
                                                    Best tool-routing accuracy
                                                    and numeric synthesis. Any
                                                    other effort value (low,
                                                    medium, high, xhigh) adds
                                                    4-5 seconds to first-token,
                                                    which is too laggy for
                                                    conversational voice use.
      * gpt-5.4-mini + AGENT_REASONING_EFFORT=none — faster first token; a fine
                                                    cost-saving fallback.
      * gpt-4.1-mini                               — original non-reasoning
                                                    baseline. Leave
                                                    AGENT_REASONING_EFFORT unset
                                                    (gpt-4.x reject it).
    """
    # The AI Search connection is REQUIRED: it is the agent's corpus. An agent
    # without it would answer from model priors alone, which is worse than not
    # deploying at all — so this fails fast with an actionable message rather
    # than a raw SDK traceback.
    try:
        # First Foundry data-plane call, so this is where a just-created role
        # assignment surfaces as 401 while it propagates. The wait only covers
        # 401/403 — a genuine 404 still falls through to the message below.
        azs_connection = wait_for_data_plane(
            lambda: _find_connection(project, settings["search_connection_name"]),
            what="reading the project's connections",
        )
    except ResourceNotFoundError:
        sys.exit(
            f"ERROR: AI Search connection {settings['search_connection_name']!r} was not found "
            "in this Foundry project.\n"
            "  This connection is REQUIRED — it is what the agent answers from.\n"
            "  Fix: create it in the Foundry portal, or point SEARCH_CONNECTION_NAME at the\n"
            "  existing connection, then re-run:\n"
            "      uv run python scripts/setup_foundry_agent.py"
        )

    # The web tool is OPTIONAL in two distinct ways, and BOTH must degrade
    # gracefully: the vars may be unset, *or* they may name a connection that
    # does not exist in this project (the common case when .env is copied from
    # another environment, or when Bing is deliberately deferred). Only the
    # first used to be tolerated, so a stale name silently cost you the agent.
    bing_connection_id = None
    bing_custom_config_name = settings.get("bing_custom_config_name")
    bing_connection_name = settings.get("bing_connection_name")
    web_tool_enabled = False

    if bing_connection_name and bing_custom_config_name:
        try:
            bing_connection = _find_connection(project, bing_connection_name)
        except ResourceNotFoundError:
            print(
                f"WARNING: Grounding-with-Bing-Custom-Search connection {bing_connection_name!r} "
                "was not found in this project.\n"
                "         Creating the agent WITHOUT the web/news tool — it will answer from the\n"
                "         indexed board/meeting minutes only. This is a degraded but working agent.\n"
                "         To enable the web tool later: add the connection in the Foundry portal,\n"
                "         set BING_CONNECTION_NAME + BING_CUSTOM_CONFIG_NAME, and re-run this script."
            )
        else:
            bing_connection_id = bing_connection.id
            web_tool_enabled = True
            print(
                f"Web tool: bing_custom_search (connection {bing_connection_name!r}, "
                f"configuration {bing_custom_config_name!r})."
            )
    else:
        print(
            "Web tool: DISABLED — BING_CONNECTION_NAME / BING_CUSTOM_CONFIG_NAME not set. "
            "Creating the agent with the AI Search (board/meeting minutes) tool only. "
            "Provision a Grounding-with-Bing-Custom-Search connection and re-run this "
            "script to add the news/web tool."
        )

    tools = build_tools(
        azs_connection.id,
        settings["search_index_name"],
        bing_connection_id,
        bing_custom_config_name,
    )

    definition_kwargs = {
        "model": settings["agent_model"],
        "instructions": _load_prompt("agent", "instructions.md"),
        "tools": tools,
    }
    effort = settings.get("agent_reasoning_effort")
    if effort and not _model_supports_reasoning(settings["agent_model"]):
        print(
            f"WARNING: AGENT_REASONING_EFFORT={effort!r} is set but model "
            f"{settings['agent_model']!r} does NOT support reasoning.effort "
            "(gpt-4.x / gpt-4o reject it with a 400 on every response, which "
            "makes the avatar go silent). Ignoring reasoning.effort. Unset "
            "AGENT_REASONING_EFFORT in .env to silence this warning."
        )
        effort = None
    # Safety default for the gpt-5 family: if the developer forgot to set
    # AGENT_REASONING_EFFORT, fall back to "none" rather than letting the model
    # use its server-side default ("medium"), which adds 4-5s to first-token —
    # too laggy for conversational voice. Scoped to gpt-5 specifically: the
    # o-series reasoning models do NOT accept effort="none" (they take
    # low/medium/high), so we must not blanket-default every reasoning model.
    if not effort and (settings["agent_model"] or "").strip().lower().startswith("gpt-5"):
        effort = "none"
        print(
            "AGENT_REASONING_EFFORT not set for gpt-5 family — defaulting to "
            "'none' for low conversational latency. Set it explicitly to override."
        )
    if effort:
        definition_kwargs["reasoning"] = Reasoning(effort=effort)
        print(f"Applying reasoning.effort={effort!r}.")
    else:
        print(
            "Skipping reasoning.effort — not set or not supported by this model. "
            "Set it ONLY for reasoning models (o-series, gpt-5 family)."
        )

    agent = project.agents.create_version(
        agent_name=settings["agent_name"],
        definition=PromptAgentDefinition(**definition_kwargs),
        description=agent_description(),
    )
    print(f"Agent created (id: {agent.id}, name: {agent.name}, version: {agent.version})")
    print(
        f"Persona name: {resolve_avatar_display_name()!r} — from AVATAR_DISPLAY_NAME "
        "if set, else the active avatar model. This is what the agent calls itself, "
        "and it must match the name on the stage."
    )
    return agent, web_tool_enabled


def main() -> int:
    settings = load_settings()
    project = AIProjectClient(
        endpoint=settings["project_endpoint"],
        credential=DefaultAzureCredential(),
    )
    _agent, web_tool_enabled = create_agent(project, settings)
    if not web_tool_enabled:
        print(
            "\nAgent is READY but DEGRADED: no web/news tool, so it answers from the indexed\n"
            "documents only. Add a Grounding-with-Bing-Custom-Search connection to the Foundry\n"
            "project, set BING_CONNECTION_NAME + BING_CUSTOM_CONFIG_NAME, then re-run:\n"
            "    uv run python scripts/setup_foundry_agent.py"
        )
        return EXIT_DEGRADED
    return 0


if __name__ == "__main__":
    sys.exit(main())
