# Avatar-Forge Teams meeting media bot (.NET / Windows)

> **Phase 2b, issue #27 — Slice 1 (audio).** This is the thin .NET/Windows media
> relay described in [`docs/teams-meeting-bot.md`](../docs/teams-meeting-bot.md).
> It joins a Teams meeting, captures the **mixed participant audio**, and forwards
> raw PCM16 over a WebSocket to the **unchanged** Python backend
> (`backend/acs/bridge.py::AcsVoiceBridge`). All answering / RAG / turn-taking
> stays in Python.

## Why this exists (the one-paragraph version)

A browser/Teams-tab client can only ever hear **its own mic** (Teams client
isolation), and ACS Call Automation server-side media does **not** carry Teams
*meeting* audio — both proven live. The **only** way for Nuru to hear everyone in
the room is the Graph **Real-Time Media Platform** (`Microsoft.Skype.Bots.Media`),
which is **.NET + Windows-only**. So this is a small, separate Windows service that
acts as a dumb media pump into the existing Python brain.

## ⚠️ Platform reality — must build AND run on Windows

`Microsoft.Graph.Communications.Calls.Media` carries a native Windows media stack.
The bot **must run on a Windows Server guest OS** (Cloud Service, Service Fabric +
VMSS, IaaS VM, or AKS Windows node pool). It **cannot** run on Linux/macOS or an
Azure Web App / Linux Container App. You may *edit* the C# anywhere; you must
*build & host* on Windows (`RuntimeIdentifier=win-x64`).

## Architecture

```
Teams meeting ──mixed audio──▶ [Skype.Bots.Media AudioSocket]
                                        │ PCM16 16 kHz
                                        ▼
                               [VoiceLiveBridgeClient]  ──WSS──▶  Python /ws/acs/audio
                                        ▲                          (AcsVoiceBridge ▸ VoiceSessionHandler
                                        │ PCM16 (Nuru's answer)     ▸ Voice Live ▸ Foundry RAG+news)
                               [AudioSocket.Send] ──audio──▶ Teams meeting
```

The seam is the **already-built** `/ws/acs/audio` endpoint. The bot just speaks its
wire protocol (`AudioMetadata` → base64-PCM16 `AudioData` frames; inbound
`AudioData` to play, `StopAudio` for barge-in). The only Python-side requirement for
Slice 1 is two env flags: `MEETING_BOT_ENABLED=true` (serves `/ws/acs/audio` without an
ACS resource) and `ACS_AUDIO_SAMPLE_RATE=16000` (matches the media platform). Both are
already set on the deployed app and wired through bicep.

## Project layout

| Path | Role |
| --- | --- |
| `Program.cs` | ASP.NET host; binds config, starts the bot, maps controllers. |
| `Configuration/BotOptions.cs` | Strongly-typed config (`Bot:*`). |
| `Bot/MeetingBot.cs` | Owns the `ICommunicationsClient`; `JoinMeetingAsync` / `LeaveAsync`. |
| `Bot/CallHandler.cs` | Per-call media plumbing: AudioSocket ⇄ bridge. |
| `Bot/AuthenticationProvider.cs` | App-only Graph token (MSAL) + inbound validation. |
| `Bridge/VoiceLiveBridgeClient.cs` | **The Python contract.** WS client speaking the AcsVoiceBridge protocol. Unit-tested, no media-SDK deps. |
| `Http/JoinController.cs` | Operator API: `POST /api/join`, `POST /api/leave`. |
| `Http/CallingController.cs` | Bot Framework calling webhook (`POST /api/calling`). |
| `infra/host.bicep` | **Standalone** Windows VM + NSG + calling-bot registration. |

## Configuration

Set via `appsettings.json` or environment (`Bot__*`). **Never commit the secret.**

