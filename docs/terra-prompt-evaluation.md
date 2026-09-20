# Terra concise v4 prompt evaluation

Status: **passed the bounded promotion gate** after the financial query/fallback
adjustment. The final tested instructions are selected by the repository's
normal agent loader. This is not a claim of perfect answer completeness or
reliable retrieval of every financial figure; limitations remain below.

The final prompt is
[`prompts/agent/instructions.md`](../prompts/agent/instructions.md).
It begins with the dynamic persona, contains all four supported placeholders,
and expands the shared onboarding block once. It removes source-specific rule
examples and repeated routing/voice instructions while preserving meeting-only
internal grounding, public-fact routing, date resolution, financial units and
honest missing-evidence handling.

V4 deliberately allows each selected tool at most once per turn.
The previous prompt allowed a refinement within its three-call cap. This is a
behavioral difference, not merely fewer prompt tokens; any measured result
compares the complete instruction sets, not length in isolation.

## Evaluation scope

Use an isolated copy of the current production prompt agent, preserving all
definition fields except instructions: Terra, reasoning none, native semantic
Search/top-5, existing Bing Custom Search and tool settings.

The bounded smoke comparison uses six questions per arm, one repetition each:
assistant onboarding, catalogue-only date lookup, two meeting-content questions,
public financial results and a public share-price question. Both arms receive
the same frozen catalogue and fresh conversation context. Alternate arm order
between question pairs. Do not run a full model matrix or any audio calls.

The existing `bench_agent_evaluation.py` streaming measurement helper is reused.
SDK inference retries are disabled. Admission pacing occurs between complete
calls; evidence writes follow stream completion. TTFT is first received text,
not first audible speech or rendered avatar onset. Tool event intervals are
client-observed lifetimes, not independently measured server execution.

Foundry evaluation/optimizer MCP tools are not available in this session.
This uses the user's reviewed draft and the repository's existing native-agent
SDK harness, not a generated optimizer-service candidate or an automated
LLM-judge evaluation. Static contract checks do not establish model behavior.
Small-sample answer assessment must remain separate from those checks.

Private questions, source evidence, answers and raw traces remain outside Git.
Only aggregate results and the candidate prompt belong in this repository.
No production agent version, Container App, index, model-mode prompt or
canonical azd settings were changed during evaluation. Production adoption is
a separate, guarded instruction-only update after merging the tested source.

## Results

Measured on 20 September 2026. Exactly **25 paid text requests** completed:
12 in the initial paired comparison, three stricter-grounding follow-ups,
three financial-query checks, two explicit-fallback checks and five final
regressions. No inference retries, failures or audio calls. Production v9's
complete definition/version was identical before and after every test run.

### Size and version identity

Counts use the configured display name Nuru. "Rendered" means after replacing
all four placeholders, including the shared onboarding block.

| Instructions | Source characters | Rendered characters | Rendered words |
|---|---:|---:|---:|
| Previous production v9 | 17,702 | 18,855 | 2,943 |
| Initial candidate in the six-pair test | 4,318 | 5,494 | 856 |
| Rejected stricter-grounding candidate | 4,600 | 5,776 | 904 |
| Final v4 | 4,755 | 5,931 | 923 |

The final prompt is **68.5% smaller by rendered character count**. Character
and whitespace-word counts are not model token counts.

The final revision targets financial statements/income statements and explicitly
acknowledges an unverified total before providing a supported, labelled related
figure. Initial paired results below do **not** belong to this later revision.

| Candidate revision | SHA-256 of rendered instructions |
|---|---|
| Initial paired candidate | `bd7e6dd552b1027f1ef6acca2f2be5abb8ec3d8ff1be88eba6dd460909d9fd64` |
| Rejected stricter-grounding candidate | `265895bb528d330c96a4a09b704d949410dfb2673e49baf1bc7bfb94c46b3dca` |
| Final v4 | `6635e036771ecf7aba9c86a512537fefc619a256e57b434d9679053ea444c130` |

### Initial paired comparison

Times are first received text, in seconds, not audio onset.

| Case | Current v9 | Initial candidate |
|---|---:|---:|
| O1: assistant onboarding | 3.394 | 1.514 |
| C1: latest meeting date from catalogue | 1.924 | 1.512 |
| Q1: latest meeting's dividend decision | 2.670 | 2.530 |
| Q7: dated meeting's fine and audit actions | 3.954 | 1.727 |
| W2: published annual revenue | 3.105 | 4.263 |
| W3: current share-price request | 3.912 | 2.750 |

| Metric | Current v9 | Initial candidate |
|---|---:|---:|
| Correct selected tool sequence | 6/6 | 6/6 |
| Citation/URL markers in answer text | 0/6 | 0/6 |
| Answers exceeding 70 words | 0/6 | 0/6 |
| Text emitted before observed tool completion | 0/6 | 0/6 |
| Median first text, all six cases | 3.250 s | 2.129 s |
| Median first text, four tool cases only | 3.508 s | 2.640 s |
| Median full response completion | 3.653 s | 2.751 s |
| Total reported tokens across six responses | 53,209 | 33,032 |

The initial candidate was faster to first text in five of six pairs and used
37.9% fewer reported tokens. This is **not a proven production speedup**:
there is one observation per case/arm, live results and cache hits differ,
and prompts also differ in query wording and allowed refinement behavior.
The financial-results case was slower. No useful p95 or significance claim
is made from this sample.

