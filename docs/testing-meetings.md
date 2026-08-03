# Testing the two in-meeting paths

The assistant can join a Teams meeting two different ways. They are **not**
interchangeable, they are tested differently, and they must never be pointed at the same
meeting at the same time.

| | Browser joiner | Media bot |
| --- | --- | --- |
| Page / API | `acs-join.html` | `POST :9441/api/join` |
| Runs on | your laptop's browser tab | the Windows VM |
| Joins the meeting as | anonymous guest, via the ACS Calling Web SDK | the bot's Entra identity, via Graph calling |
| WebSocket | `/ws/acs/browser` | `/ws/acs/audio` |
| Brain (Voice Live + Foundry agent) | **the container app** | **the container app** |
| Needs an **ACS resource** | **yes** (`/api/acs/token` mints the guest token) | no — joins via Graph |
| Needs the VM running | no | **yes** (~$283/mo if left on) |
| **Hears** | **the other participants** — via the `srcObject` hook, verified live | **everyone in the meeting** |
| Face | browser decodes fMP4 → canvas → ACS tile | Python decodes → NV12 → `VideoSocket` |
| Status | proven in real meetings | **proven in real meetings** — joins, hears the room, answers aloud with a lip-synced tile |

**Neither path is a self-contained system, and the VM is not a second brain.** The
media bot holds no Voice Live session, no agent and no search index — it joins the
call, pulls raw PCM, and opens a WebSocket *back* to the container app
(`Bot__BridgeWebSocketUrl` → `wss://<container-app>/ws/acs/audio`). Both sockets then
hand off to the same `VoiceSessionHandler`. So the container is always required; the
only question either path answers is **who joins the meeting and where the audio comes
from**. This is also why `acs-join.html` keeps working with the VM deallocated, and why
the two rows above disagree only about the ACS resource and the VM.

> ⚠️ **The joiner page can look enabled when it isn't.** `ACS_ENABLED` is
> `ACS_ENDPOINT or ACS_CONNECTION_STRING or MEETING_BOT_ENABLED`, so deploying the
> `in-call` profile *without* an ACS resource makes `/api/acs/config` report
> `enabled: true` — the page loads and offers to join, then `/api/acs/token` fails with
> a 500 because there is no resource to mint a VoIP identity from. If you want the
> browser joiner, set `ENABLE_ACS=true` as well; the two flags are independent.

> ⚠️ **Never run both in one meeting.** Two assistants would hear each other's answers and
> feed back. Leave one before starting the other.

> **Set these once per shell.** Everything below refers to them, so nothing in this
> runbook is tied to one deployment:
>
> ```powershell
> $rg      = azd env get-value AZURE_RESOURCE_GROUP
> $appName = azd env get-value SERVICE_APP_NAME
> $appUrl  = azd env get-value SERVICE_APP_URI          # https://<app>.<region>.azurecontainerapps.io
> $bot     = azd env get-value MEETING_BOT_OPERATOR_API # https://<vm-fqdn>:9441  (in-call profile only)
> ```
>
> The VM and NSG names (`avatar-meetingbot-vm`, `avatar-meetingbot-nsg`) are
> deterministic, so those are written literally.

The "Hears" row used to be the decisive difference, and it no longer is. The browser
joiner originally captured only the *operator's* microphone, so it could answer only what
**you** said — which was the whole reason the media bot existed. That changed on
2026-08-03: `acs-join.js` now intercepts `HTMLMediaElement.prototype.srcObject` and takes
remote participants' audio from the DOM, because the SDK has to attach those streams to a
media element in order to play them.

Verified in a real meeting with the microphone disabled (`?mic=0`), so the only possible
source was another participant:

```text
capture stats: maxRms=0.15861 remoteStreams=1 wiredTracks=2 remoteMeters=2
               remoteMaxRms=0.18466 micCapture=False
User transcript: 'Hey Simone, how are you?'
```

Two honest limits. Only **one** other human was present, so per-participant vs. mixed
stream delivery is still unobserved; and `wiredTracks=2` against `remoteStreams=1` means
a second stream was attached — plausibly our own outgoing audio — which the `selfTalking`
half-duplex gate suppresses, making that gate load-bearing. See
[d-in-call-headless.md](channels/d-in-call-headless.md) for detail.

The media bot still differs in kind: it receives Teams' mixed audio through a supported
first-party API, whereas the browser rides an SDK implementation detail. If a future SDK
renders remote audio purely through Web Audio, `remoteMaxRms` returns to 0. `?remote=0`
disables the hook without a redeploy.

---

## Common prerequisites

Both paths need a **classic** join link:

```
https://teams.microsoft.com/l/meetup-join/19%3ameeting_...%40thread.v2/0?context=...
```

The short `https://teams.microsoft.com/meet/<id>` link carries **no thread id and no
tenant**, so neither path can use it. Get the classic form from the invite body: right-click
*"Click here to join the meeting"* → **Copy link**.

If the meeting is organized in a tenant other than the bot's own, that tenant needs the
one-time admin consent described in `meeting-bot/README.md`.

