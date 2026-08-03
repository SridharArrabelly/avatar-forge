# Teams Meeting Bot — design & architecture (channel C, issue #27)

> **Goal in one sentence:** let the avatar (Nuru) **join a Teams meeting, hear every
> participant — including remote callers — and answer their spoken questions aloud**,
> grounded in the same board-minutes + news knowledge it already uses.

This document is the **single source of truth** for *why* this part of the system looks the
way it does. It is deliberately detailed because the design is non-obvious: it mixes a
Python service and a .NET service on purpose, and that decision needs to be defensible.

- [1. The problem, precisely](#1-the-problem-precisely)
- [2. The three options we evaluated](#2-the-three-options-we-evaluated)
- [3. The decision (and why)](#3-the-decision-and-why)
- [4. Final architecture](#4-final-architecture)
- [5. How audio flows (the bridge)](#5-how-audio-flows-the-bridge)
- [6. Identity, permissions & admission](#6-identity-permissions--admission)
- [7. Turn-taking & barge-in](#7-turn-taking--barge-in)
- [8. Hosting & infrastructure](#8-hosting--infrastructure)
- [9. Compliance & consent](#9-compliance--consent)
- [10. The two-step delivery plan](#10-the-two-step-delivery-plan)
- [11. Risks, costs & open questions](#11-risks-costs--open-questions)
- [12. Glossary](#12-glossary)

---

## 1. The problem, precisely

The avatar already answers spoken questions beautifully **on the web** (mic → Azure Voice
Live → Foundry agent with AI Search RAG + Bing news → spoken answer). Channel C is **not**
about answering — that pipeline is reused untouched. The hard, new problem is **meeting
media transport**: getting *the room's* audio into that pipeline and the avatar's voice
back into *the room*.

Two facts, **proven live**, shape everything below:

1. **A browser / Teams-tab client can only hear its own microphone.** This is Teams
   *client isolation* — by design, a web client never receives other participants' audio
   streams. We verified this three independent ways (our own logs showed `wiredTracks=0`,
   `maxRms=0`; the SDK exposes no public remote-audio track; Microsoft's own guidance
   confirms it). **No setting changes this.** So the shipped browser path can never hear
   a room.

2. **ACS Call Automation server-side media does not deliver Teams *meeting* audio.** Its
   bidirectional audio streaming works for ACS / PSTN / Teams-*user* calls, but not for a
   Teams *meeting*. So "use ACS from Python" — attractive because ACS *has* a Python SDK —
   does **not** solve the room-audio problem either.

Therefore the room's audio can only be obtained **server-side, through Microsoft's Teams
calling/media platform**, which is the next section.

---

## 2. The three options we evaluated

| Option | How it admits to the meeting | Can it hear the **whole room**? | Language / host | Verdict |
| --- | --- | --- | --- | --- |
| **(a) ACS Call Automation + Teams interop** | ACS service joins via meeting link | **No** — does not carry Teams *meeting* audio (proven) | Python ✅ / Linux ACA ✅ | ❌ Doesn't meet the goal |
| **(b) Graph Real-Time Media bot** (`Microsoft.Skype.Bots.Media`) | Graph `JoinGroupCall` as an application-hosted media bot | **Yes** — receives the mixed participant audio | **.NET only / Windows only** | ✅ The only path that hears everyone |
| **(c) Browser / shared-stage tab** | Embed the web avatar on the meeting stage | **No** — client isolation (proven) | Python/JS ✅ | ⚠️ Great for *presence*, not for *hearing* |

The uncomfortable truth: **the only option that actually hears the room is (b), and (b) is
.NET + Windows-only.** There is no Python SDK for `Microsoft.Skype.Bots.Media`, and the
media stack must run on a Windows guest OS. This is a genuine platform constraint, not a
preference.

---

## 3. The decision (and why)

> **D-2b: Keep the Python pipeline as the unchanged "brain", and add one *thin* .NET /
> Windows media-bot as the meeting's ears and mouth. Do not rewrite the solution to .NET.**

### Why mix languages instead of going all-.NET

- **Only one component is .NET/Windows-locked** — the media ingestion. Everything else
  (Voice Live, the Foundry agent, RAG, the web app, infra) is fully
  supported in Python and already deployed and working.
- **The .NET bot carries no business logic.** It is a *media pump*: join → grab PCM →
  forward → play back. All intelligence (STT, retrieval, answer, TTS, turn-taking) stays
  in Python, behind a clean, language-agnostic WebSocket seam that **already exists**
  (`/ws/acs/audio`, see §5).
- **A full .NET rewrite would not even remove the tax.** The real cost here is *Windows
  hosting for the media stack*, not the C#-vs-Python choice. Rewriting the whole working
  app to .NET would throw away a deployed system, add large risk, and you would *still*
  need the Windows media host. It buys nothing where it actually hurts.

### What this costs (be honest)

1. **A Windows host** (VM / VMSS / Windows-node AKS) — the media bot cannot ride the
   existing Linux Azure Container App. This is the biggest operational tax.
2. **One extra network hop** (PCM over the bridge WebSocket) on top of Voice Live's
   first-token latency. Mitigated by co-locating the bot in the same region.
3. **A second toolchain** (.NET build + NuGet + a separate container/host + a bicep
   module). Contained, because the bot's scope is small and stable.

The boundary is a deliberate, documented seam — not tangled interop.

---

## 4. Final architecture

```mermaid
flowchart LR
    P["<b>Microsoft Teams meeting</b><br/><i>in:</i> participants speak<br/><i>out:</i> the avatar's voice"]

    subgraph Win[".NET media bot (NEW) · Windows host"]
        direction TB
        J["Graph Calling SDK<br/>JoinGroupCall + signalling"]
        A["Skype.Bots.Media<br/>AudioSocket in/out"]
        B["Bridge client<br/>WS to /ws/acs/audio"]
        J --- A
        A <-- "PCM16" --> B
    end

    subgraph Py["Python backend (UNCHANGED brain) · Linux ACA"]
        direction TB
        WS["/ws/acs/audio<br/>+ AcsVoiceBridge"]
        H["VoiceSessionHandler"]
        WS <-- "send_audio_bytes up<br/>send_binary down" --> H
    end

    V["Azure Voice Live"]
    F["Foundry agent<br/>AI Search RAG + Bing news"]

    P <== "mixed room audio up<br/>the avatar's voice down" ==> A
    B <== "AudioMetadata + AudioData over WSS<br/>both directions" ==> WS
    H <-- "PCM16 up · PCM16 answer down" --> V
    V <-- "turn in · answer out" --> F
```

**Two services, one clean seam:**

| | .NET media bot (new) | Python backend (existing) |
| --- | --- | --- |
| **Runs on** | Windows host | Linux Azure Container App |
| **Owns** | Meeting join, media sockets, the bridge client | Voice Live, Foundry, RAG, turn-taking, barge-in |
| **Knows about answering?** | No — it only moves audio | Yes — all of it |
| **Talks to the other via** | `wss://<app>/ws/acs/audio` (PCM16 `AudioData` frames) | the same WebSocket |

The seam is the **already-built** `/ws/acs/audio` endpoint and `AcsVoiceBridge`
(`backend/acs/`). It was written for ACS Call Automation's wire format
(`AudioMetadata` then base64-PCM16 `AudioData` frames). **The .NET bot mimics that exact
wire format**, so the Python side needs little or no change — the bot simply becomes a new
producer/consumer of a protocol the backend already speaks.

---

## 5. How audio flows (the bridge)

The contract on `/ws/acs/audio` (see `backend/acs/bridge.py::AcsVoiceBridge`):

**Inbound — bot → Python (the room speaking):**
1. On connect, send one metadata frame:
   `{"kind":"AudioMetadata","audioMetadata":{"sampleRate":16000,"channels":1,"encoding":"pcm"}}`
2. Then a stream of audio frames (20 ms each):
   `{"kind":"AudioData","audioData":{"data":"<base64 PCM16>","silent":false}}`
   The bridge base64-decodes and calls `handler.send_audio_bytes(pcm)` → Voice Live.

**Outbound — Python → bot (Nuru answering):**
- Voice Live emits PCM16; `bridge.send_binary` wraps it as
  `{"Kind":"AudioData","AudioData":{"Data":"<base64 PCM16>"}}` and sends it down the same
  socket. The bot decodes and writes it to its outbound `AudioSocket` → into the meeting.
- Barge-in: when a human starts talking, Voice Live interrupts and the bridge sends
  `{"Kind":"StopAudio"}`; the bot **flushes its outbound audio buffer** immediately so
  Nuru stops mid-sentence.

**Format:** 16-bit PCM, mono. Voice Live accepts 16 kHz or 24 kHz; the Graph media platform
delivers 16 kHz mono — so we run the seam at **16 kHz** end-to-end and no resampling is
needed. (`ACS_AUDIO_SAMPLE_RATE` already governs this on the Python side.)

> **Design win:** because the protocol, turn-taking, and barge-in already live in Python,
> the .NET bot is small and "dumb". All future answer/behavior changes stay in Python.

---

## 6. Identity, permissions & admission

The bot needs its **own** Entra app registration — dedicated to it,
because an Entra app can back only one Azure Bot resource (reusing one fails with
`MsaAppId is already in use`).

**Graph application permissions** (admin-consented once per tenant that hosts meetings):

- `Calls.JoinGroupCall.All`
- `Calls.JoinGroupCallAsGuest.All`
- **`Calls.AccessMedia.All`** — this is the one that unlocks the room's raw audio
- `OnlineMeetings.Read.All`

**Also required:**

1. **A client secret** on that app — the bot authenticates to Graph with it
   (`az ad app credential reset`).
2. **An Azure Bot registration with a *calling* webhook** — Graph delivers call
   signaling (incoming/established/participants) to this HTTPS endpoint. Follows the
   the repo's additive-module pattern plus the calling webhook URL; codified in
   `infra/modules/meetingBotHost.bicep`.
3. **Teams app manifest** with `supportsCalling: true`, and a **tenant policy** that
   allows bots in meetings.

The bot acquires its token against the **meeting organizer's** tenant, not its own, so
any tenant can host the meeting provided it has admin-consented the app. A tenant
whose admin you cannot reach is a hard blocker — see
[`admin-checklist.md`](../admin-checklist.md).

**Admission (lobby vs auto-admit):** the bot joins via `JoinGroupCall` using the
meeting's join information. Whether it lands in the lobby or is auto-admitted is
governed by the meeting's options; for a smooth demo the organizer sets the
bot/everyone to auto-admit, or admits it once from the lobby.
---

## 7. Turn-taking & barge-in

So Nuru never talks over the room, the **existing** policy in `AcsVoiceBridge` is reused
unchanged:

- **Wake-phrase gate** (`ACS_REQUIRE_WAKE_PHRASE`, `ACS_WAKE_PHRASES`, e.g. *"Hey Nuru"*):
  the avatar stays silent during normal conversation and only answers an utterance that
  addresses her. Non-addressed responses are cancelled early to save tokens/latency.
- **Barge-in:** Voice Live's semantic VAD detects a human starting to speak and interrupts;
  the bridge emits `StopAudio`; the bot flushes playback. Nuru yields immediately.
- **Idle watchdog:** after `ACS_IDLE_TIMEOUT_S` of no speech the media session closes.

The .NET bot does **not** implement any of this — it just carries audio and honours
`StopAudio`. Behaviour stays centralised in Python.

---

## 8. Hosting & infrastructure

| Piece | Host | Notes |
| --- | --- | --- |
| Python brain (Voice Live, Foundry, bridge) | **Linux Azure Container App** (today) | Unchanged. |
| .NET media bot | **Windows** (VM / VMSS / Windows-node AKS / Windows container) | Required by the media stack — **cannot** be Linux/ACA. |
| Public **media endpoint** | on the Windows host | TLS cert + a signaling port (TCP) + a media port range, reachable from Teams. |
| Azure Bot registration | global | Calling webhook → the bot's signaling URL. |
| ACS resource | optional | Only if a fallback ACS path is kept; not required for option (b). |

All new infra is **additive and conditional** (mirrors
`communicationServices.bicep`): a deploy **without** channel C enabled behaves exactly as
today. The Windows host is the one piece that is materially new and carries ongoing cost.

**Recommended first host:** a single **Windows Server VM** (simplest to stand up and debug
the media stack) → graduate to VMSS / Windows-node AKS if scale/HA is needed later.

---

## 9. Compliance & consent

A live, automated participant that listens to a meeting has obligations even when it does
**not** record:

- **Disclosure:** participants must be told an AI assistant is present and listening.
  Surface it via the bot's display name (e.g. "Nuru (AI assistant)") and a one-time chat
  notice when it joins.
- **Consent / notification:** follow the organization's and jurisdiction's meeting-
  notification rules; some regions require explicit notice that audio is processed.
- **Data handling:** audio is streamed to Azure Voice Live for real-time STT/answer; it is
  not persisted by the bot. If transcripts are ever stored, that becomes a records-retention
  decision to document separately.
- **No covert capture:** the far-side `getDisplayMedia` workaround (browser interim) must
  keep its explicit, user-initiated button — never silent.

These are surfaced to the user in `teams/README.md` (Compliance section) and must be
re-confirmed before any production use.

---

## 10. The two-step delivery plan

### Step 1 — **Audio**: Nuru hears everyone and answers aloud *(this is the definition of done for #27)*

1. **Mint the client secret** on `avatar-forge-meeting-bot`; store it safely (azd env / Key
   Vault), never in source.
2. **Register a calling-enabled Azure Bot** with a calling webhook URL.
3. **Build the .NET media bot** (`meeting-bot/`): Graph Communications Calling SDK +
   `Skype.Bots.Media`; `JoinGroupCall`; inbound/outbound `AudioSocket`; **bridge client** to
   `/ws/acs/audio` speaking the `AudioMetadata`/`AudioData` protocol; honour `StopAudio`.
4. **Stand up the Windows host** + public media endpoint (cert + ports). Start with a
   single Windows VM.
5. **Teams manifest** `supportsCalling: true`; confirm the tenant policy allows the bot in
   meetings.
6. **Live test:** bot joins a real meeting, hears multiple participants, answers on the
   wake phrase, yields on barge-in. Measure added latency.

**Outcome:** the avatar is a real audio participant that answers the room. No face yet.

> **Status: built and live-verified.** Steps 1–6 are complete. The calling-bot
> registration and Windows host are codified in `infra/modules/meetingBotHost.bicep`
> and deploy with `azd up`; the .NET bot lives under `meeting-bot/`; the Python↔.NET
> bridge contract (`VoiceLiveBridgeClient`) is unit-tested. The bot joins real
> meetings, hears every participant, and answers aloud. Operating instructions:
> [`c-in-call-media-bot.md`](./c-in-call-media-bot.md); code-local detail and the
> traps that cost debugging time: [`meeting-bot/README.md`](../../meeting-bot/README.md).

### Step 2 — **Face**: Nuru is visible in the meeting

Same bot foundation; a second slice. The route is **decided**:

- **✅ Route A — server VideoSocket (CHOSEN):** capture the Azure avatar's WebRTC
  video → decode → NV12 → feed the bot's `VideoSocket` → a true participant **camera
  tile**, lip-synced to the answer audio because *both streams come from one Voice Live
  avatar synthesis*. This is a real-time transcode pipeline in .NET on the Windows host
  — the riskiest sub-system (latency, CPU, frame pacing) — but it is the only route that
  delivers a genuinely **synced** face, which is the user's explicit requirement.
- **❌ Route B — shared-stage Companion (REJECTED):** embedding the existing web avatar on
  the meeting stage is cheap, but its lips are driven by a *separate* browser synthesis
  with no timing relationship to the audio the bot speaks — an **unsynced** face. The user
  and the rubber-duck review agreed an unsynced face is misleading/waste, so Route B is
  not pursued.

**The full Route A design — architecture, data flow, why audio and video must share one
synthesis, component changes, the aiortc↔Voice Live feasibility risk, and the phased
increments — is in [`c-design-avatar-video.md`](./c-design-avatar-video.md).**

**Build order (as executed):** audio leg → video scaffold (flag-gated
`VideoSocket` + placeholder NV12 tile) → the hard increment (server-side avatar WebRTC
capture → real NV12 frames over the bridge). Audio value was never blocked on the video
work, and `Bot:EnableVideo=false` still yields the byte-for-byte audio-only session.

> **Status: complete and live-verified.** `MeetingBotService.CreateLocalMediaSession`
> adds the outbound NV12 `VideoSocket`; `CallHandler` runs the playout loop; the bridge
> carries `VideoData` frames decoded from Voice Live's avatar stream in Python. Lip-sync
> is driven by both streams coming from one synthesis, as designed. Implementation
> detail: [`c-design-avatar-video.md`](./c-design-avatar-video.md).
---

## 11. Risks, costs & open questions

| Risk / unknown | Impact | Mitigation |
| --- | --- | --- |
| **Windows media host** is new operational surface | Cost + ops burden | Start with one Windows VM; keep it conditional/additive; document teardown |
| **Tenant policy** may disallow bots in meetings | Blocks join | Verify policy early (global admin can set it); part of Step 1.5 |
| **Added latency** (extra PCM hop) on top of Voice Live first-token | Slower replies | Co-locate bot + backend in the same region; 16 kHz, 20 ms frames; measure |
| **Lobby/admission** friction | Bot stuck in lobby | Set auto-admit for the demo, or admit once |
| **Media-stack setup** (certs, ports, public reachability) is fiddly | Slow first join | Follow the Graph Communications sample topology; single VM first |
| **Route A video** transcode cost | High CPU / latency | Sized the host at 4 vCPU (`D4s_v5`); 360p/15fps; measured at ~125 ms added transport |

**The genuinely hard parts** were (1) standing up the Windows media host + endpoint and
(2) Route A video. Both are now done; everything else reuses code/identity that already
existed.

---

## 12. Glossary

- **Voice Live** — Azure real-time speech service (STT + TTS + avatar) the answer pipeline
  is built on; unchanged here.
- **Foundry agent** — Azure AI Projects agent with AI Search RAG + Bing news that produces
  the answers; unchanged here.
- **AcsVoiceBridge** — the existing Python adapter (`backend/acs/bridge.py`) that turns an
  `AudioData` WebSocket into a Voice Live session, with turn-taking and barge-in.
- **Graph Communications Calling SDK** — Microsoft's .NET SDK for calling/online-meeting
  bots (`Microsoft.Graph.Communications.Calls`).
- **Skype.Bots.Media** — the Windows-only real-time media library that exposes the meeting's
  `AudioSocket`/`VideoSocket`.
- **Application-hosted media** — the bot processes media itself (vs. service-hosted), which
  is what lets it access the room's raw audio.
- **Client isolation** — originally recorded here as "Teams' rule that a web/client leg
  only receives its own mic, the reason the browser path can't hear the room."
  **That was wrong**, and it is worth keeping the correction visible: it conflated *the
  ACS SDK declining to hand you a track* (`getMediaStreamTrack()` returns nothing) with
  *the client not receiving the audio at all*. The client plainly does receive it — that
  is how the call is audible — and intercepting `HTMLMediaElement.prototype.srcObject`
  reaches it. Verified live; see
  [d-in-call-headless.md](./d-in-call-headless.md). The media bot's real advantage is
  that it reads the room through a **supported API** rather than an implementation
  detail.

---

*See also: [`architecture.md`](../architecture.md) for the overall system, and
[`c-in-call-media-bot.md`](./c-in-call-media-bot.md) for the operator steps and admin
requests.*
