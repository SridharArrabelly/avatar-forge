"""Dispatch for tools the model calls by name.

Reached from ``event_handlers.handle_conversation_item`` once the model has
emitted a function call and its arguments. The return value is JSON-serialised
straight back into the conversation, so it must always be a plain dict — an
exception here would strand the turn with the user listening to silence.

Only populated when Voice Live is bound to a model (``VOICE_BINDING=model``).
In agent mode the Foundry agent runs its own tools inside the agent runtime and
nothing arrives here.
"""

import json
import logging

from .tools import search_minutes, search_web

logger = logging.getLogger(__name__)


async def execute_function(name: str, arguments: str) -> dict:
    """Execute a tool by name and return a JSON-serialisable result."""
    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        logger.warning(f"Tool {name}: arguments were not valid JSON: {arguments!r}")
        args = {}
    if not isinstance(args, dict):
        args = {}

    if name == "search_minutes":
        return await search_minutes(query=args.get("query", ""))
    if name == "search_web":
        return await search_web(query=args.get("query", ""))

    logger.warning(f"Model called an unknown tool: {name!r}")
    return {"error": f"Unknown tool: {name}"}
