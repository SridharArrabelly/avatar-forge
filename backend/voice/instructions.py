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
    "every answer in a tool: search_minutes for the organisation's own board "
    "and executive meetings, search_web for external news and market activity. "
    "Do not answer either from memory. Keep replies to two or three spoken "
    "sentences with no markdown."
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
    """The model-mode system prompt with the persona name substituted.

    The name is resolved per call rather than cached with the body — it is the
    one part that can differ between deployments sharing this image.
    """
    return _load_body().replace("{{AVATAR_NAME}}", resolve_avatar_display_name())
