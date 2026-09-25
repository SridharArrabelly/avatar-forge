# Evaluation history

> **Historical record — exploratory end-to-end configuration results, not
> isolated model ability or a current production recommendation.** The original
> measurements, question sets, commands, deployment snapshots and conclusions
> below are preserved for provenance, not as the plan for new runs. Historical
> first-token timings are not measurements of meaningful first audible speech.
>
> **Customer policy tests are excluded from all new evaluation.** Their presence
> below, or in old harness defaults, does not authorize running them. Follow the
> [assistant evaluation guide](assistant-evaluation.md) instead: current work is
> **retrieval only**, with an explicit retrieval-results review gate before any
> model or voice bakeoff. Keep all new source text, gold answers, traces and
> customer material in private artifacts, not in this repository.

---

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
(160bn / 177.8bn / 210.8bn / 218bn). `mtn-investor.com` — MTN's own IR site, and
the host that was missing — serves the same numbers as **HTML tables**
(`key-financial-tables.php`, `summary-group-income-statement.php`).
Expect now: those pages in the result set, and **R226.7bn** consistently.
**Measured 10 Aug 2026: 3/3 runs returned R226.7bn**, which reconciles to MTN's
published statement (188 001 Rm restated FY2024, +20.6%). Consistency *and*
accuracy — see the model-mode baseline for the full reconciliation.

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
  un-retried run showed 18/30, almost all transient errors. The inspected legacy
  helper resets its timer for each attempt, so those explicit backoffs are
  outside successful-attempt latency. Do not attribute a large maximum to
  backoff without per-attempt evidence. SDK-internal retry contributions in
  the early runs were not separately recorded.
- **A fresh session per question (model mode)**, so conversation history cannot
  contaminate later answers.
- **Latency reports different boundaries.** `first token` is a text-stream
  observation, not first audible speech. `completion` includes the full answer
  and tool round-trips. Neither is a measurement of perceived avatar latency.
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

## Paced reasoning/retrieval matrix without changing the live agent

Use `scripts/bench_routing_matrix.py` to compare reasoning `none`/`low` at
AI Search `top_k=5/8` and matching Bing `count=5/8`. Each configuration gets
the same selected questions, three independent rounds, and a separate
subtotal for the historical ten (minutes + web). By default it includes all
15 core questions; use `--groups minutes web` when the policy corpus is absent
or excluded. The script copies the **live agent definition**, changes only
the requested model deployment, reasoning effort and retrieval breadth, and
creates a uniquely named benchmark agent in the same Foundry project. It reads
back that definition before inference and deletes the copy afterwards; it never
updates the source agent or either Container App.

`--model` may select another **already deployed** model in the same project
without switching the live agent. Resuming with a different benchmark model is
rejected; use a separate output directory for each model.

```powershell
$env:PYTHONIOENCODING='utf-8'
uv run python scripts\bench_routing_matrix.py `
  --env-file .azure\avatar-agent-env\.env `
  --output-dir "$env:TEMP\avatar-luna-benchmark-20260918" `
  --model gpt-5.6-luna --runs 3 --groups minutes web `
  --interval 8 --token-budget 240000 --reserve-tokens 30000
```

Use a **new, empty output directory outside the repository**. It contains all
rounds' answers, retrieved passages, citations, usage, errors, and exact agent
definitions; these may include private meeting and policy text and must not be
committed. The source environment file is read, not copied or changed. Azure CLI
credentials are used explicitly, so a deployed managed-identity client ID cannot
accidentally select the local authentication path.

The example is 120 scored turns (10 questions x 3 rounds x 4 configurations),
run serially; including policies gives 180. These pacing arguments reserve at
least 30,000 tokens per attempt against a 240,000-token rolling 61-second budget,
with at least eight seconds between request starts. Observed usage can increase
the reservation. This leaves headroom below a 333,000 TPM deployment; it is a
conservative client-side estimate, **not** a guarantee against server admission
limits or concurrent traffic. Transient errors honor numeric Retry-After headers
and wait at least 65 seconds before retrying. Permanent or exhausted errors stop
the matrix rather than silently producing a success-shaped score.

Unlike the original agent harness, this runner saves **every** round and requires
a completed response event. It supplies `TODAY` plus a silent meeting catalogue,
matching the current prompt's requirement for relative-date questions. Successful
attempt latency excludes pacing and retry sleeps; wall time and retry errors are
stored separately. Routing is still only the first-tool classifier: inspect all
tool calls, retrieved evidence, and answers before making a quality claim.

`--efforts none --breadths 5 --runs 1 --question-limit 1` is a setup pilot, not a
scored comparison. `--efforts medium` is available for a separately requested
follow-up; medium is not part of the default matrix.

`--resume` reuses completed matching turns from the output directory, allowing
the selected groups to be narrowed without rerunning successful answers. It
rejects changed source definitions, catalogues, dates, and duplicate completed
turns. After forcibly stopping a process, delete only its recorded temporary
benchmark agent before resuming; normal completion and handled errors perform
that cleanup automatically. Excluded groups remain in the private original
evidence but are not included in the resumed scores.

---

# Model-mode baseline (`gpt-realtime-2`)

Recorded 10 Aug 2026. **A standalone baseline, not a comparison** with the agent
numbers below — those were taken on a 10-question set that no longer exists, and
the two bindings differ by more than the model (see the caveat at the end).

```
uv run python scripts/bench_routing_model.py --runs 3 --tier core
```

