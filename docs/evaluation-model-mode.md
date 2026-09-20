# Realtime model-mode evaluation

> **Latency attribution update, 20 September 2026:** the historical local runner
> inserted token-based waits before tool-followup responses. The production
> handler does not contain that wait. Raw timings below remain measured values
> for that client path, but are not clean production timings or pure model-speed
> rankings. See [the latency investigation](realtime-latency-investigation.md)
> for the 51-turn reconstruction, four instrumented probes and token audit.
> The harness is now corrected to pace between complete attempts and buffer
> event writes until attempt completion. **No new unpaced live cohort has been
> run, so there is no replacement end-to-end TTFA statistic yet.** Historical
> values have not been altered by subtracting a median or presented as new data.

> **Post-evaluation scope update, 20 September 2026:** active instructions now
> advertise meeting minutes and public web information only, and the original
> onboarding tiles have shared spoken-answer guidance. This prompt change is
> covered by targeted smoke checks, not a repeat of the full study below.
> Recorded benchmark prompts, scores and timings remain historical evidence.

Companion to [evaluation-results.md](evaluation-results.md), which records the
**agent-mode** (Foundry agent + managed tools) comparison. This page is the
**model-mode** (Voice Live bound directly to a realtime model, no agent) track:
a different runtime, different tool-calling surface, and a separate resource
footprint. It does not rewrite or supersede the agent-mode history.

## Independence from the agent-mode evaluation

- Runs entirely against **`avatar-model-env1` / `rg-avatar-model-env1`**
  (Foundry account `cog-avatar-model-env1-fv74sw2xppeaa`, project
  `proj-avatar-model-env1`, Search `srch-avatar-model-env1-fv74sw2xppeaa`,
  region `swedencentral`). It never reads, queries, or depends on
  `avatar-agent-env` / `rg-avatar-agent-env`, which the user may tear down and
  rebuild independently at any time.
- Uses an isolated Search index, `eval-model-sections-v1-dc53`, created solely
  for this evaluation. It carries the same 60 whole-section blocks
  (byte-equivalent text, IDs, and source hashes) as the frozen agent-mode study
  corpus, but lives in the model-mode Search service, not a cross-resource-group
  link. The production knowledge index was not read or modified.
- The production model-mode container, its revision, and its configured
  index/settings are left unchanged; this evaluation only adds a new,
  independent Search index and runs the benchmark script locally.

## Candidate shortlist and exclusions

Only two candidates reached the scored benchmark:

- `gpt-realtime-2.1`
- `gpt-realtime-2.1-mini`

Both are served through **Global** managed-model processing (not DataZone),
using Voice Live API version `2026-04-10` and voice `en-ZA-LeahNeural`.

Excluded, with the reason recorded rather than a fabricated score:

- **`mai-mm-realtime`** — returned `invalid_model` / unsupported in Sweden
  Central on both the `2026-04-10` and `2026-06-01-preview` Voice Live API
  versions. Not attempted further.
- **`phi4-mm-realtime`** — audio binding alone worked, but function-call
  probes against the actual production prompt and tool set returned either
  empty output or spoken JSON with zero structured function-call events, on
  both API versions. This finding is specific to this prompt/tool profile, not
  a claim that Phi cannot call functions in any configuration. The user
  reviewed this and explicitly chose to proceed with only the two GPT
  candidates rather than substitute an older realtime model or continue
  testing MAI/Phi.

Unsupported candidates are excluded from the results entirely; they are not
assigned a zero or placeholder quality score.

## Tool and evidence parity with the agent-mode study

Model mode has no managed `azure_ai_search` tool — Voice Live's session schema
offers only `FunctionTool`/`MCPTool` — so `search_minutes` in
`backend/voice/tools.py` owns retrieval directly. It was brought to parity with
the agent-mode defaults recorded above so the two tracks are evidence-comparable:

| Setting | Old model-mode default | Now (matches agent-mode default) |
|---|---|---|
| Query mode | always `vector_semantic_hybrid` | `AI_SEARCH_QUERY_TYPE=semantic` (BM25 lexical + semantic rerank, no vector query) |
| Top-k | 4 | `AI_SEARCH_TOP_K=5` |
| Snippet cap | 1200 chars | `AI_SEARCH_SNIPPET_CHARS=12000` chars (the versioned section layout's own cap, so a full section is no longer sliced mid-thought) |
| Vector recall (`K_NEAREST`) | 40, always spent | 40, spent only in `vector` / `vector_simple_hybrid` / `vector_semantic_hybrid` modes |

`AI_SEARCH_QUERY_TYPE` accepts the same five values as the agent-mode
`AzureAISearchQueryType` enum: `simple`, `semantic`, `vector`,
`vector_simple_hybrid`, `vector_semantic_hybrid`. Settings are read from the
environment on every call (not cached at import) and an explicit blank or
unrecognised value fails the call with a safe, logged error instead of
silently falling back to a default. The model may also request a `top` between
1 and 8 per call; an out-of-range value is likewise a safe error, not a silent
clamp. The legacy always-hybrid profile (`vector_semantic_hybrid`, top 4,
1200-char snippets) is preserved as `LEGACY_QUERY_TYPE` / `LEGACY_TOP` /
`LEGACY_SNIPPET_CHARS` for reference, but is no longer the default.

Passages now additionally return `id` and `source` (the indexed document's
key and originating filename) alongside the existing `title`, `type`, `date`,
`extract` — additive fields, not a schema change. Content past the configured
snippet cap is truncated explicitly: the passage carries `truncated: true` and
`original_chars` with the untruncated length, rather than being shortened with
no signal that anything was cut. An unconfigured Search client
(`AZURE_SEARCH_ENDPOINT`/`SEARCH_INDEX_NAME` unset) is reported as an
`error`, distinct from a genuinely empty result set — the two must not be
conflated as "nothing found". A search failure is caught and returned as a
safe tool error rather than raising into the realtime tool loop, and only a
length/fingerprint of the query is ever logged, never the raw query or
returned passages.

## Web IQ metadata correction

`search_web`'s `published` field used to be filled from whichever of
`lastUpdatedAt`, `crawledAt`, or `datePublished` was present first. `crawledAt`
and `lastUpdatedAt` are index-maintenance timestamps — a crawl date only
proves the crawler visited that day, it does not prove the page was published
or updated then, and it says nothing about a live quote's as-of time. Labelling
them `published` asserted a fact Web IQ never returned. `published` is now
populated only from `datePublished` when the API actually supplies one, and is
empty otherwise. `last_updated` and `crawled_at` are returned separately, in
full (not date-truncated), so recency ranking still has the signal — the tool
result's note now explicitly tells the model not to infer a publication date
or a current price/quote time from a crawl or update timestamp alone. Result
budgets (4 results / 800 chars), the allow-listed hosts, the request/auth
shape, and query filtering were not changed.

## Execution model and timing definitions

The benchmark (`scripts/bench_realtime_evaluation.py`) executes tool calls
**locally**, in the benchmark process — this measures the realtime model +
local retrieval/Web IQ path, not end-to-end latency through the deployed
container/avatar pipeline. Two timings are recorded and kept separate:

- **First-any** (text/audio/transcript) — the first content of any kind the
  model emits, including a spoken pre-tool preamble (e.g. Mini's observed
  "Calling the readiness probe now" before its tool result).
- **Final-answer-first-received** — the first content of the terminal,
  tool-free completed response. A pre-tool preamble must never win on this
  metric; the two channels are tracked independently and never concatenated
  when a response emits both a text and an audio/transcript channel for the
  same turn.

An empty terminal response is a failure (no silent content retry), and spoken
JSON is never parsed as a substitute for a structured function-call event. The
model, API version, and voice used are never silently substituted mid-run.

## Pacing and quota posture

A global minimum 8-second spacing is enforced between calls. Token budgeting
uses a configurable ceiling (90k default) with an adaptive 15k initial
reservation, summed across every tool/response round of a turn, with observed
reset handling — reservations are estimates against Voice Live's documented
limits (120k TPM / 30 new connections per minute), not observed quota
telemetry, since no `rate_limits.updated` events were observed in small
preflight probes.

The corrected runner applies discretionary token smoothing only between
complete attempts. A real service-directed reset/backoff is still honored and
marked in `latency_trace.service_wait_observed`. Event snapshots are bounded
in memory and redacted/fsynced after an attempt, outside measured answer
arrival. Completed attempts remain durable; an unclean exit can lose the
in-flight event buffer. The historical runs below used the older policy and
are labelled accordingly.

## Status of this evaluation

Runtime Search parity, the Web IQ metadata fix, and the explicit-`--env-file`
loader (`load_explicit_environment` in `scripts/setup_aisearch_index.py`, which
loads an explicit file even when `PYTHON_DOTENV_DISABLED=1` is set) have been
restored in this worktree and are covered by
[`tests/test_realtime_retrieval.py`](../tests/test_realtime_retrieval.py) and
[`tests/test_webiq_dates.py`](../tests/test_webiq_dates.py) (all offline,
mocked Search/Web IQ clients, no network).

A bounded pilot and, subsequently, the full authorized 198-turn oracle+live
benchmark against `avatar-model-env1` (both approved candidates, 17 frozen
cases, 3 runs each) have since been run to completion from this worktree:
198/198 turns completed, 0 failed, 0 unavailable, 0 skipped, 0 retries. The
run was graded (blinded, source-grounded, against the frozen `required_facts`
rubric) and a channel-labeling/derivation bug in the first grading pass
(conflating the `AUDIO_TRANSCRIPT` transcript-timing channel with true `PCM`
audio latency, and under-scoping `first_any`/tool-routing/token-usage
derivations) was subsequently found and corrected; no turns were regraded and
no raw telemetry was altered. Turn-level records, the corrected grading
report, machine-readable summary, and a per-turn grading-traceability
artifact are kept as private evaluation outputs alongside the real
credentials/endpoint data they reference, intentionally **outside this
repository** rather than committed here — see the parent session for exact
artifact paths and the current final status before treating any result
number quoted elsewhere as authoritative over those artifacts.

Offline regression coverage for the timing/token channel-selection logic used
when deriving grading metrics from raw turn records lives in
[`scripts/grading_metrics.py`](../scripts/grading_metrics.py) and
[`tests/test_grading_metrics.py`](../tests/test_grading_metrics.py) — it
specifically guards against reintroducing the
`AUDIO_TRANSCRIPT`-labelled-as-audio and `responses[0]`-instead-of-whole-turn
mistakes described above.

## Corrected results

**Timing-column status:** the earlier transcript/PCM naming correction is
reflected below, but these remain **legacy client-path observations including
obsolete continuation pacing**. They are retained for provenance, not as clean
production latency or isolated model-speed rankings. Quality/routing grades
are unaffected by this measurement interpretation.

The run contains 48 oracle and 51 live turns per model: three repetitions of
16 fixed-evidence oracle cases and 17 live-tool cases. The live-price case
has no oracle counterpart. These are repeated observations of a small,
frozen question set, not 51 distinct live questions per model. Quality
grading is AI-assisted and source-grounded, not a human-panel certification.

| Live-stage metric | gpt-realtime-2.1 | gpt-realtime-2.1-mini |
|---|---:|---:|
| Factually correct answers | 51/51 | 50/51 |
| Strictly complete answers | 39/51 (76.5%) | 31/51 (60.8%) |
| Correct tool routing | 51/51 | 48/51 |
| Legacy transcript receipt, median / p95 (client pacing included) | 2.610 / 3.045 s | 2.676 / 3.986 s |
| Legacy PCM receipt, median / p95 (client pacing included) | 3.144 / 3.749 s | 3.084 / 4.419 s |
| Turns with pre-tool spoken preambles | 0/51 | 10/51 |

Timing starts immediately before user-turn submission, excludes connection
and session setup, and includes tool rounds. PCM means actual received audio
bytes, not transcript text, playback, or avatar rendering.
Preambles are excluded from final-answer timing. All p95 values use
nearest-rank; the plain `TEXT` channel was unused.

With identical oracle evidence and tools disabled, both models were
factually correct on 48/48 turns. Mini was more often strictly complete
(41/48 versus 38/48) and had lower oracle PCM median/p95
(1.204/1.614 s versus 1.338/2.441 s). Oracle tools-disabled compliance
is not a routing-quality score. The live result therefore supports full
2.1 for this tool-driven workload, not a claim that it wins every dimension.

Both models used the correct MTN FY2025 total revenue in all six live W2
turns combined, and each honestly abstained on all 12 oracle/live
missing-number controls. Mini's three live W5 turns incorrectly called
`search_minutes` instead of `search_web`. Its one factual error was a
share-price unit conversion: reporting ZAR 19,586.00 instead of R195.86.
Digits traceable to a source do not make an incorrect unit interpretation
factually correct.

Some omissions were retrieval-related: Mini's live Q8 queries missed the
deadline-bearing section (and one returned only an agenda snippet).
Other answers omitted required details or qualifiers. Strict incompleteness
does not necessarily mean hallucination, and successful fixed-query
retrieval checks do not guarantee every model-generated query succeeds.

## Observed retrieval latency

These are whole local tool invocations from `tool_calls[].duration_ms`,
including client/initial overhead, not Search-server-only or deployed-app
measurements. All 102 live turns made exactly one completed tool call.

| Model | Tool | Calls | Median / p95 |
|---|---|---:|---:|
| gpt-realtime-2.1 | AI Search | 36 | 0.422 / 1.396 s |
| gpt-realtime-2.1 | Web IQ | 15 | 1.389 / 1.703 s |
| gpt-realtime-2.1-mini | AI Search | 39 | 0.418 / 1.331 s |
| gpt-realtime-2.1-mini | Web IQ | 12 | 1.262 / 2.420 s |

Mini's three misrouted W5 turns account for the different tool sample
counts; this is not a matched-query retrieval comparison. Do not subtract
aggregate medians to estimate generation time.

Whole-turn reported live tokens were 296,477 for full 2.1 and 294,649 for
Mini, accumulated across tool and terminal-response rounds. Similar token
counts do not imply similar cost; model pricing, audio/text usage, and
caching require separate accounting.

**Candidate for application acceptance testing:** `gpt-realtime-2.1`.
It had better observed live completeness/routing. Its legacy client-path PCM
p95 was 0.671 s lower, while Mini's median was 0.059 s lower, but those
differences include benchmark pacing and must not be read as production
speedups. No statistical significance or broad accuracy guarantee is claimed. This comparison
used text input and locally executed tools, excluding microphone/ASR/VAD,
playback, and avatar latency. Cross-mode comparisons also differ in prompt,
web provider, voice, and execution path. No default change, production
promotion, or deployment follows automatically from these results.

## Quick control: gpt-realtime-2

A subsequent bounded control ran `gpt-realtime-2` on all 17 live questions
once: 17 completed, no failures or retries. It used the same frozen cases,
prompt, catalogue, production tools, retrieval configuration, API version
and voice as the study above. The comparison below selects **run 1 only**
from the existing 2.1 and Mini records, giving 17 turns per model; it does
not compare the new single pass against their 51-turn aggregates.

| Metric | gpt-realtime-2 | gpt-realtime-2.1 | gpt-realtime-2.1-mini |
|---|---:|---:|---:|
| Legacy TTFT, median / p95 (client pacing included) | 3.245 / 3.680 s | 2.619 / 3.045 s | 2.508 / 3.793 s |
| Legacy TTFA, median / p95 (client pacing included) | 3.817 / 4.331 s | 3.136 / 3.891 s | 2.981 / 4.001 s |
| Factually correct answers | 17/17 | 17/17 | 17/17 |
| Strictly complete answers | 12/17 | 12/17 | 10/17 |
| Required facts covered | 36/46 (78.3%) | 39/46 (84.8%) | 36/46 (78.3%) |
| Correct tool routing | 17/17 | 17/17 | 16/17 |

Here TTFT means receipt of the first spoken-transcript delta of the answer,
not a plain-text model-output channel or internal model computation.
TTFA means receipt of the first PCM audio chunk, not completion of the
answer or audible playback. No turn in these three run-1 groups had a
pre-tool spoken preamble, so first-any PCM timing equals answer PCM timing.

The quick sample showed greater required-fact coverage for 2.1. Its recorded
client-path median audio arrived 0.681 s earlier than 2.0, but the
token-dependent benchmark waits confound that latency comparison. It did
**not** have more strictly complete answers than 2.0 in this sample.
Mini had the lowest median, but lower strict completeness and one misroute.
The existing three-pass Mini findings remain as reported above.

The 2.0 run's AI Search tool median/p95 was 0.304/1.008 s (12 calls);
Web IQ was 0.715/1.274 s (5 calls). The first Web IQ call, W4 at 1.274 s,
is included, not discarded as warmup. Thus 2.0's observed tool calls were
faster while its overall answer arrival was slower. Tool latency is not a
fixed property of a model; do not subtract aggregate medians to estimate
generation time.

This was a later run, not a contemporaneous/interleaved experiment; live
queries, evidence and service/network conditions may differ. At n=17,
nearest-rank p95 is the sample maximum, not a robust tail estimate.
Quality grades are one source-grounded AI-assisted pass; fact IDs and
aggregates were independently reconciled, not independently regraded by a
second content reviewer. Raw turns and detailed comparison artifacts remain
private. The runner now accepts `gpt-realtime-2` explicitly via `--models`,
but its default two-model roster and application defaults are unchanged.

## Default promoted to gpt-realtime-2.1

Following this evaluation, `gpt-realtime-2.1` has been promoted to the
default `VOICELIVE_MODEL` for model-mode deployments (backend fallback,
infra/Bicep fallback and compiled ARM, `.env.example`, and the
configuration/deployment/voice-binding docs), based on its better observed
live fact coverage/routing and the observations available at the time.
The subsequent pacing audit means the historical latency rankings are not
independent evidence of production speed superiority. The default has not
been changed by this audit. `gpt-realtime-2` and `gpt-realtime-2.1-mini`
remain available as explicit `VOICELIVE_MODEL` overrides. Agent mode is
unaffected (still Terra/none, and still omits `VOICELIVE_MODEL`). This
benchmark's own default two-model roster (`gpt-realtime-2.1`,
`gpt-realtime-2.1-mini`) and the historical results recorded above are
unchanged.
