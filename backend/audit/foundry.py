"""Recovering agent-binding tool I/O from the Foundry conversations API.

Why this module exists: in agent binding the Foundry agent runs AI Search and
web search *inside the agent runtime*, and Voice Live relays nothing to us — no
function call, no output item, no progress event. Verified repeatedly against a
live deployment on turns that demonstrably retrieved. So the tool detail that
issue #30 asks for cannot be observed from the stream at all.

It can, however, be read back afterwards. ``response.created`` carries a
``conversation_id``, and ``conversations.items.list(conversation_id)`` returns
the whole turn:

===========================================  ==================================
item type                                    carries
===========================================  ==================================
``message`` role=user                        the question
``remote_function_call``                     tool name, ``call_id``, arguments
``remote_function_call_output``              matching ``call_id``, retrieved
                                             documents with full passage text
``message`` role=assistant                   the final answer
===========================================  ==================================

Two properties make this safe to rely on:

* Conversations **outlive the session**, so this fetch has no deadline and runs
  entirely off the hot path, in the writer.
* There is **no list operation** on conversations, so the id cannot be
  recovered later. It must be captured live — see ``RESPONSE_CREATED`` in
  ``backend/voice/event_handlers.py``. Miss it and the turn is unauditable.
"""

import asyncio
import json
import logging
from typing import Any, Optional

from .records import ToolCall, TurnRecord

logger = logging.getLogger(__name__)

SOURCE = "foundry-conversation-item"

# The fetch is off the hot path, but it still runs in the writer, so it is
# time-bounded: a slow Foundry call must not back the writer up behind it.
# On timeout the record is written with toolsPending=True — incomplete, but
# explicitly so, which is far better than silently missing tools.
_FETCH_TIMEOUT_S = 10.0

_client = None
_client_lock: Optional[asyncio.Lock] = None


def _get_lock() -> asyncio.Lock:
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


async def _get_client():
    """Build the Foundry OpenAI client once and reuse it.

    Constructed lazily but *outside* any turn, and cached, so no session ever
    pays for TLS setup or token acquisition.
    """
    global _client
    if _client is not None:
        return _client
    async with _get_lock():
        if _client is not None:
            return _client
        try:
            from azure.ai.projects import AIProjectClient

            from ..config import PROJECT_ENDPOINT

            if not PROJECT_ENDPOINT:
                logger.warning("[AUDIT] PROJECT_ENDPOINT unset — cannot reconcile agent tools")
                return None

            credential = await asyncio.to_thread(get_sync_credential)
            project = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=credential)
            _client = await asyncio.to_thread(project.get_openai_client)
            logger.info("[AUDIT] Foundry conversations client ready")
        except Exception as e:
            logger.warning(f"[AUDIT] could not create Foundry client: {e}")
            return None
    return _client


