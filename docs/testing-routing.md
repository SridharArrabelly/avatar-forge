# Tool-routing test questions

A quick checklist to verify each turn routes to the correct tool, shared by
**both** voice bindings. Run it after changing any routing rule in
`prompts/agent/instructions.md` or `prompts/realtime/instructions.md`.

- **Internal** questions should hit the **AI Search index** — `azure_ai_search`
  in agent mode, `search_minutes` in model mode. That index holds **two**
  corpora: board / exec **meeting minutes**, and MTN **policy documents**.
  Both sit behind the *same* tool, so both are scored `internal`.
- **External** questions should hit the **curated web** — `bing_custom_search`
  in agent mode, `search_web` in model mode (MTN investor relations, financial
  results, leadership, newsroom/media, JSE market data, and trusted telecom news
  / regulators).

> **What the policy questions do and do not test.** Because minutes and policies
> share one tool and one index, these questions cannot test *which corpus* a hit
> came from — that is retrieval-level and is measured separately. What they test
> is that a policy question **does not leak to the web tool**. That is a real,
> previously-shipped failure: a prompt asserting "only meeting minutes are
> internal" sends *"what is our gift policy"* to Bing, which does not hold MTN's
> internal policies.

In **agent mode** the prompt is stored server-side, so re-provision before testing:

```powershell
uv run python scripts/setup_foundry_agent.py
```

In **model mode** the prompt ships in the container image (`azd deploy`) — or hand
the file straight to `scripts/bench_routing_model.py`, which needs no deployment.

Then ask each question (live in the browser, or via a harness below) and confirm
the tool that fires matches the "Expected" column.

## Core set (15 questions — 5 minutes / 5 policies / 5 web)

### Minutes — expect the AI Search tool

1. What did we decide about dividends in the last board meeting?
2. What were the action items from the February 2026 board meeting?
3. Who attended the October 2025 board meeting?
4. Summarise the customer experience discussion from the October 2025 board meeting.
5. What strategy did the board agree in the 15 September 2023 meeting?

### Policies — expect the AI Search tool (same tool, same index)

Ordered by how strongly the surface form pulls towards the web tool, so a
partial pass still tells you something.

6. What is our gift policy?
7. What is the maximum value of a gift I can accept from a supplier?
8. Who owns a patent created by one of our employees?
9. Am I eligible for a study bursary?
10. What does our responsible betting policy say about data breaches?

### Web — expect the web tool

11. Who is MTN's Group CFO?
12. What was MTN's FY2025 revenue?
13. What is MTN's share price today?
14. What is Vodacom doing in fintech?
15. What is MTN's Ambition 2025?

