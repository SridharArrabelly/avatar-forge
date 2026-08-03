# Channel D — In-call avatar (headless browser)  ⟵ placeholder

**Status: not built.** This page exists to hold the design and, more importantly,
to **fix the comparison criteria before the work starts** — so the eventual
choice between this and [channel C](c-in-call-media-bot.md) is a decision rather
than a justification written afterwards.

---

## The idea

Instead of a server-side media bot using the Graph Communications calling stack,
run a **headless browser** (Chromium) that joins the meeting as an ordinary
participant through the Teams web client, and wire its microphone and speaker to
the existing Voice Live pipeline. This is the approach taken by the ADIA design.

### Reference architecture *(proposed — nothing here is built or verified)*

```mermaid
flowchart LR
    CAL["Microsoft 365 calendar"]

    subgraph ACA["Azure Container App"]
        direction TB
        W["Calendar watcher"]
        CH["Headless Chromium<br/>Playwright"]
        API["FastAPI backend<br/><i>the existing brain, unchanged</i>"]
        W -- "launch session" --> CH
        CH -- "raw PCM 24 kHz over WS" --> API
    end

    TM["Teams meeting"]
    VL["Azure Voice Live<br/>+ Foundry agent"]

    CAL -- "Graph: find meeting + joinUrl" --> W
    CH == "ACS Web SDK join" ==> TM
    API -- "Graph: post chat" --> TM
    API <-- "audio up · answer down<br/>function call = act" --> VL
```

Two things this diagram implies that [channel C](c-in-call-media-bot.md) does not:

- **The join is scheduled, not operator-triggered.** A calendar watcher finds the
  meeting via Graph and launches a browser session. C needs a human to `POST /api/join`.
  That watcher is net-new work, and reading the calendar is itself a Graph permission.
- **The whole thing runs in a container**, so it can scale to zero between meetings —
  the single biggest cost argument against C's always-on Windows VM.

> **Discrepancy to resolve before building.** The reference diagram says *ACS Web SDK
> join*, but the description above says *Teams web client*. These are different
> mechanisms with different consequences: the ACS Web SDK joins as an anonymous interop
> guest (we already run this in C's browser joiner), whereas driving the real Teams web
> client means a signed-in account and a licence.
>
> **Update (2026-08-03) — the ACS variant may not be a dead end after all.** This note
> previously said the ACS Web SDK "cannot hear other participants", so D "collapses".
> The observation behind that is real and stands: `getMediaStreamTrack()` on a
> `RemoteAudioStream` yields nothing — measured live, `wiredTracks 0, maxRms 0`. But
> that is a fact about **one SDK method**, not about the browser.
>
> The [ADIA reference implementation](#adia) takes the remote stream from the DOM
> instead: it intercepts `HTMLMediaElement.prototype.srcObject`, so when the SDK
> attaches a participant's stream to an element in order to *play* it, the page keeps a
> reference and routes it into a capture worklet. The SDK is never asked.
>
> `acs-join.js` now carries that interception behind a diagnostic (`?mic=0` isolates it
> from the microphone; `capture_stats.remoteMaxRms` is the readout). **Until that has
> been run in a real meeting with someone else speaking, D's viability is open, not
> settled — in either direction.**

## <a id="adia"></a>Prior art — the ADIA implementation

A client-built agent (`ADIA-Agent`) implements this same architecture end to end and is
worth reading before writing any of it here:

| Concern | How ADIA does it |
| --- | --- |
| Host | `mcr.microsoft.com/playwright/python` container, one browser context per meeting |
| Join | ACS Web Calling SDK, `agent.join({ meetingLink })`, anonymous guest |
| Room audio in | intercepts `HTMLMediaElement.prototype.srcObject` → capture worklet → WS |
| Agent audio out | overrides `getUserMedia` to return a `MediaStreamDestination`, so the "microphone" *is* the agent |
| Devices | no PulseAudio or Xvfb — `--use-fake-device-for-media-stream` plus the override |

Two things it does **not** establish. Its own README says the incoming-audio path is
preview and *"validate against a real meeting"*, and its tests cover config, prompts and
Voice Live helpers — **nothing exercises the audio bridge or the browser**. So it is a
credible architecture, not evidence that the capture works.

Also inherited by any anonymous-guest design: the guest has **no Teams chat identity**
(ADIA posts chat through a separate M365 service account), and each meeting costs a full
Chromium session, so concurrency is expensive.

## Why it is worth evaluating

Channel C works, but its cost is not the VM — it is the **admin dependency**. C
requires Graph application permissions, admin consent, and a Teams application
access policy that only a Teams administrator can grant. In tenants where those
are unobtainable, C is simply unavailable regardless of engineering effort.

The hypothesis under test: **a browser joins as a guest, so none of that applies.**
If true, D removes every hard blocker in C. That is the entire case for it.

## What is unknown

Recorded honestly, before any work:

- **Which joiner mechanism it actually is** — ACS Web SDK (anonymous guest, known
  to be deaf to other participants) or the real Teams web client (needs an account
  and a licence). See the discrepancy note above; this one question decides whether
  the channel is viable at all
- Whether meeting policy permits anonymous/guest join in the target tenant
- Whether a camera tile can be published (the avatar's *face*, not just voice) —
  C achieves this; a browser may be limited to screen share
- Audio fidelity and added latency versus C's measured budget
- Stability over long meetings, and behaviour when the lobby is enabled
- Container cost and whether it can scale to zero (a real advantage over C's
  always-on VM at ~$283/month)
- Whether it violates any acceptable-use terms — **check this first**, because a
  negative answer ends the evaluation immediately

## <a id="comparison"></a>Comparison criteria — agreed up front

Both options are scored on the same axes. Fill this in after building D; do not
add or drop criteria afterwards.

| Criterion | C — Graph media bot | D — headless browser |
| --- | --- | --- |
| **Admin dependency** *(decisive)* | Graph consent + **Teams access policy** | Hypothesis: none |
| Hears the whole room | Yes | ? |
| Publishes a camera tile (the face) | Yes | ? |
| Added latency over the Voice Live budget | ~125 ms transport | ? |
| Audio fidelity | Verified clean | ? |
| Idle cost | ~$283/month VM (`D4s_v5`), no scale-to-zero | ? |
| Operational fragility | Native Windows media stack; documented traps | ? |
| Terms-of-use standing | Supported, first-party API | ? |
| Effort to reach parity | Built | ? |

**Decision rule:** if D clears admin dependency *and* publishes a camera tile at
comparable quality, it supersedes C and C should be retired rather than kept in
parallel. Maintaining two in-call implementations is a cost nobody is paying for.
If D cannot show a face, it is a fallback for policy-blocked tenants, not a
replacement.

## When to build it

After channel C is documented and stable — which it now is. Track under the
follow-up to issue #27.
