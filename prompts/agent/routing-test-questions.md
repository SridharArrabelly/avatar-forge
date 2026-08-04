# Tool-routing test questions

A quick checklist to verify the agent routes each turn to the correct tool
after changing the routing rules in `instructions.md`.

- **Internal** questions should fire **`azure_ai_search`** (board / exec
  meeting minutes — the only corpus in the AI Search index).
- **External** questions should fire **`bing_custom_search`** (the curated
  web allow-list: MTN investor relations, financial results, leadership,
  newsroom/media, JSE market data, and trusted telecom news / regulators).

After editing the prompts, re-provision the agent so the change goes live:

```powershell
uv run python scripts/setup_foundry_agent.py
```

Then ask each question (live in the browser, or via
`uv run python scripts/test_foundry_agent.py`) and confirm the tool that
fires matches the "Expected" column.

## Core set (10 questions)

### Internal — expect `azure_ai_search`

1. What did we decide about dividends in the last board meeting?
2. What were the action items from the February 2026 board meeting?
3. Who attended the October 2025 board meeting?
4. Summarise the customer experience discussion from the October 2025 board meeting.
5. What strategy did the board agree in the 15 September 2023 meeting?

### External — expect `bing_custom_search`

6. Who is MTN's Group CFO?
7. What was MTN's FY2025 revenue?
8. What is MTN's share price today?
9. What is Vodacom doing in fintech?
10. What is MTN's Ambition 2025?

## Why these matter

