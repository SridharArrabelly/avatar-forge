# Avatar Forge — Microsoft Teams app (tab + conversational bot)

> **This folder is the Teams *packaging* for every Teams-facing channel.** For *what to
> deploy and why*, start at the channel hub — this file covers the mechanics of building
> the package.
>
> - **Channel B — Teams personal tab** → [`docs/channels/b-teams-tab.md`](../docs/channels/b-teams-tab.md)
> - **Channel C — Teams conversational bot** → [`docs/channels/c-teams-chat-bot.md`](../docs/channels/c-teams-chat-bot.md)
> - **Channel D — in-call media bot**: its *runtime* lives in [`meeting-bot/`](../meeting-bot/), but its
>   manifest flags are built here → [`docs/channels/d-in-call-media-bot.md`](../docs/channels/d-in-call-media-bot.md)
> - Manual/admin steps → [`docs/admin-checklist.md`](../docs/admin-checklist.md)

This folder packages the Avatar Forge web app as a **Microsoft Teams app** with two
surfaces in **one package**: a personal **tab** that embeds the web UI (channel B) and
an installable, @mentionable **conversational bot** (channel C). Both are additive and
sideloadable with **no Teams-admin access required**.

- **Personal tab** (below): an anonymous, sideloaded prototype that embeds
  the existing web UI (mic + WebRTC avatar). No SSO, no org publishing.