| Key | Value (current deployment) |
| --- | --- |
| `Bot:AppId` | `fcae883a-6107-42ec-8fd5-c24023ada525` (`avatar-forge-meeting-bot`, multi-tenant) |
| `Bot:TenantId` | `b1cd5b73-a77b-4002-a5a6-1599e4c4ee37` |
| `Bot:AppSecret` | from env `BOT_CLIENT_SECRET` (stored in azd env, git-ignored) |
| `Bot:ServiceFqdn` | `avatar-meetingbot-newtenant.swedencentral.cloudapp.azure.com` (`host.bicep` output) |
| `Bot:CertificateThumbprint` | a publicly-trusted cert in `LocalMachine\My` matching the FQDN |
| `Bot:BridgeWebSocketUrl` | `wss://ca-avatar-newtenan-ahfjen5fzzjgi.purpleocean-4494944c.swedencentral.azurecontainerapps.io/ws/acs/audio` |
| `Bot:BridgeSampleRate` | `16000` |
| `Bot:EnableVideo` | `true` (Slice 2A — the avatar camera tile) |
| `Bot:VideoWidth` / `Bot:VideoHeight` / `Bot:VideoFps` | `640` / `360` / `15` (only used when `EnableVideo=true`) |
| `Bot:ValidateInboundRequests` | `true` (default) — validate the bearer token on inbound calling notifications |

> **Inbound notification validation.** The signaling port is deliberately reachable from
> the internet (Teams must call it), so `/api/calling` validates the bearer token
> Microsoft's calling service signs its notifications with: the signature is checked
> against the keys published at
> `https://api.aps.skype.com/v1/.well-known/OpenIdConfiguration` and the audience must be
> this bot's `Bot:AppId`. Without it, anything that can reach the port could inject
> fabricated call notifications.
>
> `Bot__ValidateInboundRequests=false` is an escape hatch only — use it to confirm a
> diagnosis if genuine callbacks are ever rejected, then turn it back on. It logs a
> warning on every notification while disabled.

> **Slice 2A — the avatar's video face (now built end to end).** With
> `Bot__EnableVideo=true` the bot adds an outbound NV12 `VideoSocket` and appears as a
> **camera tile**. The frames come from Python: the bridge runs its Voice Live session in
> avatar/`websocket` mode, decodes the resulting stream and forwards real `VideoData`
> (NV12) frames, so the face is lip-synced to the voice it is speaking.
>
> **This must be enabled on BOTH sides** — `Bot__EnableVideo=true` here *and*
> `MEETING_BOT_VIDEO_ENABLED=true` on the container app. The sizes must also agree:
> `Bot__VideoWidth/Height/Fps` map to a supported `NV12_*` format via `VideoFormatFor`,
> and the bot drops any frame whose dimensions differ, falling back to its placeholder.
>
> Enabling video changes the **audio** source too, which is why the Python flag is a
> single switch rather than a cosmetic one: in avatar/`websocket` mode Voice Live stops
> emitting `response.audio.delta` and muxes the answer audio (AAC) into the same
> fragmented-MP4 stream, so the bridge recovers the audio from there instead. Both
> defaults are `false` = the audio-only Slice 1 bot, byte-for-byte unchanged. Full
> design: [`docs/teams-avatar-video.md`](../docs/teams-avatar-video.md).

## Deployed host (rg-avatar-newtenant) — already provisioned

`host.bicep` is **deployed**. Live resources:

| Resource | Value |
| --- | --- |
| Windows VM | `avatar-meetingbot-vm` (running, `Standard_D4s_v5`, swedencentral) |
| Public FQDN | `avatar-meetingbot-newtenant.swedencentral.cloudapp.azure.com` |
| Signaling endpoint | `https://<fqdn>:9441/api/calling` |
| Operator API | `https://<fqdn>:9441/api/join` |
| Calling-bot registration | `avatar-meetingbot-registration` (Teams channel, `callingWebhook` on) |
| NSG | `avatar-meetingbot-nsg` — 9441 (signaling), 8445 (media), 80 (ACME), 3389 (RDP) |

The Python side is **already live**: the container app has `MEETING_BOT_ENABLED=true`,
`MEETING_BOT_VIDEO_ENABLED=true` and `/ws/acs/audio` accepts the bot's handshake.
`MEETING_BOT_ENABLED` makes the bridge serve the bot **without** provisioning an ACS
resource.

