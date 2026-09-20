# Assistant evaluation

**Retrieval-first evaluation protocol.** The user reviewed the retrieval
results and approved isolated integration and the four-model agent comparison
on 18 September. Production promotion and the later realtime-model comparison
remain separate decisions.

- [Current evaluation results](evaluation-results.md) records findings and the
  review decision separately from this methodology.
- [Evaluation history](evaluation-history.md) preserves exploratory end-to-end
  configuration measurements. They are not isolated model-ability results or a
  current production recommendation.

The **best measured retrieval candidate** is a full-section isolated index with
**lexical candidates + semantic reranking, top 5** (`semantic_keyword` in the
runner; native `query_type='semantic'`, **not** `vector_semantic_hybrid`). It was
frozen before held-out testing. It supplied all required evidence in its measured
cases, but that is **not 100% precision**: wrong-meeting distractors remain.
See the results record for scores, timing distributions and diagnostic evidence.
**Production is unchanged.** The integrated evaluation index and agent are
separate from the live configuration; earlier experimental indexes are retained
for review. See the results record for the authorized agent and received-audio
measurements, their scopes and limitations.

## Scope and evidence handling

Use **original meeting minutes only** for the internal evaluation. Exclude
customer policy documents and questions from **all new evaluation**, even where
historical harness defaults include them. Do not use generated summaries,
rewritten minutes or an index's own output as independent ground truth.

Source text, gold answers, document manifests, retrieved passages, full traces
and customer material belong only in access-controlled **private artifacts**.
The repository may contain methodology, non-sensitive configuration identifiers
and reviewed aggregate findings, not those artifacts or customer-bearing paths.
Keep the existing historical record separate; do not append new raw evidence.

Measure a versioned configuration under stated conditions. A perfect score on a
finite test set is not a promise of **100% LLM accuracy** on future questions.

## 1. Establish independent sources and gold

Before querying the index:

1. Inventory the original documents independently of ingestion. Record stable
   source/document IDs, versions and hashes of original bytes in a private
   manifest; record extracted-text hashes separately.
2. Build the gold reference directly from those originals, with an independent
   evidence review rather than accepting a retrieved or generated answer.
   Each case needs source anchors, **required facts**, acceptable alternatives,
   and whether it is answerable, ambiguous or deliberately unanswerable.
   Cover multi-part answers, dates, names, decisions, action owners and numbers,
   not just finding a document with the right title.
3. Split **development** and **held-out** cases before tuning. Keep near-duplicate
   questions and closely related source facts together to avoid leakage. Reserve
   held-out cases and their scoring evidence from the tuning loop.
4. Predeclare repetitions, evidence thresholds, latency budgets and the permitted
   evidence/latency tradeoff, scoring denominators and review rules. Blind reviewers
   to arm/model labels where possible and require source-backed judgments with
   adjudication of disagreements. Declare numeric units, conversions, tolerances,
   rounding precision and intermediate-rounding rules **before** seeing outputs;
   a wrong year or financial measure is not a rounding error.

## 2. Verify extraction, chunking and index integrity

Trace original source -> extracted text -> chunks -> indexed records using the
independent manifest and stable chunk IDs/hashes. Check:

- Missing or duplicate documents/chunks, upload completeness and stale versions.
- Headings, tables, numbers, units, dates and attribution surviving extraction;
  chunk boundaries, overlap and truncation not separating required evidence.
- Source, title, meeting date and other filter metadata matching the originals.
- Embedding model/version, vector dimensions and coverage; index schema,
  searchable/retrievable fields, vector settings and semantic configuration.

Record integrity failures separately from ranking failures. Finding the right
document cannot compensate for a required fact that was lost during extraction
or omitted from the indexed chunks.

The [current source audit](evaluation-results.md) matched the original DOCX/XML
sources and production chunks exactly, with no lost source evidence. Its ranking
diagnostic instead shows a required lexical-rank-1 section pushed to hybrid rank
51, outside the reranking candidate window. A reranker cannot rescue evidence
that candidate generation never supplies.