- **Conversational bot** ([jump down](#channel-c--conversational-bot-issue-53)):
  a bot that answers via the same Foundry agent and deep-links back into the tab.

The avatar joining a **live call** is channel D. Its *runtime* — a .NET media bot on its
own Windows host — is **not** built from this folder (see `meeting-bot/`), but D's
**Teams surface is**: two opt-in flags here add its manifest entries. See
[In-call media](#in-call-media-issue-27--documented-elsewhere) at the bottom.

Start with the tab walkthrough, then the bot section if you're enabling it.

- Personal-scope **static tab** that embeds the existing web UI (mic + WebRTC avatar).
- **No SSO** and **no org/admin publishing** — those are a later phase you'll drive
  from the Teams admin center once the prototype works.
- Additive only: a templated manifest, two icons, a build script, a no-op-unless-in-Teams
  frontend init (`frontend/teams.js`), and one `frame-ancestors` CSP header in the backend.

> The same package built here is reused unchanged when you later publish it through the
> admin center — so the manifest stays templated and the zip stays valid (manifest + 2
> icons at the archive root).

## Contents

| File | Purpose |
| --- | --- |
| `manifest.template.json` | Teams manifest (schema v1.17) with `{{HOSTNAME}}`, `{{VERSION}}`, `{{APP_ID}}` placeholders. |
| `../assets/brand/color.png` | 192×192 color app icon (canonical brand source, shared with web + meeting bot). |
| `../assets/brand/outline.png` | 32×32 transparent outline icon (Teams recolors it). |
| `build_package.py` | Stdlib-only script that renders the manifest and zips a sideloadable package. Embeds the icons from `assets/brand/` into the zip. |
| `build/avatar-forge-teams.zip` | Build output (git-ignored). |

## Build the package

After a deploy, run it with no arguments:

```powershell
uv run python teams/build_package.py
```

The **host** and the **branding** are read from the selected `azd` environment —
`SERVICE_APP_URI` for the host (the scheme is stripped for you), and the avatar
model variables for the name — so the package matches the deployment it points
at. The resolved values are echoed on the last lines of the build output; check
them if you have more than one environment.

State the host explicitly when you are outside an `azd` context, or targeting a
different deployment:

```powershell
uv run python teams/build_package.py --hostname <your-app>.azurecontainerapps.io
```

An explicit host must be **bare** — no `https://`, no path, no port. Teams'
`validDomains` cannot contain any of those, so a value carrying them is rejected
outright rather than producing a package that fails at install time. That is why
pasting `SERVICE_APP_URI` verbatim is an error; omit `--hostname` and let the
builder derive it instead.

> **Rebuild after a redeploy:** the hostname only changes if the Container App is
> recreated (a fresh `azd up` into a new environment). A normal `azd deploy` /
> image push keeps the same host, so the existing `avatar-forge-teams.zip` stays
> valid and you do **not** need to rebuild or re-sideload. Rebuild only when the
> host changes — then re-run the command above and re-upload the new zip.

Optional flags (env var equivalents in parentheses):

- `--version` (`TEAMS_APP_VERSION`) — manifest version, default `1.0.0`.
- `--app-id` (`TEAMS_APP_ID`) — stable app GUID. If omitted, a deterministic GUID is
  derived from the hostname so rebuilds produce the same id.

Output: `teams/build/avatar-forge-teams.zip` containing `manifest.json`,
`color.png`, and `outline.png` at the archive root.

## Run it in Teams (no admin access needed)

You do **not** need to publish to an org catalog. Two sideload routes — try A first; if
your tenant has custom-app upload disabled, use B.

### Route A — Upload a custom app (personal scope)

1. In Teams, go to **Apps → Manage your apps → Upload an app → Upload a custom app**.
2. Select `teams/build/avatar-forge-teams.zip`.
3. Add the app; open the **Avatar** personal tab.
4. When prompted, **allow microphone** (and camera if requested) for the tab.

> ⚠️ Even this basic, personal sideload can be blocked: the **"Upload a custom app"**
> option only appears if the tenant policy *Allow uploading custom apps* is enabled. If you
> don't see it, you're not doing anything wrong — use Route B instead.

### Route B — Teams Developer Portal "Preview in Teams" (admin-free fallback)

The Developer Portal lets you preview a sideloaded app without the upload-custom-app
policy, and is the recommended no-admin path for this prototype.

1. Open the **Teams Developer Portal** — <https://dev.teams.microsoft.com> (also available
   as the **Developer Portal** app inside Teams).
2. **Apps → Import app** and select `teams/build/avatar-forge-teams.zip`.
3. Open the imported app and click **Preview in Teams** (top right). Teams opens and adds
   the app for you.
4. Open the **Avatar** personal tab and **allow microphone** (and camera if requested).

If neither route is available, the last admin-free option is to **add the app to a team
you own** (some tenants allow app uploads scoped to a team even when personal upload is
off) — but for this prototype Route B is the simplest.

## Validation checklist (run in Teams **web** AND **desktop**)

- [ ] The tab loads the avatar UI over HTTPS (no blank frame / framing error).
- [ ] Microphone permission prompt appears and, once granted, `getUserMedia` succeeds.
- [ ] The WebRTC avatar **video** renders and the avatar **speaks** (audio out).
- [ ] Talking to the avatar works end-to-end (the WSS voice socket connects).
- [ ] Switching the Teams theme (light ↔ dark) updates the app theme live.
- [ ] The standalone app (`uv run avatar-forge`, port 3000) is unchanged — the Teams
      SDK is never loaded outside Teams.

If the avatar video or mic fails inside Teams, that is the gating risk for 1A — capture
the client (web/desktop), the console errors, and the permission state, and report back
before proceeding.

## How the in-Teams detection works

`frontend/teams.js` activates only when the page is inside Teams — detected via the
`?inTeams=1` query param that the manifest's `contentUrl` carries, with a framed-window
fallback. Outside Teams it returns immediately and the Teams JS SDK is never fetched, so
the standalone experience is byte-for-byte the same. Inside Teams it initializes the SDK
and mirrors the host theme into the app's existing `applyTheme()` hook.

## Embedding header

The backend sends a single response header so the Teams clients can frame the app:

```
Content-Security-Policy: frame-ancestors 'self' \
  https://teams.microsoft.com https://*.teams.microsoft.com \
  https://teams.live.com https://*.teams.live.com \
  https://*.skype.com
```

Only `frame-ancestors` is set on purpose — a full CSP (`script-src`/`connect-src`/
`media-src`) would break inline JS, the WSS voice socket, and WebRTC.

## Deferred to a later phase (not in this prototype)

- Publishing through the **Teams admin center** (org catalog / targeted release / admin
  approval) — you'll do this from the admin portal once the sideloaded prototype works.
  The package built here is reused unchanged for that step.
- Real privacy-policy and terms-of-use pages (the manifest currently points at repo pages,
  which is fine for sideload).

---

# Channel C — Conversational bot (issue #53)

Channel C adds an **installable, @mentionable bot** to the **same Teams app package** (the
manifest now carries both a `staticTabs` entry **and** a `bots` entry — one app, two
surfaces). The bot answers questions using the **same Foundry agent** the voice avatar
uses (Azure AI Search RAG + Bing grounding), returns answers as Adaptive Cards with
sources, and can deep-link back into the channel B tab for the live avatar.

It is **additive**: the channel B tab and the standalone web app are unchanged, and no Node
toolchain is introduced. The bot is hosted **inside the existing FastAPI app** (new
`POST /api/messages` route) using the **Microsoft 365 Agents SDK** (`microsoft-agents-*`,
FastAPI adapter), so it ships in the same Container App — the messaging endpoint is just
the existing ACA HTTPS URL + `/api/messages`.

## What changed

| Area | Change |
| --- | --- |
| `teams/manifest.template.json` | Added a `bots` entry (`personal` + `team` + `groupchat` scopes), a `commandLists`, a `{{BOT_ID}}` placeholder, and `token.botframework.com` to `validDomains`. The static tab is untouched. |
| `teams/build_package.py` | Optional `--bot-id` / `TEAMS_BOT_ID` input fills `{{BOT_ID}}`. **When omitted, the build is tab-only** (the `bots` entry is dropped) so the channel B Tab package always builds. The zip stays flat (manifest + 2 icons). |
| `backend/bot/` | The bot: SDK app + `/api/messages` route (`app.py`), Foundry-agent bridge (`agent_runtime.py`), Adaptive Card + deep link (`cards.py`). |
| `backend/main.py` | Mounts the bot router before the static SPA; closes the agent client on shutdown. |
| `infra/` | New `modules/botService.bicep` (Azure Bot + Teams channel), conditional on a bot app id; container env + secret wiring. |

## Identity model (read this first)

The bot needs a **bot identity** = an **Entra app registration** (client id + secret),
registered as an **Azure Bot** resource with the **Teams channel** enabled. This is
**separate** from the backend's managed identity (which still reaches Foundry/Search) and
**separate** from user SSO (deferred — see below).

- **Azure Bot + Teams channel + container wiring** are created by `infra/` when you supply
  the bot app id/secret — **no Teams admin access required** (these are *Azure* RBAC
  actions in your subscription).
- **User SSO is deferred** (channel C ships with bot-framework identity only). The bot does
  not yet exchange a user token, so it does not need `webApplicationInfo` in the manifest
  for the MVP. Adding SSO later requires an exposed API scope + `webApplicationInfo` +
  token-exchange handling.

## Steps you must do yourself (portal / CLI)

These cannot be done from this repo because they create an **app registration** (an
identity object), which lives outside the resource-group deployment:

1. **Create the bot's Entra app registration** (single-tenant is simplest):
   ```powershell
   az ad app create --display-name "Avatar Forge Bot" --sign-in-audience AzureADMyOrg
   # note the appId (this is your BOT app id), then add a client secret:
   az ad app credential reset --id <bot-app-id> --append
   # note the returned password (client secret)
   ```
2. **Give azd the bot values** (the infra wires the Azure Bot + container env from these):
   ```powershell
   azd env set BOT_APP_ID <bot-app-id>
   azd env set BOT_APP_PASSWORD <bot-client-secret>   # stored as an ACA secret
   azd env set TEAMS_APP_ID <teams-app-id>            # same id you build the package with
   ```
   > These map to the `botAppId` / `botAppPassword` / `teamsAppId` Bicep params. If
   > `BOT_APP_ID` is unset, the bot infra is skipped entirely and the deploy behaves
   > exactly as channel B.
3. **Provision + deploy**: `azd up` (or `azd provision` then `azd deploy`). This creates
   the Azure Bot, enables the Teams channel, and sets the messaging endpoint to
   `https://<aca-host>/api/messages`. The endpoint is also emitted as the
   `BOT_MESSAGING_ENDPOINT` output.

## Build the package (with the bot id)

The bot is **additive and opt-in**: omit `--bot-id` to build the tab-only channel B package
(the `bots` entry is dropped). To include the bot, pass `--bot-id` (the bot app id from step 1):

```powershell
uv run python teams/build_package.py `
  --hostname <your-app>.azurecontainerapps.io `
  --bot-id <bot-app-id>
```

`--app-id` / `TEAMS_APP_ID` behaves as before (deterministic from the hostname if
omitted). Output is the same flat `teams/build/avatar-forge-teams.zip`. **Sideload it
exactly as in channel B** (Route A or Route B above) — the same package now installs both
the tab and the bot.

## Validate the bot (web AND desktop)

- [ ] **Personal chat:** open the bot, send a question, get an answer **with a Sources
      list** and an **"Open the live avatar"** button that launches the tab.
- [ ] **Group chat:** add the app, **@mention** the bot, get an answer (bots only see
      messages they're @mentioned in, in group/meeting chat).
- [ ] **Meeting chat:** add the app to a meeting, **@mention** the bot in the meeting chat,
      get an answer (chat-only — no in-call media yet; that's channel D / #27).
- [ ] **Parity:** the same question asked to the bot and to the voice avatar returns
      consistent answers + citations (both go through the same Foundry agent).
- [ ] **No regressions:** the channel B tab still loads and the standalone web app
      (`uv run avatar-forge`) is unchanged.

> The bot endpoint also answers `GET /api/messages` with `{"status":"ok"}` for a quick
> liveness check once deployed.

## Known gating risks

- **Turn latency:** a grounded answer can take several seconds (AI Search + Bing). To stay
  within the Teams ~15s activity-response window, the bot **acknowledges immediately** (typing
  indicator) and runs the Foundry agent in the **background**, then posts the answer (Adaptive
  Card with sources) as a **proactive message** to the same conversation. `BOT_RUN_TIMEOUT_S`
  (default 60s) caps the background run; on timeout the bot posts a brief "took too long" reply.
  This requires the bot's app id (`CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID`) to be
  set so proactive `continue_conversation` can authenticate.
- **Conversational memory:** the MVP treats each turn statelessly. Multi-turn memory
  (threading via the Responses API) is wired but off by default to avoid cross-user
  context bleed in group/meeting chats.
- **SSO:** deferred — the bot does not yet know *who* asked. Add `webApplicationInfo` +
  token exchange in a follow-up if per-user identity is required.

---

# In-call media (issue #27) — documented elsewhere

The avatar joining the **call itself** — hearing every participant and answering aloud
with a lip-synced camera tile — is **channel D**. Its runtime is a separate .NET media
bot on a Windows host and is **not** packaged from this folder. Its **Teams surface**
is, via the two opt-in flags below.

| What you want | Where it lives |
| --- | --- |
| Deploy it / admin steps / cost | [`docs/channels/d-in-call-media-bot.md`](../docs/channels/d-in-call-media-bot.md) |
| Why it is built this way | [`docs/channels/d-design-media-bot.md`](../docs/channels/d-design-media-bot.md) |
| How the avatar's face gets into the call | [`docs/channels/d-design-avatar-video.md`](../docs/channels/d-design-avatar-video.md) |
| The bot's own code, build and traps | [`meeting-bot/README.md`](../meeting-bot/README.md) |
| Running a live test | [`docs/testing-meetings.md`](../docs/testing-meetings.md) |

This folder contributes exactly two things to channel D, both opt-in and both
manifest-only:

| Flag | Manifest effect | What it gives you |
| --- | --- | --- |
| `--enable-calling` | `bots[0].supportsCalling: true` | Lets the app be invoked as a calling bot in meetings |
| `--enable-companion` | keeps `configurableTabs` | The in-meeting control panel (side panel / stage) |

```powershell
uv run python teams/build_package.py --hostname <host> --bot-id <MEETING_BOT_APP_ID> --enable-calling
```

Both default to **off**, so a package built without them is byte-identical to the
channel C chat-only shape.

Note the bot id: a calling manifest must name the **calling** bot's app registration, not
the chat bot's. They are different Entra apps (an app can back only one Azure Bot), so a
package cannot be both C's chat bot and D's calling bot — build a separate one.

That is needed only for the app's in-meeting presence. The calling bot itself joins via
Graph application permissions and does **not** require the app to be installed in the
meeting, so channel D works with no Teams package at all.

> **Historical note.** Earlier revisions of this file described channel D as an
> ACS-based, audio-only participant. Live testing disproved that design: ACS
> `connect_call` media streaming does not carry Teams *meeting* audio, and a browser/ACS
> client leg can only hear its own microphone. The shipped design is the Graph
> Real-Time Media bot, and it carries video as well as audio. The full reasoning,
> including what was ruled out and why, is in
> [`d-design-media-bot.md`](../docs/channels/d-design-media-bot.md).
