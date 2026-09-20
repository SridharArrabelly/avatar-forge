# Realtime latency attribution

20 September 2026. **Scope: offline attribution and bounded diagnostics, not a
production inference optimization or prompt/retrieval/voice change.**

The user's final scope limited new inference to two AI Search turns and two
Web IQ turns. All four completed, with one attempt and one tool call each.
The broader twenty-turn/prompt/snippet A/B matrix has **not** been run.
Separately, existing full `gpt-realtime-2.1` live traces (51 turns) and provider
usage records (51 live + 48 oracle turns) were audited without new inference.

Answer timings below use the [corrected timing method](evaluation-model-mode.md#corrected-results).
Discretionary smoothing now applies between complete attempts, not tool-followup
responses. Genuine service backoff remains honored and explicitly identified.
Event capture is buffered and flushed after attempts. No new paid inference
was run after that correction.

## Main finding

The corrected full-model answer PCM median/p95 is **2.705 / 3.592 s** across
51 live turns. The benchmark's token-based continuation wait was a measurement
defect, not a delay in the production handler.

In the previous implementation of `Pacer.account_usage()` in
[`bench_realtime_evaluation.py`](../scripts/bench_realtime_evaluation.py),
every response added this delay to a client deadline:

```python
delay = total_tokens * 60 / client_token_budget
```

The next `response.create` waited for that deadline. For a tool turn, this
included a wait between the tool result and requesting the final answer.
The deadline was driven by **pre-tool response usage**, before the retrieved
content existed. Tool execution consumed some or all of the interval.
Measurement policy version 2 keeps this accounting for between-attempt
admission only; it no longer delays ordinary tool continuations.

The production handler in
[`event_handlers.py`](../backend/voice/event_handlers.py) has no corresponding
token-derived continuation sleep. It starts execution when arguments arrive,
waits for the tool and response completion, sends the result, and requests the
next response directly.

Consequences:

- A faster tool can merely leave more benchmark waiting, hiding its benefit.
- A smaller prompt/schema can reduce benchmark waiting as well as real model
  work, confounding an ablation.
- Oracle/no-tool turns have no second response to incur this intra-turn wait.
- Corrected local timings are not production timings or pure model-speed rankings.
- Removing this benchmark artifact corrects measurement; it does **not**
  remove a delay from production, where the delay does not exist.

## Timing definitions and instrumentation

New additive instrumentation records:

- T0: immediately before submitting the user item.
- T1: actual tool execution start, after arguments are available. Function-call
  announcement, argument-ready and preceding response-done times are separate.
- T2: tool execution/result validation ends.
- T3: awaited SDK function-output send completes, not server acknowledgement.
- T4: first transcript delta of the terminal answer.
- T5: first PCM bytes of that answer.

The four diagnostic probes also recorded response-create request/send
boundaries, planned and actual client waits, service-defer deadlines, and
synchronous trace-write spans. Existing pacing and payload behavior were
deliberately retained for those probes. The corrected runner instead records
in-memory `trace_capture_spans`; redaction, file writes and fsync occur after
the measured attempt.

T1-T0 is client-observed model/argument/network/handler latency, not pure
provider compute. T5-T4 is a transcript-to-audio delivery gap, not an isolated
TTS-engine measurement. Connection setup, microphone/ASR, playback and avatar
rendering are excluded.

## Corrected breakdown: all 51 live full-model turns

Times are milliseconds; p95 is nearest-rank. This combines 36 Search and 15
Web IQ turns. Component medians/percentiles do not add to total medians.

| Component | Median | p95 |
|---|---:|---:|
| User submission -> tool execution-start trace | 858 | 1,034 |
| Tool execution | 431 | 1,496 |
| Tool-output send request -> first answer transcript, wait excluded | 498 | 846 |
| Final response-create request -> first transcript | 495 | 844 |
| Transcript -> first PCM | 500 | 934 |
| Corrected answer transcript receipt | 2,139 | 3,045 |
| Corrected answer PCM receipt (TTFA) | 2,705 | 3,592 |

The output-send marker in this cohort is logged **before** sending; it is not
the exact T3 send-completion marker added for the four instrumented probes.
Send-completion boundaries are not retroactively inferred.

### Separate the two tools

| Component, median ms | AI Search (36 turns) | Web IQ (15 turns) |
|---|---:|---:|
| Submission -> tool start | 919 | 759 |
| Tool execution | 422 | 1,389 |
| Output-send request -> first transcript, wait excluded | 509 | 445 |
| Response-create -> first transcript | 506 | 443 |
| Transcript -> PCM | 481 | 512 |
| Corrected answer transcript receipt | 1,910 | 2,532 |
| Corrected answer PCM receipt (TTFA) | 2,454 | 3,084 |

For Web IQ, retrieval itself is the largest median stage.
The largest remaining observed Search stage is submission-to-tool execution.
None of these local measurements alone establishes production server timings.

### Corrected answer distributions by invoked tool

Times are seconds, median / nearest-rank p95. Mini's three web-case misroutes
are counted under the Search tool it actually invoked, so tool cohorts are not
matched question sets.

| Model | Tool | Turns | Transcript | PCM |
|---|---|---:|---:|---:|
| gpt-realtime-2.1 | AI Search | 36 | 1.910 / 2.923 | 2.454 / 3.494 |
| gpt-realtime-2.1 | Web IQ | 15 | 2.532 / 3.309 | 3.084 / 3.749 |
| gpt-realtime-2.1-mini | AI Search | 39 | 1.923 / 3.693 | 2.371 / 4.167 |
| gpt-realtime-2.1-mini | Web IQ | 12 | 2.676 / 3.866 | 2.950 / 4.135 |

These are terminal-answer arrivals, not first-any audio. Mini emitted pre-tool
spoken preambles on 10/51 live turns; its separate corrected first-any
transcript median/p95 is 1.794 / 3.401 s and first-any PCM is 2.223 / 3.770 s.
Full 2.1 had no such preambles, so its first-any and terminal timings coincide.

## Four instrumented diagnostic turns

Current production prompt/tool functions were used locally with
`gpt-realtime-2.1`, API `2026-04-10`, Leah voice, semantic/k5/full-section Search,
and the existing Web IQ profile. No avatar or microphone was started.
These are current-profile diagnostics, not a controlled old-vs-new prompt A/B.

Detailed timings remain in private diagnostics rather than a second latency
table. The larger reconciled cohorts supply the distributions above. Relevant
observations:

- No server-requested defer was recorded in these four turns.
- Search arguments were 121/229 characters versus 30/33 for web calls.
  This suggests a query-generation experiment, not proof that
  shortening queries preserves retrieval quality.
- The runner's wait-until-response-done ordering differs from production's
  overlap; the probes did not identify that ordering as a large gap.
- Trace serialization/write/fsync occupied time before PCM.
  That is observer work, not necessarily an equal TTFA penalty: some can overlap
  remote computation. It must not be blindly subtracted.
- W2 had a smaller result than either Search example, yet the slowest
  response-create -> transcript span. Four calls do not establish
  a monotonic payload-size bottleneck.
- Routing was correct in 4/4. The two minutes answers still omitted some
  required detail (explicit dividend increase / full audit scope); this is
  not evidence of perfect completeness or permission to discard evidence.

## Token and context audit

**296,477 is aggregate reported usage across 51 turns and 102 responses.**
Every live turn had a tool-call response and an answer response.

| Provider-reported metric, summed across both responses per turn | Median | p95 | Sum over 51 turns |
|---|---:|---:|---:|
| Input tokens | 5,139 | 6,137 | 268,293 |
| Cached input, already included above | 3,200 | 3,200 | 152,448 |
| Uncached input | 2,147 | 3,428 | 115,845 |
| Output tokens | 550 | 934 | 28,184 |
| Total reported usage | 5,898 | 7,071 | 296,477 |

Input caching covers **56.82%** of the aggregate input. These counters are not
an invoice: model rates, cached-input pricing and text/audio pricing differ.
The 15,000-token client reservation is also not observed usage.

The context for one response is much smaller:

| Response input | Median | p95 | Maximum |
|---|---:|---:|---:|
| Before tool execution | 1,669 | 1,689 | 1,689 |
| Final answer, after tool result | 3,474 | 4,449 | 4,983 |

Across both responses, output comprised 5,676 text tokens and 22,508 audio
tokens. The tool-call-only response contributed 2,248 output text tokens
(median 47, p95 63 per turn), with no audio. Reported reasoning tokens were zero.
Repeated input is counted again for the second response; that is not a
296K-token conversation.

### Requested component attribution

| Component | What can actually be established |
|---|---|
| Input/output/cache per turn | Exact provider fields above, with both rounds retained |
| Tool-call tokens | Exact usage for the function-call-only response: median 47 output tokens |
| Tool-result tokens | Not separately exposed; median input growth is 1,802 tokens, including the call, result and protocol framing |
| System prompt | Historical 4,432 characters; current probes 4,467 |
| Catalogue/system reference | Historical 1,045 characters; current probes 1,043 |
| Prior conversation/history | Fresh sessions for every benchmark turn; no earlier user turns. Within-turn tool messages remain context |
| Tool schemas/descriptions | Historical combined serialized schemas 1,540 characters; current 1,290 |
| Retrieved Search content | Historical median 9,297 extract characters / 10,630 serialized-result characters per call |
| Web IQ result content | Historical median 3,169 extract characters / 4,747 serialized-result characters per call |

Exact prompt/catalogue/schema/result token partitions are **not exposed by the
service**. No local tokenizer was available for estimates; character counts
have not been relabelled as tokens. Source JSON serialization and provider
message framing are different things.

The historical run cannot diagnose growth in a long-running production
conversation: it intentionally starts fresh sessions. Production appends items
to an existing conversation. Its long-session memory behavior needs a separate
multi-turn trace before changing retention; follow-up references must remain
correct.

### Exact provider counts for the four new turns

| Case | Input across both rounds | Cached subset | Uncached input | Output | Final-response input |
|---|---:|---:|---:|---:|---:|
| Q1 | 5,380 | 2,944 | 2,436 | 562 | 3,821 |
| Q7 | 5,240 | 1,536 | 3,704 | 374 | 3,657 |
| W1 | 4,281 | 1,536 | 2,745 | 133 | 2,726 |
| W2 | 4,423 | 1,536 | 2,887 | 149 | 2,866 |

## The 12,000-character cap

It is a **per-passage ceiling**, not the amount sent every time.
None of the 173 historical Search passages reached it; the largest saved
passage was 5,114 characters. This does not establish whether any earlier
upstream stage shortened a source.

The new Search results contained five/four passages, totalling 8,031/8,892
extract characters (9,356/9,997 including serialized metadata/notes).
Read-only slicing calculations, **not model A/B results**, show:

| Proposed cap per passage | Q7 extract characters removed | Q1 extract characters removed |
|---|---:|---:|
| 5,000 | 0 | 2 |
| 3,000 | 926 | 2,002 |
| 1,500 | 2,669 | 3,812 |

Thus 5,000 is effectively a no-op for these examples, while 1,500 would discard
substantial source text. No inference used these clipped variants, and there is
no evidence yet that their quality/latency tradeoff is acceptable.

## Measurement correction and cost-controlled next steps

1. **Measurement correction implemented; no production optimization.** Token
   smoothing is enforced at admission to the next attempt. A genuine provider
   reset/Retry-After can still delay a continuation and is marked explicitly;
   it must not be hidden in an unqualified latency comparison. Events are
   snapshotted in a bounded in-memory buffer and redacted/fsynced at attempt
   boundaries. Overflow invalidates the attempt, rather than silently dropping
   evidence. An unclean process exit may lose the in-flight event buffer;
   completed attempts remain durable. This does not change prompts, payloads,
   tool execution or production inference behavior.
2. **Use normal production tests before another paid matrix.** Metadata-only
   tracing can capture server-side phase boundaries and token usage without
   logging questions, transcripts, prompts or retrieved passages. Text
   submission and server-observed speech-stop are different T0 origins.
   A WebRTC avatar session may not expose PCM to the backend: missing T5 must
   remain unavailable, not be replaced with transcript or a predicted speaking
   cue. End-user audible/rendering latency needs separate client-side evidence.
3. **Only if the evidence justifies it, isolate result-processing cost.** Replay the same question/tool with
   full evidence versus a small source-grounded evidence payload, holding tool
   execution time constant. Keep required facts, dates and provenance; do not
   replace evidence with an unsupported synthetic answer. Only then test
   1,500/3,000/5,000 limits against the frozen fact checklist.
4. **Then test prompt/schema/query minimization.** Preserve routing and grounding
   rules. Measure whether shorter generated date-and-topic queries reduce the
   observed argument-generation span without lowering retrieval coverage.
   Do not remove the newly restored onboarding behavior or useful conversation
   history merely to lower token counters.
5. **Confirm on the deployed path before shipping an optimization.** Match
   server-side markers in real application turns; the local harness and the
   production execution path are not identical. No production change is
   justified solely by subtracting waits from old records.

### Why not infer A/B results from the existing runs?

Existing runs are useful for locating delays and measuring context sizes;
they have been reused here rather than repeated. They did not hold question,
evidence, generated query, cache state, service load and timing policy constant
while varying just one prompt or payload size. They therefore cannot establish
that a proposed reduction *causes* faster answering or preserves quality.

A new controlled comparison is justified only for a specific bottleneck seen
in the corrected/deployed trace. Do not run a broad size/prompt/model matrix by
default. First check concise-query retrieval coverage with low-cost Search
checks; only a viable candidate should incur a small paired realtime test.
Shorter payloads are not automatically preferable, and the 5,000-character cap
is nearly a no-op in the observed examples.

**Current recommendation:** collect a small production baseline with the
correct boundaries, then choose one experiment. The measurement defect is
fixed in the harness; a new production optimization has not been selected or
applied.

## Agent-mode measurement audit

The independent agent harnesses were checked rather than presumed to share the
realtime defect. No new agent inference was run.

| Dataset/path | Same within-turn token wait? | Disposition |
|---|---|---|
| 792 agent text turns (`bench_agent_evaluation.py`) | No | Keep the published TTFT/completion values |
| 72 agent PCM samples (`bench_agent_audio.py`) | No | Keep received-audio values, with transport/answer-boundary limits |
| Legacy agent reasoning/retrieval matrix | No | Keep values; do not reinterpret unexplained outliers |
| Early `bench_routing_agent.py` runs | No explicit equivalent | SDK-internal retries were not separately recorded |

For text, admission pacing precedes `stream_turn`, T0 is inside that function,
and token accounting happens after it returns. Explicit retry/cooldown waits
are outside the successful-attempt timer. Its outer wall-clock metric includes
those waits but is not the TTFT column in the agent-results table.

For audio, T0 follows connection/session setup and catalogue submission; the
three-second spacing and result-file writes occur after the completed turn.
First received PCM is not necessarily first useful answer audio and is not
microphone/VAD-to-playback or avatar rendering.

Both paths still include client/SDK processing and network/server work that
was not separately attributed. Text event conversion happens before its
timestamp, so observer overhead is not proven zero. These caveats do not
justify inventing revised numbers or subtracting the realtime wait from agent
results. Historical claims that a large maximum "usually means backoff" or
that text TTFT approximates audible speech have been corrected.

Source anchors: `bench_agent_evaluation.py` `stream_turn`/`run_turn`,
`bench_agent_audio.py` `audio_turn`, `bench_routing_matrix.py` `Pacer` and
`stream_turn`, and `summarize_agent_evaluation.py` metric selection.

## Optional production trace

The backend supports `ENABLE_LATENCY_TRACE=true` with `LOG_LEVEL=INFO`.
It defaults to **off**; enablement is an explicit operational choice.
This is a runtime setting, not an azd/Bicep
parameter; setting an azd variable alone does not forward it to a container.
It is independent of `ENABLE_AUDIT` and does not enable conversation capture.
When off, existing `[LATENCY]` diagnostics are unchanged.

When enabled, `backend.voice.latency_trace` emits a bounded JSON summary with
`event: "latency_trace"` at turn completion or cancellation, rather than
logging every delta. Important fields are:

| Field | Interpretation |
|---|---|
| `turn_id`, `binding`, `origin` | Generated correlation ID, model/agent binding and T0 origin. Text uses server receipt of submission; audio uses server receipt of `speech_stopped`, not physical microphone end. |
| `milestones_ms` | T0-T5 relative to that origin. Model-mode T1 is first local tool execution, T2/T3 the last tool completion/output-send completion, and T4/T5 the terminal non-function response. |
| `tools`, `response_create_sends` | Per-tool announcement, argument readiness, execution and result-send boundaries, plus actual awaited response-create sends. Multi-tool turns require these records, not subtraction of unrelated global milestones. |
| `responses`, `first_any_ms` | Per-response arrival times and provider usage. Tool preambles remain separate from the terminal answer. |
| `usage_totals` | Reported total/input/output/cache and available text/audio/reasoning details across responses. Cached input is already included in input; breakdowns overlap and must not be added as extra usage. Unknown values remain null. |
| `session_char_counts` | Model-mode instruction/catalogue/compact-schema character counts, not exact tokens or wire bytes. Tool argument/output sizes are also character counts only. |
| `trace_valid`, `correlation_valid`, `metadata_truncated` | Measurement integrity. Exclude invalid, truncated, failed or cancelled records from completed-turn latency comparisons; retain them in failure accounting. |

No questions, transcripts, prompts, arguments, tool results, source URLs or
credentials are retained or serialized by this collector. It records fixed
labels, validated provider response IDs, generated turn IDs, timing and counts.
This guarantee applies to the new trace, not to an independently enabled
content audit or all pre-existing application logs.

Response/tool/send lists are capped at 32 entries; incomplete usage totals are
null. Correlation state is capped at 128 entries and disabled explicitly on
overflow rather than silently reassigning late responses. Capture errors
invalidate measurements and produce fixed-label `[LATENCY_TRACE_ERROR]`
diagnostics without exception text. Inference continues if telemetry fails.

WebRTC may deliver audio directly to the browser. In that case **T5 is null and
`terminal_pcm` is `unobserved`**; neither transcript arrival nor a UI speaking
cue substitutes for audio. Even observed backend PCM is not browser playback
or rendered avatar onset. Managed agent tools and their terminal-answer
boundary are opaque, so agent-mode T1-T5 remain unavailable; first-any and
per-response observations are still recorded.

For a user-run baseline, use a short agreed window, filter only these trace
records, and distinguish fresh-session from follow-up turns and text from
microphone input. Preserve the existing live model/avatar/voice settings.
Verify that the deployed image includes the collector and the flag is enabled;
ordinary application logs are not a substitute for these trace records.

## Evidence and code

- Additive instrumentation: `scripts/bench_realtime_evaluation.py`.
- Regression checks: `tests/test_realtime_latency.py`,
  `tests/test_realtime_evidence.py` and `tests/test_realtime_evaluation.py`.
- Opt-in production collector: `backend/voice/latency_trace.py`;
  offline integration checks: `tests/test_production_latency_trace.py`.
- Private aggregates: `historical-latency-events.json`,
  `historical-token-audit.json`, `four-turn-components.json`,
  `observed-payload-sizes.json`, `current-token-summary.json`.
- Reproducible offline correction: private `correct_realtime_timings.py` and
  sanitized numeric `corrected-realtime-receipt.json`; no source content is
  included in the receipt.
- Four new raw turns remain in the private `instrumented-four-turns` directory.
- Archived inference/grades were not altered. No additional paid inference,
  production context reduction or inference optimization was performed.