### Current retrieval tooling

[`bench_retrieval.py`](../scripts/bench_retrieval.py) provides `audit` for an
independent original-DOCX XML source audit and `run` for live direct retrieval.
It uses [`retrieval_evaluation.py`](../scripts/retrieval_evaluation.py);
[`test_retrieval_evaluation.py`](../tests/test_retrieval_evaluation.py) checks the
helpers offline with synthetic data, not the original corpus or Azure.

[`setup_evaluation_index.py`](../scripts/setup_evaluation_index.py) creates only
new `eval-*` indexes, with `--layout paragraph|section`; `--verify-existing` is
read-only recovery/verification, not permission to overwrite an existing index.
[`smoke_retrieval_agent.py`](../scripts/smoke_retrieval_agent.py) is a **paid**
disposable-agent compatibility/evidence probe, not an offline test or a model
bakeoff. Keep its definitions, queries and full tool passages private.

The current reference set has **40 paragraph-span retrieval cases: 20 development
and 20 held-out**, with no policies. Source hashes, reference spans and full traces
stay private. Paragraph-span coverage is a retrieval diagnostic, not a substitute
for independently reviewed required-fact gold or a claim of answer correctness.

## 3. Direct retrieval A/B, without answer generation

Within each versioned index/layout, compare these runner modes at **top 5** and
**top 8**, keeping queries, filters and other settings fixed:

| Runner `--modes` value | Candidate generation | Semantic reranking |
| --- | --- | --- |
| `keyword` | Lexical/keyword only | No |
| `hybrid` | Keyword + vector fusion | No |
| `semantic` | Keyword + vector fusion | Yes |
| `semantic_keyword` | Lexical/keyword only | Yes |

The original simple-hybrid vs semantic-hybrid A/B remains part of this matrix;
lexical arms distinguish candidate-generation loss from reranking behavior.
Record embedding configuration/reuse, vector candidate counts where applicable,
semantic settings and returned breadth. Do not silently change other settings
between arms or conflate results from different index layouts.

Run both query tracks and report them separately:

- **Reference queries:** predeclared queries derived from the independent cases;
  `--query-style canonical|natural` selects wording variants. Report each style
  separately; natural wording is not a substitute for actual model-query replay.
- **Actual model-query replay:** replay the exact queries and filters captured in
  existing private tool traces, not cleaned-up paraphrases or the user's question
  substituted for an unseen query. This tests retrieval with real query wording
  without starting a new model sweep. Deduplicate repeated earlier queries and
  report unique-query counts as well as repeats. Unobserved queries remain untested.

For both reranking arms, verify actual execution using returned reranker scores;
merely requesting semantic mode does not prove it ran. Missing scores, fallback
or errors must be visible, not counted as a verified semantic comparison. An
**oracle date filter** derived from the expected source is **diagnostic only**:
report it separately, never as realistic retrieval quality or mixed into the
main unassisted/reference-query results.

Select on **required-fact evidence coverage and retrieval latency together**.
Document/source hit@k is diagnostic; evidence coverage@k is facts supported by
returned passages / applicable required facts. Also report cases with **all**
required evidence, missing facts, source or meeting contamination, and
irrelevant/duplicate chunks. Deduped overlapping chunks must not inflate coverage.
A right-document hit with the answer absent from its returned chunks is not a
complete retrieval success. Conversely, full required-evidence coverage does not
mean every returned passage is relevant: report precision/distractors and
wrong-meeting contamination separately.

For deliberately unanswerable cases, check that no returned passage is being
mistaken for supporting evidence; search returning something is not proof of
answerability. Keep these cases separate from answerable-case coverage.

### Latency protocol and selection

- Predeclare repeat counts and use a **seeded, interleaved retrieval-arm order**
  across the selected mode/top-k arms within each case/repeat block, rather than
  finishing one arm before starting another. Save the seed and actual schedule;
  hold concurrency, client location and other load conditions comparable.
