# Configuration reference

Every environment variable Avatar Forge reads, grouped by concern. This is the
**single source of truth** — [`.env.example`](../.env.example) is the copy-and-fill
template that mirrors it.

Copy the template, then fill in the required values:

```bash
cp .env.example .env
```

Conventions:

- **Required** vars must be set for the runtime backend to start and answer.
- Booleans accept `true`/`false` (also `1`/`0`, `yes`/`no`, `on`/`off`).
- Vars marked *(provisioning only)* are read by `scripts/*.py` or `azd`, **not**
  by the running server — a brownfield (BYO) deploy can leave them unset.

---

## Required — Voice Live / Foundry (runtime)

| Variable | Default | Purpose |
|---|---|---|
| `AZURE_VOICELIVE_ENDPOINT` | — | **Required.** Your Foundry / AI Services endpoint, e.g. `https://<resource>.services.ai.azure.com/`. |
| `AGENT_NAME` | `AvatarAgent` | **Required.** Name of the Foundry agent the session binds to (created via [`scripts/setup_foundry_agent.py`](../scripts/setup_foundry_agent.py)). |
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

---

## Foundry agent provisioning *(provisioning only)*

Read by [`scripts/setup_foundry_agent.py`](../scripts/setup_foundry_agent.py) at
agent-creation time; the runtime backend never talks to Bing directly.

