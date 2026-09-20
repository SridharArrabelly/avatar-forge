# `tests/` — the offline suites

**`test_` means exactly one thing in this repo: it runs offline.** No network, no Azure
credentials, no deployed resources, no cost. If you can clone the repo you can run all
of these, and none of them can touch a subscription.

Anything that *does* reach Azure lives in [`../scripts/`](../scripts/README.md) under a
prefix that says what it costs you (`setup_`, `grant_`, `smoke_`, `bench_`).

## Running them

Each file is standalone — plain `assert`s and a `main()` or `unittest`, no pytest
or test-runner config. Run one:

```powershell
uv run python tests\test_docs.py
```

Or all of them:

```powershell
Get-ChildItem tests\test_*.py | ForEach-Object { uv run python $_.FullName }
```

They anchor every path on `__file__`, so the working directory does not matter.

**One exception to "just run it": [`test_build_package.py`](test_build_package.py) needs a
*clean* shell.** It asserts what the builder does when variables are **unset**, so a
hydrated session — one you have `azd env get-values`'d into — leaks the real
`SERVICE_APP_URI` / `AVATAR_DISPLAY_NAME` over the fixtures and 4 checks fail for the
wrong reason. Open a fresh terminal rather than debugging it.

## What each one pins

| suite | the mistake it catches |
| --- | --- |
| [`test_docs.py`](test_docs.py) | Broken relative links, phantom mermaid nodes, and Azure regions named in docs that `preflight.py` does not actually support. |
| [`test_preflight.py`](test_preflight.py) | The helpers that settle subscription / location / resource group — including the no-TTY cases, where a stray prompt would hang a deploy forever with no visible question. |
| [`test_voice_binding.py`](test_voice_binding.py) | The agent/model switch, and every kwarg handed to the SDK's `connect()` — checked against the **installed** signature, so an SDK bump that silently drops an argument is caught. |
| [`test_routing_matrix.py`](test_routing_matrix.py) | Benchmark pacing, explicit stream failure handling, retry eligibility, and score denominators; mocked responses only, no Azure calls. |
| [`test_retrieval_evaluation.py`](test_retrieval_evaluation.py) | Original XML evidence offsets, complete span coverage, overlap deduplication, wrong-meeting rejection, and truncated native tool passages. Synthetic sources only. |
| [`test_evaluation_index.py`](test_evaluation_index.py) | Evaluation-only index guards and bounded readback after indexing; mocked Search responses and no Azure calls. |
| [`test_section_ingestion.py`](test_section_ingestion.py) | Normal section ingestion, excluded policy files, immutable index versions, embedding order and per-document upload failures. |
| [`test_agent_retrieval.py`](test_agent_retrieval.py) | Validated retrieval settings, isolated agent clones, exact readback and source-agent preservation. |
| [`test_evaluation_scoring.py`](test_evaluation_scoring.py) | Required original quotes must be delivered from the right source; hidden Bing evidence and negative cases are not invented recall scores. |
| [`test_agent_audio.py`](test_agent_audio.py) | Received-audio timing stays distinct from microphone/playback timing; failed or empty audio streams cannot pass. |
| [`test_agent_evaluation.py`](test_agent_evaluation.py) | Fixed-evidence/live requests, frozen settings, pacing, telemetry, narrow retries, owned cleanup and continuation without repeating successful inference. |
| [`test_evaluation_review.py`](test_evaluation_review.py) | Blinding hides model/effort/timing while preserving evidence; denominators and tail latency stay explicit. |
| [`test_avatar_identity.py`](test_avatar_identity.py) | That every surface resolves the assistant's name the same way, and that [`rename_avatar.py`](../scripts/rename_avatar.py) still writes enough variables to actually change it. |
| [`test_suggested_prompts.py`](test_suggested_prompts.py) | Original onboarding questions, shared spoken replies in both prompts/fallback, environment overrides, opt-out, and matching examples. |
| [`test_realtime_scope.py`](test_realtime_scope.py) | Minutes/web capabilities, honest unavailable-source boundaries, and legacy document formatting without advertising that legacy corpus. |
| [`test_agent_instructions.py`](test_agent_instructions.py) | Instruction-only updates preserve the existing model/tools/settings, are idempotent, and reject concurrent changes or unexpected readback. |
| [`test_realtime_latency.py`](test_realtime_latency.py) | T0-T5, argument readiness and output-send boundaries stay distinct; token smoothing no longer delays a tool-followup. |
| [`test_realtime_evidence.py`](test_realtime_evidence.py) | Bounded event capture keeps disk/redaction work outside measured turns, preserves snapshots and redaction, and fails explicitly on overflow or write errors. |
| [`test_production_latency_trace.py`](test_production_latency_trace.py) | Default-off tracing preserves legacy diagnostics and inference payloads/overlap; opt-in content-free timing distinguishes preambles, missing PCM, usage and explicit instrumentation failures. |
| [`test_agent_model_binding.py`](test_agent_model_binding.py) | That the agent binds to a model deployment that actually exists — evaluated against the generated ARM, not restated in Python. |
| [`test_webiq_binding.py`](test_webiq_binding.py) | Mode-specific container settings, explicit realtime/Web IQ defaults, azd parameter forwarding, and a credential-independent domain list with secret-backed API keys. Run `az bicep build --file infra\main.bicep` first to refresh the generated ARM. |
| [`test_agent_tool_wiring.py`](test_agent_tool_wiring.py) | That a missing **optional** tool degrades gracefully while a missing **required** one fails loudly. |
| [`test_audit.py`](test_audit.py) | That the audit trail can never cost a conversation: a full queue **drops rather than blocks**, a broken sink is contained, and no capture entry point can raise. Also pins the agent-mode tool reconstruction against the real observed conversation-item shape, and that audit is off by default. |
| [`test_build_package.py`](test_build_package.py) | The Teams manifest and the environment-scoped package filename. |
| [`test_build_query.py`](test_build_query.py) | That site scoping renders the operators Web IQ documents (a `-domain` exclusion is `-site:domain`, not `site:-domain`). |
| [`test_hybrid_search.py`](test_hybrid_search.py) | That a new Foundry project paired with BYO Search creates the project connection, cross-RG roles and index before creating the agent. |
| [`test_prompt_tool_names.py`](test_prompt_tool_names.py) | That prompt tool-name placeholders match the tools each binding really registers. |
| [`test_rbac_propagation.py`](test_rbac_propagation.py) | The retry/backoff used by `postprovision` — including that 404 is *not* retried, so a missing optional connection fails fast instead of stalling for 20 minutes. |
| [`test_set_profile.py`](test_set_profile.py) | That profile flags are authoritative rather than cumulative, so switching profiles clears the previous one's flags instead of leaving you paying for its resources. |
| [`test_ops_logs.py`](test_ops_logs.py) | That no `logger`/`print` call in `backend/` interpolates conversation content. Container stdout goes to Log Analytics, which is operational storage — a question logged there bypasses every control the audit trail applies, including `ENABLE_AUDIT=false`. Parses the AST rather than scanning lines, because most of these calls span several lines and a line-based check silently misses them. |

## If you add one

Mutation-test it: break the thing it claims to pin and confirm the suite **fails**. A
test that still passes when the property is removed is worse than no test. Assert that
your mutation actually landed before trusting the result — a regex anchored with `$`
will not match a CRLF file, and this repo is CRLF.
