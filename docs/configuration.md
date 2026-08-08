# Configuration reference

Every environment variable Avatar Forge reads, grouped by concern. This is the
**single source of truth**. [`.env.example`](../.env.example) is the copy-and-fill
template for local runtime and BYO tooling; `azd`-only settings remain here.

Copy the template, then fill in the required values:

```powershell
Copy-Item .env.example .env
```

Conventions:

- Commands are **PowerShell on Windows**; translate the syntax yourself on macOS/Linux.
- **Required** vars must be set for the runtime backend to start and answer.
- Booleans accept `true`/`false` (also `1`/`0`, `yes`/`no`, `on`/`off`).
- Vars marked *(provisioning only)* are read by `scripts/*.py` or `azd`, **not**
  by the running server — a brownfield (BYO) deploy can leave them unset.

> [!IMPORTANT]
> **Two different places, and they are not interchangeable.** `.env` configures a
> server you run **locally**. Anything that must reach a **deployed** app or shape
> the infrastructure goes in the azd environment via `azd env set NAME value` —
> `azd` never reads `.env`, so a value put only there is invisible to `azd up`.
> Infra writes its own outputs back into the azd environment, and the container
> app receives them as real environment variables. When preflight reports a
> missing value it always prints the exact `azd env set` command to run.

---

## Deployment profile *(provisioning only)*

Set with `uv run python scripts/set_profile.py` rather than by hand.

| Variable | Default | Purpose |
|---|---|---|
| `DEPLOY_PROFILE` | *(empty)* | `web` · `teams-tab` · `in-call-browser` · `in-call` (channels A–D). Selects which channel deploys, and drives the numbered step plan `scripts/preflight.py` prints. Empty keeps the pre-profile behaviour (explicit flags only). **Selecting a profile is authoritative**: it writes that profile's flags and resets the others, so switching away from `in-call` also switches off its Windows VM. |
| `PREFLIGHT_SKIP` | `false` | `true` bypasses the preprovision preflight gate. An escape hatch — nobody should be stuck behind their own tooling. |

---

## Required — Voice Live / Foundry (runtime)

| Variable | Default | Purpose |
|---|---|---|
| `AZURE_VOICELIVE_ENDPOINT` | — | **Required.** Your Foundry / AI Services endpoint, e.g. `https://<resource>.services.ai.azure.com/`. |
| `AGENT_NAME` | `AvatarAgent` | Name of the Foundry agent the session binds to in agent mode. Greenfield deployments create the default automatically; set this only to choose a different name or reuse an existing agent. |
| `AGENT_PROJECT_NAME` | `avatar-forge` | **Required.** Foundry project that owns the agent. |
| `PROJECT_ENDPOINT` | — | **Required.** Foundry project endpoint, e.g. `https://<resource>.services.ai.azure.com/api/projects/<project>`. |
| `VOICELIVE_VOICE` | `en-US-AvaMultilingualNeural` | Default avatar voice (also settable in the UI). |

Authentication is always Microsoft Entra ID via `DefaultAzureCredential` — API-key
auth is rejected on the agent path. See [auth.md](auth.md).

---

## Authentication

| Variable | Default | Purpose |
|---|---|---|
| `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` | — | Only for the (not recommended) local-Docker service-principal path. Leave unset on host (`az login`) and in Azure (managed identity). |
| `AUTH_EXCLUDE_MANAGED_IDENTITY` | `false` | Dev-laptop only: skip the ~5s IMDS managed-identity probe to cut startup pre-warm from ~7s to ~1.5s. **Leave UNSET in Azure** — Container Apps / App Service / AKS workload identity all need the IMDS path. See [auth.md](auth.md). |
| `AZURE_VOICELIVE_API_KEY` | — | Key-based fallback for the Voice Live connection. Unset is the intended posture: the backend then uses `DefaultAzureCredential`, which is what the deployed managed identity relies on. Use it only where a key is genuinely unavoidable. |
| `RBAC_PROPAGATION_TIMEOUT_S` | `1200` | Seconds [`scripts/rbac_propagation.py`](../scripts/rbac_propagation.py) waits for a freshly assigned role to become effective. Role assignments are eventually consistent, so a greenfield deploy can otherwise fail on a permission that *has* been granted but has not landed yet. |

---

## Foundry agent provisioning *(provisioning only)*

Read by [`scripts/setup_foundry_agent.py`](../scripts/setup_foundry_agent.py) at
agent-creation time; the runtime backend never talks to Bing directly.

