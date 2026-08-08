# `scripts/` — things that touch Azure

Everything in this folder reaches a live subscription: it provisions, grants, measures
or gates a deploy. Several cost money to run.

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
| `smoke_` | one live end-to-end question, to prove a deploy works | yes | no |
| `bench_` | repeated measurement; long-running, prints numbers | yes | no |
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

## Run by hand

| script | when you want it |
| --- | --- |
| [`set_profile.py`](set_profile.py) | **Step 0.** Pick a delivery channel; records `DEPLOY_PROFILE` and prints the numbered plan. |
| [`rename_avatar.py`](rename_avatar.py) | Rename the persona (`Simone` → `Nuru`) across the azd environment, container app and Foundry agent. `--model <character>` also changes `AVATAR_MODEL`; `--check-only` verifies without changing anything. |
| [`smoke_aisearch_query.py`](smoke_aisearch_query.py) | "Did the index actually ingest?" Queries it directly. |
| [`smoke_foundry_agent.py`](smoke_foundry_agent.py) | "Can the deployed agent answer?" One end-to-end question. |
| [`smoke_audit_conversation.py`](smoke_audit_conversation.py) | "Can agent-mode tool detail actually be recovered?" Replays one Foundry conversation through the **production reconciler** ([`backend/audit/foundry.py`](../backend/audit/foundry.py)) and prints the query and passages the agent used server-side. The one thing about the audit trail that cannot be proven offline. See [audit.md](../docs/audit.md). |
| [`bench_routing_agent.py`](bench_routing_agent.py) | Tool-routing accuracy and latency on the **agent** binding. |
| [`bench_routing_model.py`](bench_routing_model.py) | The same benchmark on the **model** binding. |
| [`bench_audit_latency.py`](bench_audit_latency.py) | What the audit trail charges the turn it is recording. Three arms — `ENABLE_AUDIT=false`, `AUDIT_SINK=none`, `AUDIT_SINK=file` — so capture cost and sink cost are separated. Offline; touches no Azure resource. |
| [`check_media_sdk_age.py`](check_media_sdk_age.py) | Fails once the Graph media SDK pin passes 90 days. Wired into [`../meeting-bot/MeetingBot.csproj`](../meeting-bot/MeetingBot.csproj), so a channel-D build runs it for you. |

Two files have no prefix because they are **libraries**, imported rather than run:
[`channels.py`](channels.py) (the single source of truth for profiles, their flags and
their steps) and [`rbac_propagation.py`](rbac_propagation.py) (the retry that waits out
data-plane RBAC propagation lag).

## Notes that have bitten before

- **The two benchmarks share one question set.** `bench_routing_model.py` *imports*
  `TIERS` and `classify()` from `bench_routing_agent.py` rather than copying them, so
  the two bindings cannot drift into being scored against different questions.
- **`bench_*` times completion, not first audio.** Longer answers inflate it. It is a
  routing instrument; do not quote it as a time-to-first-token figure.
- **`bench_audit_latency.py` runs each arm in a subprocess, deliberately.**
  `ENABLE_AUDIT` is read at import time, and `backend/config.py` calls
  `load_dotenv(override=True)` — so a local `.env` would *override* the arm under
  test and silently invalidate the run. Children are given a working directory
  where no `.env` is discoverable, and every arm then asserts that the config it
  resolved is the one intended. It also enlarges `AUDIT_QUEUE_MAX` for the run:
  the writer batches with a 2-second window, so a tight loop outruns it and the
  measurement would drift onto the drop path instead of the enqueue path a real
  turn takes.
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
- **Persona and character are separate knobs.** `AVATAR_DISPLAY_NAME` controls
  branding; `AVATAR_MODEL` selects the Speech character. `--model` changes the
  character and validates standard catalogue names locally.

See [`../docs/development.md`](../docs/development.md) for the full local-development
walkthrough.
