# Terra v4 full agent evaluation

**Disposition: retain the existing headline results; do not replace them.**
The full v4 run found a new factual error and did not demonstrate a live-text
or received-audio latency gain. The authorized cohort is complete. No retuning,
extra model runs, quality retries, automatic rollback or production changes
followed this assessment.

This is a separate study from the [v4 promotion pilot](terra-prompt-evaluation.md)
and the [original agent comparison](evaluation-results.md). Their results are
not overwritten or pooled here. Protocol: [assistant-evaluation.md](assistant-evaluation.md).

## Scope and identity

Measured on **20 September 2026**. Native text requests ran from
**19:39:04 to 19:56:59 UTC**; the nine fresh Voice Live sessions ran from
**19:57:19 to 19:59:04 UTC**, including connection setup.

| Frozen setting | Value |
|---|---|
| Production source | `AvatarAgent`, version **10** |
| Model / reasoning | `gpt-5.6-terra` / `none` only |
| Merged source | `b51a273e9541a0980b4424df3cd2573b5683d8ef`, PR #138 |
| Rendered instructions | 5,931 characters, Nuru persona |
| Instructions SHA-256 | `6635e036771ecf7aba9c86a512537fefc619a256e57b434d9679053ea444c130` |
| Frozen cases and rubrics SHA-256 | `9ca135e902f292c51fa207ae62d7a06678decb69f4faf87f1f4798b8f940764b` |
| Internal retrieval | Native Azure AI Search, section index, `semantic`, top 5 |
| Public retrieval | Existing native Bing Custom Search; count 8, `en-ZA`, `en` |
| Text schedule | 16 oracle cases × 3; 17 live-tool cases × 3 |
| Audio schedule | Existing Q1, Q3 and W2 cases × 3 |
| Audio configuration | Voice Live **agent path**, API `2026-04-10`, PCM16, `en-US-AvaMultilingualNeural` |

The standardized audio voice matches the prior harness, **not** the live app's
`en-ZA-LeahNeural`. There were no realtime/model-mode calls, microphone/VAD
sessions, avatar video sessions, extra model/effort arms or paid LLM-judge
requests. Public-source discovery and post-turn conversation reads were
evidence reconciliation, not additional evaluation inference.

The exact frozen questions, evidence and rubrics were reused. The catalogue
and UTC TODAY value were snapshotted separately. W3 remains live-only; its
current public quote supplement was captured on Sunday 20 September rather
than judged solely against an old number.

## Scheduled turns, attempts and recovery

| Cohort | Scheduled | Completed | Actual inference attempts | Failed attempts |
|---|---:|---:|---:|---:|
| Oracle text | 48 | 48 | 48 | 0 |
| Native live-agent text | 51 | 51 | **52** | **1** |
| Received-audio samples | 9 | 9 | 9 | 0 |
| **Total** | **108** | **108** | **109** | **1** |

The second W5 live repetition encountered provider `tool_server_error`, with
error type `server_error`. The failed attempt emitted no text and ended after
12.808 seconds. Its observed Bing item lifetime was 11.214 seconds. The existing
transient-error classifier permitted one retry after a recorded **65-second
backoff**; the next attempt completed. The scheduled turn's wall time, including
admission and recovery, was **103.927 seconds**. This was not a quality retry.

SDK inference retries were disabled. The existing maximum-six-attempt policy
was retained, but only this single retry was used. Every attempt and partial
receipt is retained. Failed attempts are excluded from successful latency
distributions, **not erased**: attempt-inclusive live quality is reported below.
No completed answer was rerun to obtain a better result.

## Measurement definitions and timing audit

- **Text TTFT:** request-stream start to first nonempty `output_text` delta.
  It is not inherently first useful answer. In this run, no completed live
  text answer emitted text before its observed tool completion.
- **Text completion:** the `response.completed` event, with valid final output,
  matching streamed text, model/effort and provider usage.
- **Tool lifetime:** client-observed `output_item.added` to
  `output_item.done`, not independently measured server execution.
- **First received audio:** text submission to the first actual decoded PCM
  bytes. SDK byte deltas are already decoded; only string deltas are
  base64-decoded. Transcript onset is a separate measurement.
- **Audio completion:** a completed `response.done` with nonempty received PCM.
  Connection/session setup is measured separately from the submitted turn.

The reviewed `bench_agent_evaluation.py`, `bench_routing_matrix.py` and
`bench_agent_audio.py` measurement engines were reused unchanged. A private
wrapper records attempt UTC origins and Voice Live event receipts. Text uses
fresh requests without conversation continuation; audio uses fresh connections.
Agent/client preparation is outside text turn timing; ordinary SDK,
authentication, observer and network overhead within a request remains included.

