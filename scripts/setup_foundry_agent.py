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

The default agent config is ``gpt-5.6-terra`` with reasoning ``none``,
lexical/semantic Search at top-5, and Grounding-with-Bing-Custom-Search.
The deployment name is overridable via ``AGENT_MODEL``. See
``docs/evaluation-results.md`` for measured quality and latency, including limits.

Run ``scripts/smoke_foundry_agent.py`` after provisioning to smoke-test the
agent end-to-end.

Required environment variables (see ``.env.example``):
    PROJECT_ENDPOINT          Foundry project endpoint
                              (https://<resource>.services.ai.azure.com/api/projects/<project>)
    SEARCH_CONNECTION_NAME    Name of the Azure AI Search connection in the project
    SEARCH_INDEX_NAME         Azure AI Search index to expose to the agent
    AGENT_NAME                Name of the Foundry agent to create / version (e.g. ``MtnAvatarAgent``)
    AGENT_MODEL               Model deployment name; defaults to ``gpt-5.6-terra``.
                              This deployment must exist in the target project.
    BING_CONNECTION_NAME      OPTIONAL. Grounding-with-Bing-Custom-Search connection in the project.
                              Leave unset to build a search-only agent; add it later and re-run.
    BING_CUSTOM_CONFIG_NAME   OPTIONAL. Bing Custom Search configuration (instance) name — the curated
                              allow-list of sites that the tool is restricted to.
    AI_SEARCH_QUERY_TYPE      OPTIONAL. simple, semantic (default), vector, vector_simple_hybrid,
                              or vector_semantic_hybrid. semantic is lexical + semantic reranking,
                              without vector retrieval.
    AI_SEARCH_TOP_K           OPTIONAL. Positive integer; defaults to 5.

An explicit ``--env-file`` overrides process values only for keys in that file.
``--clone-from`` stages an isolated ``AGENT_NAME`` using the source's exact
definition, replacing only Search index_name/query_type/top_k. It needs only
PROJECT_ENDPOINT, AGENT_NAME, and SEARCH_INDEX_NAME, plus the optional retrieval
settings above; model, prompt, reasoning, connections, and Bing stay unchanged.
An existing target must already have the exact requested definition.

Auth: uses ``DefaultAzureCredential`` - run ``az login`` first. The signed-in
identity needs "Foundry User" on the Foundry **account** (subscription
Owner/Contributor grant no ``Microsoft.CognitiveServices`` data actions, so they
are not sufficient). ``azd up`` assigns it; a new assignment can take several
minutes to take effect, which this script waits out.

Usage:
    uv run python scripts/setup_foundry_agent.py
    uv run python scripts/setup_foundry_agent.py --env-file path/to/evaluation.env --clone-from MtnAvatarAgent

Exit codes:
    0  agent created with every configured tool
    3  agent created, but the OPTIONAL web/news tool was left out (degraded, not failed)
    1  nothing usable was created — see the error text
"""

from __future__ import annotations

import argparse
import copy
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
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from dotenv import dotenv_values, load_dotenv

from rbac_propagation import wait_for_data_plane

# Repo root on sys.path so this deploy-time script and the runtime backend share
# ONE persona-name rule instead of each keeping its own copy — which is exactly
# how the agent ended up introducing itself as "Avatar" while the stage showed
# "Simone". Redundant under `uv run` (the project is installed editable) but makes
# a plain `python scripts/setup_foundry_agent.py` work too.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.avatar_identity import resolve_avatar_display_name  # noqa: E402
from backend.onboarding import expand_onboarding  # noqa: E402

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
        expand_onboarding(text).replace("{{AVATAR_NAME}}", resolve_avatar_display_name())
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
# It was originally tuned against the production config — gpt-5.4 with
# reasoning.effort="none" — and scores 30/30 on the BOUNDARY routing harness
# there. Later retrieval-first comparisons reused the same prompt. The effort
# remains "none" by default for conversational latency, so read
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


def _retrieval_settings(settings: dict | None = None) -> tuple[AzureAISearchQueryType, int]:
    """Validate retrieval settings before any agent can be published."""
    settings = settings if settings is not None else {}
    query_type = settings.get(
        "ai_search_query_type", os.getenv("AI_SEARCH_QUERY_TYPE", "semantic")
    )
    top_k = settings.get("ai_search_top_k", os.getenv("AI_SEARCH_TOP_K", "5"))
    try:
        query_type = AzureAISearchQueryType(query_type.strip())
    except (AttributeError, ValueError):
        choices = ", ".join(item.value for item in AzureAISearchQueryType)
        raise ValueError(
            f"AI_SEARCH_QUERY_TYPE must be one of: {choices}; got {query_type!r}."
        ) from None
    try:
        if isinstance(top_k, bool) or not isinstance(top_k, (str, int)):
            raise ValueError
        parsed_top_k = int(top_k)
        if parsed_top_k <= 0:
            raise ValueError
    except ValueError:
        raise ValueError(f"AI_SEARCH_TOP_K must be a positive integer; got {top_k!r}.") from None
    return query_type, parsed_top_k


def _validate_clone_names(source_name: str, target_name: str) -> None:
    if not isinstance(source_name, str) or not source_name.strip():
        raise ValueError("--clone-from must name a source agent.")
    if not isinstance(target_name, str) or not target_name.strip():
        raise ValueError("AGENT_NAME must name an isolated target agent.")
    if source_name.strip().casefold() == target_name.strip().casefold():
        raise ValueError("--clone-from and AGENT_NAME must differ; the source agent is read-only.")


def load_settings(
    env_file: str | Path | None = None, *, clone_from: str | None = None,
    instructions_only: bool = False,
) -> dict:
    """Read settings, optionally overlaying an explicit file onto the process."""
    if env_file is None:
        load_dotenv()
    else:
        env_file = Path(env_file)
        if not env_file.is_file():
            raise FileNotFoundError(f"Explicit env-file does not exist or is not a file: {env_file}")
        with env_file.open(encoding="utf-8") as stream:
            values = dotenv_values(stream=stream)
        # Even a bare key overrides the process; retrieval validation rejects it
        # as empty instead of silently inheriting an unrelated deployment value.
        os.environ.update({key: value if value is not None else "" for key, value in values.items()})
    if instructions_only:
        settings = {
            "project_endpoint": (os.getenv("PROJECT_ENDPOINT") or "").strip(),
            "agent_name": (os.getenv("AGENT_NAME") or "").strip(),
        }
        if not all(settings.values()):
            raise EnvironmentError("--update-instructions requires PROJECT_ENDPOINT and AGENT_NAME.")
        return settings
    query_type, top_k = _retrieval_settings()
    settings = {
        "project_endpoint": os.getenv("PROJECT_ENDPOINT"),
        "search_connection_name": os.getenv("SEARCH_CONNECTION_NAME"),
        "search_index_name": os.getenv("SEARCH_INDEX_NAME"),
        "agent_name": os.getenv("AGENT_NAME"),
        "agent_model": os.getenv("AGENT_MODEL", None if clone_from is not None else "gpt-5.6-terra"),
        "ai_search_query_type": query_type.value,
        "ai_search_top_k": top_k,
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
    required = ("project_endpoint", "search_index_name", "agent_name")
    if clone_from is None:
        required += ("search_connection_name", "agent_model")
    missing = [k for k in required if not (settings[k] or "").strip()]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(m.upper() for m in missing)}. "
            "See .env.example."
        )
    if clone_from is not None:
        _validate_clone_names(clone_from, settings["agent_name"])
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

    count defaults to 5 (env: ``BING_COUNT``) — the measured production value.
    A controlled A/B against count=8 (web-only questions, 3 runs x 5 questions
    per arm, Bing count the only variable) found 8 buys no quality: routing was
    15/15 in both arms and answer length was unchanged, while 8 produced the
    *less* complete answer on the revenue question. 5 cuts total tokens 24%
    (130,028 -> 98,364) and is neutral-to-faster on first token. See
    docs/evaluation-history.md.
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
                    count=int(os.getenv("BING_COUNT", "5") or "5"),
                ),
            ]
        )
    )


def build_tools(
    search_connection_id: str,
    search_index_name: str,
    bing_connection_id: str | None = None,
    bing_custom_config_name: str | None = None,
    *,
    query_type: str | None = None,
    top_k: int | None = None,
) -> list:
    """Build the tool list for the agent: AI Search + (optional) Bing Custom Search.

    AI_SEARCH_QUERY_TYPE defaults to SEMANTIC: BM25 candidates followed by
    semantic reranking, without vectors. AI_SEARCH_TOP_K defaults to 5.
    This is the tested whole-section configuration. Explicit legacy settings
    (vector_simple_hybrid / 8) remain supported; index migrations are separate.
    """
    retrieval = {}
    if query_type is not None:
        retrieval["ai_search_query_type"] = query_type
    if top_k is not None:
        retrieval["ai_search_top_k"] = top_k
    query_type, top_k = _retrieval_settings(retrieval)
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
                    query_type=query_type,
                    top_k=top_k,
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
                            use its server-side default). Set explicitly to override;
                            latency depends on the model and its tool workflow.
      * o-series          — must set a supported value explicitly (low/medium/
                            high); they do NOT accept "none".

    The new default is Terra / none with section-based semantic/top-5 retrieval.
    GPT-5.4 / none remains a measured alternative. Read the current evaluation
    rather than treating historical routing scores as complete answer quality.
    """
    query_type, top_k = _retrieval_settings(settings)
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
        query_type=query_type,
        top_k=top_k,
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
    # use its server-side default, whose latency depends on the model and tools.
    # Scoped to gpt-5 specifically: the
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


def update_agent_instructions(project: AIProjectClient, agent_name: str) -> object:
    """Publish instructions only, retaining the existing agent definition."""
    if not isinstance(agent_name, str) or not agent_name.strip():
        raise ValueError("AGENT_NAME must name an existing agent.")
    agent_name = agent_name.strip()
    instructions = _load_prompt("agent", "instructions.md")
    if not instructions:
        raise ValueError("Agent instructions must not be empty.")
    source = project.agents.get(agent_name)
    source_version = source.versions.latest.version
    previous = project.agents.get_version(agent_name, source_version)
    original = copy.deepcopy(previous.definition.as_dict())
    if original.get("kind") != "prompt":
        raise ValueError("--update-instructions requires an existing prompt agent.")
    if project.agents.get(agent_name).versions.latest.version != source_version:
        raise RuntimeError("Agent changed while reading its definition; no update was published.")
    if original.get("instructions") == instructions:
        print(f"Agent {agent_name!r} version {source_version} already has these instructions.")
        return previous

    modified = copy.deepcopy(original)
    modified["instructions"] = instructions
    body = {"definition": modified}
    for field in ("description", "metadata"):
        value = getattr(previous, field, None)
        if value is not None:
            body[field] = copy.deepcopy(value)
    created = project.agents.create_version(agent_name=agent_name, body=body)
    published = project.agents.get_version(agent_name, created.version)
    if published.definition.as_dict() != modified:
        raise RuntimeError("Published agent definition differs from the requested instruction-only update.")
    if project.agents.get(agent_name).versions.latest.version != created.version:
        raise RuntimeError("Another agent version became latest during the instruction update.")
    print(
        f"Updated {agent_name!r} instructions: version {source_version} -> {created.version}; "
        "model, tools and other definition settings unchanged."
    )
    return published


def retrieval_definition(source: dict, settings: dict) -> dict:
    """Copy a source definition, changing only its single Search resource."""
    query_type, top_k = _retrieval_settings(settings)
    index_name = settings.get("search_index_name")
    if not isinstance(index_name, str) or not index_name.strip():
        raise ValueError("SEARCH_INDEX_NAME must name the target index.")
    modified = copy.deepcopy(source)
    tools = modified.get("tools") or []
    search_tools = [tool for tool in tools if isinstance(tool, dict) and tool.get("type") == "azure_ai_search"]
    if len(search_tools) != 1:
        raise ValueError("Cloning requires exactly one Azure AI Search tool and one index resource.")
    resource = search_tools[0].get("azure_ai_search")
    indexes = resource.get("indexes") if isinstance(resource, dict) else None
    if not isinstance(indexes, list) or len(indexes) != 1 or not isinstance(indexes[0], dict):
        raise ValueError("Cloning requires exactly one Azure AI Search index resource; source is ambiguous or missing it.")
    indexes[0].update(index_name=index_name, query_type=query_type.value, top_k=top_k)
    return modified


def clone_agent(project: AIProjectClient, settings: dict, source_name: str) -> object:
    """Stage a guarded retrieval-only clone without updating any source resource."""
    target_name = settings.get("agent_name")
    _validate_clone_names(source_name, target_name)
    target_name = target_name.strip()
    _retrieval_settings(settings)
    index_name = settings.get("search_index_name")
    if not isinstance(index_name, str) or not index_name.strip():
        raise ValueError("SEARCH_INDEX_NAME must name the target index.")
    source_name = source_name.strip()
    source = wait_for_data_plane(
        lambda: project.agents.get(source_name), what="reading the source agent",
    )
    source_version = source.versions.latest.version
    source_version_details = project.agents.get_version(source_name, source_version)
    original = copy.deepcopy(source_version_details.definition.as_dict())
    modified = retrieval_definition(original, settings)

    try:
        target = project.agents.get(target_name)
    except ResourceNotFoundError:
        target = None
    if target is not None:
        agent = project.agents.get_version(target_name, target.versions.latest.version)
        if agent.definition.as_dict() != modified:
            raise RuntimeError(
                f"Target agent {target_name!r} already has a different definition. "
                "No changes made; choose a fresh AGENT_NAME rather than overwrite it."
            )
        print(f"Target agent {target_name!r} already matches; reusing version {agent.version}.")
    else:
        body = {"definition": modified}
        for field in ("description", "metadata"):
            value = getattr(source_version_details, field, None)
            if value is not None:
                body[field] = copy.deepcopy(value)
        created = project.agents.create_version(agent_name=target_name, body=body)
        agent = project.agents.get_version(target_name, created.version)
        if agent.definition.as_dict() != modified:
            raise RuntimeError(
                f"Target agent {target_name!r} definition readback differs from the requested clone."
            )

    current_source = project.agents.get(source_name)
    current_version = current_source.versions.latest.version
    current_definition = project.agents.get_version(source_name, current_version).definition.as_dict()
    if current_version != source_version or current_definition != original:
        raise RuntimeError("Source agent changed during cloning; this script never writes the source.")
    print(
        f"Verified isolated agent {target_name!r} version {agent.version}; "
        f"source {source_name!r} version {source_version} is unchanged."
    )
    return agent


def _credential() -> DefaultAzureCredential:
    """Credential for Foundry data-plane calls, pinned to a deliberate identity.

    ``DefaultAzureCredential`` orders its chain shared-token-cache -> VS Code ->
    **then** Azure CLI, and it never forwards a tenant to ``AzureCliCredential``
    (azure-identity ``_credentials/default.py``: ``AzureCliCredential(
    process_timeout=process_timeout)``, no ``tenant_id``). So ``AZURE_TENANT_ID``
    cannot constrain it, and on a machine signed in to more than one tenant an
    ambient editor/broker login silently outranks ``az login``. Every call then
    fails with

        Token tenant <other-tenant> does not match resource tenant.

    which reads like a broken deployment rather than a stale login somewhere else
    on the box -- and, mid-rename, leaves the persona name applied to some
    surfaces but not the agent.

    Drop the ambient caches and keep the credentials this script is actually
    meant to use: environment / workload / managed identity in CI and ACA, and
    ``az`` or ``azd`` locally.

    ``process_timeout`` is raised from the 10s default because ``az`` is slow to
    answer while throttled, and the default expires mid-deploy.
    """
    return DefaultAzureCredential(
        process_timeout=90,
        exclude_shared_token_cache_credential=True,
        exclude_visual_studio_code_credential=True,
        exclude_powershell_credential=True,
        exclude_broker_credential=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="Explicit .env file; supplied keys override the process environment.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--clone-from", help="Read-only source agent to clone into an isolated AGENT_NAME, changing only Search retrieval.")
    mode.add_argument("--update-instructions", action="store_true", help="Publish instructions only on existing AGENT_NAME, preserving its model, tools and other settings.")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(
            args.env_file, clone_from=args.clone_from,
            instructions_only=args.update_instructions,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    try:
        with _credential() as credential, AIProjectClient(
            endpoint=settings["project_endpoint"], credential=credential,
        ) as project:
            if args.update_instructions:
                update_agent_instructions(project, settings["agent_name"])
                return 0
            if args.clone_from is not None:
                clone_agent(project, settings, args.clone_from)
                return 0
            _agent, web_tool_enabled = create_agent(project, settings)
    except HttpResponseError as exc:
        if "does not match resource tenant" not in str(exc):
            raise
        expected = os.getenv("AZURE_TENANT_ID", "").strip() or "(the deployment's tenant)"
        print(
            f"\n{exc}\nAuthenticated against the wrong tenant.\n\n"
            f"  this deployment is in : {expected}\n"
            f"  the token came from   : see 'Token tenant ...' above\n\n"
            "Some other Azure login on this machine is winning over `az login`. Check:\n"
            "    az account show --query tenantId -o tsv\n"
            "    Get-AzContext | Select-Object Tenant       # Azure PowerShell\n"
            "    azd auth login --check-status\n\n"
            "Sign the offending one into the right tenant, or sign it out:\n"
            f"    az login --tenant {expected}\n"
            "    Disconnect-AzAccount\n",
            file=sys.stderr,
        )
        return 1
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
