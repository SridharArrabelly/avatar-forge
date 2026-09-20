# Assistant evaluation results

Protocol: [assistant-evaluation.md](assistant-evaluation.md).
Earlier exploratory model comparisons: [evaluation-history.md](evaluation-history.md).

This page covers **agent mode**. The **realtime/model-mode** comparison is in
[evaluation-model-mode.md](evaluation-model-mode.md), with corrected realtime
timings. Its separate
[latency attribution and measurement correction](realtime-latency-investigation.md)
explains the old runner's within-turn waiting. Do not assume a defect in one
harness applies to another without checking its timing path.

The later [concise v4 prompt evaluation](terra-prompt-evaluation.md) records
the separate prompt-only smoke comparison and financial query/fallback gate.
The larger study below retains its original instructions and results; it was
not rerun or regraded for v4.
The subsequent [full v4 evaluation](terra-v4-full-evaluation.md) completed
99 text turns and nine audio samples but failed the result-replacement gate.
Its measurements are documented separately; the numerical headlines below
remain unchanged.

> **Measurement audit, 20 September 2026:** the 792-turn agent text harness
> paces/retries before each successful stream timer, and the 72-sample agent
> audio harness spaces calls after completion. Neither has the realtime
> runner's token-based tool-followup wait. Their numerical results below are
> retained, not adjusted by subtracting waits. These remain client-observed
> timings with SDK/observer/network costs; received PCM is not audible playback
> or a verified first answer-bearing audio boundary. See the
> [cross-harness audit](realtime-latency-investigation.md#agent-mode-measurement-audit).

This page reports measured stages separately. A retrieval pass is not an answer
quality pass, and a direct Search API timing is not voice response latency.
Private original text, evidence labels, queries and result bodies are retained
outside the repository.

## Completed comparison: recommended candidate

**Terra / reasoning `none` / section index / native `semantic` / k=5 is the
strongest observed overall candidate in this run.** Bing count stayed at 8.
This is a recommendation for the next evaluated configuration, not automatic
production promotion or a universal accuracy guarantee.

The following table compares the **none** settings. Each has 51 live-agent
responses; quality was scored blind before model identities were revealed.

| Model | Factually correct responses | Strict full-detail passes | TTFT median / p95 | First received audio median |
|---|---:|---:|---|---:|
| GPT-5.4 | 50/51 | 24/51 | **2.36s / 4.61s** | 3.20s |
| GPT-5.4-mini | 46/51 | 20/51 | 2.30s / 4.35s | 2.99s |
| Luna | 47/51 | 19/51 | 2.55s / 4.07s | 2.91s |
| **Terra** | **51/51** | **25/51** | **2.56s / 4.79s** | **2.47s** |

**Factually correct is not the same as fully answering every required detail.**
Terra/none had no identified false or unverified factual claims, but 26 of its
51 responses were incomplete against the frozen detail checklist. Its internal
responses were grounded in observed passages; live Bing passage grounding
remains unobservable. The audio column has only nine samples per configuration,
uses a standardized voice, and measures received PCM rather than audible
playback; see its section below.

GPT-5.4/none remains a close, lower-text-latency alternative: approximately
**0.20 seconds faster** at the median, not the earlier comparison's 1.5-second
none-versus-low difference. Terra/none had the cleaner observed factual and
formatting record and led this small received-audio sample. Three repeats per
question do not establish statistically universal superiority.

Terra/low did not improve the tradeoff: 50/51 factually correct responses,
24/51 strict passes, 2.87s median TTFT and 2.81s median received audio. Luna/low
had the most strict passes (28/51) but four false responses and a tool-budget
violation, so strict completeness alone is not a sound selection rule.
Mini's tiny text-median advantage came with missing tool calls and errors.

The main lesson is **not** that all earlier misses belonged to a model.
Fixing source delivery restored complete required internal evidence to all
three larger models. Fixed-evidence controls then separated accurate but
incomplete generation from actual retrieval/tool-use failures. Public-web
grounding still has evidence and completeness limitations shared across models.
No configuration met the complete-answer checklist on every question.

All **792 text turns and 72 agent-mode audio samples** completed and have
reconciled per-response grades. Grading was AI-assisted and source-based, not a
human-panel certification. Original grades, blinded packets, supplemental
public-fact adjudication and the single provider-error recovery remain available
privately. At completion of this agent study, production had not been switched
and the realtime study had not started; the later realtime results are linked
above rather than folded into this historical agent-mode cohort.

### Defaults approved after the user's manual test

Following the successful manual test, the repository defaults were changed to:

```dotenv
AGENT_MODEL=gpt-5.6-terra
AGENT_REASONING_EFFORT=none
AI_SEARCH_TOP_K=5
AI_SEARCH_QUERY_TYPE=semantic
CHUNKING_MODE=section
DOCUMENT_SCOPE=minutes
```

Fresh infrastructure defaults match Terra version `2026-07-09` with
`DataZoneStandard`; existing explicit settings still override defaults.
Bing count remains 8, and realtime-model defaults, embedding SKU and semantic
ranker billing were not changed. Explicit `window` + `all` ingestion and
`vector_simple_hybrid` + top-8 retrieval remain available for compatibility.

These are code/configuration defaults, not an automatic production migration.
Existing window indexes are retained, and applying section mode to them is
rejected until a new index name is selected. The measured outcomes below remain
historical observations of their recorded configurations.

## 2026-09-18: agent-mode minutes retrieval

**Candidate for review:** whole-section evidence blocks, **lexical retrieval
with semantic reranking**, `top=5`. This is Azure AI Search query type `semantic`,
not `vector_semantic_hybrid`. Hybrid retrieval was tested; it was not assumed to
be the best option.

The frozen candidate supplied every required original evidence unit on:

- 20 development cases x 3 rounds: **60/60** complete.
- 20 held-out cases x 3 rounds, canonical queries: **60/60** complete.
- The same held-out cases with predeclared natural-language paraphrases x 3:
  **60/60** complete.
- 21 distinct searches captured from 75 prior single-search minutes turns:
  **21/21** complete, without calling the models again.

These are **201 retrieval checks**, not 201 independent questions and not a
claim of universal 100% accuracy. The cases cover date-scoped attendance,
decisions, actions and discussion sections. Cross-meeting synthesis, unknown
meetings, arbitrary question families and final answer generation are not
certified by this result.

### Original-document and index integrity

The reference was extracted independently from each original DOCX's
`word/document.xml`, rather than built from Search results.

| Check | Result |
|---|---|
| Original minutes documents | 10 |
| Policy documents read or indexed | 0 |
| Tables / tracked changes in this corpus | 0 / 0 |
| Production extraction matches independent XML text | 10/10 |
| Expected / existing production chunks | 110 / 110 |
| Chunk IDs, text, source, position and dates | Matched |
| Required original evidence present somewhere in the index | All cases |

**No missing-source or ingestion-text loss was found in this corpus.** That
does not mean retrieval was adequate: present-in-index and returned-to-agent
are different tests.

The source reference contains 40 cases: four intents for each of ten meetings.
Five meetings (20 cases) were used for development; the other five were held
out from this tuning loop. Some meetings/questions had appeared in historical
experiments, so "held out" means held out from the current retrieval tuning,
not never previously seen.

Required evidence units are complete original paragraphs for the relevant
section, or the complete original attendance span. Coverage is calculated by
mapping the text of returned chunks back to original character intervals and
taking their union. Duplicate/overlapping chunks do not inflate coverage, and
a correct document ID or meeting title alone cannot pass. This is a deliberately
conservative source-section completeness measure, not an LLM answer grade.

### Development experiments and root cause

All layouts preserve the same required source evidence. No labels were relaxed
to make an experiment pass.

| Layout | Blocks | Hybrid k5 / k8 | Hybrid + reranking k5 / k8 |
|---|---:|---|---|
| Existing 1,200-character windows, 200 overlap | 110 | 10/20 / 12/20 | 14/20 / 15/20 |
| Whole paragraphs with section context | 212 | 5/20 / 5/20 | 12/20 / 17/20 |
| Whole sections with section context | 60 | 14/20 / 15/20 | 17/20 / 17/20 |

The paragraph variant was not promoted merely because it was more structured.
It improved some semantic retrieval but made plain hybrid coverage worse.

Rescoring the **actual delivered tool passages** from 75 earlier single-search
minutes turns against this new conservative source-coverage rubric found
complete required evidence in **32/75**. Their queries deduplicated to the
21-query replay cohort, which the frozen candidate subsequently passed 21/21
without a new answer-generation run. This is evidence that the retrieval
pipeline mattered; it is not a claim that all 43 incomplete-evidence turns had
wrong answers, nor that a model is excused for inventing facts.

One concrete failure explained why adding a reranker alone was insufficient:
for a dated discussion query, the required section was **lexical rank 1 but
hybrid rank 51**. It fell outside the semantic ranker's top-50 candidate window.
The reranker could not recover evidence it never received. This observation
motivated a lexical-only control, not a change to the answer model.

On the section layout, lexical retrieval alone passed 19/20 development cases.
Lexical retrieval **plus semantic reranking** passed 20/20 at both k5 and k8.
k5 was frozen before held-out testing because k8 added context and wrong-meeting
distractors without increasing required-evidence coverage.

### Retrieval latency: repeated, interleaved comparison

The table below uses the **same section index**, 20 development cases x 3
rounds per arm, `top=5`. Arm order was interleaved with a fixed random seed.
Authentication/connection warm-up and pacing sleeps are outside these timings.
The client runs on the developer's Windows machine; absolute timings should
not be assumed identical to requests originating inside the deployed app.

| Retrieval mode | Complete evidence | Median | p95 | Maximum |
|---|---:|---:|---:|---:|
| Lexical/BM25 | 57/60 | 220 ms | 258 ms | 266 ms |
| Hybrid, no reranking | 42/60 | 452 ms | 1,442 ms | 1,806 ms |
| Hybrid + semantic reranking | 51/60 | 520 ms | 1,386 ms | 1,652 ms |
| **Lexical + semantic reranking** | **60/60** | **290 ms** | **325 ms** | **376 ms** |

In this setup, reranking added approximately **70 ms median** over lexical
search and closed its remaining coverage gap. The winning pipeline was still
faster than the tested hybrid path. That does not isolate which part of vector
querying caused the difference, nor invalidate latency observations made under
older configurations.

### Frozen-candidate validation

The index definition, evidence reference and held-out query formulations were
recorded before validation. The chosen layout/mode/top-k were not changed after
viewing these results.

| Cohort | Complete evidence | Median | p95 | Maximum |
|---|---:|---:|---:|---:|
| Development, 3 rounds | 60/60 | 290 ms | 325 ms | 376 ms |
| Held-out canonical, 3 rounds | 60/60 | 285 ms | 324 ms | 334 ms |
| Held-out paraphrases, 3 rounds | 60/60 | 292 ms | 322 ms | 4,089 ms |
| Recorded model-generated queries, deduplicated | 21/21 | 283 ms | 328 ms | 334 ms |

The paraphrase cohort contains one client-observed 4.09-second outlier. Its
Search `elapsed-time` header was **207 ms**. It remains in the measured mean
and maximum; it is not attributed entirely to reranking. Median server elapsed
times for the four cohorts were 86.5 / 84 / 87 / 81 ms respectively.

**Coverage is not precision.** Wrong-meeting distractors still appeared:
12/300 returned hits in the canonical held-out cohort and 6/300 in the
paraphrase cohort. All required evidence was present, but the model must still
select the correct dated source. That remains a separate grounding test.

Mean returned content was approximately 6,476 characters on development,
7,676 on canonical held-out queries and 9,074 on paraphrases. The generation
phase must measure the token/latency consequences of this context; retrieval
timing alone does not establish the best end-to-end voice configuration.

### Native agent compatibility

Capability probes used disposable agents, a single control model and fixed
queries; they were not a model-quality bake-off.

- The existing index accepted native `vector_semantic_hybrid` in the current
  service/SDK. The older comment claiming this always fails was obsolete.
- Native query type `semantic` with the isolated section index returned complete
  required evidence for a development discussion case and a held-out,
  naturally worded attendance case.
- The submitted query was checked against the actual tool query. The delivered
  tool **text** was mapped back to the originals, not scored merely by IDs.
- Both evidence probes passed; disposable agents were deleted and the source
  agent version remained unchanged.

Probe total duration includes model/tool orchestration and is deliberately not
reported as pure retrieval latency.

### Agent-mode Bing grounding: explicit observability boundary

Microsoft documents that Grounding with Bing Search and Bing Custom Search do
**not** expose raw tool output to developers. They expose the grounded response,
search-query references and citations. The empty Bing result bodies in earlier
runs are therefore not evidence of an empty retrieval result.

Reference: [Bing grounding tool documentation, How it works](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/bing-tools#how-it-works).

Consequently:

- Do not claim Bing passage recall/coverage using the AI Search metrics above.
- Use approved public-source snapshots for the fixed-evidence answering test.
- Evaluate live Bing as an end-to-end grounding path, retaining query and
  citation metadata and independently checking dated claims.
- Keep unsupported causes **indeterminate** rather than automatically blaming
  the model or retrieval.
- Web IQ remains the separate model-mode web tool. It has not been substituted
  for Bing in agent mode.

### Azure state and review gate

Production `knowledge-index`, live agent bindings, Container Apps and azd
environment settings were not written by these experiments. Two isolated indexes were created:

- `eval-minutes-sections-20260918-dc53`: paragraph experiment, retained for review.
- `eval-minutes-sections-full-20260918-dc53`: frozen section candidate.

Together their embedding preparation consumed 44,285 reported embedding tokens.
The first paragraph upload briefly preceded Search visibility; the setup
script now waits for bounded, exact readback and supports read-only recovery
without repeating embeddings or overwriting an index.

A final repeat of the production readback could not complete because the Search
endpoint connection timed out; a separate bounded reachability check and bounded
Search/Foundry API recheck also timed out. The last successful native probes confirmed the source agent version was
unchanged. The retrieval measurements above had already completed. This final
connectivity issue is not hidden in a model score and does not constitute a
successful final production-health verification.

**Status at completion of this retrieval experiment:** paused for review,
without starting a new four-model comparison or promoting the candidate.
The user subsequently approved normal-tooling integration in an isolated
evaluation configuration, then a four-model agent comparison at fixed k=5.
That later phase must retain the same source references, distinguish
model-generated query failures from answering failures, and respect Bing's
observability limit.

The latest requested model-mode shortlist is `realtime-2.1`, `2.1-mini`, and
`mai-mm-realtime`, superseding the earlier realtime/Phi shortlist. Exact service
identifiers and regional/model/tool/avatar compatibility remain to be verified
before that separate track. No model-mode runs are part of this agent evaluation.

The realtime model-mode track that followed (candidate shortlisting, capability
preflight, restored runtime-Search parity and Web IQ metadata fixes, and the
oracle/live benchmark itself) is documented separately in
[evaluation-model-mode.md](evaluation-model-mode.md); it does not rewrite the
agent-mode history recorded on this page.

### Reproduction tooling

- [`bench_retrieval.py`](../scripts/bench_retrieval.py): read-only original-source
  audit and direct Search A/B; all output directories must be outside the repo.
- [`retrieval_evaluation.py`](../scripts/retrieval_evaluation.py): independent
  DOCX extraction, source intervals, frozen cases and evidence scoring.
- [`setup_evaluation_index.py`](../scripts/setup_evaluation_index.py): explicit,
  new `eval-*` indexes only; never updates an existing production index.
- [`smoke_retrieval_agent.py`](../scripts/smoke_retrieval_agent.py): temporary
  native-agent compatibility and delivered-evidence probe.
- [`test_retrieval_evaluation.py`](../tests/test_retrieval_evaluation.py):
  offline synthetic-source regression checks.

Run `--help` on the relevant script for arguments. `--date-filter` is an
oracle diagnostic, not evidence that a native agent dynamically applies that
filter. It was **not used** in the winning configuration. The development
and held-out results above used the unmodified query formulations recorded in
their private manifests.

## 2026-09-19: integrated retrieval baseline

The normal ingestion command now reproduces the tested whole-section layout,
through the shared `backend/document_sections.py` formatter. Its output was
checked against all **60 frozen section documents**, including text, IDs,
metadata and source hashes. During that evaluation the legacy window layout
remained the default; the user subsequently approved section/minutes as the
repository default, with explicit legacy options retained.

The staged evaluation configuration is:

| Component | Verified configuration |
|---|---|
| Versioned evaluation index | `eval-knowledge-sections-v1-dc53`, 60 section blocks |
| Isolated evaluation agent | `AvatarAgentRetrievalEvalV1-dc53`, version 1 |
| Search tool | Native `semantic`, top-k 5 |
| Other tools | Exact source Bing definition retained, count 8 |
| Source at activation/recheck | `AvatarAgent`, version 7, GPT-5.4 / none |
| Preserved fields | Model, prompt, reasoning, non-Search tools and connections |

The formerly failing attendance query also passed a native-agent probe against
the integrated index: the delivered text contained the complete required
original attendance span. This is a capability/evidence probe, not an answer
quality result. Production `knowledge-index`, live agent bindings and both
Container Apps were not updated by this activation.

### Retrieval measured inside the deployed app

A read-only worker ran in the existing agent-mode Container App replica with its
managed identity, explicitly targeting the evaluation index. It interleaved
BM25 and BM25 + semantic reranking on the **20 held-out natural-language
queries, three repeats each**, top-k 5. Returned text hashes were checked
against the frozen index before evidence scoring. No gold answers or raw
retrieved passages were printed from the container.

| Mode | Complete required evidence | Median retrieval | p95 | Maximum |
|---|---:|---:|---:|---:|
| BM25 only | 54/60 | 22.8 ms | 67.8 ms | 71.3 ms |
| **BM25 + semantic reranking** | **60/60** | **99.1 ms** | **134.1 ms** | **151.8 ms** |

Authentication/connection warm-up, local console transfer and pacing were
excluded. Median Search server elapsed time was 16.5 ms and 93 ms respectively.
The reranking cost is real, approximately **76 ms median** here, and it closes
the remaining evidence gap. These server-near results are not interchangeable
with the earlier desktop-originated measurements or with end-to-end voice
latency. Six wrong-meeting hits remained among the semantic arm's 300 results;
scope selection is still part of the answering evaluation.

Initial console setup hit a command-length error and a console-service 429
with a ten-minute Retry-After. The delay was honored, and the successful
measurement used one streamed console connection. These were **measurement
transport failures**, not Search or model inference failures. A partial
temporary worker file from the earlier transfer was explicitly removed.

The model comparison is authorized to proceed with this fixed retrieval
configuration. `bench_routing_matrix.py --fixed-retrieval` preserves all source
tool settings rather than sweeping Search and Bing breadth together. Fresh
source-based rubrics and fixed-evidence controls are frozen before the new
four-model inference calls; no previous quality percentage is treated as a
model-only baseline.

### Controlled-answering stage: factual correctness is not completeness

The new comparison uses 17 frozen cases: the historical ten questions, five
additional meeting questions, and two missing-information controls. Policies
remain excluded. Sixteen cases have fixed source evidence; the live-price
question is deliberately excluded from this control stage. Four models x
two reasoning settings x three repeats gives **384 fixed-evidence responses**.

The oracle stage disables tools and supplies the same source passages with a
documented evidence-only overlay. Grading is blinded to model, effort and
latency. The source fact checklist and numerical tolerances were frozen before
inference; factual correctness, completeness, grounding and spoken formatting
are recorded separately.

| Fixed-evidence cohort | Responses | Factually correct | Factual errors | Unverified | Full required-detail pass |
|---|---:|---:|---:|---:|---:|
| Minutes and missing-information controls | 288 | 286 | 0 | 2 | 117 |
| Stable public facts | 96 | 94 | 2 | 0 | 94 |
| Total | 384 | 380 | 2 | 2 | 211 |

The minutes control's **169 incomplete answers were not classified as false
answers**. They omitted required details, qualifiers, targets, dates or roles
from the frozen checklist despite those passages being supplied. All 48
missing-information control responses refused to invent the requested amounts.
This separates a completeness/prompt-detail-selection problem from retrieval
availability. The two public factual errors were explicit priority-count errors.

These aggregate control results are complete. They do not yet establish a
production winner: live tool use, generation with retrieved evidence, latency,
and observability limits must be evaluated separately.

### Live-agent stage: fixed retrieval, four models

All **408 live-agent turns** and **384 fixed-evidence turns** completed:
17 live cases and 16 oracle cases, three repeats at each model/effort setting.
Search stayed `semantic` / k=5 on the section index; Bing stayed at count 8.
Effort/case order was seeded and interleaved, with a separate paced worker per
model deployment. All four text deployments were DataZoneStandard.

| Model | Effort | First expected tool | Complete internal evidence | TTFT median | TTFT p95 | Completion median |
|---|---|---:|---:|---:|---:|---:|
| GPT-5.4 | none | 51/51 | 30/30 | 2.36s | 4.61s | 3.26s |
| GPT-5.4 | low | 51/51 | 30/30 | 3.31s | 7.32s | 4.26s |
| GPT-5.4-mini | none | 46/51 | 24/30 | 2.30s | 4.35s | 2.71s |
| GPT-5.4-mini | low | 38/51 | 20/30 | 2.73s | 5.66s | 3.21s |
| Luna | none | 51/51 | 30/30 | 2.55s | 4.07s | 3.08s |
| Luna | low | 51/51 | 30/30 | 2.81s | 6.29s | 3.58s |
| Terra | none | 51/51 | 30/30 | 2.56s | 4.79s | 3.23s |
| Terra | low | 51/51 | 30/30 | 2.87s | 6.07s | 3.49s |

The evidence denominator is the ten answerable internal cases x three rounds,
not web or missing-information controls. Coverage checks the required original
quotes in the **delivered** passages; it does not grade the answer. All eight
configurations used server-echoed `tool_choice=auto` and the correct agent
reference. Mini's 5/13 missing-call turns at none/low therefore were not caused
by accidentally leaving the oracle tool-disabled request mode on.

First-tool correctness also has limits: GPT-5.4/low made one web call after an
internal lookup on an internal question. Luna/low used four calls on one turn,
exceeding the prompt's three-call limit. These are separate scope/budget flags;
a correct first call does not erase them. No measured first text preceded
completion of a later tool call in this run.

**Recovery is part of the record.** One streamed GPT-5.4 provider error occurred
after a Bing call, before answer text. It was not a model quota 429. The runner
stopped safely after 713 successful turns, and its initial error classifier
failed to recognize the provider's explicit retryable streaming error. A
narrow retry fix and checked continuation completed only the 79 unfinished
turns. All 713 prior successes were verified unchanged; original failure and
cancellation records remain in the private evidence. Source, cases, catalogue,
seed and tool settings were preserved. No oracle or completed Mini requests
were repeated.

Timings above are successful-attempt measurements, excluding pacing, setup
and recovery/backoff waits. The interruption and cold reconnection can affect
comparability; this is not an uninterrupted load/SLA test. The 792 completed
responses reported 6,393,125 tokens, including cached input; failed-attempt,
pilot and audio usage are outside that total. Temporary benchmark agents were
cleaned up and the isolated source definition remained unchanged.

### Internal-answer quality: truth and required detail

These are the finalized blinded minutes grades: ten answerable questions and
two missing-information controls, three repeats (**36 answers per arm**).
An answer can be factually correct and grounded while omitting a required
detail. The strict-pass column must not be relabelled an accuracy percentage.

| Model | Effort | Factually correct | False / unverified factual status | Grounded | Strict full-detail passes | Correct negative refusals |
|---|---|---:|---|---:|---:|---:|
| GPT-5.4 | none | 36/36 | 0 / 0 | 36/36 | 16/36 | 6/6 |
| GPT-5.4 | low | 35/36 | 0 / 1 | 35/36 | 13/36 | 5/6 |
| GPT-5.4-mini | none | 33/36 | 1 / 2 | 31/36 | 10/36 | 6/6 |
| GPT-5.4-mini | low | 26/36 | 4 / 6 | 24/36 | 10/36 | 5/6 |
| Luna | none | 36/36 | 0 / 0 | 36/36 | 13/36 | 6/6 |
| Luna | low | 35/36 | 1 / 0 | 35/36 | 20/36 | 6/6 |
| Terra | none | 36/36 | 0 / 0 | 36/36 | 16/36 | 6/6 |
| Terra | low | 36/36 | 0 / 0 | 36/36 | 15/36 | 6/6 |

**The completeness rubric is intentionally demanding.** For example, all
24 fixed-evidence dividend answers included the approval and conditional
forward guidance, yet none passed every detail requirement. Most omitted
that the exact figures were not in the minutes, or the earnings rationale.
Other frequent misses were action deadlines/stakeholder inputs, risk-review
scope, climate/reporting commitments, and audit scope. These are frozen
checklist misses, not evidence that all those answers were false.

The same pattern appears when retrieval is removed from the experiment.
It therefore warrants review of the required-detail checklist against the
intended concise voice experience, and of prompt/detail-selection behaviour,
before treating strict completeness as a production acceptance threshold.
The scores have **not** been relaxed after seeing the outcomes.

There were six false internal answers in the live text cohort, compared with
zero in the fixed-evidence minutes control. Five occurred in Mini arms; one
Luna/low answer confused a plan-presentation deadline with implementation.
Correct refusals on the absent-amount controls were 46/48 live versus 48/48
with fixed evidence. Source-correct but unsupported claims and unresolved
claims remain separate categories from demonstrated falsehoods.

For internal questions, GPT-5.4/none and Terra/none both achieved 36/36
factual/grounded answers and 16/36 strict passes. GPT-5.4/none had the lower
text first-token median in this sample. Mini's approximately 64 ms median
advantage over GPT-5.4/none is not a persuasive trade for its missing tool
calls, lower evidence coverage and factual errors. This is an internal-track
comparison; the live web track and audio limitations still matter.

### Public-web quality and the remaining grounding gap

The five web questions produced 15 live answers per configuration. Independent
public-source checks establish factual support, not the hidden Bing passages.
The original short excerpts proved too narrow for some correct extra details
(CFO appointment date, named financial products and strategy duration).
Supplemental official sources were checked blind, and a separate adjudication
resolved those facts uniformly without changing questions, numeric tolerances,
oracle inputs or model outputs. Original judgments remain preserved.

| Model | Effort | Factually correct | False / not established | Full-detail passes |
|---|---|---:|---|---:|
| GPT-5.4 | none | 14/15 | 1 / 0 | 8/15 |
| GPT-5.4 | low | 15/15 | 0 / 0 | 10/15 |
| GPT-5.4-mini | none | 13/15 | 1 / 1 | 10/15 |
| GPT-5.4-mini | low | 10/15 | 3 / 2 | 6/15 |
| Luna | none | 11/15 | 4 / 0 | 6/15 |
| Luna | low | 12/15 | 3 / 0 | 8/15 |
| Terra | none | 15/15 | 0 / 0 | 9/15 |
| Terra | low | 14/15 | 1 / 0 | 9/15 |

The most important contrast is **FY2025 revenue**. With the issuer's income
statement supplied, all **24/24** controlled answers gave the correct total.
In live text grounding, only **3/24** did so completely: 19 supplied service
revenue without the requested total, and two gave a wrong total. In the
additional audio sample, only **1/24** provided the correct requested total;
22 were service-only/incomplete and one was false under the frozen precision
rule. Accurate partial figures are not complete answers to the scored question.

This does not prove that Bing alone caused every miss: its raw context is
hidden, so query formulation, retrieval, context presentation and answer
selection cannot all be isolated on that path. It does demonstrate that
the four models can read the figure correctly when supplied adequate evidence.
Choosing a different model or increasing reasoning effort is not a demonstrated
complete fix for the live financial-grounding path.

Other findings:

- All 24 live CFO answers passed after appointment-date verification.
- Price errors included a JSE/rand/previous-close magnitude mismatch:
  the same close was reported as R19.59 rather than R195.86. Other price misses
  were missing required as-of/delay details, not necessarily wrong prices.
  No differing price was rejected solely because it differed from a snapshot.
- Fintech errors included an obsolete customer target presented as current.
  Correct branded-product details were not rejected merely because the short
  original reference excerpt omitted them.
- Strategy errors were explicit priority/framework substitutions, not penalties
  for giving a concise thematic summary or omitting optional successor detail.

The full-detail checklist should be reviewed against the intended voice UX:
for example, a correctly dated previous close can still fail its frozen
feed-delay-disclosure requirement. This report preserves that strict result
instead of relabelling it factual inaccuracy or quietly changing the bar.

### Agent-mode received-audio sample

The additional audio cohort contains **72 successful turns**: three fixed
questions (dividend decision, attendance and FY2025 revenue), three repeats
per configuration. These are Voice Live **agent-mode** calls, not realtime
model-mode tests. Conversation auditing recovered the actual Search calls and
passages afterward; Bing raw content remains unavailable by design.

| Model | Effort | First received PCM median | Revenue question median | p95 / max |
|---|---|---:|---:|---:|
| GPT-5.4 | none | 3.20s | 3.48s | 3.90s |
| GPT-5.4 | low | 2.70s | 9.17s | 9.88s |
| GPT-5.4-mini | none | 2.99s | 3.61s | 4.58s |
| GPT-5.4-mini | low | 2.95s | 7.93s | 9.12s |
| Luna | none | 2.91s | 3.57s | 3.65s |
| Luna | low | 3.29s | 8.66s | 11.57s |
| Terra | none | 2.47s | 3.57s | 3.84s |
| Terra | low | 2.81s | 6.24s | 10.28s |

**Scope matters:** timing starts at text submission on an established Voice Live
session and ends at receipt of the first nonempty PCM delta. It does not
measure microphone input, VAD, playback buffering, actual audibility or avatar
video. Connection setup was separate, with medians approximately 2.2 seconds.
All arms used `en-US-AvaMultilingualNeural` for a standardized comparison;
the live app currently uses `en-ZA-LeahNeural`, so this is not a production-voice
latency claim. Each cell has only nine samples (three per question); nearest-rank
p95 is therefore the maximum.

The longer low-effort revenue samples involved additional Bing searches:
the audited none arms each used one search; low used one to three. That is
an observed workflow difference, not proof that all the delay is reasoning.
Two Mini dividend samples had no Search call, and a fast packet is not a
grounded answer. Audio transcripts are graded separately before treating
these response times as quality-qualified.

All 48 internal audio transcripts were factually correct, but two Mini turns
lacked a Search call and therefore lacked delivered evidence. Only 13/48 met
every required-detail item. For the 24 revenue transcripts, 23 contained
correct factual claims, but only one answered with the required total;
the others were incomplete or, in one case, numerically false. Thus **a fast
received-audio packet must not be read as a fully successful answer**.
Transcript checks do not certify pronunciation, acoustic quality or playback.

### Evidence and reproducibility

Private evidence is retained under `fixed-retrieval-20260919`:
frozen cases and public excerpts, original and recovered run directories,
source-invariance/cleanup receipts, blinded review packets, and audio
conversation audits. The consolidated text dataset is `shootout-recovery`;
`shootout` is retained unchanged as the interrupted original.

The public-source reference uses strict financial figures: R226.707bn or
correctly rounded R226.7bn total revenue, not the earlier exploratory
approximation tolerance. Where a live answer adds a public fact omitted by a
brief reference excerpt, independent **blinded factual adjudication** can
corroborate that claim with a dated primary source. Such supplemental evidence
is kept separately; it does not change the frozen question, numerical rule,
oracle input or model output, and cannot prove what hidden Bing passages
contained. Unresolved claims remain unverified rather than automatically false.

### Operational checks before any production promotion

Final readbacks confirmed the live `AvatarAgent` stayed at version 7 with the
same full definition, production `knowledge-index` retained its original
110 chunks/schema, and the isolated evaluation index retained its 60 verified
section blocks. The live container still points at `AvatarAgent` and
`knowledge-index`; this work has not silently switched production retrieval.

The Search service is **Basic, one replica/partition, with semantic ranker on
the `free` billing plan**. That plan has a finite monthly allowance; after it
is consumed, semantic requests can return billing errors. Before a larger
follow-up or production promotion, review the remaining allowance or explicitly
approve the Standard semantic billing plan. No billing tier was changed here.
See [Microsoft's semantic-ranker billing guidance](https://learn.microsoft.com/en-us/azure/search/semantic-how-to-enable-disable).

A read-only Bing configuration check confirmed that the MTN investor domain
is allow-listed, but the official Vodacom domains used by the independent
reference are not. This is source-coverage context, not proof of which hidden
passages Bing returned. The allow-list was not changed during the experiment.
