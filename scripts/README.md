# `scripts/` — things that touch Azure

Operational scripts in this folder provision, grant, measure or gate an Azure
deployment. Several cost money to run. The explicitly marked offline evaluation
helpers only process private local evidence. One local helper,
[`sync_via_mirror.py`](sync_via_mirror.py), sets up the venv on networks that
block PyPI and installs git hooks that keep it in step with `uv.lock`.

The offline suites — no network, no credentials, free — live in
[`../tests/`](../tests/README.md).

## The naming convention

The prefix answers one question: **what does running this cost me?**

| prefix | what it does | needs Azure credentials | changes anything |
| --- | --- | --- | --- |
| `setup_` | creates or updates an Azure resource | yes | **yes** |
| `grant_` | assigns RBAC | yes | **yes** |
| `rename_` | rewrites one setting on every surface that holds a copy of it | yes | **yes** |
| `check_` | reads a published version and fails a build | network only | no |
| `smoke_` | a live compatibility/end-to-end check | yes | some use temporary data/resources; retrieval-agent probe creates/deletes a disposable agent |
| `bench_` | repeated measurement; long-running, prints numbers | yes | usually no; matrix creates/deletes isolated benchmark agents |
| `sync_` | installs pinned dependencies into your local `.venv` | no | your `.venv` only — never Azure, never `uv.lock` |
| *(no prefix)* | imported by other scripts — not meant to be run directly | — | — |

`test_` is deliberately **not** in this table. In this repo it means *offline*, and
those files are in [`../tests/`](../tests/README.md). If a file starts with `test_`,
running it cannot cost you anything.

## Deploy path — invoked by [`azure.yaml`](../azure.yaml)

These four run automatically during `azd up`. **Renaming one breaks the deploy**, because
`azure.yaml` calls them by path on both the PowerShell and bash branches.

| script | hook | what it does |
| --- | --- | --- |
| [`preflight.py`](preflight.py) | `preprovision` | Gate: regions, providers, tooling, per-profile inputs. Also settles subscription / location / resource group so `azd` does not stop to prompt mid-provision. |
| [`grant_byo_rbac.py`](grant_byo_rbac.py) | `postprovision` | Grants runtime RBAC when bringing your own Foundry/Search resources. |
| [`setup_aisearch_index.py`](setup_aisearch_index.py) | `postprovision` | Creates the index and ingests `data/`. Idempotent. |
| [`setup_foundry_agent.py`](setup_foundry_agent.py) | `postprovision` | Creates the agent and wires its tools. Idempotent; publishes a new version. |

Re-run them by hand with `azd hooks run postprovision` (there is no `azd postprovision`
command), or individually as `uv run python scripts/<name>.py`.

For isolated retrieval activation, both setup scripts accept `--env-file FILE`.
Index setup defaults to `CHUNKING_MODE=section`, using a new versioned index
and an optional `--manifest FILE` receipt. Explicit `window` retains
legacy/general ingestion. Agent setup
supports `--clone-from SOURCE_AGENT`: a different target `AGENT_NAME` inherits
the exact source prompt/model/reasoning/Bing configuration while replacing only
Search index, query type and top-k. See [configuration](../docs/configuration.md).
Normal agent setup defaults to Terra / none / semantic / top-5; explicit settings
override those defaults. Setup-only variables are not added to the running container. Future matrix runs
can use `--fixed-retrieval` to preserve that frozen Search/Bing configuration.

## Run by hand