| Variable | Default | Purpose |
|---|---|---|
| `BING_CONNECTION_NAME` | `groundingwithbingcustquraml` | Foundry connection for Grounding with Bing Custom Search (the agent's only external tool). |
| `BING_CUSTOM_CONFIG_NAME` | `mtn-avatar-search` | Bing Custom Search configuration name — the curated domain allow-list the web tool is restricted to. |
| `AGENT_MODEL` | `gpt-5.4` | Foundry model deployment the agent runs on. Recommended: `gpt-5.4` + `AGENT_REASONING_EFFORT=none` (best tool routing; 30/30 on the harness). `gpt-5.4-mini` is a cheaper fallback; `gpt-4.1-mini` is the documented baseline. See [architecture.md](architecture.md#tool-calling-accuracy). |
| `AGENT_REASONING_EFFORT` | `none` | Reasoning effort. **Model-dependent:** `gpt-4.x`/`gpt-4o` reject it (leave **unset** — they 400, manifesting as a silently non-speaking avatar); `gpt-5.x` accept `none\|low\|medium\|high\|xhigh`; o-series accept `low\|medium\|high`. For voice latency the validated value is `none` (real reasoning adds 4–5s to first token). The script also selects the prompt variant from this. |
| `AI_SEARCH_TOP_K` | `8` | Chunks pulled from the meeting-minutes index per turn. |
| `BING_COUNT` | `8` | Snippets returned from the Bing Custom Search allow-list per turn. |
| `AGENT_ID` | — | Optional explicit agent id; when empty the agent is resolved by `AGENT_NAME`. |

---

## AI Search & index build *(provisioning only)*

Read by [`scripts/setup_aisearch_index.py`](../scripts/setup_aisearch_index.py) and
[`scripts/test_aisearch_query.py`](../scripts/test_aisearch_query.py).

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

---

## Greenfield model deployment *(azd provision only)*

Read **only** when `azd` creates a new Foundry model deployment
([`infra/main.bicep`](../infra/main.bicep)). Unused for a brownfield (BYO Foundry)
deploy. Keep `MODEL_DEPLOYMENT_NAME` aligned with `AGENT_MODEL`, and `MODEL_VERSION`
matched to `MODEL_NAME` (an invalid pair fails the deployment).

| Variable | Default | Purpose |
|---|---|---|
| `MODEL_NAME` | `gpt-5.4` | OpenAI model to deploy. |
| `MODEL_VERSION` | `2026-03-05` | Model version (must match `MODEL_NAME`). |
| `MODEL_DEPLOYMENT_NAME` | `gpt-5.4` | Deployment name (the agent binds to it). |
| `MODEL_SKU_NAME` | `GlobalStandard` | Deployment SKU. |
| `MODEL_CAPACITY` | `50` | TPM (thousands) capacity. |

---

## Runtime tuning

| Variable | Default | Purpose |
|---|---|---|
| `DEVELOPER_MODE` | `false` | `true` exposes the settings panel, live transcript, and per-event debug logging. `false` (production) auto-starts an avatar-only experience. |
| `MEETING_CATALOG_TTL_S` | `900` | Seconds the backend caches the meeting catalogue it fetches from AI Search and injects at session start ([`backend/voice/catalog.py`](../backend/voice/catalog.py)). |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | — | App Insights connection string for telemetry. |

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
| `TURN_DETECTION_SILENCE_MS` | `500` | Silence (ms) before the user's turn ends. Lower = snappier. |
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
label) are deliberately separate knobs, so you can run e.g. the "Lisa" avatar but
brand it "Nuru".

| Variable | Default | Purpose |
|---|---|---|
| `AVATAR_ENABLED` | `true` | Show the avatar at all. |
| `AVATAR_OUTPUT_MODE` | `webrtc` | `webrtc` \| `websocket`. |
| `IS_PHOTO_AVATAR` | `false` | Render a photo-realistic avatar (`vasa-1`) instead of the standard video avatar. |
| `IS_CUSTOM_AVATAR` | `false` | Use an avatar trained in your own Speech resource. A **modifier, not a separate type**: it combines with `IS_PHOTO_AVATAR` (see the combinations below). |
| `AVATAR_NAME` | `Lisa-casual-sitting` | Standard avatar character (used when both flags are false). |
| `CUSTOM_AVATAR_NAME` | — | Custom avatar **model** id; free-text, must match a model provisioned in your Speech resource. Used whenever `IS_CUSTOM_AVATAR=true` (custom video **or** custom photo); case is preserved and no style suffix is parsed. Pointing at a non-existent model breaks rendering. |
| `PHOTO_AVATAR_NAME` | `Anika` | Prebuilt photo-realistic character. Used when `IS_PHOTO_AVATAR=true` and no custom name applies. |
| `AVATAR_BACKGROUND_IMAGE_URL` | — | Optional background image behind the avatar. |
| **`AVATAR_DISPLAY_NAME`** | — | **The single branding knob.** Sets the bold name shown top-left on the avatar stage **and** names the Teams bot. Purely cosmetic — does **not** select the avatar model. Unset: the bot uses `Avatar`; the stage label derives from the selected avatar model. |
| `AVATAR_TAGLINE` | `Your Digital Assistant` | Italic tagline under the name in the stage identity lockup. Company-agnostic by default; set a branded value (e.g. `Your MTN Digital Assistant`) per deployment. Empty hides the tagline line. |

> **Default vs. shipped default.** The Default column is the backend's fallback when
> a variable is **unset**. The values Avatar Forge actually ships with are different:
> both [`.env.example`](../.env.example) and the `azd` parameters
> ([`infra/main.parameters.json`](../infra/main.parameters.json)) set
> `IS_PHOTO_AVATAR=true` and `PHOTO_AVATAR_NAME=Simone`, so a stock local run or
> deploy renders the **Simone photo avatar**, not `Lisa-casual-sitting`.

### Avatar flag combinations

`IS_PHOTO_AVATAR` and `IS_CUSTOM_AVATAR` are independent, so all four combinations are valid.

| `IS_PHOTO_AVATAR` | `IS_CUSTOM_AVATAR` | Renders | Name taken from |
|---|---|---|---|
| `false` | `false` | Standard video avatar | `AVATAR_NAME` |
| `false` | `true` | Custom video avatar | `CUSTOM_AVATAR_NAME` |
| `true` | `false` | Prebuilt photo avatar (`vasa-1`) | `PHOTO_AVATAR_NAME` |
| `true` | `true` | Custom photo avatar (`vasa-1` with `customized=true`) | `CUSTOM_AVATAR_NAME` |

When `IS_CUSTOM_AVATAR=true` but `CUSTOM_AVATAR_NAME` is empty, the name falls back
to `PHOTO_AVATAR_NAME` (photo mode) or `AVATAR_NAME` (video mode) instead of sending
an empty character. That fallback only prevents a blank avatar; it does not select
your custom model, so always set `CUSTOM_AVATAR_NAME` when you enable the flag.

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

## Teams conversational bot (Phase 2a, issue #53)

Only needed when hosting the Teams bot. The bot reuses the same Foundry agent
(`AGENT_NAME` / `PROJECT_ENDPOINT`) for answers. Bot identity comes from the Azure
Bot registration + its Entra app — see [`teams/README.md`](../teams/README.md) for
the portal/CLI steps. If `TEAMS_BOT_ID` / `BOT_APP_ID` is unset, the bot infra is
skipped and the deploy behaves exactly like Phase 1 (tab-only).

| Variable | Default | Purpose |
|---|---|---|
| `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID` | — | Bot app client id (Microsoft 365 Agents SDK convention). |
| `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET` | — | Bot app client secret (stored as an ACA secret by infra). |
| `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID` | — | Tenant id of the bot's Entra app. |
| `TEAMS_BOT_ID` | — | The bot's Microsoft App ID (GUID). Also fills `{{BOT_ID}}` in the manifest via [`teams/build_package.py`](../teams/build_package.py). |
| `TEAMS_APP_ID` | — | The Teams app (manifest) id; used to build deep links from the bot back into the personal tab. Match the id used to build the package. |
| `TEAMS_TAB_ENTITY_ID` | `avatarForgeHome` | The static-tab entity id the bot deep-links to. |
| `BOT_RUN_TIMEOUT_S` | `60` | Max seconds a grounded Foundry run executes in the background before a "took too long" reply. Answers are delivered as a proactive message (ack-then-background-run), so this is **not** bound by the Teams ~15s turn window. |

## Teams in-call audio participant (Phase 2b, issue #27)

Opt-in. The avatar joins a Teams **meeting** as an audio participant via Azure
Communication Services (ACS) Call Automation and answers spoken questions aloud using
the same Voice Live + Foundry pipeline. **Off unless ACS is configured** — every
`/api/acs/*` endpoint returns 503 and the bridge never runs, so a deploy without it is
unchanged. Audio-only and non-recording by design. See
[`teams/README.md`](../teams/README.md#phase-2b--in-call-audio-participant-issue-27).

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_ACS` | `false` | **azd/infra only.** When `true`, provisions the conditional `communicationServices.bicep` and passes `ACS_ENDPOINT` to the container. |
| `ACS_DATA_LOCATION` | `United States` | **azd/infra only.** Data residency geography for the ACS resource. |
| `ACS_ENDPOINT` | — | ACS resource endpoint (`https://<acs>.communication.azure.com/`). Set automatically by infra; enables Phase 2b. Auth via the container's managed identity (needs a role on the ACS resource). |
| `ACS_CONNECTION_STRING` | — | Alternative to `ACS_ENDPOINT` + managed identity (includes endpoint + key). Takes precedence when set; simplest for local/dev. Enables Phase 2b. |
| `ACS_CALLBACK_BASE_URL` | — | Public HTTPS base URL ACS uses for call-event callbacks and the media WebSocket. Defaults to the app's own external ingress; set for local dev behind a Dev Tunnel/ngrok. |
| `ACS_AUDIO_SAMPLE_RATE` | `24000` | PCM sample rate (Hz) for the ACS↔Voice Live bridge. `24000` matches Voice Live (no resample); `16000` also valid. |
| `ACS_WAKE_PHRASES` | `hey nuru,nuru` | Comma-separated, case-insensitive phrases that invoke a spoken answer (turn-taking, so she never talks over the room). |
| `ACS_REQUIRE_WAKE_PHRASE` | `true` | Require a wake phrase before answering (half-duplex). Set `false` in a 1:1 test meeting to answer every turn. |
| `ACS_IDLE_TIMEOUT_S` | `0` | Leave the call after N seconds of inactivity (`0` disables). |
