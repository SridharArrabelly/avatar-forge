"""Routing A/B for MODEL mode — the counterpart to ``bench_routing_agent.py``.

``bench_routing_agent.py`` drives the Foundry *agents* REST endpoint, so it can
only ever score agent mode. This drives a real Voice Live **model** session over
the websocket and registers the same in-process tools the app registers
(``backend/voice/tools.py``), so a prompt can be scored on the binding it will
actually run on.

Method, mirroring the agent-mode A/B:

* Both arms run in **one process** against **one session shape**. The only field
  that differs between them is ``instructions``. Everything else — tools, tool
  choice, catalogue injection, model, region — is identical.
* Arms are **interleaved per round** (``--runs`` rounds, A then B each round) so
  service-side drift lands on both arms rather than on whichever ran second.
* The questions and the classifier are imported from ``routing_questions.py``
  rather than copied, so the two harnesses cannot silently diverge. ``classify()``
  already recognises the model-mode tool names.

What this measures is **routing** plus two separate latency figures:
time-to-first-token (the useful proxy for first audible word) and full turn
completion. Never substitute completion time for perceived voice latency.

Run from the repo root:

    uv run python scripts/bench_routing_model.py --runs 5 --tier boundary
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
        if (p / "scripts" / "routing_questions.py").is_file()
    ),
    None,
)
if ROOT is None:
    raise SystemExit("Run this from inside the repo — scripts/routing_questions.py not found.")


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
from backend.voice.auth import close_credential  # noqa: E402
from backend.voice.catalog import close_search_client, get_meeting_catalog  # noqa: E402
from backend.voice.functions import execute_function  # noqa: E402
from backend.voice.tools import (  # noqa: E402
    SEARCH_MINUTES_TOOL,
    SEARCH_WEB_TOOL,
    build_realtime_tools,
    close_web_client,
    web_search_available,
)

_spec = importlib.util.spec_from_file_location(
    "routing_questions", str(ROOT / "scripts" / "routing_questions.py")
)
routing_questions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(routing_questions)

# Imported, never copied: routing_questions.py owns the shared question set, so the
# two bindings cannot silently drift apart and be scored against different questions.
# It is imported directly rather than through bench_routing_agent.py — going via the
# agent harness would exec that module and, transitively, smoke_foundry_agent.py,
# pulling azure.identity / azure.search.documents / openai into a model-mode run
# that needs none of them.
TIERS = routing_questions.TIERS
classify = routing_questions.classify

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
) -> tuple[list[str], str, float | None, float]:
    """One question on a fresh session.

    Returns (tools fired, answer, first-token seconds, completion seconds).

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
        first_token: float | None = None
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
                    if first_token is None and delta:
                        first_token = time.perf_counter() - t0
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

        return (
            tools_called,
            "".join(text_parts).strip(),
            first_token,
            time.perf_counter() - t0,
        )


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
    firsts: list[float] = []
    transcripts: list[str] = []

    for idx, (q, expected) in enumerate(questions):
        try:
            fired, text, first_token, dt = await ask(
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
        if first_token is not None:
            firsts.append(first_token)
        marks.append(f"Q{idx+1}:{'.' if ok else 'X'}")
        first_label = f"{first_token:.1f}s" if first_token is not None else "n/a"
        transcripts.append(
            f"Q{idx+1} [{expected}->{primary} "
            f"{'OK' if ok else 'MISROUTE'} "
            f"first={first_label} complete={dt:.1f}s] {q}\n    {text}\n"
        )
        await asyncio.sleep(1.5)

    return {
        "label": label,
        "score": score,
        "n": len(questions),
        "marks": marks,
        "lats": lats,
        "firsts": firsts,
        "transcripts": transcripts,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3, help="interleaved rounds per arm")
    ap.add_argument("--tier", choices=sorted(TIERS), default="boundary")
    ap.add_argument(
        "--groups",
        default="",
        help=(
            "optional comma-separated core groups: minutes,policies,web. "
            "When Web IQ is unavailable its schema remains as a routing-only stub, "
            "so internal questions still have a real competing tool."
        ),
    )
    ap.add_argument(
        "--arms",
        default="LIVE=prompts/realtime/instructions.md",
        help="comma-separated LABEL=path pairs; give two or more to A/B them",
    )
    args = ap.parse_args()

    questions = TIERS[args.tier]
    if args.groups:
        group_of = routing_questions.group_of
        requested = {g.strip() for g in args.groups.split(",") if g.strip()}
        unknown = requested - set(routing_questions.GROUPS)
        if unknown:
            raise SystemExit(f"Unknown --groups values: {sorted(unknown)}")
        questions = [(q, expected) for q, expected in questions if group_of(q) in requested]
        if not questions:
            raise SystemExit(
                f"No {args.tier!r} questions matched groups {sorted(requested)}."
            )
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

    web_live = await web_search_available()
    tools = await build_realtime_tools()
    if not web_live:
        # Routing can only be measured when both choices exist. The production
        # app correctly omits an unusable web tool; this benchmark keeps its
        # schema as a competitor and lets search_web return its local
        # "not configured" error if the model misroutes to it.
        tools.append(SEARCH_WEB_TOOL)
    credential = DefaultAzureCredential()
    try:
        catalog = await get_meeting_catalog()

        print("=" * 74)
        print(f"MODEL-mode routing A/B — env={ENV_NAME}  model={VOICELIVE_MODEL}")
        print(f"  tier={args.tier} ({len(questions)} questions)  rounds={args.runs}")
        print(f"  tools registered : {[t['name'] for t in tools]}")
        print(
            "  web search       : "
            + ("LIVE" if web_live else "STUB (routing only; no network call)")
        )
        print(f"  catalogue        : {len(catalog) if catalog else 0} chars")
        for label, text in arms:
            print(f"  arm {label:<4}        : {len(text)} chars")
        print("=" * 74)
        if not web_live and any(
            expected == "external" for _, expected in questions
        ):
            print(
                "  WARNING: external routing can be scored against the stub, but "
                "answer quality\n           cannot. Set WEBIQ_API_KEY before "
                "trusting external answers.\n"
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
                first = (
                    sum(r["firsts"]) / len(r["firsts"]) if r["firsts"] else 0.0
                )
                complete = sum(r["lats"]) / len(r["lats"]) if r["lats"] else 0.0
                missing = len(r["lats"]) - len(r["firsts"])
                print(
                    f"  round {rnd} {label:<4}: {r['score']}/{r['n']}  "
                    f"first {first:4.1f}s  complete {complete:4.1f}s  "
                    f"missing-first={missing}  [{' '.join(r['marks'])}]"
                )

        out_dir = Path(__file__).resolve().parent
        print("\n" + "-" * 74)
        for label, rounds in results.items():
            total = sum(r["score"] for r in rounds)
            n = sum(r["n"] for r in rounds)
            lats = [x for r in rounds for x in r["lats"]]
            firsts = [x for r in rounds for x in r["firsts"]]
            avg_total = sum(lats) / len(lats) if lats else 0.0
            avg_first = sum(firsts) / len(firsts) if firsts else 0.0
            missing = len(lats) - len(firsts)
            body = "\n".join(t for r in rounds for t in r["transcripts"])
            (out_dir / f"answers_model_{label}.txt").write_text(body, encoding="utf-8")
            print(
                f"  {label:<4}: routing {total}/{n}   "
                f"first-token avg {avg_first:.2f}s (n={len(firsts)}, "
                f"missing={missing})   completion avg {avg_total:.2f}s "
                f"(n={len(lats)})"
            )
        print(f"  transcripts -> scripts/answers_model_<ARM>.txt")
        print("-" * 74)
    finally:
        # The benchmark imports the same process-wide clients as FastAPI but does
        # not run the app lifespan that normally closes them. Release them here
        # so a successful run does not finish with aiohttp leak warnings.
        await close_web_client()
        await close_search_client()
        await close_credential()
        await credential.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