The new audio observer buffers event metadata in memory: it performs no
per-event file writes, fsync, hashing or injected waits. It takes its own
receipt timestamp before event conversion/bookkeeping, but the reused audio
helper takes the published timing timestamp after that synchronous observer
work. This added overhead versus the prior audio run is nonzero and
unquantified; observer correctness tests do not establish zero timing impact.
No guessed overhead has been subtracted, and the audio difference must not
be attributed entirely to the prompt.

**The earlier realtime runner wait defect was not an agent-mode defect.**
Text admission uses the existing 8-second interval, 200,000-token/61-second
budget and 30,000-token reserve before whole attempts. Audio spacing is three
seconds between completed samples. There is no discretionary within-turn
sleep. Service backoff is recorded separately. No waits were subtracted from
these measurements and no warm-up turns were dropped.

All p95 values use **nearest rank**. Three repeats per case are correlated,
small samples, not a statistical guarantee. Comparisons below are
**noncontemporaneous observations**, not isolated causal prompt effects:
dates, cache state, tool results, query wording and allowed refinement behavior
differ. No nonpaired text/audio medians are subtracted to invent TTS latency.

### Successful latency comparison

Seconds; each cell is **median / p95**.

| Measurement | Original Terra/none | Production-v10 instructions |
|---|---:|---:|
| Oracle TTFT, n=48 | 1.599 / 2.371 | 1.563 / 2.851 |
| Oracle completion, n=48 | 2.080 / 2.970 | 2.264 / 5.681 |
| Live-agent TTFT, n=51 | **2.558 / 4.793** | **2.682 / 5.314** |
| Live-agent completion, n=51 | 3.232 / 6.127 | 3.352 / 6.125 |
| First received PCM, **n=9** | **2.473 / 3.840** | **3.891 / 4.640** |
| Audio response completion, n=9 | 4.648 / 6.024 | 5.448 / 6.248 |
| Audio connection/session setup, n=9 | 2.196 / 2.458 | 3.123 / 8.391 |

The live-text median was **0.124 seconds slower**, and the received-audio
median **1.418 seconds slower**, in this sample. The oracle median was only
0.036 seconds lower, while its tail/completion measurements were higher.
These data do **not** substantiate the pilot's initial-candidate speedup as a
full final-prompt result.

Current audio transcript onset was **2.425 / 3.812 seconds**; it must not be
reported as PCM onset. A total of **8,244,600 decoded PCM bytes** was received.
These are not audible playback, speech-end, first answer-bearing audio or
rendered-avatar measurements.

### Native tool event lifetimes

Successful calls only; seconds, median / p95.

| Tool | n | Original event lifetime | Current event lifetime |
|---|---:|---:|---:|
| Azure AI Search | 36 | 0.556 / 1.027 | 0.689 / 2.208 |
| Bing Custom Search | 15 | 1.689 / 4.541 | 1.769 / 4.933 |

The failed Bing attempt is reported separately above. Voice Live did not expose
native tool lifecycle intervals in its streamed events. Read-only retrieval of
all nine completed conversations confirmed routing and evidence after the
fact; it cannot reconstruct missing tool timings or prove audio/tool ordering.

### Early, middle, late and repetition checks

Each stage ran repetitions consecutively, so its equal thirds are also
repetitions 1, 2 and 3. No first turn was excluded. Seconds, median / p95.

| Stage | Third / repetition | n | First text or PCM | Completion |
|---|---|---:|---:|---:|
| Oracle | Early / 1 | 16 | 1.620 / 4.692 | 2.325 / 7.805 |
| Oracle | Middle / 2 | 16 | 1.560 / 2.851 | 2.253 / 5.763 |
| Oracle | Late / 3 | 16 | 1.507 / 3.505 | 2.272 / 3.961 |
| Live | Early / 1 | 17 | 2.658 / 6.989 | 3.341 / 8.163 |
| Live | Middle / 2 | 17 | 2.575 / 5.355 | 3.225 / 8.629 |
| Live | Late / 3 | 17 | 2.940 / 5.314 | 3.463 / 6.125 |
| Audio | Early / 1 | 3 | 3.891 / 4.061 | 5.448 / 5.695 |
| Audio | Middle / 2 | 3 | 4.288 / 4.640 | 5.868 / 6.248 |
| Audio | Late / 3 | 3 | 2.764 / 4.305 | 4.625 / 5.897 |