- **Q3** — "who attended" must trigger a search, not a deferral ("I need to
  check the record"). This was a real miss before the prompt was tightened.
- **Q5 vs Q10** — the key contrast: *meeting-scoped* strategy ("what did the
  board agree…") is internal, but *general / published* strategy ("MTN's
  Ambition 2025") is public → web.
- **Q6, Q7** — current leadership and published revenue must come from the
  web, never from model memory or the minutes.
- **Q1, Q2** — relative ("last") and named dates confirm the meeting
  catalogue still resolves dates correctly.

## Boundary / edge cases (optional, manual)

- "Who is on MTN's board?" → **web** (public governance, not a meeting).
- "Who chairs the board?" → **web** (current office-holder).
- "What's our revenue?" (note the word *our*) → **web** — "our / we / MTN's"
  must not force an internal lookup.
- "What is MTN's share price on 31 March?" → **web** (a date alone must not
  force internal; only meeting/minutes framing does).
- "Compare what the board discussed on fintech with Airtel's public
  strategy." → **both** (`azure_ai_search` first, then `bing_custom_search`).
- "Compare MTN and Airtel fintech." → **web only** (purely public, no
  internal side).

## Pass criteria

All 10 core questions route to the expected tool. Spot-check that answers
to external questions are tool-grounded (e.g. a named CFO, a revenue figure,
a share price) rather than vague or invented.

---

# Automated harness (`route_test.py`)

The manual checklist above is for a quick eyeball. For repeatable, multi-run
scoring — routing **and** latency **and** answer-quality — use the batch
harness below. It drives the **live** Foundry agent over the same 10
questions, N times, and reports:

- **Routing**: which hosted tool fired per turn (`azure_ai_search` →
  *internal*, `bing_custom_search` → *external*), scored against the expected
  column, per-run and per-question pass rates.
- **Latency**: avg / min / max **total turn time** (note: this is full turn
  time, not time-to-first-token; Bing / AI-Search round-trips dominate).
- **Answers**: writes the full run-1 answer for every question to
  `answers_<label>.txt` so you can compare **answer quality** between configs
  side by side, not just routing.

## How it works (mechanics)

- Hits the agent's per-endpoint OpenAI-protocol URL
  `{PROJECT_ENDPOINT}/agents/{AGENT_NAME}/endpoint/protocols/openai` with an
  AAD bearer token (`DefaultAzureCredential` → scope `https://ai.azure.com/.default`).
- Injects the **meetings catalogue** (via `tfa._fetch_catalog()`, reused from
  `scripts/test_foundry_agent.py`) as a system message before each question —
  this is what lets relative/named-date questions ("last meeting",
  "February 2026") resolve, mirroring the real runtime.
- **Tool detection**: streams the response; collects `output_item.done` items
  whose `type` ends in `_call` (excluding `function_call`). Internal fires as
  `azure_ai_search_call`; external as `bing_custom_search_preview_call`.
  `classify()` maps these to internal/external.
- **Throttling defenses** (critical for the gpt-5.x family): 4-attempt retry
  with `5s * attempt` backoff, plus 1.5s spacing between calls. Without these,
  rapid bursts surface as `ERR` and tank the score (one early un-retried run
  showed 18/30 — almost all transient errors, not real misses). A single very
  large `max` latency (e.g. 500s+) is usually one turn stuck in retry-backoff,
  **not** real inference — read the per-question avg, not the global max.
- ASCII-safe printing + `answers_*.txt` written UTF-8 (agent answers contain
  `【…†source】` citation chars that crash the Windows cp1252 console; set
  `$env:PYTHONIOENCODING='utf-8'`).

## How to run

```powershell
# 1. Provision the config you want to test (env vars WIN over .env):
$env:PYTHONIOENCODING='utf-8'
$env:AGENT_MODEL='gpt-5.4'            # or gpt-5.4-mini, gpt-4.1-mini
$env:AGENT_REASONING_EFFORT='none'   # none|low (only for reasoning models)
# retrieval breadth (defaults to 8/8 now; unset to use defaults):
#   $env:AI_SEARCH_TOP_K='8'; $env:BING_COUNT='8'
uv run python scripts/setup_foundry_agent.py

# 2. Run the harness N times with a label (label names the transcript file):
uv run python <path>/route_test.py --runs 3 --label gpt_5_4_none_8_8
#   -> prints per-run + summary, writes answers_gpt_5_4_none_8_8.txt
```

Notes:
- `setup_foundry_agent.py` `create_version()` is idempotent — re-running with
  the same definition does **not** bump the version; the live runtime
  references the agent by **name** → always uses the latest version. After
  experiments, re-provision the CHOSEN final config so the live agent isn't
  left on an experimental one.
- The model choice no longer swaps the prompt: `instructions.md` is the only
  agent prompt and it is loaded unconditionally, whatever `AGENT_MODEL` is.
- The harness locates the repo root by walking up from the working directory,
  so run it from anywhere inside a clone.

## The harness

```python
"""Batch routing test: drive the live Foundry agent with 10 questions and
report which hosted tool each turn fires (azure_ai_search vs bing_custom_search).

Run from the repo root:  uv run python <path to this file>
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
     if (p / "scripts" / "test_foundry_agent.py").is_file()),
    None,
)
if ROOT is None:
    raise SystemExit("Run this from inside the repo — scripts/test_foundry_agent.py not found.")

spec = importlib.util.spec_from_file_location(
    "tfa", str(ROOT / "scripts" / "test_foundry_agent.py")
)
tfa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tfa)

# (question, expected) where expected in {"internal", "external"}
QUESTIONS = [
    ("What did we decide about dividends in the last board meeting?", "internal"),
    ("What were the action items from the February 2026 board meeting?", "internal"),
    ("Who attended the October 2025 board meeting?", "internal"),
    ("Summarise the customer experience discussion from the October 2025 board meeting.", "internal"),
    ("What strategy did the board agree in the 15 September 2023 meeting?", "internal"),
    ("Who is MTN's Group CFO?", "external"),
    ("What was MTN's FY2025 revenue?", "external"),
    ("What is MTN's share price today?", "external"),
    ("What is Vodacom doing in fintech?", "external"),
    ("What is MTN's Ambition 2025?", "external"),
]


def classify(itype: str) -> str:
    t = itype.lower()
    if "azure" in t or "ai_search" in t:
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
    args = ap.parse_args()

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

    print(f"\n########## CONFIG: {args.label or 'agent'} | runs={args.runs} "
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
    print("  per-question pass rate:")
    for idx, (q, expected) in enumerate(QUESTIONS):
        rate = f"{correct[idx]}/{args.runs}"
        avg = sum(qlat[idx]) / len(qlat[idx]) if qlat[idx] else 0.0
        flag = "" if correct[idx] == args.runs else "  <-- MISS"
        print(f"    Q{idx+1:<2} [{expected:8}] {rate}  ({avg:4.1f}s){flag}  {q[:52]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

---

# Model shootout results (for the record)

All runs: same 10 questions, `n=3` (30 turns total), catalogue injected,
`reasoning.effort=none` unless noted. Routing score = turns that fired the
expected tool.

## @ top_k=5 / count=5 (n=3)

| Config | Routing | Avg turn | Notes |
|---|---|---|---|
| gpt-4.1-mini | 30/30 | 4.8s | perfect routing |
| gpt-5.4-mini / none | 29/30 | **3.5s** | fastest; 1 tool-firing slip |
| gpt-5.4-mini / low | 27/30 | 4.8s | **worst on both axes** (max 10.2s) — `low` dominated, dropped |

## @ top_k=8 / count=8 (n=3) — production breadth

| Config | Routing | Avg turn | Answer quality |
|---|---|---|---|
| **gpt-5.4 (full) / none** | **30/30** | 5.2s (min 3.3 / max 9.5) | **best** — fired every tool; accurate, well-structured |
| gpt-5.4-mini / none | 27/30 | **3.4s** (min 1.6 / max 6.1) | weaker (see below) |
| gpt-4.1-mini | 30/30 | 5.1s | complete but verbose; some cross-meeting blending at 8/8 |

## Answer-quality findings (from `answers_*.txt`)

- **gpt-5.4-mini reproduced the original deferral bug** in one run: Q1
  *"…but I need to check the minutes for the exact decision"* and Q3 *"I do
  not see the names… Want me to pull the full attendance list?"* — i.e. it
  answered/deferred without firing the tool. This is exactly the failure that
  started the routing rewrite.
- **gpt-5.4-mini glitches**: Q8 share price rand/cents confusion
  ("twenty-one thousand two hundred and sixty-nine cents"); Q7 FY revenue came
  back as **R218bn** (vs the more consistent ~R178bn from the full model).
- **gpt-5.4 (full)** fired the right tool on all 30, gave the full attendee
  list (Q3), correctly scoped Q5 to the ESG meeting, and returned FY revenue
  **~R178bn** consistently — no cents glitch. Minor nit: sometimes prefixes
  answers with a literal "Headline:" (odd when spoken; tune in the prompt if
  it persists).
- **gpt-4.1-mini @8/8** stayed perfectly routed but **blended other meetings'
  content into Q5** (the wider top_k=8 contaminated the scope); it also
  volunteered an unprompted CFO salary figure on Q6 (hallucination risk).

## Decision

**Production config: `gpt-5.4` (full) / `reasoning.effort=none` / top_k=8 /
count=8** (reasoning prompt variant). It eliminates the "I need to check"
deferrals that motivated this work (30/30, no deferrals), gives the cleanest
and most accurate answers, at a cost of ~+1.8s/turn vs the mini. `gpt-4.1-mini`
is a strong perfectly-routing fallback; `gpt-5.4-mini` is fastest but still
slips into the deferral failure mode.

> **Known open issue (not model/prompt):** FY2025 revenue is inconsistent
> across runs/models (160bn / 177.8bn / 210.8bn / 218bn). This is a
> web-grounding / source-parsing gap on the allow-listed financial pages
> (service vs total revenue, page formatting), same category as the
> board-of-directors-as-an-image gap on mtn.com/leadership. Routing is
> correct; the fix is Azure-side source coverage, not the prompt.
