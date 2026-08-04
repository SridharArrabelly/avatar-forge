"""Loads the model-mode persona prompt.

Only used when Voice Live is bound to a model. In agent mode the persona lives
on the Foundry agent and is baked in at deploy time by
``scripts/setup_foundry_agent.py``; nothing here runs.

Read once and cached: the prompt is fixed for the process lifetime, and a disk
read on the session-start path is latency nobody gets back.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from ..avatar_identity import resolve_avatar_display_name
from .tools import SEARCH_MINUTES_TOOL, SEARCH_WEB_TOOL

logger = logging.getLogger(__name__)

PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "realtime" / "instructions.md"
)

# Everything above the first horizontal rule is commentary for whoever edits the
# file, not instruction for the model. Sending it would spend prefill on every
# turn explaining to the model why its own prompt is short.
SEPARATOR = "\n---\n"

FALLBACK = (
    "You are {{AVATAR_NAME}}, an executive assistant speaking aloud. Ground "
    "every answer in a tool: {{SEARCH_TOOL}} for the organisation's internal "
    "board minutes and official policies, {{WEB_TOOL}} for current public news, "
    "leadership and market activity. Do not answer either from memory. Keep "
    "replies to two or three spoken sentences with no markdown."
)


@lru_cache(maxsize=1)
def _load_body() -> str:
    try:
        text = PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as e:
        # Falling back keeps a packaging mistake from taking the whole voice
        # path down: a terser persona still answers correctly and is grounded.
        logger.error(f"Could not read {PROMPT_PATH}: {e}. Using the fallback prompt.")
        return FALLBACK
    _, sep, body = text.partition(SEPARATOR)
    return (body if sep else text).strip()


def load_realtime_instructions() -> str:
    """The model-mode system prompt with the persona and tool names substituted.

    The name is resolved per call rather than cached with the body — it is the
    one part that can differ between deployments sharing this image.

    The tool placeholders exist because a single authored prompt serves both
    bindings, which register *different* tool names: agent mode has
    azure_ai_search / bing_custom_search, model mode the two below. Naming
    either set literally would leave the other mode describing tools that do
    not exist. The names are read off the registered schemas so a rename in
    tools.py cannot silently desynchronise the prompt from the tool surface.
    """
    return (
        _load_body()
        .replace("{{AVATAR_NAME}}", resolve_avatar_display_name())
        .replace("{{SEARCH_TOOL}}", SEARCH_MINUTES_TOOL["name"])
        .replace("{{WEB_TOOL}}", SEARCH_WEB_TOOL["name"])
    )