| Variable | Default | Purpose |
|---|---|---|
| `DEPLOY_BING_GROUNDING` | `true` | **Infra-only, agent mode only.** `azd up` deploys the whole web tool — the Bing account, the curated site allow-list and the Foundry connection — and sets the two variables below automatically. Set `false` to skip it (it is a billable resource); the agent then answers from AI Search alone. Only takes effect on a greenfield deploy (there must be a Foundry project to attach the connection to). **Ignored under `VOICE_BINDING=model`**, which has no agent to attach a managed tool to and uses Web IQ instead. |
| `BING_SKU_NAME` | `G2` | **Optional, infra-only.** Bing pricing tier when `DEPLOY_BING_GROUNDING=true`. `G2` is the tier this project has run on; `G1` is the lower tier. |
| `BING_CONNECTION_NAME` | *(unset — web tool disabled)* | **Optional.** Foundry connection for Grounding with Bing Custom Search (the agent's only external tool). Set for you when `DEPLOY_BING_GROUNDING=true`; otherwise name an existing connection. Leave unset for a search-only agent; naming a connection that doesn't exist skips the tool with a warning rather than failing. |
| `BING_CUSTOM_CONFIG_NAME` | *(unset — web tool disabled)* | **Optional.** Bing Custom Search configuration name — the curated domain allow-list the web tool is restricted to. Set for you when `DEPLOY_BING_GROUNDING=true`; otherwise required alongside `BING_CONNECTION_NAME`. |
| `AGENT_MODEL` | `gpt-5.4` | Foundry model deployment the agent runs on. Recommended: `gpt-5.4` + `AGENT_REASONING_EFFORT=none` (best tool routing; 30/30 on the harness). `gpt-5.4-mini` is a cheaper fallback; `gpt-4.1-mini` is the documented baseline. See [architecture.md](architecture.md#tool-calling-accuracy). |
| `AGENT_REASONING_EFFORT` | `none` | Reasoning effort. **Model-dependent:** `gpt-4.x`/`gpt-4o` reject it (leave **unset** — they 400, manifesting as a silently non-speaking avatar); `gpt-5.x` accept `none\|low\|medium\|high\|xhigh`; o-series accept `low\|medium\|high`. For voice latency the validated value is `none` (real reasoning adds 4–5s to first token). Left unset on a `gpt-5.x` model the script defaults it to `none` rather than let the service default (`medium`) apply. It does **not** select a prompt — there is one agent prompt. |
| `AI_SEARCH_TOP_K` | `8` | Chunks pulled from the meeting-minutes index per turn. |
| `BING_COUNT` | `8` | Snippets returned from the Bing Custom Search allow-list per turn. |

> **The curated site allow-list is not an environment variable.** It is the
> `bingAllowedDomains` parameter in [`infra/main.bicep`](../infra/main.bicep) — a list of
> `{ domain, includeSubPages, boostLevel }` entries, where `boostLevel` is `SuperBoost` or
> `Boosted`. It lives in bicep because it is a security boundary: Bing enforces it as a
> hard allow-list, so nothing outside it is reachable, which is what makes an open-web tool
> safe to hand an executive assistant. Edit it there before deploying so it points at your
> own sources — the checked-in list is a sample.

---

## AI Search & index build *(provisioning only)*

Read by [`scripts/setup_aisearch_index.py`](../scripts/setup_aisearch_index.py) and
[`scripts/smoke_aisearch_query.py`](../scripts/smoke_aisearch_query.py).

| Variable | Default | Purpose |
|---|---|---|
| `AZURE_SEARCH_ENDPOINT` | — | **Required (runtime + build).** `https://<service>.search.windows.net`. |
| `SEARCH_CONNECTION_NAME` | `aisearch-connection` | **Required.** AI Search connection name in the Foundry project. |
| `SEARCH_INDEX_NAME` | `knowledge-index` | **Required.** Index name to create/update and query. |
| `AZURE_SEARCH_API_KEY` | — | Optional; if unset, AI Search uses `DefaultAzureCredential`. |
| `EMBEDDING_DEPLOYMENT` | `text-embedding-3-small` | Foundry-deployed embedding model (1536 dims). Changing it requires a one-off `RECREATE_INDEX=true` rebuild (vector dims are immutable). |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` | API version for the embedding calls. |
| `DATA_DIR` | `./data` | Corpus directory ingested into the index. |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1200` / `200` | Chunking window (chars) and overlap. |
| `RECREATE_INDEX` | `false` | `true` drops and recreates the index. |
| `SEARCH_VECTOR_PROFILE` / `SEARCH_HNSW_ALGO` / `SEARCH_SEMANTIC_CONFIG` / `SEARCH_VECTORIZER` | `default-*` | Internal structural names; override only to stay compatible with an index built with different names. |
| `SEARCH_VECTOR_FIELD` | `content_vector` | Name of the vector field queried at **runtime** by `search_minutes` in model mode ([`backend/voice/tools.py`](../backend/voice/tools.py)). Unlike the row above it is read on every query, not only at build time, so it must match the field the index was actually built with. |

---

## Greenfield model deployment *(azd provision only)*

Read **only** when `azd` creates a new Foundry model deployment
([`infra/main.bicep`](../infra/main.bicep)). Unused for a brownfield (BYO Foundry)
deploy. Keep `MODEL_VERSION` matched to `MODEL_NAME` — an invalid pair fails the
deployment.

| Variable | Default | Purpose |
|---|---|---|
| `MODEL_NAME` | `gpt-5.4` | **Which** model to pull from the catalogue. |
| `MODEL_VERSION` | `2026-03-05` | Model version (must match `MODEL_NAME`). |
| `MODEL_DEPLOYMENT_NAME` | `gpt-5.4` | What to **call** that deployment. |
| `MODEL_SKU_NAME` | `GlobalStandard` | Deployment SKU. |
| `MODEL_CAPACITY` | `250` | TPM (thousands) capacity, so `250` is 250K tokens/minute. On `GlobalStandard` this is a **rate ceiling, not a reservation** — billing is per token consumed, so raising it costs nothing and only buys headroom against 429s. Every turn resends the full agent prompt plus retrieved chunks, so the old `50` was easy to trip under demo load. Your regional ceiling: `az cognitiveservices usage list -l <region>`. |

### Why `MODEL_NAME` and `MODEL_DEPLOYMENT_NAME` are both needed

They look redundant because the defaults are identical, but they are two different
fields on the Azure resource and neither can be dropped:

```bicep
resource deployment 'Microsoft.CognitiveServices/accounts/deployments@...' = {
  name: modelDeploymentName            // what you call it — the alias callers use
  properties: {
    model: { name: modelName, version: modelVersion }   // which model it actually is
  }
}
```

So you can deploy `gpt-5.4` under the name `chat-prod`, then swap the model behind
that name later without changing a single caller. That indirection is the whole point
of a deployment name, and it is why callers — including the Foundry agent — reference
`MODEL_DEPLOYMENT_NAME`, never `MODEL_NAME`.

> **`AGENT_MODEL` is a *deployment* name too, despite the variable name.** It is what
> the agent binds to. On a greenfield deploy you do **not** set it — it follows
> `MODEL_DEPLOYMENT_NAME` automatically, because the agent has to bind to the
> deployment the template just created. Set it explicitly only for BYO Foundry, where
> the deployment already exists and this template did not name it.
> [`tests/test_agent_model_binding.py`](../tests/test_agent_model_binding.py) pins
> that behaviour.

---

## Voice Live binding — agent mode / model mode

Voice Live binds either to the Foundry agent (default) or straight to a realtime
speech-to-speech model. Full design record, trade-offs and measured numbers:
**[voice-binding.md](voice-binding.md)**.

Leaving all of these unset is exactly today's behaviour.

| Variable | Default | Purpose |
|---|---|---|
| `VOICE_BINDING` | `agent` | `agent` binds the Foundry agent; `model` binds a realtime model directly and moves the tools in-process. Any unrecognised value falls back to `agent`. |
| `VOICELIVE_MODEL` | `gpt-realtime-2` | Realtime model bound when `VOICE_BINDING=model`. Voice Live manages it — no model deployment and no quota request. Ignored in agent mode. Verified to bind in swedencentral: `gpt-realtime-2`, `gpt-realtime-1.5`, `gpt-realtime`. |

Model mode takes the agent out of the picture, and its managed `azure_ai_search`
and `bing_grounding` tools go with it — the tool surface becomes in-process Python
(`backend/voice/tools.py`). The historically named `search_minutes` tool queries
the same mixed `knowledge-index` of meeting minutes **and official policies**;
`search_web` is Web IQ and is advertised to the model **only when a key is set**.

| Variable | Default | Purpose |
|---|---|---|
| `WEBIQ_API_KEY` | — | Enables the `search_web` tool in model mode. Passed to the container app as a **secret**, never as a plain environment variable. Unset leaves the web tool off entirely and the assistant answers from the internal minutes-and-policies corpus alone. |
| `WEBIQ_BASE_URL` | `https://api.microsoft.ai/v3` | Web IQ endpoint. |
| `WEBIQ_ALLOWED_DOMAINS` | *derived from `bingAllowedDomains`* | Comma-separated hosts that scope the search, e.g. `jse.co.za,mtn.com`. Web IQ has no server-side allow-list — its request model exposes no `site` field — so [`build_query()`](../backend/voice/tools.py) compiles these into `site:a OR site:b` operators on the query, which is the mechanism the Web IQ API documents. Same intent as `bingAllowedDomains`, and by default the **same sources**: leave this empty and `main.bicep` derives the bare hosts from `bingAllowedDomains`, so the two bindings cannot drift apart. Set it only to make model mode diverge deliberately. **Write bare hosts, not URLs and not `www.`** — see the two notes below. |
| `WEBIQ_LANGUAGE` | `en` | Result language hint. |
| `WEBIQ_REGION` | `ZA` | Result region hint. |
| `WEBIQ_USE_ENTRA` | — | Set to authenticate to Web IQ with Entra instead of an API key. |

> **`site:` matches a domain and every subdomain under it — it is not a hostname
> filter.** Two consequences, both measured against the live API:
>
> - **Do not prefix with `www.`.** `site:www.jse.co.za` excludes `senspdf.jse.co.za`,
>   which is where the JSE's SENS filings live — 9 of 10 results for a SENS query.
>   The bare host keeps them.
> - **Staging mirrors come in for free.** `site:sashares.co.za` returns hits on
>   `dev.sashares.co.za`, and Web IQ once ranked that mirror *first* for a share-price
>   question, quoting a two-month-old figure. `search_web` drops results whose host is
>   a strict subdomain of an allowed domain when the extra labels are all
>   non-production markers (`dev`, `staging`, `uat`, …).
>
> This is also where model mode is structurally weaker than agent mode — though not
> in *which* sources it may cite. Both bindings work from the same list: `main.bicep`
> derives this allow-list from `bingAllowedDomains` by stripping each entry to its
> bare host and de-duplicating (17 URLs → 13 hosts), so a source added for one
> binding is available to both. What does not survive the trip is **precision**:
> entries in `bingAllowedDomains` are **path-scoped and boosted** (`/investors`,
> `/mtn-shares`), and `site:` can express neither. Model mode reads whole hosts where
> agent mode reads curated, rank-adjusted sections — so `reuters.com` stands in for
> `reuters.com/world/africa`. Web-grounded answers are therefore not strictly
> comparable between bindings even though the source set matches.

Model mode also owns its own answer-shaping knobs, because there is no agent to
carry them:

| Variable | Default | Purpose |
|---|---|---|
| `REALTIME_MAX_TOKENS` | `1200` | Ceiling on a single spoken response. A voice answer that runs long is worse than one that stops early — the listener has already got the point. |
| `REALTIME_INTERIM_TEXTS` | — *(empty: off)* | Comma-separated canned lines the **platform** speaks while a tool call is in flight, e.g. `Let me check.,One moment.`. Empty disables the spoken filler. Model mode only. |
| `REALTIME_INTERIM_THRESHOLD_MS` | `300` | How long a tool call must run before an interim line is spoken. Deliberately below the tool floor (~280 ms web, ~714 ms minutes); at the SDK default of 2000 ms it never fired at all. Irrelevant while `REALTIME_INTERIM_TEXTS` is empty. |

> **The two fillers are independent.** The on-screen "thinking" indicator is a
> frontend affordance driven by `response_created` and is unaffected by these
> settings. `REALTIME_INTERIM_TEXTS` controls only the **spoken** cue.
>
> They do differ in what they can know. In model mode the on-screen cue names the
> tool exactly, because the tool call is ours; in agent mode no tool event reaches
> the client at all, so it predicts from the question and only commits once the
> turn is demonstrably slow — see
> [architecture.md](architecture.md#naming-the-wait).
>
> Turning the spoken cue off does not guarantee silence: with no platform line to
> fill the gap the model tends to improvise its own preamble, which is unbounded
> where the canned line was four words. See
> [voice-binding.md](voice-binding.md) and issue #77.

> **Note.** The binding is a **deployment-wide** setting — a client cannot choose it.
> To compare the two, set `VOICE_BINDING` and redeploy. That keeps the comparison
> honest: both modes run the same code path production runs, with nothing switched at
> the edge. See [voice-binding.md](voice-binding.md) for the measured trade-off.

## Conversation audit trail

Persists every turn's user question, tool results, and model answer. **Off by
default**; when off it costs nothing and deploys nothing. Full design, record
schema, and query examples in **[audit.md](audit.md)**.

> [!IMPORTANT]
> Enabling this records the substance of conversations. Confirm retention,
> access control, and any notice/consent obligation before switching it on in an
> environment real people use.

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_AUDIT` | `false` | Master switch. Also a **Bicep parameter** — `azd env set ENABLE_AUDIT true` provisions the Cosmos account, database, container and the app's data-plane role assignment. While `false`, none of it is created. |
| `AUDIT_SINK` | `cosmos` | Where records go. `cosmos` (production), `file` (JSONL at `audit-log.jsonl`, for local development), or `none` (accept and discard — used to isolate storage cost during latency A/B testing). If the chosen sink cannot be built, `AUDIT_SINK_FALLBACK` decides what happens. |
| `AUDIT_SINK_FALLBACK` | `error` | What to do when `AUDIT_SINK` cannot be built — an unset `AUDIT_COSMOS_ENDPOINT`, a Cosmos account that cannot be reached or authorised, or an unrecognised sink name. `error` raises `AuditSinkUnavailable` out of startup, so the app does not serve conversations it silently fails to record. `file` or `none` accept a **degraded** trail instead; both are ephemeral on Container Apps and are lost on the next revision. A degraded trail is reported by `stats()` and on `/health`. |
| `AUDIT_COSMOS_ENDPOINT` | — | Cosmos account endpoint. Set automatically by infra when audit is enabled; only set by hand for a BYO Cosmos account. |
| `AUDIT_COSMOS_DATABASE` | `audit` | Database name. |
| `AUDIT_COSMOS_CONTAINER` | `turns` | Container name. Partition key is `/sessionId`. |
| `AUDIT_RETENTION_DAYS` | `365` | Written to each record as a Cosmos-native `ttl`, so expiry needs no cleanup job. `0` or negative keeps records forever. Also a Bicep parameter. |
| `AUDIT_REDACT` | `true` | Mask obvious secret/PII patterns (bearer tokens, JWTs, connection-string secrets, card-like digit runs) in text, tool arguments and tool results before persisting. Defence in depth, **not** a compliance control — see [audit.md](audit.md#redaction). |
| `AUDIT_QUEUE_MAX` | `1000` | Bounded capture queue, so a sink outage costs capped memory. When full, records are **dropped and counted** rather than awaited — blocking here would stall the event loop carrying audio. |
| `AUDIT_TOOL_PAYLOAD_MAX_KB` | `32` | Cap on each captured tool payload. Retrieved passages are by far the largest field. |
| `AUDIT_RECONCILE_AGENT_TOOLS` | `true` | In **agent** binding, recover tool name/arguments/results from the Foundry conversation after the turn has finished. Never runs on the turn path. Set `false` to skip it — records are still written, with tool detail absent and `meta.toolsPending` left `true`. |

---

## Runtime tuning

| Variable | Default | Purpose |
|---|---|---|
| `DEVELOPER_MODE` | `false` | `true` exposes the settings panel, live transcript, and per-event debug logging, so settings can be changed and tried live while testing. `false` (production) auto-starts an avatar-only experience with settings locked. Set it on a deployment with `azd env set DEVELOPER_MODE true` — it is a Bicep parameter, so a value set imperatively with `az containerapp update` would be reverted by the next `azd provision`. It changes no pipeline default: the settings panel is pre-populated with the same values production uses. It does **not** expose `VOICE_BINDING`, which is deployment-wide. |
| `MEETING_CATALOG_TTL_S` | `900` | Seconds the backend caches the meeting catalogue it fetches from AI Search and injects at session start ([`backend/voice/catalog.py`](../backend/voice/catalog.py)). |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | — | Set by infra, but **nothing reads it yet**. The App Insights resource is deployed and the string is injected into the container, while the app emits no telemetry of its own — OpenTelemetry is deliberately not a dependency. Container stdout still reaches Log Analytics, which is operational storage and not an audit trail. When telemetry does ship, `meta.operationId` on each audit record is what joins a trace back to that turn's content — see [audit.md](audit.md). |
| `LOG_LEVEL` | `INFO` | Root logging level (`DEBUG`, `INFO`, `WARNING`, …). `DEVELOPER_MODE=true` already raises per-event detail; use this to quieten or deepen logs independently. |
| `HOST` | `0.0.0.0` | Interface the server binds to. Leave as-is in a container. |
| `PORT` | `3000` | Port the server listens on. The container image and `targetPort` in Bicep both assume `3000`; change both together or ingress breaks. |
| `VOICELIVE_API_VERSION` | `2026-01-01-preview` | Voice Live REST/WebSocket API version. Pin only to work around a regression — the code is written against this version. |

---

## Conversation & turn detection (UI session defaults)

Applied when `DEVELOPER_MODE=false`; each also has a matching control in developer mode.

| Variable | Default | Options / notes |
|---|---|---|
| `SR_MODEL` | `mai-transcribe-1` | `azure-speech` \| `mai-transcribe-1`. |
| `RECOGNITION_LANGUAGE` | `auto` | `auto` or a BCP-47 tag (ignored for MAI Transcribe, which auto-detects). |
| `USE_NOISE_SUPPRESSION` | `true` | Audio pre-processing. |
| `USE_ECHO_CANCELLATION` | `true` | Audio pre-processing. |
| `TURN_DETECTION_TYPE` | `azure_semantic_vad` | `server_vad` \| `azure_semantic_vad`. |
| `TURN_DETECTION_SILENCE_MS` | `500` | Silence (ms) before the user's turn ends. Applies to **every channel**, including in-call. With `EOU_DETECTION_TYPE` on, this is only the fallback for speech that trails off without a clean sentence boundary — lowering it is snappier but cuts off people who pause mid-sentence. |
| `ENABLE_BARGE_IN` | `true` | Let the user interrupt the avatar by speaking. Drives both client and server (`interrupt_response`) — keep in sync. |
| `REMOVE_FILLER_WORDS` | `true` | VAD ignores "um"/"uh"/… so small noises don't cancel a reply. |
| `EOU_DETECTION_TYPE` | `semantic_detection_v1` | `none` \| `semantic_detection_v1` \| `semantic_detection_v1_multilingual`. |
| `ENABLE_PROACTIVE` | `false` | Let the agent speak first / interject. |
| `PROACTIVE_GREETING` | — | Verbatim opening line when `ENABLE_PROACTIVE=true`. The one place for a client-specific greeting. |

---

## Voice

| Variable | Default | Options / notes |
|---|---|---|
| `VOICE_TYPE` | `standard` | `standard` \| `custom` \| `personal`. |
| `VOICE_SPEED` | `100` | Speech rate %, range 50–150 (step 5). |
| `VOICE_TEMPERATURE` | `0.9` | DragonHD / personal voices only, range 0.0–1.0. |

(The voice **name** is set with `VOICELIVE_VOICE` at the top; both feed the same UI field.)

---

## Avatar — model & identity

The avatar **model** (which face renders) and the **display name** (the branding
label) are separate knobs, so you can run e.g. the "Lisa" avatar but brand it
"Nuru". They are not *independent* by default, though: leave the name unset and it
is derived from the model you selected, so a deployment running the `Simone` photo
avatar is called "Simone" everywhere without configuring anything.

| Variable | Default | Purpose |
|---|---|---|
| `AVATAR_ENABLED` | `true` | Show the avatar at all. |
| `AVATAR_OUTPUT_MODE` | `webrtc` | `webrtc` \| `websocket`. |
| `AVATAR_TYPE` | `standard-photo` | **Canonical selector:** `standard-video`, `standard-photo`, `custom-video`, or `custom-photo`. |
| `AVATAR_MODEL` | `Simone` | **Canonical model id:** a standard catalogue name or the custom model provisioned in your Speech resource. |
| `AVATAR_BACKGROUND_IMAGE_URL` | — | Optional background image behind the avatar. |
| `ENABLE_AVATAR_SPEAKING_STYLE` | `false` | Opt into the grayscale idle / full-color speaking treatment with a yellow speaking tint. Disabled preserves the avatar's original appearance. |
| **`AVATAR_DISPLAY_NAME`** | *(the avatar model's name)* | **The branding knob.** Sets the bold name on the avatar stage, the name the assistant calls itself, the Teams bot name, the wake phrase and the Teams package name. Purely cosmetic — does **not** select the avatar model. **Unset it falls back to the friendly name of the *active* avatar model** (`Simone`, or `Lisa-casual-sitting` → `Lisa`), so every surface agrees without setting anything; `Avatar` only if that is empty too. Set it to override, e.g. run the `Lisa` avatar but call her `Nuru`. |
| `AVATAR_TAGLINE` | `Your Digital Assistant` | Italic tagline under the name in the stage identity lockup. Company-agnostic by default; set a branded value (e.g. `Your MTN Digital Assistant`) per deployment. Empty hides the tagline line. |

> **One name, every surface.** The stage label, the assistant's own answers ("I'm
> Simone"), the Teams bot welcome, the Teams package name, the meeting-roster name
> and the wake phrase all resolve the name with the same rule
> ([`backend/avatar_identity.py`](../backend/avatar_identity.py)):
> `AVATAR_DISPLAY_NAME` → the active avatar model's friendly name → `Avatar`.
> Pinned by `uv run python tests/test_avatar_identity.py`.

### Selecting an avatar

For a new deployment, set only the canonical pair:

```dotenv
AVATAR_TYPE=custom-photo
AVATAR_MODEL=Nuru
AVATAR_DISPLAY_NAME=Nuru
```

The application derives the UI modality from this pair. You do **not** copy the
model into several variables.

> **Renaming after a deploy: use the script.** The name lives on three surfaces and
> all three have to move together, so there is one command for it:
>
> ```powershell
> uv run python scripts/rename_avatar.py Nuru
> uv run python scripts/rename_avatar.py Nuru --check-only   # verify, change nothing
> ```
>
> It writes the **azd environment** (so a later `azd up` cannot revert the rename),
> the **container app** (stage name, tagline, wake phrase), and the **Foundry agent**
> (what she actually *says*) — then reads each surface back independently and exits
> non-zero if they disagree. See [`scripts/README.md`](../scripts/README.md).
>
> `AVATAR_DISPLAY_NAME` is branding; `AVATAR_MODEL` selects the Speech character.
> To change the character explicitly:
>
> ```powershell
> uv run python scripts/rename_avatar.py Nuru --model Elise
> ```
>
> The script validates standard catalogue models locally. Custom models are checked
> by Voice Live against your Speech resource.
>
> The third surface is the one that catches people out: the assistant's *spoken*
> persona is rendered into the agent's prompt and **frozen into an agent version at
> push time**, so it does not follow an environment change. Skip it and the stage
> reads "Nuru" while she introduces herself as "Simone".
>
> No `azd up` is needed — the script uses `az containerapp update --set-env-vars`,
> which merges into the live revision in about a minute. Prefer that to a bare
> `azd provision`, which can revert the container app to the placeholder image.
> The equivalent by hand, if you want to see what it does:
>
> ```powershell
> azd env set AVATAR_DISPLAY_NAME Nuru
> az containerapp update -g <rg> -n <app> --set-env-vars AVATAR_DISPLAY_NAME=Nuru
> uv run python scripts/setup_foundry_agent.py   # re-brands the agent prompt
> ```
>
> `azd up` re-runs that last script for you when Avatar Forge created the Foundry
> account (the greenfield default). Run it by hand after `azd deploy`, or when you
> brought your own Foundry account — neither triggers the postprovision hook.
>
> The final check cannot be automated: open the app and ask *"what is your name?"*

> **Default vs. shipped default.** The Default column is the backend's fallback when
> a variable is **unset**. The values Avatar Forge actually ships with are different:
> both [`.env.example`](../.env.example) and the `azd` parameters
> ([`infra/main.parameters.json`](../infra/main.parameters.json)) set
> `AVATAR_TYPE=standard-photo` and `AVATAR_MODEL=Simone`, so a stock local run or
> deploy renders the **Simone photo avatar**, not `Lisa-casual-sitting`.


---

## Avatar UX (additive frontend features)

Delivered to the browser via `/api/config` and applied in both normal and developer
mode. Captions are **off** by default; suggested prompts, the on-stage composer
(web), and the stop button are **on**.

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_CAPTIONS` | `false` | Show the live caption band under the avatar (mirrors the transcript stream — no extra model calls). |
| `CAPTIONS_SHOW_USER` | `false` | Also briefly show the user's last utterance in the caption band (only when `ENABLE_CAPTIONS=true`). |
| `ENABLE_SUGGESTED_PROMPTS` | `true` | Show the first-load onboarding hint + 2–3 tappable example chips. |
| `ONBOARDING_HINT` | *(derived)* | The one-line hint above the chips. By default the **frontend** derives it from the *effective* composer state: `Tap the mic or type to ask me anything` when the composer is shown, otherwise `Tap the mic to ask me anything` (e.g. inside Teams). Set explicitly to override everywhere. |
| `SUGGESTED_PROMPTS` | *(3 generic)* | Pipe-separated example questions, e.g. `What can you help me with?\|Tell me about your services\|How do I get started?`. |
| `ENABLE_TEXT_INPUT` | `true` | **Host-aware.** Shows the on-stage text composer on the standalone **web** app; **always hidden inside the Microsoft Teams client** (the bot chat tab has Teams' native compose box; the avatar tab is voice-first — type via the chat tab, or in a call via the meeting chat with an `@mention`). This var is an optional **web-only** override (set `false` to hide on web too) and can **never** force the composer on in Teams. Developer mode keeps its own text input. |
| `ENABLE_STOP_BUTTON` | `true` | Show a small Stop control next to the mic so the user can cut the avatar off mid-answer. Always visible while the avatar is on screen (greyed when idle, red while speaking); reuses the barge-in interrupt path. Teams bot chat is text-only and unaffected. |

---

## Teams app package *(build only)*

Read by [`teams/build_package.py`](../teams/build_package.py) when it templates
`manifest.template.json` into an uploadable zip. Precedence is **command-line flag →
process env / `.env` → selected azd environment**, so a build run after `azd up`
inherits that deployment's host and branding while any local value still overrides
it — see [`teams/README.md`](../teams/README.md).

| Variable | Flag | Default | Purpose |
|---|---|---|---|
| `TEAMS_HOSTNAME` | `--hostname` | *(derived from `SERVICE_APP_URI`)* | Host of the deployed app (`<name>.azurecontainerapps.io`, **no scheme, path or port** — Teams' `validDomains` cannot hold them, so a value carrying them is rejected). Becomes every URL in the manifest. When unset, taken from the selected azd environment's `SERVICE_APP_URI` with the scheme stripped, so the no-argument build targets your deployment. |
| `TEAMS_APP_NAME` | `--name` | *(the resolved persona name)* | Short name shown in Teams — the assistant's persona name. Falls back to `AVATAR_DISPLAY_NAME` and, when that is unset, to the active avatar model's friendly name. Those are read from the azd environment too, so a package built against a deployment calling itself "Simone" is named Simone without setting this. |
| `TEAMS_APP_FULL_NAME` | `--full-name` | `<name> — Azure Voice Live Avatar` | Long name shown on the app's detail page. |
| `TEAMS_APP_VERSION` | `--version` | `1.0.0` | Manifest version. Teams refuses a re-upload unless this increases. |
| `TEAMS_APP_ID` | `--app-id` | *(uuid5 of the hostname)* | Manifest app id (GUID). Derived deterministically from the hostname when unset, so rebuilds match. |
| `TEAMS_BOT_ID` | `--bot-id` | — | Bot app id to embed. **Omit to build a tab-only package** — Teams rejects an upload whose bot is not registered in your tenant. |
| `TEAMS_ENABLE_CALLING` | `--enable-calling` | `false` | Sets `supportsCalling: true`. Pair with the **calling** bot's id (`MEETING_BOT_APP_ID`). |
| `TEAMS_ENABLE_COMPANION` | `--enable-companion` | `false` | Adds the in-meeting side panel / stage tabs. Off keeps the package identical to the tab-only shape. |

## Teams in-call avatar (channels C and D, issue #27)

Opt-in. The avatar joins a Teams **meeting**, hears every participant, and answers
spoken questions aloud with a lip-synced camera tile, using the same Voice Live +
Foundry pipeline. **Off unless enabled** — every `/api/acs/*` endpoint returns 503 and
the bridge never runs, so a deploy without it is unchanged. Non-recording by design.

The `ACS_*` prefix is historical: these settings govern the in-call media bridge
regardless of which transport feeds it (the Graph media bot on `/ws/acs/audio`, or the
browser joiner on `/ws/acs/browser`). `MEETING_BOT_ENABLED=true` is enough on its own —
it serves the media bot **without** provisioning an ACS resource. See
[`docs/channels/d-in-call-media-bot.md`](channels/d-in-call-media-bot.md).

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_ACS` | `false` | **azd/infra only, and the one flag that provisions ACS.** When `true`, deploys the conditional `communicationServices.bicep` and passes `ACS_ENDPOINT` to the container. **Required by channel C** (the browser guest joiner); channels A–B and D do not need it. Set for you by `DEPLOY_PROFILE=in-call-browser`. |
| `ACS_DATA_LOCATION` | `United States` | **azd/infra only.** Data residency geography for the ACS resource. |
| `ACS_ENDPOINT` | — | ACS resource endpoint (`https://<acs>.communication.azure.com/`). Set automatically by infra when `ENABLE_ACS=true`. Auth via the container's managed identity (needs a role on the ACS resource). |
| `ACS_CONNECTION_STRING` | — | Alternative to `ACS_ENDPOINT` + managed identity (includes endpoint + key). Takes precedence when set; simplest for local/dev. |
| `ACS_CALLBACK_BASE_URL` | — | Public HTTPS base URL ACS uses for call-event callbacks and the media WebSocket. Defaults to the app's own external ingress; set for local dev behind a Dev Tunnel/ngrok. |
| `ACS_AUDIO_SAMPLE_RATE` | `24000` | PCM sample rate (Hz) for the ACS↔Voice Live bridge. `24000` matches Voice Live (no resample); `16000` also valid. |
| `ACS_WAKE_PHRASES` | *(derived from the persona name)* | Comma-separated, case-insensitive phrases that invoke a spoken answer (turn-taking, so it never talks over the room). Defaults to `hey <name>,<name>` lower-cased, where `<name>` is the resolved persona name — so you say "hey Simone" to the avatar shown as Simone. Set this only to override. |
| `ACS_REQUIRE_WAKE_PHRASE` | `true` | Require a wake phrase before answering, so she stays quiet unless addressed. Set `false` in a 1:1 test meeting to answer every turn. |
| `ACS_IDLE_TIMEOUT_S` | `0` | Leave the call after N seconds of inactivity (`0` disables). |
| `ACS_FOLLOWUP_WINDOW_S` | `90` | Seconds after an answer during which a follow-up needs **no** wake phrase, so a real back-and-forth doesn't require saying the name every turn. Raised from 30s after live testing: questions plainly aimed at her landed 35-40s after the previous answer and were met with silence, which reads as being ignored. |

> [!NOTE]
> **The `ACS_` prefix spans two unrelated things.** Read it as two groups:
>
> - **The ACS *resource*** — `ENABLE_ACS`, `ACS_DATA_LOCATION`, `ACS_ENDPOINT`,
>   `ACS_CONNECTION_STRING`, `ACS_CALLBACK_BASE_URL`. These provision and address the
>   ACS resource, and they are needed by **channel C only** — the browser guest
>   joiner at `/acs-join.html`, where a browser tab joins a meeting as an anonymous
>   guest. Channels A–B and D never touch them. Choosing `DEPLOY_PROFILE=in-call-browser`
>   sets `ENABLE_ACS` for you, so you should not normally set it by hand. See
>   [`channels/c-in-call-headless.md`](channels/c-in-call-headless.md#deploying-it).
> - **The audio *bridge*** — `ACS_AUDIO_SAMPLE_RATE`, `ACS_WAKE_PHRASES`,
>   `ACS_REQUIRE_WAKE_PHRASE`, `ACS_IDLE_TIMEOUT_S`, `ACS_FOLLOWUP_WINDOW_S`. These
>   are read by `backend/acs/bridge.py` and **do apply to channel D**, which speaks
>   the same wire protocol over `/ws/acs/audio`. The turn-taking ones are the knobs
>   you will actually tune in a live meeting.
>
> Channel D joins through Graph calling on the Windows host and never touches the ACS
> resource; the `acs` in the paths is the protocol's name, not a dependency.

### The avatar's face in the meeting

Both in-call legs default to **audio only**. Turning the face on is one flag per leg, and
the frame geometry must match on both sides of the bridge — the .NET bot silently drops
frames whose dimensions differ from what it negotiated and shows its placeholder instead.
The matching values on the VM are `Bot__EnableVideo`, `Bot__VideoWidth/Height/Fps`.

| Variable | Default | Purpose |
|---|---|---|
| `MEETING_BOT_VIDEO_ENABLED` | `false` | Forward decoded avatar video to the .NET media bot so it renders a lip-synced camera tile. Must agree with `Bot__EnableVideo` on the VM. |
| `MEETING_BOT_VIDEO_WIDTH` | `640` | Outbound frame width. Must match `Bot__VideoWidth`. |
| `MEETING_BOT_VIDEO_HEIGHT` | `360` | Outbound frame height. Must match `Bot__VideoHeight`. |
| `MEETING_BOT_VIDEO_FPS` | `15` | Outbound frame rate. Must match `Bot__VideoFps`. |
| `ACS_AVATAR_VIDEO_ENABLED` | `false` | Ask Voice Live to synthesise avatar **video** for in-call sessions. Required for either leg to have a face. Set for you by `DEPLOY_PROFILE=in-call-browser`. |
| `BROWSER_JOIN_VIDEO_ENABLED` | `false` | Browser-joiner leg only: decode the avatar in the browser and publish it as the ACS video tile. Setting it `false` is the safe rollback — the voice keeps working. |

### Windows media host *(azd/infra only)*

Provisioned by `azd up` when `DEPLOY_PROFILE=in-call` (or `DEPLOY_MEETING_BOT_HOST=true`)
**and** all three required inputs are present. If any is missing the host is skipped
rather than failing partway through provisioning — `scripts/preflight.py` blocks the
deploy first and tells you which one.

| Variable | Default | Purpose |
|---|---|---|
| `MEETING_BOT_ENABLED` | `false` | Serves the media-bot bridge at `/ws/acs/audio` on the container app. Set by `DEPLOY_PROFILE=in-call`, and reset to `false` by any other profile. Independent of `ENABLE_ACS`. |
| `DEPLOY_MEETING_BOT_HOST` | `false` | Provisions `infra/modules/meetingBotHost.bicep` — Windows VM, public IP, NSG, and the Azure Bot registration with the Teams **calling** webhook. Set by `DEPLOY_PROFILE=in-call`, and reset to `false` by any other profile, so switching away stops the VM being deployed. |
| `MEETING_BOT_APP_ID` | — | **Required.** Entra app client id of the calling bot. **Must differ from `BOT_APP_ID`** — an Entra app can back only one Azure Bot resource; reusing it fails with `MsaAppId is already in use`. |
| `MEETING_BOT_APP_TENANT_ID` | *(deployment tenant)* | Tenant of that app registration. |
| `MEETING_BOT_DNS_LABEL` | — | **Required.** Globally-unique DNS label; becomes `<label>.<region>.cloudapp.azure.com` and must resolve for the TLS certificate. Preflight checks availability. |
| `MEETING_BOT_ADMIN_PASSWORD` | — | **Required.** Local administrator password for the Windows host (12–123 chars, 3 of 4 character classes). |
| `MEETING_BOT_VM_SIZE` | `Standard_D4s_v5` | 4 vCPU. This is the size proven to run the Real-Time Media Platform — a 2-vCPU host was tried and had to be resized. ~$283/month; lowering it is a false economy. |
| `MEETING_BOT_ICON_URL` | *(empty)* | Public URL of the bot icon shown in Teams. |
| `MEETING_BOT_ADMIN_USERNAME` | `avatarbot` | Local administrator account created on the Windows host. You need it to RDP in for the `setup-host.ps1` stages. |

### On the Windows host itself

These are **not** azd variables — `setup-host.ps1 -Stage Run` writes them as machine
environment variables on the VM, and the .NET bot binds them to its `Bot` options.

| Variable | Purpose |
|---|---|
| `Bot__AppId` / `Bot__TenantId` | The calling bot's Entra app registration (`-BotAppId` / `-BotTenantId`). |
| `BOT_CLIENT_SECRET` | That app's client secret (`-BotSecret`). Never put it in `appsettings.json`; the bot reads it from the environment only. |
| `Bot__ServiceFqdn` / `Bot__CertificateThumbprint` | The host FQDN and the trusted TLS certificate matching it. |
| `Bot__BridgeWebSocketUrl` | `wss://<container-app>/ws/acs/audio` — where the bot streams meeting audio to the Python brain. |
| `Bot__EnableVideo` / `Bot__VideoWidth` / `Bot__VideoHeight` / `Bot__VideoFps` | Must match the `MEETING_BOT_VIDEO_*` values above. |
