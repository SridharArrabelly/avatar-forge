"""Routing A/B for MODEL mode — the counterpart to ``route_test.py``.

``route_test.py`` drives the Foundry *agents* REST endpoint, so it can only ever
score agent mode. This drives a real Voice Live **model** session over the
websocket and registers the same in-process tools the app registers
(``backend/voice/tools.py``), so a prompt can be scored on the binding it will
actually run on.

Method, mirroring the agent-mode A/B:

* Both arms run in **one process** against **one session shape**. The only field
  that differs between them is ``instructions``. Everything else — tools, tool
  choice, catalogue injection, model, region — is identical.
* Arms are **interleaved per round** (``--runs`` rounds, A then B each round) so
  service-side drift lands on both arms rather than on whichever ran second.
* The questions and the classifier are imported from ``route_test.py`` rather
  than copied, so the two harnesses cannot silently diverge. ``classify()``
  already recognises the model-mode tool names.

What this measures is **routing** — which tool the model reaches for — plus a
wall-clock figure for the whole turn. That timing is time-to-**completion**, not
time-to-first-audio; a wordier prompt inflates it directly.

Run from the repo root:

    uv run python scripts/route_test_model.py --runs 5 --tier boundary
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = next(
    (
        p
        for p in (Path.cwd(), *Path.cwd().parents)
        if (p / "scripts" / "route_test.py").is_file()
    ),
    None,
)
if ROOT is None:
    raise SystemExit("Run this from inside the repo — scripts/route_test.py not found.")


def hydrate_azd_env() -> str:
    """Pull the selected azd environment into ``os.environ``.

    There is no repo-root ``.env``; the values live in ``.azure/<env>/.env`` and
    are normally injected by the container. Existing variables win, so an
    explicit ``$env:FOO`` override still takes effect.
    """
    try:
        out = subprocess.run(
            ["azd", "env", "get-values"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
            shell=True,
        )
    except Exception as e:  # pragma: no cover - environment problem, not logic
        raise SystemExit(f"Could not run `azd env get-values`: {e}")
    if out.returncode != 0:
        raise SystemExit(f"`azd env get-values` failed:\n{out.stderr.strip()}")
    for line in out.stdout.splitlines():
        m = re.match(r'^([A-Za-z0-9_]+)="?(.*?)"?$', line.strip())
        if m and m.group(1) not in os.environ:
            os.environ[m.group(1)] = m.group(2)
    return os.environ.get("AZURE_ENV_NAME", "?")


ENV_NAME = hydrate_azd_env()

# Imported only after hydration: backend.config snapshots os.environ at import.
sys.path.insert(0, str(ROOT))

from azure.ai.voicelive.aio import connect  # noqa: E402
from azure.ai.voicelive.models import (  # noqa: E402
    FunctionCallOutputItem,
    InputTextContentPart,
    Modality,
    RequestSession,
    ServerEventType,
    SystemMessageItem,
    UserMessageItem,
)
from azure.identity.aio import DefaultAzureCredential  # noqa: E402

from backend.avatar_identity import resolve_avatar_display_name  # noqa: E402
from backend.config import VOICELIVE_API_VERSION, VOICELIVE_MODEL  # noqa: E402
from backend.voice.catalog import get_meeting_catalog  # noqa: E402
from backend.voice.functions import execute_function  # noqa: E402
from backend.voice.tools import (  # noqa: E402
    SEARCH_MINUTES_TOOL,
    SEARCH_WEB_TOOL,
    build_realtime_tools,
    web_search_configured,
)

_spec = importlib.util.spec_from_file_location(
    "route_test", str(ROOT / "scripts" / "route_test.py")
)
route_test = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(route_test)

TIERS = route_test.TIERS
classify = route_test.classify

SEPARATOR = "\n---\n"
TURN_TIMEOUT = 90.0


def load_prompt(path: Path) -> str:
    """Read a realtime prompt exactly the way the app reads it.

    Mirrors ``backend/voice/instructions.py``: strip the authoring commentary
    above the first horizontal rule, then substitute the persona name and the
    tool-name placeholders. Reproduced rather than imported because that module
    hardcodes a single path and we need to load either file.
    """
    text = path.read_text(encoding="utf-8")
    _, sep, body = text.partition(SEPARATOR)
    body = (body if sep else text).strip()
    return (
        body.replace("{{AVATAR_NAME}}", resolve_avatar_display_name())
        .replace("{{SEARCH_TOOL}}", SEARCH_MINUTES_TOOL["name"])
        .replace("{{WEB_TOOL}}", SEARCH_WEB_TOOL["name"])
    )


def _evt_name(evt) -> str:
    """Normalised event name that matches either shape the SDK may hand back.

    ``str()`` on the enum gives ``ServerEventType.SESSION_UPDATED`` while the
    raw wire value is ``session.updated``. Upper-casing and mapping ``.`` to
    ``_`` collapses both to a form ending in ``SESSION_UPDATED``, so a change in
    how the SDK deserialises cannot silently stop every match.
    """
    return str(getattr(evt, "type", "")).upper().replace(".", "_")


def _is_function_call(item) -> bool:
    name = str(getattr(item, "type", "")).upper().replace(".", "_")
    return name.endswith("FUNCTION_CALL")


async def ask(
    endpoint: str,
    credential,
    instructions: str,
    tools: list[dict],
    catalog: str | None,
    question: str,
) -> tuple[list[str], str, float]:
    """One question on a fresh session. Returns (tools fired, answer, seconds).

    A new session per question keeps the arms from contaminating each other
    through conversation history — the same isolation the agent-mode harness got
    for free by creating a new response each time.
    """
    tools_called: list[str] = []
    text_parts: list[str] = []
    call_names: dict[str, str] = {}
    pending: dict[str, object] = {}

    async with connect(
        endpoint=endpoint,
        credential=credential,
        api_version=VOICELIVE_API_VERSION,
        model=VOICELIVE_MODEL,
    ) as conn:
        await conn.session.update(
            session=RequestSession(
                modalities=[Modality.TEXT],
                instructions=instructions,
                tools=tools,
                tool_choice="auto",
            )
        )
        deadline = time.monotonic() + 30
        async for evt in conn:
            if _evt_name(evt).endswith("SESSION_UPDATED"):
                break
            if _evt_name(evt).endswith("ERROR"):
                raise RuntimeError(f"session.update rejected: {_err(evt)}")
            if time.monotonic() > deadline:
                raise TimeoutError("no SESSION_UPDATED")

        if catalog:
            await conn.conversation.item.create(
                item=SystemMessageItem(content=[InputTextContentPart(text=catalog)])
            )
        await conn.conversation.item.create(
            item=UserMessageItem(content=[InputTextContentPart(text=question)])
        )

        t0 = time.perf_counter()
        await conn.response.create()

        deadline = time.monotonic() + TURN_TIMEOUT
        async for evt in conn:
            name = _evt_name(evt)

            if name.endswith("ERROR"):
                raise RuntimeError(_err(evt))

            item = getattr(evt, "item", None)
            if item is not None and _is_function_call(item):
                cid = getattr(item, "call_id", None)
                fname = getattr(item, "name", None)
                if cid and fname:
                    call_names[cid] = fname
                    if fname not in tools_called:
                        tools_called.append(fname)

            elif "FUNCTION_CALL_ARGUMENTS_DONE" in name:
                cid = getattr(evt, "call_id", "")
                fname = call_names.get(cid, "")
                if fname and fname not in tools_called:
                    tools_called.append(fname)
                pending[cid] = asyncio.create_task(
                    execute_function(fname, getattr(evt, "arguments", "") or "")
                )

            elif "DELTA" in name and ("TEXT" in name or "TRANSCRIPT" in name):
                delta = getattr(evt, "delta", None)
                if isinstance(delta, str):
                    text_parts.append(delta)

            elif name.endswith("RESPONSE_DONE"):
                if not pending:
                    break
                # Feed every completed tool result back, then ask for the answer.
                for cid, task in list(pending.items()):
                    result = await task
                    await conn.conversation.item.create(
                        item=FunctionCallOutputItem(
                            call_id=cid, output=json.dumps(result)
                        )
                    )
                pending.clear()
                await conn.response.create()

            if time.monotonic() > deadline:
                raise TimeoutError(f"turn exceeded {TURN_TIMEOUT:.0f}s")

        return tools_called, "".join(text_parts).strip(), time.perf_counter() - t0


def _err(evt) -> str:
    e = getattr(evt, "error", None)
    return str(getattr(e, "message", None) or e or "unknown error")[:200]


async def run_arm(
    label: str,
    instructions: str,
    questions: list[tuple[str, str]],
    endpoint: str,
    credential,
    tools: list[dict],
    catalog: str | None,
) -> dict:
    score = 0
    marks = []
    lats: list[float] = []
    transcripts: list[str] = []

    for idx, (q, expected) in enumerate(questions):
        try:
            fired, text, dt = await ask(
                endpoint, credential, instructions, tools, catalog, q
            )
        except Exception as e:
            marks.append(f"Q{idx+1}:ERR")
            transcripts.append(f"Q{idx+1} [ERROR] {q}\n    {type(e).__name__}: {e}\n")
            continue
        primary = ([classify(t) for t in fired] or ["none"])[0]
        ok = primary == expected
        score += ok
        lats.append(dt)
        marks.append(f"Q{idx+1}:{'.' if ok else 'X'}")
        transcripts.append(
            f"Q{idx+1} [{expected}->{primary} "
            f"{'OK' if ok else 'MISROUTE'} {dt:.1f}s] {q}\n    {text}\n"
        )
        await asyncio.sleep(1.5)

    return {
        "label": label,
        "score": score,
        "n": len(questions),
        "marks": marks,
        "lats": lats,
        "transcripts": transcripts,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3, help="interleaved rounds per arm")
    ap.add_argument("--tier", choices=sorted(TIERS), default="boundary")
    ap.add_argument(
        "--arms",
        default="LIVE=prompts/realtime/instructions.md",
        help="comma-separated LABEL=path pairs; give two or more to A/B them",
    )
    args = ap.parse_args()

    questions = TIERS[args.tier]
    arms: list[tuple[str, str]] = []
    for spec in args.arms.split(","):
        label, _, rel = spec.partition("=")
        path = ROOT / rel.strip()
        if not path.is_file():
            raise SystemExit(f"Prompt not found: {path}")
        arms.append((label.strip(), load_prompt(path)))

    endpoint = os.environ.get("AZURE_VOICELIVE_ENDPOINT", "").strip()
    if not endpoint:
        raise SystemExit("AZURE_VOICELIVE_ENDPOINT is not set in this azd environment.")

    tools = build_realtime_tools()
    credential = DefaultAzureCredential()
    try:
        catalog = await get_meeting_catalog()

        print("=" * 74)
        print(f"MODEL-mode routing A/B — env={ENV_NAME}  model={VOICELIVE_MODEL}")
        print(f"  tier={args.tier} ({len(questions)} questions)  rounds={args.runs}")
        print(f"  tools registered : {[t['name'] for t in tools]}")
        print(f"  web search       : {'ON' if web_search_configured() else 'OFF'}")
        print(f"  catalogue        : {len(catalog) if catalog else 0} chars")
        for label, text in arms:
            print(f"  arm {label:<4}        : {len(text)} chars")
        print("=" * 74)
        if not web_search_configured():
            print(
                "  WARNING: the web tool is not registered, so every 'external' "
                "question\n           has nowhere correct to route. Set "
                "WEBIQ_API_KEY before trusting\n           these numbers.\n"
            )

        results: dict[str, list[dict]] = {label: [] for label, _ in arms}
        for rnd in range(1, args.runs + 1):
            # Alternate which arm leads so neither always runs on a cold service.
            order = arms if rnd % 2 else list(reversed(arms))
            for label, instructions in order:
                r = await run_arm(
                    label, instructions, questions, endpoint, credential, tools, catalog
                )
                results[label].append(r)
                avg = sum(r["lats"]) / len(r["lats"]) if r["lats"] else 0.0
                print(
                    f"  round {rnd} {label:<4}: {r['score']}/{r['n']}  "
                    f"avg {avg:4.1f}s  [{' '.join(r['marks'])}]"
                )

        out_dir = Path(__file__).resolve().parent
        print("\n" + "-" * 74)
        for label, rounds in results.items():
            total = sum(r["score"] for r in rounds)
            n = sum(r["n"] for r in rounds)
            lats = [x for r in rounds for x in r["lats"]]
            avg = sum(lats) / len(lats) if lats else 0.0
            body = "\n".join(t for r in rounds for t in r["transcripts"])
            (out_dir / f"answers_model_{label}.txt").write_text(body, encoding="utf-8")
            print(f"  {label:<4}: routing {total}/{n}   latency avg {avg:.2f}s")
        print(f"  transcripts -> scripts/answers_model_<ARM>.txt")
        print("-" * 74)
    finally:
        await credential.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