### Answer assessment and promotion blocker

Assistant review used the existing private source-grounded fact rubrics, not
a new paid LLM judge. Routing/style checks are separate from factual quality.

- Both arms reproduced the shared getting-started reply and answered the
  catalogue date correctly without tools.
- Both meeting answers covered key facts but omitted required detail: the
  dividend announcement caveat in Q1 and the all-operating-companies audit
  scope in Q7. Correct routing is not a complete-answer score.
- For FY2025 revenue, the verified official income-statement reference is
  **R226,707 million total revenue**, equivalently R226.707 billion or R226.7
  billion when rounded to one decimal. The initial candidate instead claimed
  **R226.3 billion**. The current prompt did not invent a total, but returned
  only labelled service revenue and therefore did not fully answer the case.
- The initial share-price answers had the correct reference amount and
  last-close framing, without a cents/rand conversion error. Source-delay
  qualifications were not fully preserved.

The native tool events expose queries and lifecycle timing, but their result
`content` is null in these captures. Thus the financial claim can be checked
against the verified reference, but this run cannot establish whether its
immediate cause is incorrect retrieved evidence or the model's handling of
that evidence. Additional uncaptured claims are not certified as grounded.

### Rejected stricter-grounding candidate: three follow-ups

After strengthening financial grounding, only three candidate calls were run.
These are **not** a second paired comparison or a replacement for the six-case
baseline. No broader matrix was run.

| Case | First text | Observed outcome |
|---|---:|---|
| W2: annual revenue | 4.928 s | Still incorrect: **R226.9 billion**, rather than the verified R226.707 billion |
| Q11: exact dividend amount absent from minutes | 2.272 s | Correctly stated that the amount was absent and would be published in the results announcement; did not invent an amount |
| W3: share price | 4.276 s | Correct amount, explicit Friday 18 September close, Sunday market closure and delayed-price wording; exact 15-minute qualifier still omitted |

All three selected the expected single tool, stayed within 70 words and
emitted no citation/URL markers or pre-tool text. They used 23,675 reported
tokens in total. The tighter wording **did not resolve the revenue error**;
that candidate was not promoted.

### Query/fallback adjustment and final gate

The next change directed financial queries to issuer financial statements or
income statements and restored a labelled related-metric fallback. Three
fresh-context revenue calls produced **no invented total**: two explicitly
acknowledged an unverified total, while one gave only labelled service revenue.
Because that third response omitted the missing-total acknowledgement, the
fallback instruction was tightened once more.

The final v4 was then frozen and checked on two revenue repetitions plus five
regressions. The exact same rendered prompt hash appears in both run manifests.

| Final v4 check | First text | Outcome |
|---|---:|---|
| Revenue, repetition 1 | 5.015 s | Explicitly unverified total; correctly labelled R218.5bn service revenue |
| Revenue, repetition 2 | 3.497 s | Correct R226.7bn total, separately labelled R218.5bn service revenue |
| Getting started | 2.141 s | Exact shared onboarding reply, no tool |
| Latest meeting date | 1.623 s | Correct catalogue date, no tool |
| Dividend decision | 2.371 s | Correct meeting decision, increase, conditional outlook and announcement caveat |
| Fine/audit question | 1.683 s | Correct fine, approximate SIM count, owner and deadline; full audit scope still omitted |
| Share price | 3.542 s | Correct saved-reference amount and prior-close date; Sunday closure explicit, exact 15-minute qualifier omitted |

All seven completed, chose the expected tool sequence, stayed within 70 words,
and emitted no citation/URL markers or text before observed tool completion.
The final seven-call median first text was **2.371 s**; the five tool calls had
a **3.497 s** median. They used **42,343 reported tokens**. These are descriptive
statistics, not a matched speedup against the earlier six-case baseline:
the final sample contains two revenue calls and was collected later.

The financial gate accepts a correct total **or explicit, accurately labelled
fallback**, not a guessed total. It passed 2/2 for the final prompt. Only 1/2
actually supplied the requested total, so the Bing completeness issue is not
solved. Audit-scope and exact quote-delay omissions are also retained as known
limitations, not hidden inside an "all facts passed" score.

### Disposition

The user approved promotion conditional on the narrow query/fallback test and
regression checks. The final v4 meets that bounded gate and replaces the active
agent instructions; model, reasoning, tools, retrieval settings and model-mode
instructions are not part of this change. No broad quality or audible-latency
guarantee is inferred from this small text-only sample.

For further financial improvements, obtain inspectable evidence for the managed
Bing lookup and verify that the requested metric is available. Raw Bing context
is hidden here; queries, retrieved evidence and model interpretation have not
been independently isolated.

Seven related offline suites passed, including the five prompt authoring
contracts, placeholder expansion, persona/onboarding, instruction-only update,
unchanged model-mode scope and documentation links. Mutation checks confirmed
that duplicate onboarding and removal of the current-date anchor are rejected.
These checks validate authoring and integration, not factual model behavior.

Private evidence folders `paired-smoke-1`, `candidate-followup-1`,
`financial-query-gate-1`, `financial-fallback-gate-1` and
`financial-query-regression-1` contain
frozen instructions, cases, catalogue, schedule, source before/after snapshots,
per-turn answers/usage/tool events and summaries. Six uniquely named
evaluation-only prompt agents are retained for review; no hosted compute was
deployed and neither production environment was changed.