> **Restarting the service:** stop it and wait for port **8445** to actually clear before
> starting again. The Real-Time Media platform binds that port, and if the previous
> process still holds it the new one dies at startup with
> `Media platform failed to initialize -> AddressInUse`, which looks alarmingly like a
> media-stack fault but is only a restart race.

## Bot status — BUILT, DEPLOYED & RUNNING on the host ✅

As of the latest deploy the bot is **live on the VM as a Windows service**, not just
scaffolded:

| Item | Status |
| --- | --- |
| VM resized to 4 vCPU (`Standard_D4s_v5`) | ✅ media platform needs ≥ 2 cores |
| TLS cert (Let's Encrypt via win-acme) | ✅ thumbprint `C6B8756C3015D51F6916A192EA4FF460BF88AE6F`, expires 2026-10-28 |
| VC++ x64 redistributable | ✅ installed (native media stack links `vcruntime140`/`msvcp140`) |
| `dotnet publish -r win-x64 --self-contained` | ✅ builds; native media DLLs auto-bundled by the `CopySkypeNativeMedia` target |
| `AvatarForgeMeetingBot` Windows service | ✅ **Running**, HTTPS bound on `:9441` |
| Media platform init | ✅ initializes cleanly (no `NativeMedia`/cores error) |
| Public endpoint | ✅ `https://<fqdn>:9441/api/health` → `{"status":"ok"}` over the trusted cert |

**What this proves live:** the `TODO(prod)` risks the plan flagged (cert/TLS binding,
the win-x64 native media stack, the Windows-service host) are all resolved — the bot
starts, binds TLS with a publicly-trusted cert, initializes the Real-Time Media
platform, and answers its operator API from the public internet. What is **not** yet
proven is the in-meeting behaviour (join/admission, hearing the room, answering aloud,
latency) — that needs the Teams manifest uploaded + a live meeting (steps below).

## Host setup — `scripts/setup-host.ps1`

A 4-stage helper drives the Windows host. **Stage Prep is already done** on the
deployed VM (firewall rules + .NET 8 SDK / ASP.NET runtime, via `az vm run-command`).
The remaining stages are operator-only (need the private repo on the VM + interactive
cert issuance + a real meeting):

```pwsh
# On the VM (RDP in), from a clone of this repo:
.\meeting-bot\scripts\setup-host.ps1 -Stage Cert  -Email you@example.com   # win-acme Let's Encrypt (HTTP-01, port 80)
.\meeting-bot\scripts\setup-host.ps1 -Stage Build                          # git clone + dotnet publish -r win-x64
.\meeting-bot\scripts\setup-host.ps1 -Stage Run   -Thumbprint <cert-tp> `
    -BridgeUrl wss://ca-avatar-newtenan-ahfjen5fzzjgi.purpleocean-4494944c.swedencentral.azurecontainerapps.io/ws/acs/audio `
    -BotSecret <BOT_CLIENT_SECRET>                                         # set Bot__* + install/start the Windows service
```

> Note: this is a **private** repo, so the Build stage needs git auth on the VM
> (e.g. a PAT or `gh auth login`).

## Runbook (operator — Windows host required)

1. ✅ **Host + calling registration** — already deployed (`host.bicep`). To
   re-deploy/update:
   ```pwsh
   az deployment group create -g rg-avatar-newtenant `
     -f meeting-bot/infra/host.bicep `
     -p botAppId=fcae883a-6107-42ec-8fd5-c24023ada525 `
        botAppTenantId=b1cd5b73-a77b-4002-a5a6-1599e4c4ee37 `
        adminPassword='<strong-password>' dnsLabel=avatar-meetingbot-newtenant
   ```
2. ✅ **Python side** — already live: `MEETING_BOT_ENABLED=true`,
   `ACS_AUDIO_SAMPLE_RATE=16000`, `ACS_REQUIRE_WAKE_PHRASE=true` on the container app
   (and persisted in the azd env, wired through bicep so a full `azd up` keeps them).
3. ✅ **Prep stage** — firewall + .NET 8 SDK/ASP.NET runtime + VC++ x64 redist installed
   on the VM; VM sized to 4 vCPU for the media platform.
4. ✅ **TLS cert installed** — Let's Encrypt cert issued via `setup-host.ps1 -Stage Cert`
   (win-acme, HTTP-01). Thumbprint `C6B8756C3015D51F6916A192EA4FF460BF88AE6F`, auto-renew
   task scheduled.
5. ✅ **Bot built & published on the VM** — `dotnet publish -r win-x64 --self-contained`;
   native media DLLs auto-bundled by the csproj `CopySkypeNativeMedia` target.
6. ✅ **Running** — `Bot__*` env + `BOT_CLIENT_SECRET` set (Machine scope); the
   `AvatarForgeMeetingBot` Windows service is installed and **Running**, HTTPS on `:9441`.
   `https://<fqdn>:9441/api/health` → `{"status":"ok"}` from the public internet.
7. **(USER) Teams manifest:** build with `python teams/build_package.py --enable-calling`
   (sets `supportsCalling: true`), then upload it in Teams ("Apps → Manage your apps →
   Upload an app"). Note: uploading the manifest is only needed for the chat/tab surface
   and for in-meeting *app* presence — the calling bot joins via Graph application
   permissions (see "Which tenant can the bot join?" below) and does **not** require the
   app to be installed in the meeting.
8. **(USER) Live test:** start a Teams meeting **in a tenant that has Teams *and* has
   admin-consented this bot** (see below), then
   `POST https://avatar-meetingbot-newtenant.swedencentral.cloudapp.azure.com:9441/api/join { "joinUrl": "<classic meetup-join link>" }`.
   Nuru should appear in the roster, hear the room, and answer aloud on the wake phrase
   ("nuru" / "hey nuru"). Watch latency (joiner + media hop on top of Voice Live
   first-token).

### Which tenant can the bot join? (multi-tenant + admin consent)

The bot acquires its Graph token **against the meeting's organizer tenant** (the `Tid`
encoded in the classic `meetup-join` link), not its own home tenant — so a meeting
hosted in any tenant can be joined **provided that tenant has admin-consented the bot**.
This mirrors how third-party meeting bots join customer tenants. Two facts must both hold:

- **The link must be the classic `…/l/meetup-join/19%3ameeting_…%40thread.v2/…?context=…`
  form.** The new short `…/meet/<id>?p=…` share link carries no thread id or tenant and
  cannot be joined. Get the classic link from the invite body ("Click here to join the
  meeting" → right-click → Copy Link).
- **The organizer's tenant must have admin-consented the bot app** (`fcae883a-…`, now
  registered **multi-tenant**). A directory admin in that tenant grants this **once** via:

  ```
  https://login.microsoftonline.com/<TARGET_TENANT_ID_OR_DOMAIN>/adminconsent?client_id=fcae883a-6107-42ec-8fd5-c24023ada525
  ```

  This creates the bot's service principal in that tenant and grants the declared Graph
  application permissions (`Calls.JoinGroupCall.All`, `Calls.AccessMedia.All`,
  `Calls.JoinGroupCallAsGuest.All`, `OnlineMeetings.Read.All`). The bot needs **no Teams
  license** in that tenant.

> ✅ **The host tenant now has Teams.** The current host tenant
> (`b1cd5b73-a77b-4002-a5a6-1599e4c4ee37`, `diax18547011.onmicrosoft.com`) is licensed for
> Microsoft 365, and you hold global admin there — so meetings can be **organized in the
> same tenant that hosts the bot**, and the admin consent above is self-service. This is
> the first configuration in which the media bot is actually testable; the previous host
> tenant had no Teams licence, which is precisely why in-meeting behaviour went untested
> for so long.
>
> If you organize the meeting somewhere else instead, that tenant needs the one-time
> admin consent above from someone with directory rights in it.

## What is verified vs. pending

- ✅ **`VoiceLiveBridgeClient` — the Python contract — is unit-tested** (metadata,
  outbound `AudioData`, inbound `AudioData` dispatch, `StopAudio` barge-in all pass
  a round-trip against a mock server).
- ✅ `infra/host.bicep` compiles clean (`az bicep build`).
- ✅ **The media-SDK code builds, publishes and RUNS on the Windows host** — the bot
  starts as a Windows service, initializes the Real-Time Media platform, binds HTTPS
  with a publicly-trusted cert, and serves its API from the public internet. The
  previously-`TODO(prod)` cert/TLS binding in `Program.cs` is implemented and verified.
- ✅ **Inbound calling notifications are validated** — `AuthenticationProvider` now checks
  the notification's bearer token against the calling service's published signing keys and
  this bot's app id, instead of accepting everything. Verified against the deployed build:
  a missing header, a non-bearer scheme, a garbage token, and a forged token carrying the
  correct audience *and* issuer but an invalid signature are all rejected.
- ⏳ **In-meeting behaviour is the only thing left to confirm live:** join + lobby
  admission, hearing the mixed room audio, answering aloud, barge-in, and end-to-end
  latency. This needs the Teams manifest uploaded (`--enable-calling`), a tenant meeting
  policy allowing bots, and a real meeting — see the runbook steps 7–8.

## Traps that cost real debugging time

Three failures here look like something they are not. Each is recorded with the symptom
you will actually see, because the symptom points the wrong way.

**1. `Could not load file or assembly 'Microsoft.Skype.Internal.Media.H264NetCore'` —
for a file that is plainly sitting in the folder.** The app publishes *self-contained*,
and a self-contained host builds its trusted-assembly list purely from `deps.json`. The
`CopySkypeNativeMedia` target copies the Skype media DLLs as **content**, not as package
references, so they never appear in `deps.json` and the media platform's dynamic
`Assembly.Load` cannot resolve them — even though the bytes are right there. Fixed by an
`AssemblyLoadContext.Default.Resolving` hook at the very top of `Program.cs` that probes
`AppContext.BaseDirectory`. It must stay the first executable statement, before any media
code runs. Note `Ijwhost.dll` must be present alongside them: these are mixed-mode
(C++/CLI) images and without it they fail to load *and* make a correct fix look broken.

**2. `Failed to bind ...:8445: address already in use`, reported as a media-platform
initialization failure.** It is a restart race, not a media fault — the previous process
still holds the media port. Stop the service, poll
`Get-NetTCPConnection -LocalPort 8445 -State Listen` until it is clear, then start.

**3. Playout counters are the fastest diagnosis for in-call media.** `CallHandler` logs
periodically:

| counter | meaning if non-zero / rising |
| --- | --- |
| `underruns` | the bridge is not keeping up with the 50 fps drain. A silent frame is now sent to keep the stream contiguous — early builds skipped the slot instead, which left a hole in the waveform and was audible as a **steady tick** once the avatar made the stream continuous. |
| `placeholder` | no real frame was available, so the solid-colour tile went out. Sustained non-zero means the Python video path is not delivering. |
| `mismatched` | the platform negotiated a size different from the configured one. Real frames are sent with a format derived from the frame itself, so this is informational — but a build that *dropped* mismatched frames showed the avatar tile appearing and then going blank. |
| `queued` | outbound backlog. The video queue is capped at `MaxQueuedVideoFrames`; the **oldest** frames are dropped so the face stays close to the voice rather than drifting behind it. |

> **The single assumption that caused two separate production bugs:** with the avatar
> enabled, the Voice Live stream is *continuous* — roughly 16 video deltas/second and
> near-continuous audio for the whole session, with idle stretches being exact digital
> silence rather than an absence of data. Any logic that infers "she is speaking" from
> "a chunk arrived" is wrong. Gate on measured speech energy instead.

## Cost / honesty note

Per the ADR in `docs/teams-meeting-bot.md`, this breaks the pure-Python / Linux-ACA
guardrail **only** for the media leg, because no alternative can hear the room. The
brain stays Python; this service stays a dumb pump. The real tax is the Windows host
+ certs + one extra PCM hop — not the language.