These check *routing only*. For whether the web tool came back with usable
**sources**, see [Web retrieval quality](#web-retrieval-quality-manual--not-scored-by-the-harnesses)
below — Q12 appears in both, scored differently in each.

## Why these matter

- **Q3** — "who attended" must trigger a search, not a deferral ("I need to
  check the record"). This was a real miss before the prompt was tightened.
- **Q5 vs Q15** — the key contrast: *meeting-scoped* strategy ("what did the
  board agree…") is internal, but *general / published* strategy ("MTN's
  Ambition 2025") is public → web.
- **Q11, Q12** — current leadership and published revenue must come from the
  web, never from model memory or the minutes.
- **Q1, Q2** — relative ("last") and named dates confirm the meeting
  catalogue still resolves dates correctly.

The five policy questions each probe a different way of *not* looking like an
internal question:

- **Q6** — the canonical phrasing. If this leaks to the web, the prompt's
  routing rule is simply wrong and nothing below it matters.
- **Q7** — a rule question that never says the word "policy". Routing must key
  on *"is this an MTN rule?"*, not on a keyword.
- **Q8** — **highest leak risk.** Patent ownership sounds like a question about
  general law, so the pull towards the web is strongest here. MTN's IP policy is
  what actually answers it.
- **Q9** — first-person HR shape ("am I eligible…"), which reads as a personal
  question rather than a document lookup.
- **Q10** — names the policy explicitly. This one is the regression guard: if
  *this* misroutes, routing is broken outright rather than merely ambiguous.

Note the deliberate collision with **Q12** ("our revenue" → web) and the
boundary case *"what's our revenue?"*: the word *our* does **not** decide
routing. What follows it does — *our revenue* is a published fact, *our gift
policy* is an internal rule.

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

All 15 core questions route to the expected tool. `bench_routing_agent.py`
prints a **per-group** breakdown as well as the headline, because the failure
that matters most hides inside a good total: a prompt that treats only minutes
as internal still scores 10/15 with minutes and web perfect, and every drop
concentrated in the policies group. Read the groups, not the total.

Spot-check that answers to web questions are tool-grounded (a named CFO, a
revenue figure, a share price) rather than vague or invented, and that policy
answers name the document in human form ("the Gift Policy") rather than a
filename.

**Not covered here:** whether a policy answer came from the *right* policy, and
whether the agent refuses a policy-shaped question with no matching document
(e.g. "what is our work-from-home policy?"). Retrieval always returns
*something*, so absence has to be handled by the prompt and verified at the
agent level — routing scores cannot see it.

## Web retrieval quality (manual — not scored by the harnesses)

The same blind spot, on the **web** side. These three questions check *which
sources come back*, not which tool fires. All three route to the web tool
unambiguously — no surface-form ambiguity, nothing to get wrong — so the
harnesses would award them a free pass and inflate the score. They are
deliberately **kept out of `bench_routing_agent.py`**; the routing set stays at
15 core + 6 boundary.

They are the three cases the widened allow-list (13 → 21 hosts, PR #124) was
chosen on, so they double as the **allow-list regression set**: if these
degrade, a domain has been dropped or the non-prod filter has over-matched.

Read the **sources**, not just the answer. A fluent answer built on a 2024
article is the failure being tested for.

**R1. How is MTN addressing foreign exchange losses in Nigeria?**

The sharpest case — the old list was not merely staler, it was *backwards*.
All four results came from one publication and a median of 829 days old,
led by 2024 naira-devaluation pieces ("5-point plan to solve MTN's Nigeria
woes", "MTN's tale of woe in Nigeria"). Those describe the losses being
*incurred*; the question asks how they are being *addressed*, and by then MTN
Nigeria had cleared the FX debt. Expect now: the debt clearance and naira
recovery (`punchng.com`, `techcabal.com`, `businessday.ng`), across three or
more hosts.

**R2. What was MTN's FY2025 revenue?** (= core Q12, judged on sources here)

Not a case of *no* sources — the old list returned the FY-25 results
**PDFs** (`mtn.com` presentation deck, JSE SENS announcements). The figure was
in them, but as slide/PDF layout, which is why runs disagreed
(160bn / 177.8bn / 210.8bn / 218bn — see the known-issue note at the end of
this file). `mtn-investor.com` — MTN's own IR site, and the host that was
missing — serves the same numbers as **HTML tables**
(`key-financial-tables.php`, `summary-group-income-statement.php`).
Expect now: those pages in the result set, and *the same figure across
repeated runs*. Consistency is the test; a single plausible answer proves
nothing.

**R3. How does MTN's return on equity compare with Vodacom and Airtel Africa?**

A comparative-metric question the old list had no source for. It returned
retail-investor content at a 840-day median — "battle of the
telecommunications giants", "if you invested R1,000 in MTN, Vodacom…" — which
is adjacent to the question but does not contain an ROE comparison. Expect
now: `investing.com` ROE and peer-comparison pages carrying the actual ratios.
Worth noting the open-web arm did *worse* here, not better: a LinkedIn post, a
Blogspot page and an AI-generated analyst site.

### Negative control

**"What is MTN's biggest competitive threat in South Africa right now?"**
should be **unchanged** — the widened and restricted lists returned identical
results for it. The additions earn their place on Nigeria/Ghana coverage,
primary IR financials and comparative metrics; South African telecom news was
already well covered. If this one *changes*, something unintended moved.

### Caveats

- Retrieval was benched (n=10 questions, single run, Web IQ only) but **Bing
  has never been benched with the widened list** — it ranks differently (path
  scoping, boost levels), so agent mode is plausible-but-unproven here.
- The benched "expanded" arm included a staging mirror,
  `stg18326.businessday.ng`. The non-prod filter shipped in the same PR now
  blocks it, so expect `businessday.ng` proper instead. Seeing any `stg*` /
  `dev*` / `preprod*` host in a result set is a bug, not a curiosity.


---

# Automated harnesses

The manual checklist above is for a quick eyeball. For repeatable, multi-run
scoring use the batch harnesses. **The question set itself lives in
`scripts/routing_questions.py` and is imported by both** — that module is the
single source of truth for the *data*; this file is the prose rationale behind it.
Change a question in one place and both bindings pick it up.

| binding | harness | what it drives |
| --- | --- | --- |
| `VOICE_BINDING=agent` | `scripts/bench_routing_agent.py` | the live Foundry agent, over its per-endpoint OpenAI-protocol URL |
| `VOICE_BINDING=model` | `scripts/bench_routing_model.py` | a live Voice Live **model** session over the websocket, registering the app's own in-process tools |

Both import `TIERS`, `GROUPS` and `classify()` from `routing_questions.py`, so
they are scored against **identical questions** and cannot drift apart. That
module has no prefix because it is a library, not something you run, and it
imports nothing outside the standard library — reading the questions costs
nothing. (It used to live inside `bench_routing_agent.py`, which forced the
model-mode harness to exec the *agent* harness, and transitively
`smoke_foundry_agent.py`, just to read a list of strings.)

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
- **Latency reports two different figures.** `first token` is the useful proxy
  for first audible word; `completion` includes the full answer and tool
  round-trips. Never quote completion as perceived avatar latency.
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

# Internal regression when Web IQ is unavailable. The harness keeps a local
# search_web schema as a competing routing-only stub; no web request is made.
uv run python scripts/bench_routing_model.py --runs 3 --tier core --groups minutes,policies
```

Notes:
- `create_version()` is idempotent — re-running with the same definition does
  **not** bump the version, and the runtime resolves the agent by **name**, so it
  always uses the latest. After experiments, re-provision the CHOSEN config so the
  live agent is not left on an experimental one.
- `AGENT_MODEL` no longer swaps the prompt: `prompts/agent/instructions.md` is the only
  agent prompt and is loaded unconditionally.
- Both harnesses locate the repo root by walking up from the working directory.
- When Web IQ is not configured, the model harness retains `search_web` as a
  routing-only stub. That makes a policy-to-web leak observable instead of
  awarding internal questions a free pass because only one tool exists. External
  answer quality is still untestable until `WEBIQ_API_KEY` is configured.

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
>
> **Update — allow-list widened (PR #124).** That diagnosis held. The old list
> could reach the figure only through FY-25 **PDFs**; `mtn-investor.com`, which
> publishes the same numbers as HTML tables, was blocked by the allow-list. It
> is now included. This is **expected to** resolve the inconsistency and has
> **not yet been re-measured** — the bench proved the source is now retrievable,
> not that the spoken figure is stable. Re-run R2 above several times and
> compare; leave this note open until it is.
