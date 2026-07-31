# Configuration reference

Every environment variable Avatar Forge reads, grouped by concern. This is the
**single source of truth** — [`.env.example`](../.env.example) is the copy-and-fill
template that mirrors it.

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
| `DEPLOY_PROFILE` | *(empty)* | `web` · `teams-tab` · `teams-chat` · `in-call`. Selects which channel deploys, and drives the numbered step plan `scripts/preflight.py` prints. Empty keeps the pre-profile behaviour (explicit flags only). |
| `PREFLIGHT_SKIP` | `false` | `true` bypasses the preprovision preflight gate. An escape hatch — nobody should be stuck behind their own tooling. |

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
| `DEPLOY_BING_GROUNDING` | `true` | **Infra-only.** `azd up` deploys the whole web tool — the Bing account, the curated site allow-list and the Foundry connection — and sets the two variables below automatically. Set `false` to skip it (it is a billable resource); the agent then answers from AI Search alone. Only takes effect on a greenfield deploy (there must be a Foundry project to attach the connection to). |
| `BING_SKU_NAME` | `G2` | **Optional, infra-only.** Bing pricing tier when `DEPLOY_BING_GROUNDING=true`. `G2` is the tier this project has run on; `G1` is the lower tier. |
| `BING_CONNECTION_NAME` | *(unset — web tool disabled)* | **Optional.** Foundry connection for Grounding with Bing Custom Search (the agent's only external tool). Set for you when `DEPLOY_BING_GROUNDING=true`; otherwise name an existing connection. Leave unset for a search-only agent; naming a connection that doesn't exist skips the tool with a warning rather than failing. |
| `BING_CUSTOM_CONFIG_NAME` | *(unset — web tool disabled)* | **Optional.** Bing Custom Search configuration name — the curated domain allow-list the web tool is restricted to. Set for you when `DEPLOY_BING_GROUNDING=true`; otherwise required alongside `BING_CONNECTION_NAME`. |
| `AGENT_MODEL` | `gpt-5.4` | Foundry model deployment the agent runs on. Recommended: `gpt-5.4` + `AGENT_REASONING_EFFORT=none` (best tool routing; 30/30 on the harness). `gpt-5.4-mini` is a cheaper fallback; `gpt-4.1-mini` is the documented baseline. See [architecture.md](architecture.md#tool-calling-accuracy). |
| `AGENT_REASONING_EFFORT` | `none` | Reasoning effort. **Model-dependent:** `gpt-4.x`/`gpt-4o` reject it (leave **unset** — they 400, manifesting as a silently non-speaking avatar); `gpt-5.x` accept `none\|low\|medium\|high\|xhigh`; o-series accept `low\|medium\|high`. For voice latency the validated value is `none` (real reasoning adds 4–5s to first token). The script also selects the prompt variant from this. |
| `AI_SEARCH_TOP_K` | `8` | Chunks pulled from the meeting-minutes index per turn. |
| `BING_COUNT` | `8` | Snippets returned from the Bing Custom Search allow-list per turn. |
| `AGENT_ID` | — | Optional explicit agent id; when empty the agent is resolved by `AGENT_NAME`. |

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
| `IS_PHOTO_AVATAR` | `false` | Render a photo-realistic avatar (`vasa-1`) instead of the standard video avatar. |
| `IS_CUSTOM_AVATAR` | `false` | Use an avatar trained in your own Speech resource. A **modifier, not a separate type**: it combines with `IS_PHOTO_AVATAR` (see the combinations below). |
| `AVATAR_NAME` | `Lisa-casual-sitting` | Standard avatar character (used when both flags are false). |
| `CUSTOM_AVATAR_NAME` | — | Custom avatar **model** id; free-text, must match a model provisioned in your Speech resource. Used whenever `IS_CUSTOM_AVATAR=true` (custom video **or** custom photo); case is preserved and no style suffix is parsed. Pointing at a non-existent model breaks rendering. |
| `PHOTO_AVATAR_NAME` | `Anika` | Prebuilt photo-realistic character. Used when `IS_PHOTO_AVATAR=true` and no custom name applies. |
| `AVATAR_BACKGROUND_IMAGE_URL` | — | Optional background image behind the avatar. |
| **`AVATAR_DISPLAY_NAME`** | *(the avatar model's name)* | **The branding knob.** Sets the bold name on the avatar stage, the name the assistant calls itself, the Teams bot name, the wake phrase and the Teams package name. Purely cosmetic — does **not** select the avatar model. **Unset it falls back to the friendly name of the *active* avatar model** (`Simone`, or `Lisa-casual-sitting` → `Lisa`), so every surface agrees without setting anything; `Avatar` only if that is empty too. Set it to override, e.g. run the `Lisa` avatar but call her `Nuru`. |
| `AVATAR_TAGLINE` | `Your Digital Assistant` | Italic tagline under the name in the stage identity lockup. Company-agnostic by default; set a branded value (e.g. `Your MTN Digital Assistant`) per deployment. Empty hides the tagline line. |

> **One name, every surface.** The stage label, the assistant's own answers ("I'm
> Simone"), the Teams bot welcome, the Teams package name, the meeting-roster name
> and the wake phrase all resolve the name with the same rule
> ([`backend/avatar_identity.py`](../backend/avatar_identity.py)):
> `AVATAR_DISPLAY_NAME` → the active avatar model's friendly name → `Avatar`.
> Pinned by `uv run python scripts/test_avatar_identity.py`.

> **Renaming after a deploy needs one extra step.** The stage, bot, package and
> wake phrase pick the new name up from the environment, but the assistant's
> *spoken* persona is baked into the Foundry agent's prompt when the agent is
> built — so a rename that skips that step leaves her still introducing herself by
> the old name:
>
> ```powershell
> azd env set AVATAR_DISPLAY_NAME Nuru
> azd up
> uv run python scripts/setup_foundry_agent.py   # re-brands the agent prompt
> ```
>
> `azd up` re-runs that script for you when Avatar Forge created the Foundry
> account (the greenfield default). Run it by hand after `azd deploy`, or when you
> brought your own Foundry account — neither triggers the postprovision hook.

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

## Teams conversational bot (channel C, issue #53)

Only needed when hosting the Teams bot. The bot reuses the same Foundry agent
(`AGENT_NAME` / `PROJECT_ENDPOINT`) for answers. Bot identity comes from the Azure
Bot registration + its Entra app — see [`teams/README.md`](../teams/README.md) for
the portal/CLI steps. If `TEAMS_BOT_ID` / `BOT_APP_ID` is unset, the bot infra is
skipped and the deploy behaves exactly like channel B (tab-only).

| Variable | Default | Purpose |
|---|---|---|
| `BOT_APP_ID` | — | **The one you set.** `azd env set BOT_APP_ID <id>` — the chat bot's Entra app (client) id. Provisions `botService.bicep` and populates the `CONNECTIONS__*` values below. Unset ⇒ the bot infra is skipped entirely. |
| `BOT_APP_PASSWORD` | — | That app's client secret. Stored as an ACA secret by infra, never as a plain env var. |
| `BOT_APP_TENANT_ID` | *(deployment tenant)* | Tenant of the bot's Entra app. |
| `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID` | — | Bot app client id (Microsoft 365 Agents SDK convention). **Set for you by infra** from `BOT_APP_ID`; the backend reads this name first. |
| `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET` | — | Bot app client secret (stored as an ACA secret by infra). |
| `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID` | — | Tenant id of the bot's Entra app. |
| `TEAMS_BOT_ID` | — | The bot's Microsoft App ID (GUID). Also fills `{{BOT_ID}}` in the manifest via [`teams/build_package.py`](../teams/build_package.py). |
| `TEAMS_APP_ID` | — | The Teams app (manifest) id; used to build deep links from the bot back into the personal tab. Match the id used to build the package. |
| `TEAMS_TAB_ENTITY_ID` | `avatarForgeHome` | The static-tab entity id the bot deep-links to. |
| `BOT_RUN_TIMEOUT_S` | `60` | Max seconds a grounded Foundry run executes in the background before a "took too long" reply. Answers are delivered as a proactive message (ack-then-background-run), so this is **not** bound by the Teams ~15s turn window. |

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
| `TEAMS_ENABLE_CALLING` | `--enable-calling` | `false` | Sets `supportsCalling: true`. Pair with the **calling** bot's id (`MEETING_BOT_APP_ID`), not the chat bot's. |
| `TEAMS_ENABLE_COMPANION` | `--enable-companion` | `false` | Adds the in-meeting side panel / stage tabs. Off keeps the package identical to the tab-only shape. |

## Teams in-call avatar (channel D, issue #27)

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
| `ENABLE_ACS` | `false` | **azd/infra only.** When `true`, provisions the conditional `communicationServices.bicep` and passes `ACS_ENDPOINT` to the container. **Not needed by channels A–D** — see the note below the table. |
| `ACS_DATA_LOCATION` | `United States` | **azd/infra only.** Data residency geography for the ACS resource. |
| `ACS_ENDPOINT` | — | ACS resource endpoint (`https://<acs>.communication.azure.com/`). Set automatically by infra when `ENABLE_ACS=true`. Auth via the container's managed identity (needs a role on the ACS resource). |
| `ACS_CONNECTION_STRING` | — | Alternative to `ACS_ENDPOINT` + managed identity (includes endpoint + key). Takes precedence when set; simplest for local/dev. |
| `ACS_CALLBACK_BASE_URL` | — | Public HTTPS base URL ACS uses for call-event callbacks and the media WebSocket. Defaults to the app's own external ingress; set for local dev behind a Dev Tunnel/ngrok. |
| `ACS_AUDIO_SAMPLE_RATE` | `24000` | PCM sample rate (Hz) for the ACS↔Voice Live bridge. `24000` matches Voice Live (no resample); `16000` also valid. |
| `ACS_WAKE_PHRASES` | *(derived from the persona name)* | Comma-separated, case-insensitive phrases that invoke a spoken answer (turn-taking, so it never talks over the room). Defaults to `hey <name>,<name>` lower-cased, where `<name>` is the resolved persona name — so you say "hey Simone" to the avatar shown as Simone. Set this only to override. |
| `ACS_REQUIRE_WAKE_PHRASE` | `true` | Require a wake phrase before answering (half-duplex). Set `false` in a 1:1 test meeting to answer every turn. |
| `ACS_IDLE_TIMEOUT_S` | `0` | Leave the call after N seconds of inactivity (`0` disables). |
| `ACS_FOLLOWUP_WINDOW_S` | `30` | Seconds after an answer during which a follow-up needs **no** wake phrase, so a real back-and-forth doesn't require saying the name every turn. |

> [!NOTE]
> **The `ACS_` prefix spans two unrelated things.** Read it as two groups:
>
> - **The ACS *resource*** — `ENABLE_ACS`, `ACS_DATA_LOCATION`, `ACS_ENDPOINT`,
>   `ACS_CONNECTION_STRING`, `ACS_CALLBACK_BASE_URL`. **No channel in the A–D ladder
>   needs these.** They serve the separate browser joiner (`/acs-join.html`), where a
>   browser tab joins a meeting as an anonymous guest. Leave `ENABLE_ACS` at `false`
>   unless you are specifically using that page.
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
| `ACS_AVATAR_VIDEO_ENABLED` | `false` | Ask Voice Live to synthesise avatar **video** for in-call sessions. Required for either leg to have a face. |
| `BROWSER_JOIN_VIDEO_ENABLED` | `false` | Browser-joiner leg only: decode the avatar in the browser and publish it as the ACS video tile. Setting it `false` is the safe rollback — the voice keeps working. |

### Windows media host *(azd/infra only)*

Provisioned by `azd up` when `DEPLOY_PROFILE=in-call` (or `DEPLOY_MEETING_BOT_HOST=true`)
**and** all three required inputs are present. If any is missing the host is skipped
rather than failing partway through provisioning — `scripts/preflight.py` blocks the
deploy first and tells you which one.

| Variable | Default | Purpose |
|---|---|---|
| `MEETING_BOT_ENABLED` | `false` | Serves the media-bot bridge at `/ws/acs/audio` on the container app. Implied by `DEPLOY_PROFILE=in-call`. Independent of `ENABLE_ACS`. |
| `DEPLOY_MEETING_BOT_HOST` | `false` | Provisions `infra/modules/meetingBotHost.bicep` — Windows VM, public IP, NSG, and the Azure Bot registration with the Teams **calling** webhook. Implied by `DEPLOY_PROFILE=in-call`. |
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
