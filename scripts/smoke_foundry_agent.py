"""Smoke test harness for the configured Foundry agent.

Streams a single response from an already-provisioned Foundry agent to
verify it works end-to-end. Mirrors the live Voice Live runtime by
injecting a MEETINGS LIST system message (fetched from AI Search) so
catalogue questions ("what was my last meeting?") behave the same way
they do in production.

Required environment variables (see ``.env.example``):
    PROJECT_ENDPOINT       Foundry project endpoint
    AGENT_NAME             Name of the existing Foundry agent to test
    AZURE_SEARCH_ENDPOINT  AI Search endpoint (for MEETINGS LIST injection)
    SEARCH_INDEX_NAME      AI Search index name

Auth: uses ``DefaultAzureCredential`` - run ``az login`` first.

Usage:
    uv run python scripts/smoke_foundry_agent.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from dotenv import load_dotenv


def load_settings() -> dict:
    """Read required settings from the environment."""
    load_dotenv()
    settings = {
        "project_endpoint": os.getenv("PROJECT_ENDPOINT"),
        "agent_name": os.getenv("AGENT_NAME"),
    }
    missing = [k for k in ("project_endpoint", "agent_name") if not settings[k]]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(m.upper() for m in missing)}. "
            "See .env.example."
        )
    return settings


def _fetch_catalog() -> str | None:
    """Mirror of ``backend/voice/catalog.py`` for the CLI smoke test.

    The live Voice Live handler injects a MEETINGS LIST system message
    at session start so the model can answer catalogue questions
    (first / last / count / list) directly without hallucinating. The
    smoke test path needs the same context — otherwise the model will
    invent meeting dates based on today's calendar. This function
    fetches the catalogue synchronously and returns the same text
    format as the async runtime version.

    Returns the catalogue string, or None if AI Search is unreachable
    or the env vars are missing.
    """
    endpoint = os.getenv("AZURE_SEARCH_ENDPOINT", "").strip().rstrip("/")
    index = os.getenv("SEARCH_INDEX_NAME", "").strip()
    if not endpoint or not index:
        return None

    api_key = os.getenv("AZURE_SEARCH_API_KEY", "").strip()
    credential = AzureKeyCredential(api_key) if api_key else DefaultAzureCredential()
    client = SearchClient(endpoint=endpoint, index_name=index, credential=credential)
    try:
        # Mirrors backend/voice/catalog.py: filter on chunk_index eq 0 to
        # return one doc per meeting (~10 rows) instead of every chunk.
        results = client.search(
            search_text="*",
            filter="chunk_index eq 0",
            select=["title", "meeting_date"],
            top=200,
        )
        by_date: dict[str, str] = {}
        for r in results:
            date_iso = r.get("meeting_date")
            title = r.get("title") or ""
            if not date_iso:
                continue
            if date_iso not in by_date:
                by_date[date_iso] = title
        if not by_date:
            return None
        ordered = sorted(by_date.items(), key=lambda kv: kv[0])
        lines = [
            "MEETINGS LIST — the complete authoritative roster of board / "
            "executive meetings currently in the AI Search index. Use this "
            "to answer first / last / count / listing questions directly "
            "(no tool call), and to phrase precise content searches by "
            "exact meeting date."
        ]
        for date_iso, title in ordered:
            date_part = date_iso.split("T", 1)[0]
            try:
                dt = datetime.strptime(date_part, "%Y-%m-%d")
                pretty = f"{dt.day} {dt.strftime('%B %Y')}"
            except ValueError:
                pretty = date_iso
            if title and title.strip():
                lines.append(f"- {pretty}  ({title.strip()})")
            else:
                lines.append(f"- {pretty}")
        lines.append(
            f"Total: {len(ordered)} meeting(s). Earliest is the first entry, "
            "latest is the last entry."
        )
        return "\n".join(lines)
    except Exception as e:
        print(f"(Smoke test: catalogue fetch failed: {e})")
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def smoke_test(agent_name: str) -> None:
    """Prompt the user once and stream a response from the agent.

    Injects the MEETINGS LIST as a system message before the user's
    question, mirroring what ``backend/voice/handler.py`` does for live
    Voice Live sessions. Without this, catalogue questions ("what was
    my last meeting?") trigger model hallucination based on today's
    date instead of the real index contents.
    """
    try:
        user_input = input(
            "\nEnter a question to test the agent (Ctrl+C to skip):\n> "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nSkipped smoke test.")
        return
    if not user_input:
        print("No question entered; skipping smoke test.")
        return

    catalog = _fetch_catalog()
    request_input: list[dict] | str
    if catalog:
        meeting_count = catalog.count("\n- ")
        print(f"(Injecting MEETINGS LIST: {meeting_count} meetings, {len(catalog)} chars)")
        request_input = [
            {"type": "message", "role": "system", "content": catalog},
            {"type": "message", "role": "user", "content": user_input},
        ]
    else:
        print("(No catalogue available — agent will answer without MEETINGS LIST context.)")
        request_input = user_input

    # Foundry PromptAgents (and hosted agents) only surface their full
    # server-side tool list (Azure AI Search, MCP, etc.) when invoked through
    # the per-agent endpoint. The project-level OpenAI client with
    # extra_body={"agent_reference": ...} only exposes universal tools
    # (web_search), so the model never fires azure_ai_search. Point the
    # OpenAI client at the agent endpoint directly — same path the Foundry
    # playground uses.
    from openai import OpenAI
    project_endpoint = os.environ["PROJECT_ENDPOINT"].rstrip("/")
    agent_base_url = f"{project_endpoint}/agents/{agent_name}/endpoint/protocols/openai"
    cred = DefaultAzureCredential()
    token = cred.get_token("https://ai.azure.com/.default").token
    openai = OpenAI(
        base_url=agent_base_url,
        api_key=token,  # AAD token used as bearer
        default_query={"api-version": "v1"},
    )
    stream = openai.responses.create(
        stream=True,
        tool_choice="auto",
        input=request_input,
        parallel_tool_calls=True,
    )

    for event in stream:
        if event.type == "response.output_text.delta":
            print(event.delta, end="", flush=True)
        elif event.type == "response.output_item.added":
            item = event.item
            itype = getattr(item, "type", "")
            if itype.endswith("_call") and itype != "function_call":
                # Foundry hosted tool calls: azure_ai_search_call, web_search_call, etc.
                print(f"\n[tool-call:{itype}] starting", flush=True)
        elif event.type == "response.output_item.done":
            item = event.item
            itype = getattr(item, "type", "")
            if itype.endswith("_call") and itype != "function_call":
                # Dump every non-private attribute so we can see query, status, results.
                payload = {}
                for attr in dir(item):
                    if attr.startswith("_"):
                        continue
                    try:
                        val = getattr(item, attr)
                    except Exception:
                        continue
                    if callable(val):
                        continue
                    payload[attr] = val
                print(f"[tool-call:{itype}] done -> {payload}", flush=True)
            elif itype == "message" and item.content and item.content[-1].type == "output_text":
                for annotation in item.content[-1].annotations:
                    if annotation.type == "url_citation":
                        print(
                            f"\nURL Citation: {annotation.url} "
                            f"[{annotation.start_index}:{annotation.end_index}]"
                        )
        elif event.type == "response.completed":
            print()  # trailing newline after the streamed text


def main() -> int:
    settings = load_settings()
    smoke_test(settings["agent_name"])
    return 0


if __name__ == "__main__":
    sys.exit(main())