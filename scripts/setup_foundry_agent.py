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

Run ``scripts/test_foundry_agent.py`` after provisioning to smoke-test the
agent end-to-end.

Required environment variables (see ``.env.example``):
    PROJECT_ENDPOINT          Foundry project endpoint
                              (https://<resource>.services.ai.azure.com/api/projects/<project>)
    SEARCH_CONNECTION_NAME    Name of the Azure AI Search connection in the project
    SEARCH_INDEX_NAME         Azure AI Search index to expose to the agent
    AGENT_NAME                Name of the Foundry agent to create / version (e.g. ``MtnAvatarAgent``)
    AGENT_MODEL               Model deployment name to bind to the agent (e.g. ``gpt-5.4``)
    BING_CONNECTION_NAME      Name of the Grounding-with-Bing-Custom-Search connection in the project
    BING_CUSTOM_CONFIG_NAME   Bing Custom Search configuration (instance) name — the curated
                              allow-list of sites that the tool is restricted to.

Auth: uses ``DefaultAzureCredential`` - run ``az login`` first.

Usage:
    uv run python scripts/setup_foundry_agent.py
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
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

# Prompt content lives under <repo>/prompts/. See prompts/README.md for layout
# and editing conventions. The design rationale comments below explain WHY the
# prompt is shaped the way it is — they stay here (next to the load) so they
# travel with the code that depends on the prompt's structure.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(*relative: str) -> str:
    """Load a prompt file from prompts/ as UTF-8 plain text."""
    return _PROMPTS_DIR.joinpath(*relative).read_text(encoding="utf-8").strip()


AGENT_DESCRIPTION = _load_prompt("agent", "description.md")

# Agent instructions — voice-first, two variants tuned by model family.
#
# Two prompt files live under prompts/agent/:
#   * instructions-nonreasoning.md — tuned for gpt-4.x / gpt-4o (fast, literal).
#     Hard rules ("EXACTLY ONE tool per turn"), HARD ANTI-RULE block,
#     exhaustive "X → tool" examples. These models do what the prompt says,
#     no more, no less, so the tool-selection contract is stated as hard
#     rules rather than "use judgement".
#   * instructions-reasoning.md — tuned for o-series / gpt-5 (deliberate,
#     multi-step). Softer principles, allows up to 3 tool calls per turn,
#     one refined follow-up search, no exhaustive anti-rule list. These
#     models can infer "MTN's own plans live in our minutes, not on the
#     web" from a short principle.
#
# Both share: voice-first output rules (no URLs / no markdown / ≤70 words),
# the silent meeting catalogue contract, and the bing_custom_search query
# style by intent (MTN corporate / telecom industry / share price).
#
# The external tool is `bing_custom_search` (a grounded round-trip
# restricted to a curated, server-side domain allow-list) rather than
# `web_search` — the latter fans out into many calls and bloats context.
#
# The variant is selected at create_agent() time from settings["agent_model"]
# via _model_supports_reasoning() — same predicate that gates the
# reasoning.effort parameter, so the prompt and the model capability stay
# in lock-step.


def _load_agent_instructions(model: str) -> str:
    """Pick the prompt variant that matches the model family.

    Reasoning models (o-series, gpt-5) get the deliberate, multi-step prompt;
    everything else (gpt-4.x, gpt-4o) gets the literal, hard-rule prompt.
    Falls back to the non-reasoning variant if the reasoning file is missing
    so a partial deployment never bricks the agent.
    """
    if _model_supports_reasoning(model):
        path = _PROMPTS_DIR / "agent" / "instructions-reasoning.md"
        if path.is_file():
            print(
                f"Loading reasoning prompt variant "
                f"(prompts/agent/instructions-reasoning.md) for model {model!r}."
            )
            return path.read_text(encoding="utf-8").strip()
        print(
            f"WARNING: model {model!r} supports reasoning but "
            "prompts/agent/instructions-reasoning.md is missing — falling "
            "back to instructions-nonreasoning.md."
        )
    else:
        print(
            f"Loading non-reasoning prompt variant "
            f"(prompts/agent/instructions-nonreasoning.md) for model {model!r}."
        )
    return _load_prompt("agent", "instructions-nonreasoning.md")


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
    # Bing is OPTIONAL: if BING_CONNECTION_NAME / BING_CUSTOM_CONFIG_NAME are
    # unset (e.g. a greenfield deploy that provisioned Foundry + AI Search but
    # no Grounding-with-Bing-Custom-Search resource, which must be configured
    # out of band in the Bing Custom Search portal), the agent is still created
    # with the AI Search (board/meeting minutes) tool alone. The web/news tool
    # is added once Bing is configured and the script is re-run.
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


def create_agent(project: AIProjectClient, settings: dict):
    """Create a new version of the Foundry agent.

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
    azs_connection = project.connections.get(settings["search_connection_name"])

    bing_connection_id = None
    bing_custom_config_name = settings.get("bing_custom_config_name")
    if settings.get("bing_connection_name") and bing_custom_config_name:
        bing_connection = project.connections.get(settings["bing_connection_name"])
        bing_connection_id = bing_connection.id
        print(
            f"Web tool: bing_custom_search (connection {settings['bing_connection_name']!r}, "
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
        "instructions": _load_agent_instructions(settings["agent_model"]),
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
        description=AGENT_DESCRIPTION,
    )
    print(f"Agent created (id: {agent.id}, name: {agent.name}, version: {agent.version})")
    return agent


def main() -> int:
    settings = load_settings()
    project = AIProjectClient(
        endpoint=settings["project_endpoint"],
        credential=DefaultAzureCredential(),
    )
    create_agent(project, settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