- Report **per-arm p50 / p95 / max**, sample counts, failures and timeouts beside
  evidence coverage, separately by split and query track. Use repeated paired
  cases, not one request or a pooled average, to judge the latency/evidence tradeoff.
  Small samples do not establish a stable tail-latency estimate.
- Measure client request-to-consumed-response time with a monotonic clock.
  Capture **server timing when exposed**, including the raw `elapsed-time`
  response header, with its stated units and boundary; do not infer server
  processing time from client elapsed time. Record query-embedding time and reuse
  separately so arms use comparable timing boundaries.
- Separate **authentication and TLS/connection warmup**, tagged warmup requests,
  and cold/warm observations from steady-state search timings. Record overhead
  where measurable; unavailable components remain unknown, not guessed
  subtractions. Do not attribute an unmatched cold start to reranking.
- Record **pacing, retries and backoff** separately from successful request time,
  while retaining all attempts and total wall time. Do not hide failures or retry
  cost by reporting only fast successful requests.
- Select against the predeclared evidence requirements **and** latency budgets.
  Show what evidence is gained for the added latency and justify the tradeoff.
  **Do not retain reranking merely because it is enabled** or returns a score;
  that proves execution, not suitability. Selection remains subject to review.

### Native capability versus binding parity

The production native agent still uses **`VECTOR_SIMPLE_HYBRID`**; model mode
uses **semantic hybrid**. Native **`vector_semantic_hybrid` works in the current
environment**; it is not blocked by the earlier SDK-rejection assumption.
The selected lexical-reranked candidate instead uses native
**`query_type='semantic'`**, corresponding to runner **`semantic_keyword`**.
The runner's `semantic` mode means **hybrid + ranker**, not this native lexical
mode: names alone do not establish parity.

Two native evidence probes checked the **actual full tool passage text**, not
just IDs. Their definitions and evidence are recorded privately, with outcomes
in [Current retrieval results](evaluation-results.md). These are compatibility
and evidence-delivery checks, not a model-quality or voice bakeoff. Reverify the
pinned definition, actual query, full evidence and errors when changing versions
or wiring; direct Search API success or configuration acceptance alone is not
proof of native-tool behavior.

## 4. Fix on development cases, freeze, then review retrieval

Diagnose source/extraction, chunking, metadata/filtering, query formulation and
ranking separately. Make fixes **only on development cases** and rerun those
cases. Test section-aware chunking or other ingestion changes in an **isolated,
versioned development index**, retaining the baseline and the same evidence/latency
protocol; an experiment does not automatically replace the live index.
Freeze a versioned source/gold manifest, extraction/chunking settings,
embedding deployment, index/schema/semantic settings, query/filter rules and
evaluation code/config before scoring held-out cases.

Run the predeclared held-out protocol against that frozen configuration. Do not
tune on held-out failures and keep calling the same set held-out: a further
iteration needs a new version and a fresh holdout. Report sample sizes,
repetitions, failures, uncertainty and trace gaps alongside coverage and latency.

**Gate:** publish the non-sensitive retrieval findings in
[Current retrieval results](evaluation-results.md), including the frozen version,
coverage, per-arm latency and their tradeoff, remaining failures and proposed next
step, then obtain explicit review/approval.
Completing retrieval tests or getting a good aggregate score does **not** authorize
model selection, a full-pipeline model sweep or a voice bakeoff. The explicit
approval for the September 19 agent run is recorded above; it is not blanket
permission to promote a winner or start the deferred realtime-model track.

## Trace and scoring contract

Keep these records private and join them by case/run/tool-call ID:

