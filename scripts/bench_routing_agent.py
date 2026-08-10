"""Routing benchmark for AGENT mode — the counterpart to ``bench_routing_model.py``.

Drives the live Foundry agent over its OpenAI-protocol endpoint and reports which
hosted tool each turn fires (``azure_ai_search`` vs ``bing_custom_search``).

The **shared** question set and ``classify()`` live in ``routing_questions.py``,
which this module and the model-mode benchmark both import rather than copy — so
both bindings are scored against identical questions and cannot silently diverge.
They are re-exported here for callers that still reach for them via this module.

Run from the repo root:  uv run python scripts/bench_routing_agent.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from openai import OpenAI

# Find the repo root by walking up from the working directory, so the harness
# runs from any clone without editing a hardcoded path.
ROOT = next(
    (p for p in (Path.cwd(), *Path.cwd().parents)
     if (p / "scripts" / "smoke_foundry_agent.py").is_file()),
    None,
)
if ROOT is None:
    raise SystemExit("Run this from inside the repo — scripts/smoke_foundry_agent.py not found.")

spec = importlib.util.spec_from_file_location(
    "tfa", str(ROOT / "scripts" / "smoke_foundry_agent.py")
)
tfa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tfa)

# The question set and classifier live in routing_questions.py, imported by BOTH
# benchmarks so the two bindings cannot be scored against different questions.
# Loaded by path because scripts/ is not a package; it imports nothing beyond the
# standard library, so this costs nothing.
_qspec = importlib.util.spec_from_file_location(
    "routing_questions", str(ROOT / "scripts" / "routing_questions.py")
)
routing_questions = importlib.util.module_from_spec(_qspec)
_qspec.loader.exec_module(routing_questions)

MINUTES = routing_questions.MINUTES
POLICIES = routing_questions.POLICIES
WEB = routing_questions.WEB
CORE = routing_questions.CORE
BOUNDARY = routing_questions.BOUNDARY
TIERS = routing_questions.TIERS
GROUPS = routing_questions.GROUPS
group_of = routing_questions.group_of
classify = routing_questions.classify


def ask(
    openai: OpenAI, catalog: str | None, question: str
) -> tuple[list[str], str, float | None, float]:
    if catalog:
        request_input = [
            {"type": "message", "role": "system", "content": catalog},
            {"type": "message", "role": "user", "content": question},
        ]
    else:
        request_input = question
    started = time.perf_counter()
    stream = openai.responses.create(
        stream=True,
        tool_choice="auto",
        input=request_input,
        parallel_tool_calls=True,
    )
    tools: list[str] = []
    text_parts: list[str] = []
    first_token: float | None = None
    for event in stream:
        if event.type == "response.output_text.delta":
            if first_token is None and event.delta:
                first_token = time.perf_counter() - started
            text_parts.append(event.delta)
        elif event.type == "response.output_item.done":
            item = event.item
            itype = getattr(item, "type", "")
            if itype.endswith("_call") and itype != "function_call":
                tools.append(itype)
    return (
        tools,
        "".join(text_parts).strip(),
        first_token,
        time.perf_counter() - started,
    )


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--label", default="")
    ap.add_argument("--tier", choices=sorted(TIERS), default="core",
                    help="core = the saturated regression guard; boundary = the "
                         "ambiguous cases that actually discriminate; all = both")
    args = ap.parse_args()

    QUESTIONS = TIERS[args.tier]

    load_dotenv(dotenv_path=str(ROOT / ".env"))
    agent_name = os.environ["AGENT_NAME"]
    project_endpoint = os.environ["PROJECT_ENDPOINT"].rstrip("/")
    catalog = tfa._fetch_catalog()

    agent_base_url = f"{project_endpoint}/agents/{agent_name}/endpoint/protocols/openai"
    cred = DefaultAzureCredential()
    token = cred.get_token("https://ai.azure.com/.default").token
    openai = OpenAI(
        base_url=agent_base_url,
        api_key=token,
        default_query={"api-version": "v1"},
    )

    n = len(QUESTIONS)
    # Per-question correct count, time-to-first-token and completion latency.
    correct = [0] * n
    qlat: list[list[float]] = [[] for _ in range(n)]
    qfirst: list[list[float]] = [[] for _ in range(n)]
    run_scores: list[int] = []
    all_lat: list[float] = []
    all_first: list[float] = []
    transcripts: list[str] = []  # full answers (run 1) for quality review

    print(f"\n########## CONFIG: {args.label or 'agent'} | tier={args.tier} "
          f"| runs={args.runs} "
          f"| catalogue={'loaded' if catalog else 'NONE'} ##########")
    for run in range(1, args.runs + 1):
        score = 0
        line = []
        for idx, (q, expected) in enumerate(QUESTIONS):
            tools = None
            text = ""
            first_token = None
            dt = 0.0
            last_err = ""
            for attempt in range(4):  # retry transient API errors (e.g. 429)
                try:
                    tools, text, first_token, dt = ask(openai, catalog, q)
                    break
                except Exception as e:
                    last_err = str(e)
                    time.sleep(5 * (attempt + 1))
            if tools is None:
                line.append(f"Q{idx+1}:ERR")
                print(f"      Q{idx+1} ERR: {last_err[:120]}")
                continue
            qlat[idx].append(dt)
            all_lat.append(dt)
            if first_token is not None:
                qfirst[idx].append(first_token)
                all_first.append(first_token)
            primary = ([classify(t) for t in tools] or ["none"])[0]
            ok = (primary == expected)
            if ok:
                correct[idx] += 1
                score += 1
            line.append(f"Q{idx+1}:{'.' if ok else 'X'}")
            if run == 1:  # capture one full transcript per question
                first_label = (
                    f"{first_token:.1f}s" if first_token is not None else "n/a"
                )
                transcripts.append(
                    f"Q{idx+1} [{expected}->{primary} "
                    f"{'OK' if ok else 'MISROUTE'} "
                    f"first={first_label} complete={dt:.1f}s] {q}\n    {text}\n"
                )
            time.sleep(1.5)  # space calls to avoid burst throttling
        run_scores.append(score)
        print(f"  run {run}: {score}/{n}  [{' '.join(line)}]")

    if transcripts:
        safe_label = "".join(c if c.isalnum() else "_" for c in args.label)
        out = Path(__file__).resolve().parent / f"answers_{safe_label}.txt"
        out.write_text("\n".join(transcripts), encoding="utf-8")
        print(f"  (transcripts -> {out.name})")

    total = args.runs * n
    print("-" * 70)
    print(f"SUMMARY [{args.label}]: {sum(run_scores)}/{total} correct "
          f"(runs: {run_scores})")
    if all_first:
        print(
            f"  first token: avg {sum(all_first)/len(all_first):.1f}s  "
            f"min {min(all_first):.1f}s  max {max(all_first):.1f}s  "
            f"n={len(all_first)}"
        )
    missing_first = len(all_lat) - len(all_first)
    if missing_first:
        print(f"  first token: missing on {missing_first} completed turn(s)")
    if all_lat:
        print(
            f"  completion : avg {sum(all_lat)/len(all_lat):.1f}s  "
            f"min {min(all_lat):.1f}s  max {max(all_lat):.1f}s  "
            f"n={len(all_lat)}"
        )

    # Per-group breakdown. A headline score hides the failure that matters most:
    # policies leaking to the web tool shows up as a 5-point drop in ONE group
    # while minutes and web stay perfect.
    seen = [g for g in ("minutes", "policies", "web", "boundary")
            if any(group_of(q) == g for q, _ in QUESTIONS)]
    if len(seen) > 1:
        print("  by group:")
        for g in seen:
            idxs = [i for i, (q, _) in enumerate(QUESTIONS) if group_of(q) == g]
            got = sum(correct[i] for i in idxs)
            want = len(idxs) * args.runs
            lats = [x for i in idxs for x in qlat[i]]
            firsts = [x for i in idxs for x in qfirst[i]]
            avg_total = sum(lats) / len(lats) if lats else 0.0
            first_label = (
                f"{sum(firsts) / len(firsts):4.1f}s" if firsts else " n/a "
            )
            flag = "" if got == want else "   <-- "
            print(
                f"    {g:<9} {got:>3}/{want:<3}  "
                f"first {first_label}  complete {avg_total:4.1f}s{flag}"
            )
    print("  per-question pass rate:")
    for idx, (q, expected) in enumerate(QUESTIONS):
        rate = f"{correct[idx]}/{args.runs}"
        avg_total = sum(qlat[idx]) / len(qlat[idx]) if qlat[idx] else 0.0
        first_label = (
            f"{sum(qfirst[idx]) / len(qfirst[idx]):4.1f}s"
            if qfirst[idx]
            else " n/a "
        )
        flag = "" if correct[idx] == args.runs else "  <-- MISS"
        print(
            f"    Q{idx+1:<2} [{expected:8}] {rate}  "
            f"(first {first_label} / complete {avg_total:4.1f}s)"
            f"{flag}  {q[:52]}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
