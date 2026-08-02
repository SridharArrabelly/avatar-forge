# Development (run it locally)

Get Avatar Forge running on your machine in a few minutes. You do **not** need Docker
or `azd` for local development — just `uv` and `az login`. For env vars see
[configuration.md](configuration.md); for Azure deployment see
[deployment.md](deployment.md).

## Prerequisites

- **Python 3.10+**
- An active Azure account ([free account](https://azure.microsoft.com/free/ai-services))
- A **Microsoft Foundry** resource in a region that supports both Voice Live and the
  avatar — `eastus2`, `southeastasia`, `swedencentral`, `westus2`
- A base chat model deployed in Foundry (e.g. `gpt-4.1` or `gpt-5`+) — the agent binds to it
- An [Azure AI Search](https://learn.microsoft.com/azure/search/search-create-service-portal)
  service, added as a [connected resource](https://learn.microsoft.com/azure/ai-foundry/how-to/connections-add)
  in the Foundry project (its connection name → `SEARCH_CONNECTION_NAME`)

> Those four regions are the intersection of the Voice Live and avatar region sets;
> `scripts/preflight.py` holds the authoritative lists and checks them for you.
> [deployment.md](deployment.md#regions) explains what happens if you pick another.

## 1. Install uv (one-time)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

`uv` creates the `.venv` and installs dependencies automatically the first time you run
the app.

## 2. Configure your environment

```powershell
Copy-Item .env.example .env
```

Fill in at least the required runtime vars (`AZURE_VOICELIVE_ENDPOINT`, `AGENT_NAME`,
`AGENT_PROJECT_NAME`, `PROJECT_ENDPOINT`) — the full reference is in
[configuration.md](configuration.md). On a dev laptop, also set
`AUTH_EXCLUDE_MANAGED_IDENTITY=true` to skip the slow IMDS probe (see [auth.md](auth.md)).

Authenticate (the Voice Live agent path requires Entra ID — no API key):

```powershell
az login
```

### Which brain? (`VOICE_BINDING`)

The app binds Voice Live to one of two things, and the default is `agent`:

| | `VOICE_BINDING=agent` (default) | `VOICE_BINDING=model` |
| --- | --- | --- |
| Answers come from | a **Foundry agent** that owns its own tools | the **realtime model**, with tools executed by this backend |
| Web grounding | Grounding-with-Bing-Custom-Search (a native Foundry tool) | Web IQ, which needs `WEBIQ_API_KEY` in your `.env` |
| Extra local setup | none beyond the above | that key |

Switch by setting `VOICE_BINDING` in `.env` — nothing else changes, and the same
`az login` covers both. What each costs and how they compare on measured latency is in
**[voice-binding.md](voice-binding.md)**.

## 3. Run the server

```powershell
uv run avatar-forge
```

Or with uvicorn directly (auto-reload):

```powershell
uv run uvicorn backend.main:app --host 0.0.0.0 --port 3000 --reload
```

Open <http://localhost:3000>.

> Set `DEVELOPER_MODE=true` in `.env` to expose the settings panel, live transcript,
> and per-event debug logging.

## Build the Azure AI Search index

The agent answers from your own documents via an Azure AI Search index. Use
[`scripts/setup_aisearch_index.py`](../scripts/setup_aisearch_index.py) to (re)create
the index and ingest content from [`data/`](../data/).

Supported file types (auto-detected, recursive): **`.docx`, `.pdf`, `.md`,
`.markdown`, `.txt`**. To add a format, register a reader in the `READERS` dict at the
top of the script.

What the script does each run:

1. **Discover** — walks `data/` recursively for registered extensions.
2. **Read** — extracts plain text per file type (`python-docx`, `pypdf`, raw read).
3. **Chunk** — overlapping windows of `CHUNK_SIZE` chars with `CHUNK_OVERLAP` overlap.
4. **Embed** — sends chunks to the Foundry Azure OpenAI route (`text-embedding-3-small`, 1536 dims).
5. **Upload** — pushes chunks + vectors into the index, configured for **hybrid search**
   (BM25 + HNSW/cosine) with a **semantic configuration** (`default-semantic`) for L2 re-ranking.

This is a one-off bootstrap — the running app never re-ingests, it only queries.

Required roles for the signed-in user: **Search Index Data Contributor** + **Search
Service Contributor** on AI Search, and **Foundry User** on the Foundry **account**
(not the project — the embedding call is an account-level data action).

`azd up` assigns all three automatically when `AZURE_PRINCIPAL_ID` is set, which `azd`
does for you. You only need to grant them by hand when running this script against a
Foundry account you did not deploy. Note that **Azure AI Developer** is *not* a
substitute: it carries no `Microsoft.CognitiveServices` data actions, so the embedding
call returns `401 PermissionDenied` even though the role name suggests otherwise.

```powershell
uv run python scripts/setup_aisearch_index.py

# wipe + rebuild from scratch:
$env:RECREATE_INDEX = "true"
uv run python scripts/setup_aisearch_index.py
Remove-Item Env:\RECREATE_INDEX
```

## Smoke-test the index

```powershell
uv run python scripts/test_aisearch_query.py "what was discussed about dividends"
uv run python scripts/test_aisearch_query.py -k 3 "board chair election"
```

Issues a hybrid + semantic query and prints the top results with BM25/vector and
reranker scores.

## Smoke-test the live agent

```powershell
uv run python scripts/test_foundry_agent.py
```

Exercises the registered Foundry agent end-to-end (tool calls + final answer) — useful
to confirm tool routing after editing prompts or switching `AGENT_MODEL`. The routing
test checklist + model-shootout results live in
[`prompts/agent/routing-test-questions.md`](../prompts/agent/routing-test-questions.md).

## Automated tests

Everything above is a *smoke test* — it needs live Azure resources. Most of the suite
does not: **eight checks run fully offline**, with no Azure, no credentials and no
network. They are the fastest way to know you have not broken anything.

```powershell
uv run python scripts/test_docs.py             # links, mermaid, and region drift vs preflight.py
uv run python scripts/test_preflight.py        # the helpers that settle the deploy target
uv run python scripts/test_voice_binding.py    # the agent/model binding switch
uv run python scripts/test_build_query.py      # site scoping renders the operators Web IQ documents
uv run python scripts/test_avatar_identity.py  # every surface calls the assistant the same name
uv run python scripts/test_build_package.py    # the Teams package builder's manifest
uv run python scripts/test_agent_tool_wiring.py
uv run python scripts/test_rbac_propagation.py # the RBAC-propagation wait used by postprovision
```

There is no single runner — each is a standalone script, so run the one that covers what
you touched. Only [`test_aisearch_query.py`](../scripts/test_aisearch_query.py) and
[`test_foundry_agent.py`](../scripts/test_foundry_agent.py) need live Azure; those are
the smoke tests above.

Two are worth knowing in more detail.

**`test_agent_tool_wiring.py`** proves the agent's **required vs optional** tools degrade
correctly: a missing AI Search connection is fatal (it is the corpus), while a missing —
or wrongly named — Bing connection only disables the web tool. Run it after touching
`setup_foundry_agent.py`.

**`test_build_package.py`** must run in a *clean* shell. It asserts the builder's
behaviour when variables are unset, so a hydrated environment (one where you have
`azd env get-values`'d into your session) makes it fail for the wrong reason.

```powershell
cd meeting-bot\tests\BridgeContract.Tests
dotnet test
```

Eight tests lock the contract between the .NET media bot and
[`backend/acs/bridge.py`](../backend/acs/bridge.py). They need the .NET SDK but **not**
Windows and **not** the media SDK — the suite link-compiles the one client class rather
than referencing the bot project. Run them after touching either side of that protocol;
a mismatch there is silent in production, so nothing else will catch it.

## (Re)register the Foundry agent

After editing the prompts in [`prompts/agent/`](../prompts/agent/) or changing
`AGENT_MODEL` / tool wiring, re-register the agent:

```powershell
uv run python scripts/setup_foundry_agent.py
```

The script selects the prompt variant (reasoning vs non-reasoning) from `AGENT_MODEL`
and wires the AI Search + Grounding-with-Bing-Custom-Search tools. See
[`prompts/README.md`](../prompts/README.md) and
[architecture.md](architecture.md#tool-calling-accuracy).

## Regenerate the brand icons

[`assets/brand/`](../assets/brand/) is the single source for the app mark, shared by the
web app, the Teams package and the meeting bot. `color.png` (192×192) and `outline.png`
(32×32) are **drawn procedurally** by `generate_icons.py` — there is no source image, so
changing the mark means editing the drawing code in that script, then:

```powershell
uv run --with pillow python assets/brand/generate_icons.py
```

Pillow is deliberately not a project dependency (the repo is stdlib-only), hence
`--with`. Both PNGs are committed: re-run, then commit the result. The web app and bot
serve them from `/brand/*` immediately; the Teams package picks them up on its next
`build_package.py` run.

## Docker (local) — not recommended

You do **not** need Docker to run locally. The `Dockerfile` exists so `azd` can build
the image during `azd up`/`azd deploy`. Running the image locally is discouraged: it
has no `az` CLI and no IMDS, so `DefaultAzureCredential` can't authenticate unless your
tenant allows service-principal secrets (`AZURE_TENANT_ID` / `AZURE_CLIENT_ID` /
`AZURE_CLIENT_SECRET`), which many tenants block. Use the host instructions above.

## Run inside Microsoft Teams

To run the same UI as a Teams personal tab (channel B),
follow [`teams/README.md`](../teams/README.md) — it covers building the package against
your deployed hostname, the admin-free sideload routes, the bot's Azure Bot / Entra
setup, and the validation checklist. The Teams integration is fully additive: the
standalone local experience above is byte-for-byte unchanged (the Teams JS SDK is never
loaded outside Teams).
