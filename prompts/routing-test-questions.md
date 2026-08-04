# Tool-routing test questions

A quick checklist to verify each turn routes to the correct tool, shared by
**both** voice bindings. Run it after changing any routing rule in
`agent/instructions.md` or `realtime/instructions.md`.

- **Internal** questions should hit the **minutes corpus** — `azure_ai_search`
  in agent mode, `search_minutes` in model mode (board / exec meeting minutes,
  the only corpus in the AI Search index).
- **External** questions should hit the **curated web** — `bing_custom_search`
  in agent mode, `search_web` in model mode (MTN investor relations, financial
  results, leadership, newsroom/media, JSE market data, and trusted telecom news
  / regulators).

In **agent mode** the prompt is stored server-side, so re-provision before testing:

```powershell
uv run python scripts/setup_foundry_agent.py
```

In **model mode** the prompt ships in the container image (`azd deploy`) — or hand
the file straight to `scripts/bench_routing_model.py`, which needs no deployment.

Then ask each question (live in the browser, or via a harness below) and confirm
the tool that fires matches the "Expected" column.

## Core set (10 questions)

### Internal — expect the minutes tool

1. What did we decide about dividends in the last board meeting?
2. What were the action items from the February 2026 board meeting?
3. Who attended the October 2025 board meeting?
4. Summarise the customer experience discussion from the October 2025 board meeting.
5. What strategy did the board agree in the 15 September 2023 meeting?

### External — expect the web tool

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

# Automated harnesses

The manual checklist above is for a quick eyeball. For repeatable, multi-run
scoring use the batch harnesses. **The question set itself lives in
`scripts/bench_routing_agent.py` and is imported by both** — that module is the single
source of truth; this file is the prose rationale behind the questions.

| binding | harness | what it drives |
| --- | --- | --- |
| `VOICE_BINDING=agent` | `scripts/bench_routing_agent.py` | the live Foundry agent, over its per-endpoint OpenAI-protocol URL |
| `VOICE_BINDING=model` | `scripts/bench_routing_model.py` | a live Voice Live **model** session over the websocket, registering the app's own in-process tools |

`bench_routing_model.py` imports `TIERS`, `BOUNDARY` and `classify()` from
`bench_routing_agent.py`, so both bindings are scored against **identical questions** and
the two cannot drift apart.

> This file used to embed a copy of the harness source. It went stale — the copy
> still had 10 questions after the real script had grown to 16 — so the source now
> lives only in `scripts/`. Do not paste it back.

## Tool names differ per binding

The questions are binding-agnostic; the tool that should fire is not.

| intent | agent mode | model mode |
| --- | --- | --- |
| internal (minutes) | `azure_ai_search` | `search_minutes` |
| external (web) | `bing_custom_search` | `search_web` |

`classify()` recognises **both** name sets, which is why one scoring function
serves both harnesses. Prompts must not hardcode either set: they use
`{{SEARCH_TOOL}}` / `{{WEB_TOOL}}`, checked by `tests/test_prompt_tool_names.py`.

## Mechanics worth knowing

- **Catalogue injection.** Both harnesses inject the meetings catalogue as a
  system message before each question, mirroring the real runtime — this is what
  lets relative dates ("last meeting", "February 2026") resolve.
- **Throttling (agent mode).** 4-attempt retry with `5s * attempt` backoff plus
  1.5s spacing. Without it, bursts surface as `ERR` and tank the score — one early
  un-retried run showed 18/30, almost all transient errors. A single very large
  `max` latency is usually one turn stuck in backoff, **not** real inference; read
  the per-question average.
- **A fresh session per question (model mode)**, so conversation history cannot
  contaminate later answers.
- **Latency is total turn time, not time-to-first-token.** Retrieval round-trips
  dominate. Do not quote these as production latency figures.
- **Encoding.** Agent answers contain `【...†source】` citation characters that
  crash the Windows cp1252 console — set `$env:PYTHONIOENCODING='utf-8'`.
  Transcripts are written UTF-8 and are gitignored.

## How to run

```powershell
$env:PYTHONIOENCODING='utf-8'

# Agent mode — provision the config first (env vars WIN over .env)
$env:AGENT_MODEL='gpt-5.4'
$env:AGENT_REASONING_EFFORT='none'
uv run python scripts/setup_foundry_agent.py
uv run python scripts/bench_routing_agent.py --runs 3 --label gpt_5_4_none_8_8

# Model mode — no provisioning step; the prompt is passed per session
uv run python scripts/bench_routing_model.py --runs 5 --tier boundary
```

Notes:
- `create_version()` is idempotent — re-running with the same definition does
  **not** bump the version, and the runtime resolves the agent by **name**, so it
  always uses the latest. After experiments, re-provision the CHOSEN config so the
  live agent is not left on an experimental one.
- `AGENT_MODEL` no longer swaps the prompt: `agent/instructions.md` is the only
  agent prompt and is loaded unconditionally.
- Both harnesses locate the repo root by walking up from the working directory.

---

# Model shootout results (agent mode, for the record)

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
count=8.** It eliminates the "I need to check" deferrals that motivated this work
(30/30, no deferrals), gives the cleanest and most accurate answers, at a cost of
~+1.8s/turn vs the mini. `gpt-4.1-mini`
is a strong perfectly-routing fallback; `gpt-5.4-mini` is fastest but still
slips into the deferral failure mode.

> **Known open issue (not model/prompt):** FY2025 revenue is inconsistent
> across runs/models (160bn / 177.8bn / 210.8bn / 218bn). This is a
> web-grounding / source-parsing gap on the allow-listed financial pages
> (service vs total revenue, page formatting), same category as the
> board-of-directors-as-an-image gap on mtn.com/leadership. Routing is
> correct; the fix is Azure-side source coverage, not the prompt.