| script | when you want it |
| --- | --- |
| [`set_profile.py`](set_profile.py) | **Step 0.** Pick a delivery channel; records `DEPLOY_PROFILE` and prints the numbered plan. |
| [`rename_avatar.py`](rename_avatar.py) | Rename the persona (`Simone` → `Nuru`) across the azd environment, container app and Foundry agent. Three independent knobs, all optional: `--display-name` (branding), `--model <character>`, `--type <type>` — the last is what a switch to a custom avatar needs, and omitting it makes an unrecognised `--model` a question rather than a rejection. Anything you do not pass is left as deployed, so the character can move without disturbing branding. `--check-only` verifies without changing anything. Skips the agent step under `VOICE_BINDING=model`, which has no agent. |
| [`setup_evaluation_index.py`](setup_evaluation_index.py) | Creates only new isolated `eval-*` indexes with `--layout paragraph` or `--layout section`; `--verify-existing` is read-only recovery/verification. Does not replace the production index. Two evaluation indexes are retained for user review; do not treat them as a production rollout. |
| [`smoke_aisearch_query.py`](smoke_aisearch_query.py) | "Did the index actually ingest?" Queries it directly. |
| [`smoke_foundry_agent.py`](smoke_foundry_agent.py) | "Can the deployed agent answer?" One end-to-end question. |
| [`smoke_retrieval_agent.py`](smoke_retrieval_agent.py) | **Paid** disposable-agent compatibility/evidence probe. Verifies native query modes and actual full returned AI Search passages, not just chunk IDs. This is not a model/voice bakeoff; keep traces private and follow the [evaluation guide](../docs/assistant-evaluation.md). |
| [`smoke_audit_conversation.py`](smoke_audit_conversation.py) | "Which agent-mode tool details are exposed?" Replays one Foundry conversation through the **production reconciler** ([`backend/audit/foundry.py`](../backend/audit/foundry.py)) to inspect exposed queries and AI Search passages. Raw Bing grounding content is not exposed and cannot be recovered by auditing. Reads only. See [audit.md](../docs/audit.md) and the [evaluation guide](../docs/assistant-evaluation.md). |
| [`smoke_audit_cosmos.py`](smoke_audit_cosmos.py) | "Can this identity actually write the audit trail?" Round-trips one document through the **production sink** ([`backend/audit/cosmos.py`](../backend/audit/cosmos.py)) — connect, write, read back, assert redaction held, delete. Proves the Entra **data-plane** role, which is the half of the audit trail no mock can cover. Run it *before* enabling audit on a deployment. |
| [`smoke_webiq_search.py`](smoke_webiq_search.py) | "Does web grounding actually work, and is the content worth the tokens?" Calls the **production** [`search_web()`](../backend/voice/tools.py) live, then re-runs the same query with `contentFormat=passage` and `text` side by side so the difference is visible rather than argued. Reports which credential route it took; never prints the key. Needs `WEBIQ_API_KEY` — the keyless route [cannot work on a laptop](../docs/auth.md). |
| [`bench_retrieval.py`](bench_retrieval.py) | Retrieval-only `audit` / `run`: independent private original-DOCX XML audit, then live top-5/8 retrieval. Modes: `keyword`, `hybrid`, `semantic` (hybrid + ranker), `semantic_keyword` (lexical + ranker); `--query-style canonical` or `--query-style natural`. No answer generation. See the [evaluation guide](../docs/assistant-evaluation.md) for timing/evidence rules and the review gate. |
| [`bench_routing_agent.py`](bench_routing_agent.py) | Tool-routing accuracy and latency on the **agent** binding. |
| [`bench_routing_matrix.py`](bench_routing_matrix.py) | Paced reasoning/retrieval comparison using temporary copies of the live agent. Leaves the source unchanged, captures every round privately, and reports the historical minutes+web subtotal separately. |
| [`bench_agent_evaluation.py`](bench_agent_evaluation.py) | Paid fixed-evidence/live-agent comparison against the frozen section/semantic/k5 profile. `--continue-from` requires a new output directory and reuses successful turns only after verifying source, cases, catalogue and settings. |
| [`bench_agent_audio.py`](bench_agent_audio.py) | Paid Voice Live **agent-mode** sample: text submission to first received PCM audio, not microphone-to-playback or avatar latency. Uses disposable agents. |
| [`bench_realtime_evaluation.py`](bench_realtime_evaluation.py) | **Paid** model-mode oracle/live evaluation using private frozen cases and actual Search/Web IQ tools. Records per-response usage and T0-T5; discretionary pacing is between attempts, while real service waits are labelled. Event persistence is outside measured turns. See [latency attribution](../docs/realtime-latency-investigation.md) before comparing historical numbers. |
| [`bench_retrieval_worker.py`](bench_retrieval_worker.py) | Read-only timing worker for an explicitly selected `eval-*` index from the deployed container. Uses managed identity and emits result hashes, not source passages. |
| [`prepare_evaluation_review.py`](prepare_evaluation_review.py) | **Offline** blinded review-packet preparation. Keeps model mappings and timings outside reviewer packets. |
| [`summarize_agent_evaluation.py`](summarize_agent_evaluation.py) | **Offline** timing, tool-scope and evidence-availability aggregates, not automatic answer-quality grades. |
| [`bench_routing_model.py`](bench_routing_model.py) | The same benchmark on the **model** binding. |
| [`bench_audit_latency.py`](bench_audit_latency.py) | What the audit trail charges the turn it is recording. Three arms — `ENABLE_AUDIT=false`, `AUDIT_SINK=none`, `AUDIT_SINK=file` — so capture cost and sink cost are separated. Offline; touches no Azure resource. |
| [`check_media_sdk_age.py`](check_media_sdk_age.py) | Fails once the Graph media SDK pin passes 90 days. Wired into [`../meeting-bot/MeetingBot.csproj`](../meeting-bot/MeetingBot.csproj), so a channel-D build runs it for you. |
| [`sync_via_mirror.py`](sync_via_mirror.py) | `uv sync` fails with `HandshakeFailure` on `files.pythonhosted.org` because your network allows only a package mirror. Installs exactly what `uv.lock` pins through the mirror pip already uses (any vendor), hash-verified, without rewriting the lock. Run it once per clone as `uv run --no-project python scripts/sync_via_mirror.py [--extra cosmos]` (`--no-project` stops `uv run` syncing first). It then installs git hooks, so later checkouts, pulls and rebases that change `uv.lock` or `pyproject.toml` re-sync by themselves (offline first; never blocks git). `--no-hooks`, `--uninstall-hooks` and `SYNC_VIA_MIRROR_SKIP=1` opt out. See [development.md](../docs/development.md#behind-a-package-mirror-pypi-blocked). |

Files without a prefix are **libraries**, imported rather than run:
[`channels.py`](channels.py) (the single source of truth for profiles, their flags and
their steps), [`rbac_propagation.py`](rbac_propagation.py) (the retry that waits out
data-plane RBAC propagation lag), [`routing_questions.py`](routing_questions.py)
(the shared historical routing questions and classifier), and
[`retrieval_evaluation.py`](retrieval_evaluation.py) (the independent source audit
and paragraph-span retrieval evaluation helpers). The latter has
[offline synthetic tests](../tests/test_retrieval_evaluation.py).
[`evaluation_scoring.py`](evaluation_scoring.py) is another offline library:
it checks required source-quote availability without equating it to answer correctness.

## Notes that have bitten before

- **New evaluation is retrieval-first, not a routing/model sweep.** Follow the
  [assistant evaluation guide](../docs/assistant-evaluation.md). Retrieval review
  preceded the approved fixed-retrieval agent comparison. Production promotion
  and the later realtime-model track remain separate decisions. Historical
  core/default question sets can differ from current scope, so explicitly scope
  permitted cases rather than running old defaults. Routing harness scores are not a substitute
  for independent required-fact evidence coverage.
- **Retrieval selection needs evidence and latency.** Use the guide's repeated,
  seeded/interleaved arm protocol and per-arm p50/p95/max, separating client/server
  timings (including raw `elapsed-time` headers), auth/TLS warmup and pacing/retries.
  An oracle-date filter is diagnostic only. Verify real reranker scores, but do
  not retain reranking merely because it is enabled; justify its evidence/latency
  tradeoff before the review gate.
- **Runner and native semantic names differ.** Runner `semantic` is hybrid +
  ranker; runner `semantic_keyword` maps to native `query_type='semantic'`.
  Native `vector_semantic_hybrid` also works, but is not the frozen full-section,
  lexical-reranked top-5 candidate. [Current results](../docs/evaluation-results.md)
  report measured evidence coverage, not perfect precision or a production
  change; wrong-meeting distractors remain even with complete required evidence.
- **Bing grounding is not a recoverable passage trace.** Microsoft's
  [documented behavior](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/bing-tools#how-it-works)
  exposes queries, responses and citations, not raw grounding content. Do not
  promise logging/audit recovery. Later web evaluation uses fixed public snapshots
  plus cited live-grounding checks, with hidden-evidence causal attribution marked
  indeterminate. Model-mode Web IQ stays a separate track.
- **The two routing benchmarks share one question set.** Both *import* `TIERS`, `GROUPS` and
  `classify()` from [`routing_questions.py`](routing_questions.py) rather than copying
  them, so the two bindings cannot drift into being scored against different questions.
  That file is a library with no third-party imports, so pulling the questions in
  costs nothing — it was carved out of `bench_routing_agent.py`, where reading a list
  of strings meant exec'ing the agent harness and, transitively,
  `smoke_foundry_agent.py`. The historical prose rationale and exploratory
  configuration results are in
  [evaluation history](../docs/evaluation-history.md).
- **Completion timings are not first audio.** Longer answers inflate routing
  benchmark completion times. `bench_retrieval.py` measures retrieval, not answer
  generation or voice latency; neither is a meaningful first-audio measurement.
- **`bench_audit_latency.py` runs each arm in a subprocess, deliberately.**
  `ENABLE_AUDIT` is read at import time, and `backend/config.py` calls
  `load_dotenv(override=True)` — so a local `.env` would *override* the arm under
  test and silently invalidate the run. Children are given a working directory
  where no `.env` is discoverable, and every arm then asserts that the config it
  resolved is the one intended. It also enlarges `AUDIT_QUEUE_MAX` for the run:
  the writer batches with a 2-second window, so a tight loop outruns it and the
  measurement would drift onto the drop path instead of the enqueue path a real
  turn takes.
- **`smoke_audit_cosmos.py` writes one real document, and the `ttl` is the cleanup
  guarantee.** It deletes the probe on the way out, but a crash or Ctrl-C skips
  that, so the document is written with a one-hour `ttl` rather than the
  configured retention — otherwise a failed run would leave a synthetic record in
  a real container for a year. It also asserts the *configured* retention was
  computed correctly before overriding it, so the override does not hide a bug.
  Note the sink swallows upsert errors by design, so the script checks the
  returned count: a write failure shows up as `0 of 1 written`, not an exception.
- **`setup_foundry_agent.py` bakes the assistant's name into the prompt** at
  provisioning time. Rename the persona and every other surface updates on the next
  deploy, but the agent keeps the old name until this script re-runs — so the stage
  says "Nuru" while she introduces herself as "Simone". Use
  [`rename_avatar.py`](rename_avatar.py) rather than doing it by hand; that split is
  exactly what it exists to close.
- **`rename_avatar.py` verifies the *resolved* name, not the raw variables.** An empty
  `AVATAR_DISPLAY_NAME` is a legitimate configuration when the name derives from the
  active avatar model, so asserting on the raw variable would report a correct
  deployment as broken. It calls the same `resolve_avatar_display_name()` the app does.
- **Persona, character and modality are three knobs, not one — one flag each, all
  optional.** `--display-name` controls branding (`AVATAR_DISPLAY_NAME`); `--model`
  selects the Speech character and validates standard catalogue names locally;
  `--type` decides whether that character is resolved against the prebuilt
  catalogue or your own Speech resource. Moving to a custom avatar means changing
  the last two **together** — `--type` does that, and is offered interactively when
  `--model` names something the catalogue does not have.
- **What you do not pass is left as deployed.** Omit the name and
  `AVATAR_DISPLAY_NAME` is not written, so switching character or modality cannot
  quietly unpin branding someone set earlier. With nothing pinned, the persona name
  falls back to the model with its suffixes stripped (`Lisa-casual-sitting` →
  `Lisa`), which is the resolver the app itself uses — so the name the script
  verifies is the name the app will show. The bare positional form
  (`rename_avatar.py Nuru`) is shorthand for `--display-name` and still works, but
  the flag is worth preferring: `Nuru --model Nuru --type custom-photo` gives no
  clue which `Nuru` is which. Passing both with different values is refused, and a
  run that would change nothing at all points you at `--check-only`.
- **There are two catalogues, and the modality picks one.** Photo avatars
  (`Simone`, `Anika`) and video avatars (`Lisa-casual-sitting`, `Max-business`) are
  disjoint lists, both read from the pickers in `frontend/index.html` so they cannot
  drift from what the app offers. Validation follows the type **in effect** —
  `--type` when given, otherwise the deployed one — so a video character is never
  measured against the photo list.
- **`AVATAR_TYPE` set by hand is the classic half-move.** `azd env set AVATAR_TYPE
  custom-photo` updates what the *next* deploy will impose, not what is running now,
  so the avatar keeps rendering as the old character with no error anywhere. The
  script writes it to both surfaces and its VERIFY step fails on any disagreement.
- **Model mode has no agent, so there is no third surface.** `azure.yaml` already
  skips `setup_foundry_agent.py` when `VOICE_BINDING=model`; `rename_avatar.py`
  mirrors that. Running it anyway used to fail and then report a "HALF APPLIED"
  rename that had in fact fully landed.

See [`../docs/development.md`](../docs/development.md) for the full local-development
walkthrough.