Across all 99 text turns, chronological thirds of 33 had TTFT
**1.607 / 2.851**, **2.048 / 4.780**, and **2.893 / 5.314 seconds**.
Completion was respectively **2.258 / 5.763**, **2.843 / 6.094**, and
**3.357 / 6.125 seconds**. These mixed thirds are confounded by the schedule:
33 oracle; then 15 oracle plus 18 live; then 33 live. They are not evidence of
warming or degradation over time.

## Quality assessment and replacement gate

Assessment was AI-assisted, source-based and **not a blinded human-panel
certification**. The original study's grades were retained, not retroactively
changed. All required fact elements, exact numeric tolerances, abstention,
routing and style checks remain in force. Correct but incomplete answers do
not receive strict passes; an additional material false claim fails an answer
even when its core requested fact is present.

| Cohort | No identified false/unverified claims, old → new | Strict passes, old → new | Current other completed outcomes |
|---|---:|---:|---|
| Oracle text | 48/48 → 48/48 | 31/48 → **34/48** | 14 incomplete |
| Live-agent text | 51/51 → **50/51** | 25/51 → **26/51** | 24 incomplete, **1 wrong** |
| Audio transcripts | 9/9 → 9/9 | 0/9 → **3/9** | 6 incomplete |

Including the failed attempt, rather than considering only completed answers,
live-agent results are **50/52 factual-correctness passes**, **26/52 strict
passes**, 24 incomplete answers, one wrong answer and one service error.
The corresponding scheduled-turn denominators are 51. The failed attempt is
not counted as a successful answer or silently removed from reliability.

### Decisive error and retained limitations