| Record | Required detail |
| --- | --- |
| Identity | Case/split/repeat, arm-order seed and actual schedule, source and gold versions/hashes, index/config version, code revision, binding, deployment/model version, reasoning effort, prompt/catalogue/date context and tool definitions. |
| Request | Original question, reference vs replay track, exact observed query and filter, query mode, top-k, vector candidates and semantic configuration. |
| Observable retrieval | Tool outcome, ordered ranks and available search/reranker scores, stable chunk/source IDs and full returned AI Search passages, evidence spans for each required fact, and the exact observable evidence supplied to generation. Record truncation or dropped passages; IDs alone do not prove delivery. |
| Hidden grounding | Bing raw grounding content is not exposed by design. Preserve exposed queries, citations, answers and tool metadata, not invented passages or promises of audit recovery. Hidden-evidence causal attribution remains indeterminate. |
| Timing | Defined start/end events, client retrieval duration and server timing/raw `elapsed-time` header when exposed, query embedding/reuse, auth/TLS warmup and cold/warm tags; keep retries, backoff, pacing, failures and total wall time separate. First token, meaningful first audio and completion apply only to later stages. Unmeasured components stay unmeasured. |
| Review | Per-fact coverage and support, unsupported claims, numeric-rule checks, reviewer/adjudication outcome, exclusions and missing observations. |

Missing queries, evidence or timing traces make the affected measurement
**indeterminate**, not a pass, zero or guessed failure. Report the indeterminate
count and denominator explicitly; do not silently drop it from headline scores.
Unavailable server timing does not erase measured client timing. Distinguish a
recorded empty retrieval on an answerable case from a missing retrieval trace.

For later answer scoring, **expected abstention** on a genuinely unsupported or
ambiguous question can be correct. Abstention or deferral on a **known-answer**
case is a failure to answer, not a quality win merely because it avoided
hallucination; use the trace to distinguish retrieval from generation failure.

## Later stages — only after the retrieval review gate

### 5. Fixed-evidence generation, then the real agent pipeline

First give each text model the **same frozen evidence**, prompt, catalogue/date
context and output constraints, with no fresh retrieval/tool calls. Compare
required-fact completeness, evidence support, numeric accuracy, unsupported
claims and abstention using the predeclared blind review. This isolates generation
from retrieval differences; it does not establish production tool behavior.

The agent candidates are **GPT-5.4**, **GPT-5.4-mini**, **GPT-5.6-Luna** and
**GPT-5.6-Terra**, using supported **`none` / `low`** reasoning settings.
**`medium` is deferred**, not part of the approved matrix.

Then test the real agent full pipeline with version-pinned prompts, catalogue,
retrieval configuration and tools. Reverify native semantic-tool operation against
the pinned configuration before claiming a matched semantic comparison; otherwise
label the configurations as different. Capture observable tool calls, available
retrieved evidence and answers, respecting the Bing limitation below. A correct
first tool is only a routing result, not evidence of answer correctness.

**Web grounding is a separate track**, not part of the current minutes-only
retrieval gate: agent mode uses **Bing grounding / Bing Custom Search**, while
model mode uses **Web IQ**. Keep their queries, domain configuration, evidence,
quality and timings separate. Do not silently substitute one tool for the other
or attribute that full-path difference solely to the model.

Microsoft's [Bing grounding documentation](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/bing-tools#how-it-works)
states that **raw grounding content is not exposed** to developers or end users.
Queries, model responses and citations are observable; logging or audit
reconciliation cannot recover the hidden passages. A citation or a separately
fetched page is not proof of the exact evidence the model received.

For the later web phase, use **fixed, versioned public-source snapshots** for
controlled evidence/generation tests, and separate **cited live-grounding checks**
for freshness, factual support and citation correctness. Keep snapshot content,
hashes and fetch timestamps in private evaluation artifacts. Preserve the
service-provided citation/query references as returned. Where live grounding evidence is
hidden, attribution of a failure to retrieval versus generation is
**indeterminate**. Model-mode Web IQ remains a separate observable tool track;
its returned passages must not be presented as recovered Bing content.

### 6. Meaningful first-audio voice latency

