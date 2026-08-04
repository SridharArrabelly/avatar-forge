"""Routing benchmark for AGENT mode — the counterpart to ``bench_routing_model.py``.

Drives the live Foundry agent over its OpenAI-protocol endpoint and reports which
hosted tool each turn fires (``azure_ai_search`` vs ``bing_custom_search``).

This module also owns the **shared** question set (``CORE``, ``BOUNDARY``,
``TIERS``) and ``classify()``, which the model-mode benchmark imports rather than
copies — so both bindings are scored against identical questions and cannot
silently diverge.

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

# (question, expected) where expected in {"internal", "external"}
#
# The core set is three groups of five. Note what the groups do and do NOT test:
#
#   minutes  + policies  -> BOTH expect "internal", because both corpora sit
#                           behind the SAME hosted tool (azure_ai_search) in the
#                           SAME index. So these five policy questions do NOT
#                           test corpus separation - that is retrieval-level and
#                           is measured separately. What they DO test is that a
#                           policy question does not LEAK TO THE WEB TOOL, which
#                           is a real and previously-shipped failure: a prompt
#                           that says "only meeting minutes are internal" sends
#                           "what is our gift policy" straight to Bing, which
#                           does not hold MTN's internal policies.
#   web                  -> expects "external".
MINUTES = [
    ("What did we decide about dividends in the last board meeting?", "internal"),
    ("What were the action items from the February 2026 board meeting?", "internal"),
    ("Who attended the October 2025 board meeting?", "internal"),
    ("Summarise the customer experience discussion from the October 2025 board meeting.", "internal"),
    ("What strategy did the board agree in the 15 September 2023 meeting?", "internal"),
]

# Ordered by how strongly the surface form pulls towards the web tool, so a
# partial pass still says something: Q1 is the canonical phrasing, Q3 sounds
# like a question about general law rather than an MTN rule.
POLICIES = [
    ("What is our gift policy?", "internal"),
    ("What is the maximum value of a gift I can accept from a supplier?", "internal"),
    ("Who owns a patent created by one of our employees?", "internal"),
    ("Am I eligible for a study bursary?", "internal"),
    ("What does our responsible betting policy say about data breaches?", "internal"),
]

WEB = [
    ("Who is MTN's Group CFO?", "external"),
    ("What was MTN's FY2025 revenue?", "external"),
    ("What is MTN's share price today?", "external"),
    ("What is Vodacom doing in fintech?", "external"),
    ("What is MTN's Ambition 2025?", "external"),
]

CORE = MINUTES + POLICIES + WEB

# The discriminating set. Every one of these is a case where the *surface form*
# of the question pulls the wrong way, so they separate a prompt that states a
# rule from one that merely lists examples. The core set above is saturated at
# 30/30, so it can only catch a regression - improvement has to show up here.
#
# Sourced from "Boundary / edge cases" in prompts/routing-test-questions.md,
# where they were recorded as manual-only.
BOUNDARY = [
    # Public governance facts, despite the word "board".
    ("Who is on MTN's board?", "external"),
    ("Who chairs the board?", "external"),
    # "our / we / MTN's" must not force an internal lookup.
    ("What's our revenue?", "external"),
    # A date alone must not force internal; only meeting/minutes framing does.
    ("What is MTN's share price on 31 March?", "external"),
    # Genuinely both - internal first, then public. Scored on the FIRST tool.
    ("Compare what the board discussed on fintech with Airtel's public strategy.",
     "internal"),
    # Purely public: superficially parallel to the one above, but no internal side.
    ("Compare MTN and Airtel fintech.", "external"),
]

TIERS = {"core": CORE, "boundary": BOUNDARY, "all": CORE + BOUNDARY}

# Reporting only. The question tuples stay 2-wide on purpose: bench_routing_model.py
# unpacks them as ``for idx, (q, expected) in enumerate(questions)``, so widening
# the tuple would break the model-mode harness. Grouping therefore lives beside the
# data, not inside it.
GROUPS = {"minutes": MINUTES, "policies": POLICIES, "web": WEB}
_GROUP_OF = {q: name for name, qs in GROUPS.items() for q, _ in qs}


def group_of(question: str) -> str:
    """Which core group a question belongs to; BOUNDARY questions fall through."""
    return _GROUP_OF.get(question, "boundary")


def classify(itype: str) -> str:
    t = itype.lower()
    # Agent mode fires hosted tools (azure_ai_search_call, bing_custom_search_
    # preview_call); model mode fires our in-process FunctionTools registered in
    # backend/voice/tools.py as search_minutes / search_web. Recognise both so
    # this harness can score either binding.
    if "azure" in t or "ai_search" in t or "minutes" in t:
        return "internal"
    if "bing" in t or "web" in t:
        return "external"
    return f"other:{itype}"


def ask(openai: OpenAI, catalog: str | None, question: str) -> tuple[list[str], str]:
    if catalog:
        request_input = [
            {"type": "message", "role": "system", "content": catalog},
            {"type": "message", "role": "user", "content": question},
        ]
    else:
        request_input = question
    stream = openai.responses.create(
        stream=True,
        tool_choice="auto",
        input=request_input,
        parallel_tool_calls=True,
    )
    tools: list[str] = []
    text_parts: list[str] = []
    for event in stream:
        if event.type == "response.output_text.delta":
            text_parts.append(event.delta)
        elif event.type == "response.output_item.done":
            item = event.item
            itype = getattr(item, "type", "")
            if itype.endswith("_call") and itype != "function_call":
                tools.append(itype)
    return tools, "".join(text_parts).strip()


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
    # per-question correct count across runs; per-question latencies
    correct = [0] * n
    qlat: list[list[float]] = [[] for _ in range(n)]
    run_scores: list[int] = []
    all_lat: list[float] = []
    transcripts: list[str] = []  # full answers (run 1) for quality review

    print(f"\n########## CONFIG: {args.label or 'agent'} | tier={args.tier} "
          f"| runs={args.runs} "
          f"| catalogue={'loaded' if catalog else 'NONE'} ##########")
    for run in range(1, args.runs + 1):
        score = 0
        line = []
        for idx, (q, expected) in enumerate(QUESTIONS):
            t0 = time.perf_counter()
            tools = None
            text = ""
            last_err = ""
            for attempt in range(4):  # retry transient API errors (e.g. 429)
                try:
                    tools, text = ask(openai, catalog, q)
                    break
                except Exception as e:
                    last_err = str(e)
                    time.sleep(5 * (attempt + 1))
            if tools is None:
                line.append(f"Q{idx+1}:ERR")
                print(f"      Q{idx+1} ERR: {last_err[:120]}")
                continue
            dt = time.perf_counter() - t0
            qlat[idx].append(dt)
            all_lat.append(dt)
            primary = ([classify(t) for t in tools] or ["none"])[0]
            ok = (primary == expected)
            if ok:
                correct[idx] += 1
                score += 1
            line.append(f"Q{idx+1}:{'.' if ok else 'X'}")
            if run == 1:  # capture one full transcript per question
                transcripts.append(
                    f"Q{idx+1} [{expected}->{primary} "
                    f"{'OK' if ok else 'MISROUTE'} {dt:.1f}s] {q}\n    {text}\n"
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
    if all_lat:
        print(f"  latency: avg {sum(all_lat)/len(all_lat):.1f}s  "
              f"min {min(all_lat):.1f}s  max {max(all_lat):.1f}s")

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
            avg = sum(lats) / len(lats) if lats else 0.0
            flag = "" if got == want else "   <-- "
            print(f"    {g:<9} {got:>3}/{want:<3}  avg {avg:4.1f}s{flag}")
    print("  per-question pass rate:")
    for idx, (q, expected) in enumerate(QUESTIONS):
        rate = f"{correct[idx]}/{args.runs}"
        avg = sum(qlat[idx]) / len(qlat[idx]) if qlat[idx] else 0.0
        flag = "" if correct[idx] == args.runs else "  <-- MISS"
        print(f"    Q{idx+1:<2} [{expected:8}] {rate}  ({avg:4.1f}s){flag}  {q[:52]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