- **W4, live repetition 3:** claimed a current 2030 financial-services target
  of **120 million customers**. The unchanged frozen reference and newly
  captured [official FY2026 Vodacom results](https://vodacom.com/news-article.php?articleID=16836)
  explicitly raise that target to **130 million**, from 120 million previously.
  This is a proven factual error, not merely unverified hidden Bing grounding.
  Its core fintech description does not rescue the overall answer.
- **W2 revenue:** oracle **3/3** supplied the exact total, R226.707 billion.
  Live **0/3** and audio **0/3** supplied total revenue; they returned correctly
  labelled R218.5 billion service revenue. One live repetition also omitted the
  instructed explicit acknowledgement that total revenue could not be verified.
  No guessed total was accepted as correct; the pilot did not solve completeness.
- **W3 quote:** all three current prices and dates matched the captured
  R195.86 close on Friday 18 September, with honest Sunday closure framing.
  All omitted the source's precise **15-minute delay** qualification and
  remained incomplete. The source was recaptured on Sunday; price equality
  alone was not used as a timeless gold answer.
- Internal detail omissions remain, including the pre-existing scope and
  qualification omissions. Q6 also lost its one previous strict pass. Detailed
  per-fact reasons and source evidence are private.

Other new public additions were checked, not automatically labelled false:
the R16.8 billion FY2026 financial-services figure and Tanzania's Visa
tap-to-pay/PayPal integrations have captured primary-source support.
No additional unverified material claims remain identified in this review.
Hidden Bing passages cannot establish whether the wrong target came from
stale retrieval or generation; this study does not assign that root cause.

### Comparable per-case strict passes

Each entry is old → new, out of three repetitions. IDs preserve private
question confidentiality; full paired reasons and answer evidence are retained
outside Git. Matching case/repetition pairs are not contemporaneous trials.

| Case | Oracle | Live |
|---|---:|---:|
| Q1 | 0 → 0 | 0 → 0 |
| Q2 | 0 → 0 | 0 → 0 |
| Q3 | 3 → 3 | 3 → 3 |
| Q4 | 2 → 1 | 0 → 0 |
| Q5 | 0 → 0 | 0 → 0 |
| Q6 | 2 → 3 | 1 → 0 |
| Q7 | 0 → 0 | 0 → 0 |
| Q8 | 3 → 3 | 3 → 3 |
| Q9 | 3 → 3 | 3 → 3 |
| Q10 | 0 → 3 | 0 → 3 |
| Q11 | 3 → 3 | 3 → 3 |
| Q12 | 3 → 3 | 3 → 3 |
| W1 | 3 → 3 | 3 → 3 |
| W2 | 3 → 3 | 0 → 0 |
| W3 | Not eligible | 0 → 0 |
| W4 | 3 → 3 | 3 → 2 |
| W5 | 3 → 3 | 3 → 3 |

Oracle pairs: four strict-pass gains, one loss, 43 unchanged. Live pairs:
three gains, two losses, 46 unchanged. Audio gains were all three Q3 samples;
Q1 and W2 remained incomplete in all repetitions.

| Audio case | Strict passes, old → new | Median PCM onset, old → new |
|---|---:|---:|
| Q1 | 0/3 → 0/3 | 2.378 → 2.750 s |
| Q3 | 0/3 → 3/3 | 2.377 → 3.891 s |
| W2 | 0/3 → 0/3 | 3.570 → 4.288 s |

### Routing, evidence and style

- **51/51** completed live text turns used exactly the expected single tool:
  36 internal, 15 public. Oracle **48/48** used no tools.
- Post-turn conversation items confirm expected single-tool routing on
  **9/9 audio samples**: six internal and three public.
- Complete required internal evidence was observed on **30/30 answerable
  live text turns**; the six negative controls have no positive recall target
  and all abstained correctly. All six internal audio samples had the required
  evidence in their recovered conversation records.
- Raw Bing passages remain hidden. Null/empty developer-visible content is
  **not** proof of empty search results. Independent public verification is
  distinct from observed passage grounding.
- All **108 completed outputs/transcripts** stayed within the frozen
  70-whitespace-word check and had no citation/URL/domain or Markdown flags.
  Completed live text had **0/51** pre-tool text cases. Audio lifecycle timing
  is unavailable, so no equivalent audio ordering count is asserted.

The overall replacement gate therefore **fails**, despite some completeness
gains. Under this run's acceptance criteria, the new factual error and absence
of demonstrated latency gains prevent replacement of the existing headlines.

## Provider usage and spending limits

These are actual provider-reported usage receipts, not token estimates.
**Cached input is a subset of input**, never an extra amount added to totals.

| Cohort | Input | Cached-input subset | Output | Reported total |
|---|---:|---:|---:|---:|
| Oracle text | 89,388 | 55,569 | 2,711 | 92,099 |
| Live-agent text, completed | 300,996 | 119,256 | 4,672 | 305,668 |
| All completed text | 390,384 | 174,825 | 7,383 | 397,767 |
| Audio | 53,325 | 17,016 | 4,233 | 57,558 |
| **All completed samples** | **443,709** | **191,841** | **11,616** | **455,325** |

Text receipts separately report 60,405 cache-write tokens; these are not added
again to input or total. Reasoning tokens were zero throughout. Audio output
comprised **802 text tokens and 3,431 audio tokens**; input audio tokens were
zero.

Original reported totals were 282,970 oracle, 513,569 live and 94,797 audio.
Current completed text used 397,767 versus 796,539 tokens, approximately
**50.1% fewer**; live text alone used approximately **40.5% fewer**.
That token reduction did not produce an observed live latency advantage here.

**Actual settled monetary spend is unavailable from these APIs.** The failed
Bing/provider attempt returned **no usage receipt**: its token consumption
and charge are **unknown, not zero**. Thus 455,325 is the reported consumption
for completed samples, not a complete invoice total. Native-tool, speech and
other service charges are not fabricated from these token counts. All 109
attempts remain accounted for even though only 108 returned usage.

## Invariance, cleanup and private evidence

The v10 definition exactly matched the parent's verified publication snapshot.
A unique owned source copy carried the complete production definition.
The live and audio copies preserved that definition; only the oracle copy
disabled tools and used the existing fixed-evidence overlay.

The historical evaluation source was never overwritten. Full production
version/definition/description/metadata and historical-source readbacks were
unchanged at **19:59:11 UTC**. All **four** newly owned agents—source, oracle,
live and audio—were deleted with ownership/version checks and absence receipts.
Original baseline artifacts and frozen rubrics remain hash-identical.
No application configuration, infrastructure or canonical environment files
were edited by this evaluation.

Private artifact set: **`terra-v4-full-20260920`**, outside every Git checkout.
It retains source/prompt/catalogue snapshots, exact schedules, all attempt
records and provider errors, per-tool events, PCM byte/event receipts, post-turn
audio conversations, the complete 108-row per-turn table, source-based
per-fact grades, paired changes, public reference supplements, usage
reconciliation and cleanup receipts. Private questions, answers and source
text are not included in this repository report.

Validation included 47 existing text-runner tests, existing audio/pacing tests,
and three private source/schedule/PCM-observer tests. The old numerical tables
remain intact. Any later publication integration or production decision belongs
to the owner; this result authorizes neither another tuning loop nor rollback.