def get_sync_credential():
    """A synchronous credential for the Foundry data plane.

    The app's shared credential is the async flavour, which the projects SDK
    cannot use. This is created once and lives for the process.
    """
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def _as_dict(item: Any) -> dict:
    for attr in ("to_dict", "model_dump"):
        fn = getattr(item, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return item if isinstance(item, dict) else {}


def _parse_args(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def _hit_count(output: Any) -> Optional[int]:
    if isinstance(output, dict):
        docs = output.get("documents")
        if isinstance(docs, list):
            return len(docs)
    if isinstance(output, list):
        return len(output)
    return None


def extract_tools(items: list) -> list[ToolCall]:
    """Turn raw conversation items into :class:`ToolCall` records.

    Calls and their outputs arrive as separate items joined by ``call_id``;
    matching them is what turns "a search happened" into "this query returned
    these passages". Outputs with no matching call are still kept, so a partial
    fetch degrades to less detail rather than to silence.
    """
    calls: dict[str, ToolCall] = {}
    ordered: list[ToolCall] = []
    outputs: dict[str, dict] = {}

    for raw in items:
        item = _as_dict(raw)
        itype = item.get("type")

        if itype == "remote_function_call":
            call_id = item.get("call_id") or item.get("id") or ""
            tool = ToolCall(
                name=item.get("name") or "unknown",
                args=_parse_args(item.get("arguments")),
                source=SOURCE,
                call_id=call_id or None,
            )
            calls[call_id] = tool
            ordered.append(tool)

        elif itype == "remote_function_call_output":
            # Same `id` fallback as the call above. Without it an output item
            # that arrives with no `call_id` collapses onto the key "", becomes
            # an anonymous ToolCall, and — having nothing to dedupe on — is
            # re-reported by every later reconcile in the session.
            call_id = item.get("call_id") or item.get("id") or ""
            outputs[call_id] = item

    for call_id, item in outputs.items():
        output = item.get("output")
        tool = calls.get(call_id)
        if tool is None:
            tool = ToolCall(
                name=item.get("name") or "unknown",
                source=SOURCE,
                call_id=call_id or None,
            )
            ordered.append(tool)
        tool.results = output
        tool.hit_count = _hit_count(output)
        if item.get("status") not in (None, "completed"):
            tool.error = str(item.get("status"))

    return ordered


def _dedupe_key(tool: ToolCall) -> str:
    """Content identity for a tool call that Foundry gave no id.

    ``call_id`` is present on every item observed in practice, but anything
    without one would bypass the ledger entirely and be re-appended on every
    reconcile for the life of the session — reintroducing exactly the unbounded
    growth the ledger exists to prevent.
    """
    if tool.call_id:
        return tool.call_id
    try:
        args = json.dumps(tool.args, sort_keys=True, default=str)
    except Exception:
        # Deliberately not `repr`: it embeds memory addresses for exotic values,
        # which differ on every fetch, so the key would never match and the
        # unbounded growth would return silently.
        args = "<unkeyable>"
    return f"{tool.name}\x00{args}"


def _dedupe_keys(tools: list[ToolCall]) -> list[str]:
    """One stable key per tool, positioned within the session's history.

    Content alone is not an identity: asking the same question twice in one
    session is two real calls, and keying them on content would drop the second
    — writing a turn that did search as ``tools: []``, the one thing the trail
    must never do silently. Anonymous calls therefore carry an ordinal.

    Items arrive newest-first, so ordinals are counted from the **oldest** end.
    History only grows at the newest end, so a given physical call keeps its
    ordinal for the life of the session; counting the other way would shift
    every ordinal on every turn and defeat the ledger entirely.
    """
    counts: dict[str, int] = {}
    keys: list[str] = [""] * len(tools)
    for i in range(len(tools) - 1, -1, -1):
        base = _dedupe_key(tools[i])
        if tools[i].call_id:
            keys[i] = base
            continue
        n = counts.get(base, 0)
        counts[base] = n + 1
        keys[i] = f"{base}\x00#{n}"
    return keys


async def reconcile(record: TurnRecord) -> bool:
    """Fill in ``record.tools`` from Foundry. Returns True on success.

    Runs in the writer task. Never called from the event loop, never awaited by
    anything a user is waiting on.
    """
    if not record.conversation_id:
        return False

    client = await _get_client()
    if client is None:
        return False

    def _fetch() -> list:
        return list(
            client.conversations.items.list(record.conversation_id, limit=100)
        )

    try:
        items = await asyncio.wait_for(
            asyncio.to_thread(_fetch), timeout=_FETCH_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"[AUDIT] Foundry fetch timed out for {record.conversation_id} — "
            f"record kept with toolsPending=true"
        )
        return False
    except Exception as e:
        logger.warning(f"[AUDIT] Foundry fetch failed for {record.conversation_id}: {e}")
        return False

    tools = extract_tools(items)
    if tools:
        # `items` is the whole session, not this turn: the conversations API has
        # no per-response filter and the items carry no timestamp, so the only
        # way to tell new calls from old ones is to remember what has already
        # been attributed. Without this, every turn re-reported all preceding
        # tool calls, growing each document until a long session would breach
        # the 2MB Cosmos item limit and stop the trail entirely.
        #
        # Keep anything already captured in-process; append what only Foundry
        # can see. In practice agent binding yields nothing in-process, so this
        # is normally a plain assignment.
        seen = record.seen_call_ids
        keys = _dedupe_keys(tools)
        known = {_dedupe_key(t) for t in record.tools}
        fresh = []
        for tool, key in zip(tools, keys):
            if key in known or (seen is not None and key in seen):
                continue
            fresh.append(tool)
            known.add(key)
            if seen is not None:
                seen.add(key)
        record.tools.extend(fresh)

    record.tools_pending = False
    return True


async def close() -> None:
    global _client
    if _client is not None:
        try:
            close_fn = getattr(_client, "close", None)
            if callable(close_fn):
                await asyncio.to_thread(close_fn)
        except Exception:
            pass
        _client = None