| | |
| --- | --- |
| model | `gpt-realtime-2` (`VOICELIVE_MODEL` default; **not** 2.1) |
| questions | core 15 × 3 rounds = **45 turns**, catalogue injected |
| web tool | **LIVE** Web IQ, 21-host allow-list (post-#124) |
| **routing** | **45/45** (15/15 every round, all three groups) |
| **first token** | **2.97s** avg (n=45, 0 missing) — historical text-stream latency, not audible speech |
| completion | 3.40s avg |

Round-to-round variance was negligible: first token 2.9 / 3.0 / 3.0s,
completion 3.4s in all three.

## Answer quality

Scanned all 45 answers for the failure modes this file tracks:

| failure mode | occurrences |
| --- | --- |
| deferral without firing a tool (*"I need to check…"*) | **0** |
| rand/cents confusion on share price | **0** |
| empty or non-answer | **0** (shortest 34 chars, mean 250) |
| literal `Headline:` prefix | 1 of 45 |

Notable answers, each stable across all three rounds:

- **Group CFO** — "Tsholofelo Molefe" every time.
- **Attendees (Q3)** — full named list (Board Chair Mcebisi Jonas, Group CEO
  Ralph Mupita, CCXO, CMO, CTO, Regional VPs, Heads of Customer Service from the
  top five OpCos, Company Secretary). This is the question whose deferral bug
  started the routing rewrite; it is answered, not deferred.
- **Share price** — R205.50, and *volunteered* that the quote was from 7 August
  and may be delayed rather than presenting it as live. Honest about staleness
  without being asked.

## FY2025 revenue — the open issue is closed

All three rounds returned **R226.7 billion**, and the figure is not merely
consistent, it is **correct**. From MTN's own income statement
(`mtn-investor.com`, now allow-listed): restated FY2024 revenue **188 001 Rm** at
**+20.6%** reported → **226 729 Rm**.

The previously recorded spread (160 / 177.8 / 210.8 / **218**bn) turns out not to
have been hallucination at all — it was the **wrong line of the right table**:

| figure | what it actually is |
| --- | --- |
| 177.8bn | FY2024 **service** revenue (177 756 Rm) — wrong year *and* wrong line |
| 218bn | FY2025 **service** revenue (177 756 × 1.229 = 218 462 Rm) — right year, wrong line |
| **226.7bn** | FY2025 **total** revenue — the answer to the question asked |

That is exactly the "service vs total revenue, page formatting" diagnosis
recorded below, confirmed. The old allow-list could only reach these numbers
through FY-25 **PDFs** (slide deck, SENS announcements); `mtn-investor.com`
publishes them as an **HTML table**, and the model picks the right row from it.

## Caveats

- **Latency here is not a model-vs-model result.** Agent mode calls **hosted**
  tools server-side; model mode calls **in-process** tools over the Web IQ REST
  API. A delta measures the whole path, not the model.
- `first token` is a text-stream measurement, not a playback measurement.
  Never quote `completion` (3.40s) as time-to-first-word.
- Single run of `n=3`, one region, one time of day.
- Routing is **saturated** at 45/45 on the core tier — it can now only detect a
  regression, not an improvement. Use `--tier boundary` to discriminate.
- `.env` overrides everything (`load_dotenv(override=True)`). This run needed
  `WEBIQ_ALLOWED_DOMAINS` refreshed to the 21-host list first — a stale local
  `.env` silently benches the *old* allow-list.

---

# Model shootout results (agent mode, for the record)

> **These predate the current question set.** They were run on **10** questions;
> the core tier is now 15 (plus 6 boundary). Treat them as a record of the
> agent-mode model choice, **not** as a baseline comparable with the model-mode
> numbers above.

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

> **Known open issue (not model/prompt) — RESOLVED 10 Aug 2026.** FY2025 revenue
> was inconsistent across runs/models (160bn / 177.8bn / 210.8bn / 218bn). The
> diagnosis recorded here — a web-grounding / source-parsing gap on the
> allow-listed financial pages (service vs total revenue, page formatting),
> routing being correct and the fix being Azure-side source coverage — held
> exactly. Adding `mtn-investor.com` (PR #124) gave the model an HTML income
> statement instead of only PDFs, and model mode now returns **R226.7bn** on all
> three runs, matching MTN's published figure. The old numbers were the wrong
> *line* of the right table, not invention. See the model-mode baseline above.
>
> Still open in the same family: the board-of-directors-as-an-image gap on
> mtn.com/leadership.

---

# Luna comparison — 18 September 2026

**Scope:** `avatar-agent-env`, deployed `gpt-5.6-luna` version `2026-07-09`,
DataZoneStandard, 333,000 TPM / 333 RPM. Four configurations:
`reasoning.effort=none/low` x AI Search `top_k=5/8`, with Bing `count` matching
`top_k`. **Ten questions x three rounds per configuration = 120 scored turns.**
These are the historical five minutes + five web questions (current core IDs
Q1-5 and Q11-15), not the policy questions or boundary tier.

Policies were explicitly excluded at the owner's request: customer policy
documents had intentionally been removed. A read-only index check found
110 meeting-minutes chunks and zero policy chunks. No policy documents were
loaded. The initial run had included policies; it was stopped, its temporary
agent deleted, and resumed with only minutes + web. Completed matching turns
were retained; all exploratory policy turns and the one-question setup pilot
are excluded from every table below.

## Configuration and preservation

The source was `AvatarAgent` version 5: Luna / none / 8/8. The matrix copied its
exact prompt and hosted tool connections into temporary agents; only reasoning
effort and retrieval breadth changed. The source definition's SHA-256 was
captured privately, and its version and definition were unchanged after the run.
Both live Container Apps and all azd environment settings were left untouched.
All benchmark agents, including the interrupted copy, were deleted.

Each question used a fresh response without conversation history. The same
ten-meeting catalogue and `TODAY: Friday, 18 September 2026 (UTC)` context were
supplied across configurations. All 120 response objects reported model
`gpt-5.6-luna` and the requested reasoning effort. The none configurations
reported zero reasoning tokens; low reported 1,859 tokens at 5/5 and 1,857
at 8/8 across their respective 30 turns.

## Routing and latency

**All four configurations routed 30/30 correctly**: minutes 15/15 and web
15/15 in each. Every question passed routing in all three rounds. This is
**100% routing, not 100% answer quality**.

| Luna configuration | Routing | First token mean / median | Completion mean / median | Maximum completion |
|---|---|---|---|---|
| none, 5/5 | 30/30 | 5.61s / 4.49s | 7.06s / 5.73s | 47.65s |
| none, 8/8 | 30/30 | 4.84s / 4.61s | 5.98s / 5.73s | 13.08s |
| low, 5/5 | 30/30 | 6.49s / 5.69s | 7.67s / 6.93s | 16.66s |
| low, 8/8 | 30/30 | 12.97s / 6.11s | 14.14s / 7.21s | 203.24s |

All turns eventually completed. **No observed HTTP 429s and no exhausted
retries.** Low/8 had one transport `APIConnectionError`, recovered after the
runner's backoff; its retry wait is excluded from the successful-attempt latency.
The 203.24s low/8 turn was a completed response, with 203.13s to first text and
completed hosted-tool statuses. Its cause was not diagnosed, so it must not be
called a reasoning-cost or throttling measurement. It remains in the mean;
the median shows the otherwise much shorter typical latency. The 47.65s
none/5 outlier also had no client retry.

The subsequent Terra comparison inspected the saved server timestamps too:
Luna's 203.24s client-observed turn spans only **7 seconds** between the response's
`created_at` and `completed_at`; the 47.65s turn spans **20 seconds**. Those
integer-resolution server windows exclude time outside the server response
lifecycle. They reinforce why the client-side tails must not be attributed
entirely to model inference; the transport/client/queue contribution was not
isolated.

Low used additional tool calls: 11/30 turns at 5/5 and 6/30 at 8/8 used two or
three calls; both none configurations used exactly one call on every turn.
The 120 scored responses reported 1,250,263 total tokens, including cached
input tokens; this excludes exploratory/pilot and unsuccessful request usage.
Pacing reserved 30,000 tokens per attempt against a 240,000-token rolling budget,
with at least eight seconds between starts.

## Answer quality

**Low/5 was the best Luna configuration in this sample: 20/30 source-review
passes, not 30/30.** These are factual/completeness verdicts, separate from
routing and speech formatting. An **unverified** answer is not proven false;
it lacks enough evidence to award a pass. No configuration reached 100%, even
if the unverified share-price answers were subsequently confirmed.

| Luna configuration | Minutes pass | Web pass | Overall pass | Incomplete | Factual/unsupported error | Unverified |
|---|---|---|---|---|---|---|
| none, 5/5 | 8/15 | 4/15 | 12/30 (40.0%) | 9 | 4 | 5 |
| none, 8/8 | 12/15 | 6/15 | 18/30 (60.0%) | 5 | 4 | 3 |
| low, 5/5 | 13/15 | 7/15 | **20/30 (66.7%)** | 5 | 2 | 3 |
| low, 8/8 | 12/15 | 6/15 | 18/30 (60.0%) | 5 | 4 | 3 |

This is a source-based review of a small, fixed question set, not a calibrated
model-wide accuracy estimate. Public answers were checked independently where
the hosted tool did not expose its results, as explained below. Do not compare
these percentages to the historical **routing** percentages.

### Minutes answer quality

All 60 minutes answers were reviewed against both the passages actually returned
on that turn and the full indexed text of the three relevant meetings. A pass
requires a materially correct, sufficiently complete answer supported by the
retrieved evidence. Reasonable compression for the 70-word voice target is
allowed. A cautious retrieval-limited nonanswer is **incomplete**, not a
hallucination; an unsupported assertion or wrong date/deadline attribution is
an **error**. Style violations are recorded separately.

| Luna configuration | Pass / 15 | Incomplete | Error | Q1 / Q2 / Q3 / Q4 / Q5 passes, each out of 3 |
|---|---|---|---|---|
| none, 5/5 | 8/15 | 6 | 1 | 3 / 2 / 0 / 3 / 0 |
| none, 8/8 | 12/15 | 3 | 0 | 3 / 3 / 3 / 3 / 0 |
| low, 5/5 | 13/15 | 2 | 0 | 3 / 3 / 3 / 3 / 1 |
| low, 8/8 | 12/15 | 2 | 1 | 3 / 3 / 3 / 2 / 1 |

- **Q3, attendance:** none/5 failed to supply the list in all three rounds;
  the other configurations answered it in all three. The meeting exists in
  the index; this was a retrieval/answer-completeness failure, not an absent
  source document.
- **Q5, meeting-scoped strategy:** remained the weakest question. Neither none
  configuration produced a passing answer; low passed once at each breadth.
  Most misses were cautious nonanswers after retrieval returned other meetings.
  One none/5 answer instead attributed other meetings' strategy to the requested
  meeting, which is a materially different and worse failure.
- **Other misses:** none/5 omitted a key action-owner group once. Low/8
  incorrectly extended a deadline to a different action once, despite correct
  routing. Its 203-second turn was judged on content independently of latency.
- **Spoken-output issues:** one overlength minutes answer in none/5, two in
  none/8, one in low/5 and one in low/8. Low/5 leaked citation markers three
  times; low/8 once. These were not deducted again from factual pass counts.

**Low/5 had the strongest minutes result in this sample, but no configuration
achieved 100% minutes answer quality.** More reasoning was not uniformly better,
and 8/8 did not eliminate the meeting-scoped strategy retrieval gap.

### Web answer quality

| Luna configuration | Pass / 15 | Incomplete | Factual/unsupported error | Unverified | Q11 / Q12 / Q13 / Q14 / Q15 passes, each out of 3 |
|---|---|---|---|---|---|
| none, 5/5 | 4/15 | 3 | 3 | 5 | 3 / 0 / 0 / 1 / 0 |
| none, 8/8 | 6/15 | 2 | 4 | 3 | 3 / 0 / 0 / 1 / 2 |
| low, 5/5 | 7/15 | 3 | 2 | 3 | 3 / 0 / 0 / 3 / 1 |
| low, 8/8 | 6/15 | 3 | 3 | 3 | 3 / 0 / 0 / 0 / 3 |

**Grounding visibility is limited:** the captured Bing result bodies were empty,
including on successful calls. Only two low/8 answers exposed citation
annotations. A tool call or URL is not proof that the answer is supported.
Stable public claims were therefore verified against primary publications;
a web pass here means independently verified factual adequacy, **not proof of
the per-turn grounding chain**.

- **CFO:** all 12 answers correctly identified Tsholofelo Molefe.
- **FY2025 revenue:** none of the 12 answers supplied the correct total.
  Eleven gave **R218.5bn explicitly labelled service revenue** but omitted the
  requested total; those are incomplete, not service/total mislabelling.
  None/8 round 2 instead gave approximately **R233bn total revenue**, an error.
  The primary income statement reports **R226,707 million = R226.707bn**
  total revenue; service revenue is **R218,500 million**. The broad source
  improvement measured previously in model mode did **not** establish that
  hosted Bing in this environment retrieves and uses the right table.
- **Share price:** all 12 were **unverified**, not declared false. Answers gave
  plausible rand-denominated prices, but the saved evidence lacked timestamped
  quote snapshots. An independently observed intraday price is not a fixed gold
  value for another minute. Even a citation to MTN's changing investor page
  cannot retrospectively establish the price at answer time.
- **Vodacom fintech:** low/5 passed all three. Other configurations introduced
  stale customer targets (120m instead of the announced 130m), widened or
  narrowed the scope of transaction metrics, or added an unsupported strategy
  claim. None/5 had two narrower verification holds over calling Vodafone Cash
  specifically a super-app; the rest of those summaries was supported.
- **Ambition 2025:** low/8 passed all three. Other misses replaced the original
  four strategic priorities with platform categories, or changed the
  "Own the Home" customer objective into a fibre-homes-passed target. A thematic
  summary was allowed; a wrong explicit four-priority enumeration was not.

Separate web speakability faults: low/5 had one bold-markdown answer; low/8
had two citation leaks. No web answer exceeded 70 whitespace-delimited words.

Primary references used for this review:

- [MTN leadership](https://www.mtn.com/leadership/?tablink=executive).
- [FY2025 Group income statement](https://mtn-investor.com/reporting/annuals-2025/summary-group-income-statement.php)
  and [results overview / revenue analysis](https://mtn-investor.com/reporting/annuals-2025/results-overview.php).
- [MTN investor quote](https://www.mtn.com/investors/) and its
  [ProfileData feed](https://irhosted.profiledata.co.za/mtngroup/2019_feeds/t02_intraday.htm).
- [Vodacom FY2026 results](https://vodacom.com/news-article.php?articleID=16836)
  and [June 2026 quarterly update](https://www.vodacom.com/news-article.php?articleID=16942).
- [MTN's four strategic priorities](https://www.mtn.ng/about/),
  [Ambition 2025 target dashboard](https://www.mtn-investor.com/mtn-ir2024/our-strategic-performance-dashboard.php),
  and [Ambition 2030](https://www.mtn-investor.com/mtn-ir2025/our-ambition-2030-strategy.php).

## Comparison with the recorded winner

| Configuration | Question set / repeats | Routing | Mean completion |
|---|---|---|---|
| Historical GPT-5.4 full / none / 8/8 | Historical 10 x 3 | 30/30 | 5.2s |
| Luna / none / 8/8, this run | Same 10 x 3 | 30/30 | 5.98s |
| Luna / low / 8/8, this run | Same 10 x 3 | 30/30 | 14.14s; median 7.21s |

The question set and repeat count now match, but this is **not a controlled,
contemporaneous model-only A/B**. The deployment, date, live web results,
prompt/context history, and cache/network conditions can differ from the old
run. GPT-5.4 was not rerun in this experiment. Its historical answer-quality
finding was qualitative, not a per-answer scored rubric, so it cannot be
converted retrospectively into an invented percentage. The existing tables also
do not contain a numerical GPT-4.1-full result; none is reconstructed here.

**Conclusion:** Luna's routing is reliable on this set, but this run does not
establish it as a quality replacement for the recorded GPT-5.4 winner. Within
Luna, low/5 offered the best reviewed answers, at a median first-token cost of
5.69s versus 4.49s for none/5. None/8 is the lower-latency alternative with
18/30 reviewed passes. Low/8 did not improve on low/5. The major remaining
gaps are meeting-specific retrieval, the total-revenue table, and evidence for
time-sensitive quotes; increasing reasoning alone is not a demonstrated fix.
**Medium was not tested.** No production model, reasoning setting, or retrieval
breadth was changed based on this experiment.

Raw responses, passages, citations, exact definitions, and per-turn timing are
retained privately in the session artifacts under `luna-matrix-20260918`.
They are not committed because they contain internal meeting text. The reusable
matrix command and pacing mechanics are documented above.

---

# Terra vs fresh GPT-5.4 baseline — 18 September 2026

This follow-up runs **the same ten minutes + web questions**, three rounds per
configuration, with policies excluded throughout. Terra has four configurations
(none/low x 5/5 and 8/8), **120 turns**. GPT-5.4 is rerun at its recorded
winning configuration (none / 8/8), **30 turns**, rather than comparing solely
with the old qualitative winner narrative.

| Deployment | Version | SKU | Observed quota |
|---|---|---|---|
| `gpt-5.6-terra` | `2026-07-09` | GlobalStandard | 501,000 TPM / 501 RPM |
| `gpt-5.4` | `2026-03-05` | GlobalStandard | 250,000 TPM / 2,500 RPM |

Both use the same Foundry resource and copies of `AvatarAgent` version 5.
The live agent was **not switched**: `--model` overrides only the isolated
benchmark copy. Prompt and catalogue hashes match each other **and the Luna
run**. A read-only check confirmed the same 110-chunk index and byte-for-byte
equivalent reference passages for all three tested meetings. The selected
model, reasoning effort, and retrieval breadth are the only definition changes.

The fresh GPT-5.4 baseline ran concurrently with the first part of Terra's
matrix, under separate per-deployment pacing budgets. This is a substantially
better controlled comparison than the historical one, but still a small,
non-randomised run against changing live web results and shared hosted tools.
Terra used the same 240,000-token rolling budget as Luna; GPT-5.4 used
180,000, with 30,000 reserved per attempt and at least eight seconds between
starts for both.

## Routing and latency

**150/150 turns completed and routed correctly**. Each configuration is 30/30,
split into 15/15 minutes and 15/15 web. There were **zero recorded API errors,
zero retries, and no observed 429s** in either run.

| Model / configuration | Routing | Client first token mean / median | Client completion mean / median | Server window mean / median |
|---|---|---|---|---|
| GPT-5.4 none, 8/8 | 30/30 | 5.05s / 4.80s | 6.47s / 6.07s | 5.53s / 5s |
| Terra none, 5/5 | 30/30 | 4.97s / 4.88s | 6.27s / 6.40s | 5.47s / 5s |
| Terra none, 8/8 | 30/30 | 28.16s / 5.74s | 29.31s / 6.88s | 6.00s / 6s |
| Terra low, 5/5 | 30/30 | 7.09s / 6.29s | 8.31s / 7.58s | 7.53s / 7s |
| Terra low, 8/8 | 30/30 | 6.56s / 6.28s | 7.92s / 7.36s | 7.27s / 7s |

**Do not read the Terra none/8 mean as model inference time.** Round 1's
share-price response took **686.37s client-observed**, but its server
`completed_at - created_at` was only **4 seconds**. The response and Bing tool
both completed without an API error or client retry. The excess time is outside
that recorded server window; its transport/client/queue cause was not isolated.
The outlier remains in the client mean, while the median and server window
make the distinction visible. Server timestamps are integer-resolution and
do not measure first-token latency or replace end-to-end client measurements.

On the directly matched none/8 setting, Terra's median first token was **5.74s**
versus **4.80s** for the fresh GPT-5.4 run. Terra none/5 was roughly comparable
at **4.88s**, but that changes retrieval breadth as well as the model.

## Answer quality

**Terra low/5 and low/8 tied for the strongest result: 23/30 source-review
passes (76.7%), versus 15/30 (50.0%) for the fresh GPT-5.4 baseline.** At the
directly matched none/8 setting, Terra passed 18/30 versus GPT-5.4's 15/30.
The larger 23/30 improvement changes reasoning effort as well as the model;
it is a configuration comparison, not a model-only causal estimate.

| Model / configuration | Minutes pass | Web pass | Overall pass | Incomplete | Factual/unsupported error | Unverified |
|---|---|---|---|---|---|---|
| GPT-5.4 none, 8/8 (fresh) | 7/15 | 8/15 | 15/30 (50.0%) | 7 | 5 | 3 |
| Terra none, 5/5 | 9/15 | 9/15 | 18/30 (60.0%) | 9 | 0 | 3 |
| Terra none, 8/8 | 9/15 | 9/15 | 18/30 (60.0%) | 9 | 0 | 3 |
| Terra low, 5/5 | 12/15 | 11/15 | **23/30 (76.7%)** | 4 | 0 | 3 |
| Terra low, 8/8 | 12/15 | 11/15 | **23/30 (76.7%)** | 4 | 0 | 3 |
| Luna low, 5/5 (earlier same day) | 13/15 | 7/15 | 20/30 (66.7%) | 5 | 2 | 3 |

Use the same cautions as the Luna review: an unverified quote is not a proven
wrong answer; a supported but incomplete answer does not pass; style faults
are separate. Qualified revenue approximations count as adequate in this
table, with exact-figure sensitivity reported below. These are fixed-set
review results, not calibrated model-wide accuracy estimates.

### Minutes answers

| Model / configuration | Pass / 15 | Incomplete | Factual/unsupported error | Q1 / Q2 / Q3 / Q4 / Q5 passes, each out of 3 |
|---|---|---|---|---|
| GPT-5.4 none, 8/8 | 7/15 | 5 | 3 | 3 / 1 / 0 / 3 / 0 |
| Terra none, 5/5 | 9/15 | 6 | 0 | 3 / 3 / 0 / 3 / 0 |
| Terra none, 8/8 | 9/15 | 6 | 0 | 3 / 3 / 0 / 3 / 0 |
| Terra low, 5/5 | 12/15 | 3 | 0 | 3 / 3 / 2 / 3 / 1 |
| Terra low, 8/8 | 12/15 | 3 | 0 | 3 / 3 / 2 / 3 / 1 |

- **Terra none:** all six attendance answers were cautious retrieval-limited
  nonanswers. Strategy answers either lacked the relevant meeting or provided
  broad themes without the distinctive agreed decisions. Neither increasing
  breadth alone nor correct routing fixed these gaps.
- **Terra low:** attendance improved to 2/3 and strategy to 1/3 at both
  breadths. Remaining nonanswers and incomplete summaries were not counted
  as passes. No cross-meeting attribution errors were found.
- **Fresh GPT-5.4:** two attendance answers substituted another meeting's
  roster; one strategy answer attributed other meetings' decisions to the
  requested date. Other misses omitted key action-owner groups or the specific
  strategy decisions. These errors occurred despite correct tool routing.
- **Style:** no requested minutes style faults were found in Terra. GPT-5.4's
  three customer-experience summaries exceeded 70 words (81/82/91) while
  still passing factual evaluation.

### Web answers

The same source-based rubric used for Luna was applied to all 75 web answers.
Stable claims were checked against the primary references above. All captured
Bing result bodies were empty and no citation annotations were present in these
runs, so the same per-turn grounding limitation applies. No web answer had a
listed URL, citation-token, markdown, or over-70-word style violation.

| Model / configuration | Pass / 15 | Incomplete | Factual/unsupported error | Unverified | Q11 / Q12 / Q13 / Q14 / Q15 passes, each out of 3 |
|---|---|---|---|---|---|---|
| GPT-5.4 none, 8/8 | 8/15 | 2 | 2 | 3 | 3 / 1 / 0 / 3 / 1 |
| Terra none, 5/5 | 9/15 | 3 | 0 | 3 | 3 / 0 / 0 / 3 / 3 |
| Terra none, 8/8 | 9/15 | 3 | 0 | 3 | 3 / 0 / 0 / 3 / 3 |
| Terra low, 5/5 | 11/15 | 1 | 0 | 3 | 3 / 2 / 0 / 3 / 3 |
| Terra low, 8/8 | 11/15 | 1 | 0 | 3 | 3 / 2 / 0 / 3 / 3 |

- **Revenue improves with low reasoning, but is not solved.** Terra none
  supplied only service revenue in all six turns. Each low configuration
  supplied the total twice and omitted it once. GPT-5.4 supplied the total
  once out of three. Of these five adequate total-revenue answers, **two**
  correctly rounded it to R226.7bn; three were explicitly qualified
  approximations (Terra about R226.5bn twice, GPT-5.4 about R226bn once).
  Approximate-answer tolerance was applied consistently, but those three
  answers should **not** be represented as exact financial-figure accuracy.
  Requiring the correctly rounded reported total instead would reduce each
  Terra low score by one and the baseline score by one.
- **CFO and fintech:** all 15 answers to each question passed. Terra avoided
  the unsupported or stale fintech specifics seen in parts of the Luna run.
- **Strategy:** all 12 Terra answers passed; they provided acceptable thematic
  summaries without falsely enumerating platform categories as the four formal
  priorities. GPT-5.4 passed once; two answers substituted narrower
  infrastructure/fintech objectives into an explicit four-priority list.
- **Share prices:** all 15 remain **unverified**, not proven false. No captured
  timestamped quote established the price at answer time. Price variation,
  delay disclaimers and the latency outlier were not used as evidence of error.

All five temporary benchmark agents were deleted. Both runs verified that
the live agent's version and full definition remained unchanged; no Container
App or azd settings were edited. Private evidence is retained under
`terra-matrix-20260918` and `gpt54-baseline-20260918` in session artifacts.

## Decision from this run

**Terra low/5 is the best next candidate from this comparison**, tied in reviewed
quality with low/8 but using fewer retrieved passages and fewer reported tokens
(303,422 versus 354,016 across 30 turns). Median first-token latency is effectively
the same for the two low settings, 6.29s versus 6.28s; this is not evidence of a
meaningful speed difference. Terra low is slower than the fresh GPT-5.4 none/8
baseline's 4.80s median, in exchange for stronger reviewed answers in this sample.

This revises the **historical winner narrative**, not the historical measurements:
the fresh GPT-5.4 baseline did not reproduce a near-perfect quality result under
today's source-based rubric and retrieval conditions. Terra reduced unsupported
cross-meeting and strategy assertions, but neither model solved the common
retrieval and evidence gaps. In particular, **23/30 is not 100%**: the best Terra
configurations still had four incomplete answers and three unverified quotes.
Requiring correctly rounded reported revenue rather than accepting qualified
approximations would reduce Terra low to **22/30** and the fresh baseline to
**14/30**; it would not reverse the ranking.

No model or configuration was promoted automatically. Medium reasoning was not
tested. The evidence supports a follow-up candidate, not a claim of universal
superiority or production readiness.

Reproduction commands, using new private output directories:

```powershell
uv run python scripts\bench_routing_matrix.py `
  --env-file .azure\avatar-agent-env\.env `
  --output-dir "$env:TEMP\avatar-terra-benchmark-20260918" `
  --model gpt-5.6-terra --runs 3 --groups minutes web `
  --interval 8 --token-budget 240000 --reserve-tokens 30000

uv run python scripts\bench_routing_matrix.py `
  --env-file .azure\avatar-agent-env\.env `
  --output-dir "$env:TEMP\avatar-gpt54-baseline-20260918" `
  --model gpt-5.4 --efforts none --breadths 8 --runs 3 `
  --groups minutes web --interval 8 --token-budget 180000 --reserve-tokens 30000
```

---

# Bing breadth A/B — `BING_COUNT` 8 vs 5 (22 September 2026)

**Why.** `BING_COUNT` defaulted to 8, justified in code as the "validated
production value" needed "to answer completely". The 30-question evaluation had
already recorded 5/5 and 8/8 as identical on every quality axis, so the
justification was untested. This run isolates Bing breadth.

**Method.** Two disposable clones of the Terra agent, differing *only* in
retrieval breadth (verified by diffing the published definitions: `top_k` 5 vs 8
and `count` 5 vs 8). Web-only question group, 3 runs x 5 questions per arm,
`gpt-5.6-terra`, reasoning effort `none`. Web questions never invoke AI Search,
so **Bing `count` was the sole active variable**. 0 errors, 0 retries.

## Routing and quality

| | count=5 | count=8 |
|---|---:|---:|
| Routing correct | 15/15 | 15/15 |
| Answer length (median words) | 33 | 35 |
| Answers over 70 words | 0 | 0 |
| Truncated / non-answers | 0 | 0 |

Two questions diverged materially, and **both favoured 5**:

- **FY2025 revenue** — count=5 returned the complete figure (total revenue
  R226.3bn plus service revenue R218.5bn). count=8 replied that total revenue
  was "not verified from the income statement". More snippets produced the less
  complete answer, directly contradicting the code comment.
- **Share price** — same underlying quote, but count=5 showed the cents-to-rand
  conversion and attributed it to the investor page, while count=8 asserted a
  "JSE quote delayed by 15 minutes" freshness claim that the cached crawl
  cannot support.

## Tokens

**130,028 -> 98,364 total tokens, a 24.4% reduction.** This is the arithmetic
consequence of three fewer snippets per turn, not a noisy measurement, and it
applies to every web turn.

## First-token latency — direction favours 5, not statistically established

| Cut | count=5 median | count=8 median | count=5 mean | count=8 mean | permutation p |
|---|---:|---:|---:|---:|---:|
| All 3 runs | 4.159 s | 4.891 s | 4.805 s | 4.671 s | 0.587 |
| Excluding run 1 | 4.067 s | 4.929 s | 4.126 s | 4.721 s | 0.104 |

**Reported honestly: the all-runs mean favours count=8 by 0.13 s.** The count=5
arm ran first and absorbed two cold-start outliers (10.13 s and 8.11 s, both in
run 1) that count=8 never paid. Medians are insensitive to those and favour
count=5 in both cuts. Excluding run 1 is defensible but post-hoc, so both cuts
are shown.

Paired by question (median of 3 runs each), count=5 was faster on **4 of 5**
questions, median paired difference **+0.39 s**.

At n=10-15 per arm this is **not significant** (p=0.104 at best). The honest
estimate is ~0.4 s, consistent in sign but unproven. **The default change rests
on the 24.4% token reduction with quality equal-or-better, not on the latency
delta.**

Reproduction:

```powershell
uv run python scripts\bench_routing_matrix.py `
  --env-file .azure\avatar-agent-env\.env `
  --output-dir "$env:TEMP\bing-breadth-20260922" `
  --model gpt-5.6-terra --efforts none --breadths 5 8 --runs 3 `
  --groups web --interval 8
```

---

# Model SKU — DataZoneStandard vs GlobalStandard (23 September 2026)

**Why.** On a fresh swedencentral environment, most live agent-mode turns hung
on the "Still working" cue and then vanished without an answer. Container logs
showed the stalled turns producing no output for 30+ s until the user spoke
again (`CANCELLED reason='turn_detected' empty_turn=True output=[]`). The
app, retrieval and quota were ruled out before the model SKU was compared.

**Isolating the layer.**

| Probe | What it bypasses | Result |
|---|---|---|
| Agent called directly (per-agent endpoint, no Voice Live) | Browser, Voice Live | 4/8 turns stalled ~60 s, always inside a model call; tools took 0.3–2 s |
| Raw model call, no agent or tools | Agent, tools, retrieval | 8/16 calls had ~61 s time to first token; the rest 0.7–1.6 s |
| Azure Monitor model metrics | — | All HTTP 200, no 429s; one `TimeToResponse` of 60.9 s |
| Resource Health | — | Available, no known issues |

So the stall sat in the model deployment itself, not in the app, the agent,
retrieval or quota.

**Method.** A temporary GlobalStandard deployment of the *same* model and
version (`gpt-5.6-terra` `2026-07-09`, 250K TPM) was created in the same account.
The two deployments were called alternately, with the order swapped every
iteration: 16 calls each, the full agent prompt, reasoning effort `none`, and
no retries.

| Deployment | Answered < 20 s | Stalled 60–87 s | Rejected | Median TTFT (answered) | Max |
|---|---:|---:|---:|---:|---:|
| DataZoneStandard (EU) | 1 | 3 | 12 | — | 86.6 s |
| GlobalStandard | **16** | 0 | 0 | 1.43 s | 2.4 s |

The 12 DataZoneStandard rejections were 11× "The system is currently
experiencing high demand … exceeds the maximum usage size allowed during peak
load" and 1× HTTP 500. This is shared EU data-zone capacity for this model, not
our quota or our code.

**Change.** The deployment was switched in place (same name, so the agent did
not change) with `az cognitiveservices account deployment create
--sku-name GlobalStandard --sku-capacity 250`. **The switch took a few minutes
to propagate.** Immediately after it, the deployment rejected every call. It
went clean partway through the next interleaved run: 9 of 16 calls were
rejected, then 7/7 succeeded.

**After propagation:**

| Probe | Result |
|---|---|
| Raw model, 16 calls | 16/16, no stalls, median 0.84 s, max 5.2 s |
| Agent directly, 8 questions (minutes + web) | 8/8 answered, first text at 1.7–5.2 s, no stalls |

**Decision.** `MODEL_SKU_NAME` now defaults to `GlobalStandard`.

**Trade-off.** GlobalStandard may process prompts in any Azure region, not just
the EU data zone. Deployments that need data residency should set
`MODEL_SKU_NAME=DataZoneStandard` explicitly and plan for this availability
risk.

**Caveats.**
- This is one time window. EU data-zone capacity may recover, so this is an
  availability finding, not a permanent ranking.
- It is not a latency claim: the answered-call timings are too few to compare
  the SKUs' speed.
- Existing environments without an explicit `MODEL_SKU_NAME` will have their
  deployment SKU changed in place on the next `azd provision`.

---

# Web IQ as the agent's web tool (24 September 2026)

**Why.** Grounding with Bing Custom Search was the slowest step of an agent web
turn, around 2 s. Model mode already used Web IQ, restricted to the trusted-site
list, and looked faster. Foundry cannot apply that site filter to a direct Web IQ
tool, so the question was whether calling our own filtered wrapper
(`/api/tools/search-web`) from the agent is faster *without* losing quality.

**Method.** Three disposable clones of the Terra agent, identical except for the
web tool, verified by diffing the published definitions:

| Arm | Web tool |
|---|---|
| `bing` | Grounding with Bing Custom Search (production at the time) |
| `webiq_wrap` | OpenAPI tool → the app's `/api/tools/search-web` → Web IQ with the site filter |
| `webiq_direct` | OpenAPI tool → Web IQ directly, **no** site filter |

Web-only question group, 3 runs × 5 questions per arm (15 turns each, one web
call per turn), `gpt-5.6-terra`, reasoning effort `none`, called on the agent
endpoint without Voice Live. 0 errors. Every clone was deleted afterwards and
the live agent was not changed.

## Latency (medians, n=15 per arm)

| | Bing | Web IQ wrapper | Web IQ direct |
|---|---:|---:|---:|
| Request → `response.created` | 1.67 s | 1.64 s | 1.67 s |
| Tool call item (Foundry's view of the search) | 1.83 s | **0.66 s** | 0.83 s |
| First token | 5.07 s | **3.88 s** | 4.25 s |
| Completed | 6.94 s | **4.53 s** | 5.38 s |
| Input tokens | 6086 | **3233** | 4232 |

Inside Azure, the wrapper's own log put the Web IQ search at 180–346 ms (median
~250 ms). The rest of the 0.66 s is Foundry's OpenAPI hop.

**Reported honestly: the wrapper arm has the worst tail.** Its p95 first token
was 13.9 s against Bing's 8.0 s, from two turns (9.1 s and 13.9 s). Our server
logged those searches at normal speed, so the stall sat inside Foundry and not
in Web IQ or the wrapper. At n=15 this is one sample of Foundry's variance, not
a property of the tool, but it is the thing to watch after a rollout.

## Answer quality

| | Bing | Web IQ wrapper | Web IQ direct |
|---|---|---|---|
| Good answers | 13/15 | **15/15** | 13/15 |
| Share price | correct 3/3 | correct 3/3 | **wrong 2/3** |
| FY2025 revenue | declined 2/3 | R226.7bn 3/3 | R226.7bn 3/3 |

The unfiltered arm quoted stale intraday prices from aggregator sites as
"today's" price, timed at 8:00 and 8:30 a.m., before the JSE opens. **The site
filter is what makes Web IQ trustworthy here**, which is why the agent calls the
wrapper rather than Web IQ directly.

## Connection keep-alive

httpx keeps an idle pooled connection for only 5 s by default, but searches in a
conversation arrive 15–40 s apart, so almost every search paid a fresh TCP+TLS
handshake. Idle-gap probes from a laptop: 1.01–1.12 s after 20 s idle against
0.35–0.41 s back to back. With a longer expiry, reuse held at 20 s and 60 s gaps
(0.33–0.40 s) and was lost between 60 and 120 s, where something in the path
drops idle connections. `search_web()` now keeps connections for 55 s
(`WEBIQ_KEEPALIVE_EXPIRY_S`), in both modes.

## Decision

`AGENT_WEB_TOOL=webiq` was added as an option ([configuration](configuration.md)),
with Bing still the default until the change is validated end to end through
Voice Live. Foundry's call to the wrapper is authenticated with a managed-identity
token when the deploy can create an Entra app registration, and with a shared key
otherwise ([auth](auth.md#the-agents-web-iq-tool-foundry-calls-the-app)).

## Fresh-environment check (24 September 2026)

The A/B used clones in the existing environment, with the tool wired up by hand.
This run deployed the branch from scratch into a new environment (`azd up`, agent
mode, `AGENT_WEB_TOOL=webiq`, MTN's `TRUSTED_WEB_SITES`, no shared key) to test
what the clones could not: the deployment itself and the managed-identity path.
AI Search Basic had no capacity in Sweden Central that day, so the test environment
used the existing Search service with its own index.

- **Deployment:** preflight created the app registration during `preprovision`, and
  the audience it stored reached the container in the same provision. No Bing
  account or connection was created. The agent came up with AI Search plus the
  OpenAPI tool in `managed_identity` mode.
- **Managed identity works.** Every web call was accepted. Foundry signs as the
  **account's** identity: with only the project's `oid` allowed, calls got `403`.
- **Probe:** the same 5 web questions × 3 runs on the deployed agent, plus 3
  minutes questions × 3. 0 errors.

| Web turns, medians (n=15) | A/B wrapper arm | Fresh environment |
|---|---:|---:|
| Request → `response.created` | 1.64 s | 0.62 s |
| Tool call item | 0.66 s | 0.43 s |
| First token | 3.88 s | 2.46 s |
| Completed | 4.53 s | 3.28 s |
| Worst first token | 13.9 s | 5.4 s (first, cold turn) |
| Input tokens | 3233 | 3240 |

This is not a controlled comparison: a new, idle Foundry account on a different
day. The 1 s drop in `response.created`, before any tool runs, says most of the gain
is Foundry-side variance rather than the tool.

**Answers: 14/15 web, 9/9 minutes.** The miss was the share price in one run. The
agent searched without the word "JSE", so Web IQ's passage extraction took a
broken price widget from MTN's investor page ("ZAR 0 -100%") instead of the "Last
close as at 23 Sep 2026 19 367c" line the other two runs quoted. With no good
figure it quoted a truncated investing.com history row (R200.74) as the 23
September close, although it said it was not a confirmed live price. It is a
source-quality failure in the same class as the unfiltered arm's, and the thing to
watch in the Voice Live check.

## Voice Live check (24 September 2026)

The last gate: spoken questions through the deployed app's avatar, timed by the
browser probe (`?probe=1`), against the same probe run earlier on the live
environment's Bing agent. `answer_ms` runs from the end of the question to the
last answer token; `total_ms` adds end-of-speech detection, recognition and
rendering.

| Medians | Live env, Bing | Test env, Web IQ |
|---|---:|---:|
| Web `answer_ms` | 5.24 s (n=3) | 3.98 s (n=2) |
| Web `total_ms` | 6.41 s | 5.29 s |
| Minutes `answer_ms` | 4.24 s (n=3) | 3.44 s (n=2) |
| Web minus minutes, `answer_ms` | 1.00 s | 0.54 s |

The samples are tiny and the environments differ, and the minutes turns, which use
no web tool, were 0.8 s faster too, so part of the gap is the fresh Foundry
account. The web-over-minutes cost, which isolates the tool, halved. The tester
judged every answer correct, and the container logs showed each web turn going
through Web IQ.

## The default

With both checks passed, `webiq` became the default for **new** environments.
Unset, `AGENT_WEB_TOOL` still resolves to `bing` for an agent-mode environment
deployed before the change, so a redeploy does not swap a live agent's tool, and
for an existing Foundry account, which Web IQ can't use. Preflight records the
resolved value in the azd env
([deployment](deployment.md#choosing-the-agents-web-tool)).
