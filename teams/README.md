# Avatar Forge — Microsoft Teams app package

> **This folder is the Teams *packaging* for every Teams-facing channel.** For *what to
> deploy and why*, start at the channel hub — this file covers the mechanics of building
> the package.
>
> - **Channel B — Teams personal tab** → [`docs/channels/b-teams-tab.md`](../docs/channels/b-teams-tab.md)
> - **Channel D — in-call media bot**: its *runtime* lives in [`meeting-bot/`](../meeting-bot/), but its
>   manifest flags are built here → [`docs/channels/d-in-call-media-bot.md`](../docs/channels/d-in-call-media-bot.md)
> - Manual/admin steps → [`docs/admin-checklist.md`](../docs/admin-checklist.md)

This folder packages the Avatar Forge web app as a **Microsoft Teams app**: a personal
**tab** that embeds the web UI (channel B), sideloadable with **no Teams-admin access
required**.

- **Personal tab** (below): an anonymous, sideloaded prototype that embeds
  the existing web UI (mic + WebRTC avatar). No SSO, no org publishing.

The avatar joining a **live call** is channel D. Its *runtime* — a .NET media bot on its
own Windows host — is **not** built from this folder (see `meeting-bot/`), but C's
**Teams surface is**: two opt-in flags here add its manifest entries. See
[In-call media](#in-call-media-issue-27--documented-elsewhere) at the bottom.

Start with the tab walkthrough, then the in-call section if you're enabling channel D.

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
| `build/teams-<env>.zip` | Build output, one per azd environment (git-ignored). |

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
> image push keeps the same host, so the existing zip stays
> valid and you do **not** need to rebuild or re-sideload. Rebuild only when the
> host changes — then re-run the command above and re-upload the new zip.

Optional flags (env var equivalents in parentheses):

- `--version` (`TEAMS_APP_VERSION`) — manifest version, default `1.0.0`.
- `--app-id` (`TEAMS_APP_ID`) — stable app GUID. If omitted, a deterministic GUID is
  derived from the hostname so rebuilds produce the same id.

Output: `teams/build/teams-<env>.zip` containing `manifest.json`,
`color.png`, and `outline.png` at the archive root, where `<env>` is the selected
azd environment (`AZURE_ENV_NAME`). Without a selected environment — an explicit
`--hostname` build — the name falls back to `teams.zip`.

> **Why the filename carries the environment.** A package is not a neutral
> artefact: the manifest bakes in that deployment's hostname, and the app id is a
> deterministic GUID *derived from* that hostname. Two environments therefore
> produce two genuinely different Teams apps that can be installed side by side —
> which is exactly what you want when comparing, say, an agent-mode and a
> model-mode deployment. Under a single fixed filename, `azd env select` followed
> by a rebuild silently overwrote the previous environment's package and nothing
> on disk told you which deployment a given zip pointed at. The build also prints
> the environment it used.

## Run it in Teams (no admin access needed)

You do **not** need to publish to an org catalog. Two sideload routes — try A first; if
your tenant has custom-app upload disabled, use B.

### Route A — Upload a custom app (personal scope)

1. In Teams, go to **Apps → Manage your apps → Upload an app → Upload a custom app**.
2. Select the zip the build printed (`teams/build/teams-<env>.zip`).
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
2. **Apps → Import app** and select the zip the build printed
   (`teams/build/teams-<env>.zip`).
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
tab-only shape.

Note the bot id: a calling manifest must name the **calling** bot's app registration
(`MEETING_BOT_APP_ID`). An Entra app can back only one Azure Bot resource, so that
registration must be dedicated to the calling bot.

That is needed only for the app's in-meeting presence. The calling bot itself joins via
Graph application permissions and does **not** require the app to be installed in the
meeting, so channel D works with no Teams package at all.

> **Historical note.** Earlier revisions of this file described channel C as an
> ACS-based, audio-only participant. Live testing disproved that design: ACS
> `connect_call` media streaming does not carry Teams *meeting* audio, and a browser/ACS
> client leg can only hear its own microphone. The shipped design is the Graph
> Real-Time Media bot, and it carries video as well as audio. The full reasoning,
> including what was ruled out and why, is in
> [`d-design-media-bot.md`](../docs/channels/d-design-media-bot.md).