Watch the backend while you test (both paths log here):

```powershell
az containerapp logs show -n $appName -g $rg --type console --follow
```

Is anything connected right now?

```powershell
curl.exe "$appUrl/api/acs/status"
# {"enabled":true,"active":false,"count":0}   <- count is live media sessions
```

---

## Browser joiner

1. Start (or join) the Teams meeting yourself, in the Teams client.
2. Open the joiner in a **separate browser tab**: `$appUrl/acs-join.html`
3. Paste the classic link → **Join meeting**. Allow the mic prompt.
4. Admit the participant from the lobby if Teams asks.
5. Ask a question **out loud into your own mic**, prefixed with the wake phrase
   (e.g. *"Nuru, what were the main risks in the last board meeting?"*).

### What to check

| | Expect |
| --- | --- |
| Roster | a participant named **Nuru** |
| Tile | the **avatar's face**, lip-synced while she answers |
| Between turns | tile falls back to the branded placard (this is intentional) |
| Voice | she answers aloud, and stops mid-sentence if you talk over her |
| **Mute** button | she goes silent; the face may keep moving (see below) |

### Backend log signature (a healthy turn)

```
[browser browser-…] avatar video stream started
[avatar] stream opened: video=h264 audio=aac -> NV12 640x360@15, PCM16 24000Hz
[avatar] first PCM16 chunk (3136 bytes)
…
[avatar] decoder stopped (video=0 audio=NNNN)
```

`video=0` is **correct** on this path — the browser decodes the picture itself, so the
server only decodes audio.

### Known, deliberate behaviours (not bugs)

- **She only hears you.** Other people in the room are inaudible to her. This is the design
  limit of the browser leg, not a fault.
- **Mute silences the voice, not the face.** Video fragments are never dropped, because
  MediaSource needs a byte-contiguous stream (the `ftyp`/`moov` init segment arrives once
  per session, so dropping fragments corrupts everything after them). Silence is enforced on
  the audio path — which is what the room actually hears.
- **Keep the joiner tab visible if you can.** Backgrounded tabs get throttled; there is an
  explicit `requestFrame()` keep-alive, but a foreground tab is the safe test.

### Rollback (voice keeps working)

```powershell
az containerapp update -n $appName -g $rg `
  --set-env-vars BROWSER_JOIN_VIDEO_ENABLED=false
```

---

## Media bot (the one that hears the room)

This is the milestone that closes #27, and it is **working end to end**: the avatar
joins, hears every participant, and answers aloud with a lip-synced camera tile.
Use this runbook to re-verify after a change.

### 1. Confirm the VM is up

```powershell
az vm start -n avatar-meetingbot-vm -g $rg   # if deallocated
curl.exe "$bot/api/health"
# {"status":"ok"}
```

### 2. Make sure the browser joiner is NOT in the meeting

`/api/acs/status` should report `count: 0`.

### 3. Join

```powershell
$link = "<paste the classic meetup-join link>"
$body = @{ joinUrl = $link } | ConvertTo-Json
Invoke-RestMethod "$bot/api/join" -Method POST -Body $body -ContentType "application/json"
# -> { "callId": "..." }
```

Keep the `callId`; to leave:

```powershell
Invoke-RestMethod "$bot/api/leave" -Method POST -ContentType "application/json" `
  -Body (@{ callId = "<callId>" } | ConvertTo-Json)
```

Lost the `callId`? Omit it and the bot leaves every call it is in:

```powershell
Invoke-RestMethod "$bot/api/leave" -Method POST -ContentType "application/json" -Body "{}"
```

### 4. The test that matters

Have **someone else** (or a second device) ask the question. If she answers *that* voice,
the media bot has done the one thing the browser path cannot.

| | Expect |
| --- | --- |
| Roster | **Nuru** appears (admit from lobby if prompted) |
| **Hearing** | answers a person who is **not** you |
| Tile | lip-synced avatar face, not the placeholder |
| Barge-in | she yields when a human starts talking |
| Latency | note it — this adds a media hop on top of Voice Live first-token |

### If the face is wrong but the voice is fine

Almost always a **dimension mismatch**: Python must emit exactly the format the bot
negotiated. Check both sides agree — `MEETING_BOT_VIDEO_WIDTH/HEIGHT/FPS` on the container
app vs `Bot__VideoWidth/Height/Fps` on the VM (defaults `640x360@15`). The bot silently
drops mismatched frames and shows its placeholder.

Bot-side logs (on the VM): look for `Video send status = Active`.

### Rollback to the proven audio-only behaviour

```powershell
az containerapp update -n $appName -g $rg `
  --set-env-vars MEETING_BOT_VIDEO_ENABLED=false
# and on the VM: Bot__EnableVideo=false, then restart the service
```

> **Restarting the bot service:** stop it, wait for port **8445** to clear, *then* start.
> Restarting too quickly fails with `Media platform failed to initialize -> AddressInUse`,
> which looks like a media fault but is only a stale listener.

### 5. Afterwards — stop paying for the VM

```powershell
az vm deallocate -n avatar-meetingbot-vm -g $rg
```
