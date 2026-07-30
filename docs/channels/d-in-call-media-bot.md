# Channel D — In-call avatar (Graph media bot)

The avatar **joins a Teams meeting as a participant**, hears the whole room, and
answers aloud with a lip-synced camera tile.

This is the only channel with true in-call presence, and the only one with hard
administrator dependencies. **Read [`../admin-checklist.md`](../admin-checklist.md)
before provisioning anything** — the VM is the expensive part and it is useless
without a Teams app access policy that only a Teams administrator can create.

Requires: [channel A](a-web.md) deployed.

**Why this shape:** [d-design-media-bot.md](d-design-media-bot.md) (the three
options evaluated, and why a .NET/Windows media relay is bridged to the Python
brain rather than rewriting either) and
[d-design-avatar-video.md](d-design-avatar-video.md) (why the face and the voice
must come from one synthesis).

---

## 1. What you get

Two join paths, sharing one brain:

| Path | Hears | Use it for |
| --- | --- | --- |
| **Browser joiner** (`acs-join.html`) | Only the operator's microphone | Quick demos, no VM, no Teams policy |
| **Media bot** (Windows VM) | **The whole room** | The real capability |

The media bot is the definition of done. The browser joiner is a genuinely useful
fallback when the admin path is blocked — it ships value with no VM at all.

**Architecture in one line:** Teams ⇄ .NET media bot on a Windows VM ⇄ WebSocket
⇄ Python backend on ACA ⇄ Voice Live + Foundry agent. The .NET side contains **no
answering logic**; it is a media relay.

Why the split: `Microsoft.Graph.Communications.Calls.Media` carries a native
Windows media stack, so it **must** run on a Windows Server guest OS. The brain
stays in Python where the rest of the product lives.

## 2. What deploys

Two deployments today — the container side via `azd`, the VM side separately:

```bash
# 1. container side: serve the media-bot bridge at /ws/acs/audio
azd env set MEETING_BOT_ENABLED true
azd env set BOT_APP_ID <app-id>          # the Azure Bot registration D also needs
azd up

# 2. VM side
az deployment group create -g <rg> -f meeting-bot/infra/host.bicep
#    then install/refresh the service on the host:
pwsh meeting-bot/scripts/setup-host.ps1
```

| Resource | Notes |
| --- | --- |
| Windows Server VM + NIC + NSG + public IP with DNS label | `meeting-bot/infra/host.bicep` |
| Azure Bot with **calling** enabled | Webhook points at the VM FQDN |
| Open ports **9441** (control API) and **8445** (media/signalling) | NSG |
| `MEETING_BOT_ENABLED=true` on the container app | Serves `/ws/acs/audio` |

> **Known gap:** the VM is provisioned outside the `azd` flow. Folding
> `host.bicep` into the main deployment gated on `MEETING_BOT_ENABLED` is the
> obvious next improvement, so channel D becomes one command like the others.

Independent of `ENABLE_ACS` — the media bot does not require an ACS resource.

## 3. Manual / admin steps

Summary; the authoritative list with "who must do it" is in
[`../admin-checklist.md`](../admin-checklist.md).

| # | Step | Who |
| --- | --- | --- |
| 1 | Entra app registration + secret | You / Entra admin |
| 2 | Graph app permissions: `Calls.JoinGroupCall.All`, `Calls.JoinGroupCallAsGuest.All`, **`Calls.AccessMedia.All`**, `OnlineMeetings.Read.All` | Entra admin |
| 3 | **Admin consent** | **Entra admin** |
| 4 | Bot calling webhook → VM FQDN | You |
| 5 | **Teams application access policy** (`New-CsApplicationAccessPolicy` + `Grant-…`) | **Teams admin — hard blocker** |
| 6 | Real TLS certificate for the VM FQDN | You |

Steps 3 and 5 are **one-time and permanent** — they survive redeploys and VM
rebuilds. Worth saying when you ask.

The bot needs **no Teams license** in the tenant.

## 4. How to verify

```powershell
# host is up
Invoke-RestMethod -Uri "https://<vm-fqdn>:9441/api/health"     # -> {"status":"ok"}

# join a meeting
$join = '<Teams meeting join URL>'
Invoke-RestMethod -Method Post -Uri "https://<vm-fqdn>:9441/api/join" `
  -ContentType 'application/json' -Body (@{ joinUrl = $join } | ConvertTo-Json)

# live media counters while in the call (underruns, dropped, bufferedMs)
Invoke-RestMethod -Uri "https://<vm-fqdn>:9441/api/stats"

# leave (callId optional — omit to leave everything)
Invoke-RestMethod -Method Post -Uri "https://<vm-fqdn>:9441/api/leave" `
  -ContentType 'application/json' -Body '{}'
```

Expected: the avatar appears as a participant, its camera tile shows the face,
and asking a grounded question produces a spoken answer.

Full test procedure: [`../testing-meetings.md`](../testing-meetings.md).

> **`/api/stats` is the only observability the bot has.** Running as a Windows
> service, its stdout goes nowhere and it writes no log file. Poll `/api/stats`
> during a call rather than hunting for logs.

**Before debugging media problems, read the "traps that cost real debugging time"
section of [`../../meeting-bot/README.md`](../../meeting-bot/README.md).** It
records failures that each cost hours — the assembly/`deps.json` resolution trap,
the port-8445 restart race, and the single assumption (that the Voice Live avatar
stream is *continuous*, so idle means digital silence rather than no data) that
caused four separate defects.

## 5. Cost & teardown

**The VM runs about $140/month** and is by far the dominant cost of this channel.
It does not scale to zero.

```bash
# stop paying for compute between test sessions (keeps the disk and the FQDN)
az vm deallocate -n <vm-name> -g <rg>
az vm start -n <vm-name> -g <rg>      # when you next need it
```

Deallocating preserves the DNS label and the Teams access policy, so restarting
costs nothing but time. **Deallocate after every test session** — this is the
single easiest way to waste money on this project.

To remove the channel entirely: delete the VM resources and set
`MEETING_BOT_ENABLED=false`, then redeploy. Channels A–C are unaffected.