Only after evidence and answer quality are understood, evaluate the actual
voice/avatar path. The user's latest model-mode shortlist is **`realtime-2.1`,
`2.1-mini`, and `mai-mm-realtime`**, replacing the earlier `gpt-realtime-2` /
`phi4-mm-realtime` shortlist. Treat these as requested model labels until their
exact service identifiers are verified. Verify model support,
region availability, tool calling and voice/avatar compatibility for each before
testing; listing a candidate does **not** claim that it is already supported.

For that later comparison, use the same approved minutes index **and retrieval
mode/top-k** in both bindings. Pointing at the same index is insufficient if the
model-mode function still uses vector hybrid while the agent uses lexical
semantic search. Verify the actual requests before claiming parity. Bing
grounding remains the agent web path; Web IQ remains the model-mode web path.

Measure end of user speech to the **first audible, answer-bearing audio at the
client**, with the speech-end/VAD and playback boundaries declared. A thinking
cue, filler, first text token, first received audio packet or completed text answer
is not that measurement. Keep connection setup, cold/warm sessions, retrieval,
generation, synthesis/transport, retries and completion visible separately.
Report quality-qualified sample sizes and latency distributions (including p50
and p95), failures and missing playback measurements rather than only averages.

If only a text-input Voice Live probe is available, record
**text submission to first received PCM audio** under that exact label. Keep
connection setup separate, save the transcript for filler/answer checks, and
do not rename that measurement time-to-first-audible-answer. The probe does
not exercise input speech recognition, VAD, browser buffering/playback or
avatar rendering.

**Keep benchmark throttling outside the measured turn.** Apply discretionary
spacing/token-budget admission before submitting a new user turn or retry
attempt, not between a tool result and its answer. Honor genuine service
Retry-After/reset instructions, but record those waits explicitly rather than
silently folding them into a model-speed comparison. Keep blocking evidence
serialization/fsync outside first-token/first-audio collection where possible;
declare buffering, durability and loss behavior.

Record submission, function announcement, argument-ready, tool execution
start/end, tool-output send completion, response-create, first answer
transcript and first observed PCM separately. Missing phases stay missing,
especially when media travels directly to the browser over WebRTC.
Historical timings collected with a different measurement policy must retain
that label. Subtracting an aggregate wait median does not create a new
unpaced measurement.

## Frozen-retrieval comparison authorized on 18 September

The approved agent roster is GPT-5.4, GPT-5.4-mini, Luna and Terra, each at
reasoning `none` and `low`. Keep native Search `semantic` / k=5 and the same
section index fixed. Keep Bing's source configuration/count fixed independently;
do not repeat a k=8 sweep merely because historical tests included one.

For the clean comparison, freeze original-source factual requirements and
public reference excerpts before inference. Report fixed-evidence answering
(`oracle`, no tools and an explicit evidence-only overlay) separately from the
real agent path. Original source instructions remain unchanged apart from that
documented oracle-stage tool-availability override. Required-fact lists,
forbidden assertions, grading rules and gold answers are never sent as model
input. Include missing-information controls and distinguish honest abstention
from failure to answer a source-answerable question.

All new work remains isolated from the production agent/index. Model-mode
tests remain deferred until the agent results are reviewed.

## Deployment snapshot — 18 September 2026

The four text deployments are **`DataZoneStandard`** on the **`swedencentral`**
resource. This snapshot is context for later comparisons, not a new measurement
or model recommendation.

| Text deployment | TPM | RPM |
| --- | ---: | ---: |
| `gpt-5.4` | 300,000 | 300 |
| `gpt-5.4-mini` | 500,000 | 500 |
| `gpt-5.6-luna` | 333,000 | 333 |
| `gpt-5.6-terra` | 333,000 | 333 |

An **EU data zone does not guarantee execution in Sweden or faster latency**.
Quotas are admission limits, not achieved throughput or latency measurements.
The **`text-embedding-3-small`** deployment remains **`GlobalStandard` at
50,000 TPM**: do **not** describe the whole retrieval/generation pipeline as
EU-only. Record the actual deployment versions, SKUs and limits with each
subsequent run instead of projecting this snapshot onto historical results.
