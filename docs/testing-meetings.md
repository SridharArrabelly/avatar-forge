# Testing the two in-meeting paths

The assistant can join a Teams meeting two different ways. They are **not**
interchangeable, they are tested differently, and they must never be pointed at the same
meeting at the same time.

| | Browser joiner | Media bot |
| --- | --- | --- |
| Page / API | `acs-join.html` | `POST :9441/api/join` |
| Runs on | your laptop's browser tab | the Windows VM |
| WebSocket | `/ws/acs/browser` | `/ws/acs/audio` |
| **Hears** | **only your machine's mic / shared audio** | **everyone in the meeting** |
| Face | browser decodes fMP4 → canvas → ACS tile | Python decodes → NV12 → `VideoSocket` |
| Needs the VM running | no | **yes** (~$283/mo if left on) |
| Status | proven in real meetings | **proven in real meetings** — joins, hears the room, answers aloud with a lip-synced tile |

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

The single most important difference is the "Hears" row. The browser joiner captures the
*operator's* audio, so it can only answer what **you** say into your own mic. The media bot
receives the meeting's mixed audio from Teams, so it can answer **anyone**. That is the
whole reason the media bot exists.

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

## Path A — browser joiner

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

## Path B — media bot (the one that hears the room)

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
