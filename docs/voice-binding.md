# Voice Live binding — agent mode and model mode

Voice Live can be bound to two different things, and the choice changes the shape
of every turn. This page is the design record for that choice: what each binding
gives you, what it costs, and what was measured rather than assumed.

- **Agent mode** (`VOICE_BINDING=agent`, the default) binds Voice Live to the
  Foundry agent. This is the original design and the one every channel has shipped
  on.
- **Model mode** (`VOICE_BINDING=model`) binds Voice Live directly to a realtime
  speech-to-speech model, and moves the tools in-process.

Both are supported. The default is unchanged, so a deployment that sets nothing
behaves exactly as it always has.

---

## 1. Why a second binding exists

The assistant's job is to answer an executive out loud, in a meeting, without an
awkward pause. Answer *quality* was never the problem — the corpus and the tools
were already good. The problem is the gap between someone finishing their question
and the avatar starting to speak.

In agent mode that gap has a fixed floor, because the turn is a chain:

```
speech ─▶ recognizer ─▶ agent reasons ─▶ managed tool ─▶ agent resumes ─▶ synthesis ─▶ speech
```

Every stage is a hop, and the recognizer has to finish before the agent can start.
Microsoft's own Voice Live documentation splits its model table along exactly this
line: `gpt-realtime` and friends take **native speech input**, while `gpt-4o`,
`gpt-4.1` and `gpt-5` take *"audio input through Azure speech to text"*. The
overview says chaining speech-to-text, dialog and text-to-speech *"can lead to
increased engineering complexity and end-user perceived latency."*

Model mode deletes the recognizer stage from the answer path. Audio goes to the
model; audio comes back.

---

## 2. The two shapes

```mermaid
flowchart LR
    subgraph agent["Agent mode — VOICE_BINDING=agent"]
        direction LR
        A1[microphone] <--> A2[recognizer]
        A2 <--> A3[Foundry agent]
        A3 <--> A4[managed tools<br/>AI Search + Bing grounding]
        A3 <--> A5[synthesis]
        A5 <--> A6[avatar]
    end
```

```mermaid
flowchart LR
    subgraph model["Model mode — VOICE_BINDING=model"]
        direction LR
        B1[microphone] <--> B2[gpt-realtime]
        B2 <--> B3[in-process tools<br/>search_minutes + search_web]
        B2 <--> B4[synthesis or native audio]
        B4 <--> B5[avatar]
    end
```

The recognizer leaves the **answer path** — transcription is still configured in both
modes (it is one field on the Voice Live session, not a component), but the model
works from the audio and no longer waits for the text. The tools also moved from
*inside Foundry* to *inside this application*.

---

## 3. What model mode gives

### Measured, against real audio

These are from a probe that speaks a real question into a real session and marks
every response boundary, not from reasoning about the architecture.

> ⚠️ **The first row is not a like-for-like comparison, and the A/B is still
> outstanding.** The two cells measure **different events** — agent mode's figure is
> time to first *token*, model mode's is time to first *audio*. They were also taken
> on **different deployments**; the one the agent numbers came from no longer exists.
> The direction is plausible, the magnitude is unverified. Treat the row as two
> separate observations until both modes are re-measured on one deployment, quoting
> the same marker. The probe already records both markers in every run, so this is a
> matter of quoting the right field, not of new instrumentation.

| | agent mode | model mode |
| --- | --- | --- |
| after the speaker stops | 3.1–4.4 s to first **token** | **1.7–2.4 s** to first **audio** |
| tool round trip | 1.3–1.9 s (managed) | **0.27–0.71 s** (in-process) |
| new Azure resources | — | **none** |

The second and third rows are sound: tool round trip is the same event measured the
same way in both, and the resource count is structural.

A single turn, instrumented end to end:

```
0.00s  speech stopped, response created
1.00s  assistant text begins
1.23s  function call starts
2.03s  audio playing
2.43s  function call result          (tool cost: 1.2s)
3.97s  the answer itself
6.24s  generation complete
```

The whole turn **generates in 6.2 s while emitting 32 s of speech** — generation
runs roughly 5× faster than realtime. This matters because it separates two
problems that are easy to confuse: *how long until she starts speaking* (latency,
which model mode improves) and *how long she then talks for* (length, which is a
prompt concern and independent of the binding).

### No new infrastructure

Voice Live manages the realtime model itself — there is no model deployment and no
quota request behind `VOICELIVE_MODEL`. Model mode provisions **nothing**. It is a
configuration change, not a deployment.

### The tools become ours

This is the part with genuine engineering consequences, and it is not optional.

Inspecting the SDK's session schema settles it:

```
ToolType values : FUNCTION, MCP          <- the complete list
tool classes    : FunctionTool, MCPTool
foundry/search  : none — no azure_ai_search, no bing_grounding
```

Binding to a model takes the agent out of the picture, and its managed tools go
with it. There is no mixing the two. So model mode ships its own:

| tool | source | measured |
| --- | --- | --- |
| `search_minutes` | the same `knowledge-index` the agent queried, hybrid + semantic | 620–714 ms |
| `search_web` | Web IQ, host-allow-listed | 268–298 ms warm |

Owning them is also the reason they are faster: an in-process function can be
cached, pre-warmed, and trimmed. A managed tool cannot.

`search_web` is advertised to the model **only when a key is configured**. With no
key the tool does not exist as far as the model is concerned, and the assistant
answers from the minutes corpus alone — the same graceful degradation the agent
path has for a missing Bing connection.

---

## 4. What model mode costs

Two real trade-offs. Neither is a bug, and both should inform the choice.

### Semantic end-of-utterance detection is unavailable

Semantic EOU is text-based, so it needs the local speech recognizer that only
exists in the cascaded pipeline. The service rejects it outright:

> Text-based end-of-utterance detection requires a local speech recognizer and is
> only supported on cascaded pipelines.

It fails the **entire** `session.update`, not just the field, so this cannot be
left for the service to ignore — the code drops it explicitly when binding to a
model. Model mode therefore falls back to plain silence-duration turn-taking and
loses the 500 ms → 300 ms tuning that agent mode benefits from.

That tuning is worth ~200 ms per turn, against model mode's ~1.5 s saving. The
trade is favourable, but it is a trade.

### The voice choice is a fork, not a preference

A realtime model can emit audio natively, with no synthesis stage at all. That is
the lowest-latency output available. But the service is explicit about what it
costs:

> Interim response in realtime pipeline requires an Azure TTS voice
> (azure-standard, azure-custom, azure-personal, or avatar-voice-sync). OpenAI
> native voices stream audio directly and cannot support interim response
> injection.

Read carefully, that sentence describes the mechanism: OpenAI voices stream audio
**directly**, which means Azure voices do *not* — they pass through a synthesis
stage. And that stage is precisely what makes Azure custom neural voice reachable.

| | OpenAI native voice | Azure voice |
| --- | --- | --- |
| synthesis stage | none | yes |
| latency | lowest | one stage more |
| custom neural voice | ✗ | ✓ |
| personal voice | ✗ | ✓ |
| interim response | ✗ | ✓ |

So for a branded avatar, the synthesis stage **is the reason to be on Voice Live**
rather than talking to a realtime model directly. Choosing an OpenAI native voice
forfeits custom voice, personal voice and interim response together — it is one
decision, not three.

Because the combination is rejected with a hard error rather than ignored, the
code null-s interim response whenever an OpenAI voice is selected, and logs why.

Verified in model mode with the avatar enabled: `AzureStandardVoice`,
`AzurePersonalVoice` and `AzureCustomVoice` are all accepted. The custom-voice
roadmap survives model mode intact.

---

## 5. Comparing the two

The binding is **deployment-wide**. There is no per-session override: a client cannot
ask for a binding, and nothing in `DEVELOPER_MODE` exposes one. To compare, set
`VOICE_BINDING`, redeploy, and run the same script of questions again.

That is deliberate. A live switch would have bought a side-by-side A/B at the cost of
a permanent flag on the session path and one more way for a deployment to answer
differently than production does. It also would not have fixed the thing that actually
makes the two hard to compare.

> **The real trap is metric definitions, not deploy variance.** The numbers already
> recorded for the two modes measure *different events* — model mode's figure is time
> to **first audio**, agent mode's is time to **first token**. Those are not the same
> quantity, and no amount of running them side by side makes them comparable. Before
> any A/B, pin both modes to the same marker.

Each websocket already opens its own Voice Live connection and shares no state, so the
constraint is purely which binding the deployment was built with.

---

## 6. Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `VOICE_BINDING` | `agent` | `agent` or `model`. Anything else falls back to `agent`. |
| `VOICELIVE_MODEL` | `gpt-realtime-2` | Realtime model bound in model mode. Managed by Voice Live — no deployment, no quota. Ignored in agent mode. |
| `WEBIQ_API_KEY` | *(unset)* | Enables `search_web`. Stored as a **container-app secret**, never a plain env var. Unset leaves the web tool off. |
| `WEBIQ_BASE_URL` | code default | Web IQ endpoint. Optional. |
| `WEBIQ_ALLOWED_DOMAINS` | *(empty)* | Comma-separated host allow-list applied to results. |

`WEBIQ_ALLOWED_DOMAINS` is the same security boundary as `bingAllowedDomains`: a
hard host allow-list is what makes an open-web tool safe to hand an executive
assistant. Leaving it empty allows any host the endpoint returns.

To switch a deployment over:

```powershell
azd env set VOICE_BINDING model
azd env set WEBIQ_API_KEY <key>          # optional, enables the web tool
azd env set WEBIQ_ALLOWED_DOMAINS "mtn.com,sashares.co.za"
azd up
```

To switch back, set `VOICE_BINDING=agent` and redeploy. Nothing is provisioned or
destroyed either way.

### The prompt moves too

In agent mode the persona lives on the Foundry agent, in `prompts/agent/`. In model
mode there is no agent to hold it, so it is sent with the session from
`prompts/realtime/instructions.md` — ~3.9 KB as actually sent (the loader strips
editor commentary), against the agent's 15–19 KB.

It is a **separate prompt on purpose**, not a copy. A prompt written for a
reasoning model does not transfer unchanged to a realtime one — realtime models
trade some reasoning depth for immediacy, and they respond to different
instructions. Two findings from tuning it are worth keeping:

- **Bounding the model's preamble beat suppressing it.** Two attempts at "never
  narrate" failed outright. "At most four words" produced a consistent
  *"One moment."* — which usefully covers the ~1.2 s tool gap. Working with the
  behaviour beat fighting it.

  > **Superseded — the acknowledgement is now suppressed, and this is being
  > retested.** Live testing found the preamble fires on nearly every tool-backed
  > turn, which is nearly every turn, and a canned phrase ahead of each answer
  > reads as a tic rather than as reassurance. Two things produced it
  > independently — the `interim_response` platform feature *and* this prompt
  > clause — so muting one alone changed nothing audible. Both are now off by
  > default. The finding above is kept because it records a real failure mode:
  > if the model reverts to *longer* improvised preambles, bounding rather than
  > forbidding is the known-good fallback. See issue for the tuning work.
- **`max_response_output_tokens` does not control spoken length.** Swept at 1200,
  400 and 200, the answer ended cleanly every time and the duration barely moved,
  because the cap counts *text* tokens and a three-sentence answer is only ~75.
  It is a guillotine that never falls. Spoken length is a prompt concern.

---

## 7. Which to choose

| Situation | Binding |
| --- | --- |
| Anything shipping today, unchanged behaviour required | **agent** |
| Perceived latency is the priority | **model** |
| The managed Bing grounding tool is required as-is | **agent** |
| Semantic EOU tuning matters more than ~1.5 s | **agent** |
| Branded custom neural voice | either — keep an Azure voice |

Agent mode remains the default deliberately. Model mode is the faster path and the
one with fewer moving parts at runtime, but it moves tool correctness from
Foundry's problem to ours, and retrieval quality is the thing to watch: the managed
`azure_ai_search` tool does its own query rewriting and semantic ranking, so a
comparison that only measures milliseconds can "win" by answering worse.

---

## 8. Testing

`scripts/test_voice_binding.py` pins the switch — 23 checks, no Azure required.

The load-bearing assertions are the **negative** ones: that agent mode gains none
of the model-mode keys. That is the property that keeps the default deployment
byte-identical, and it is the one most likely to be broken by a careless edit.

It is mutation-verified. Dropping the `DEVELOPER_MODE` gate, the OpenAI-voice
guard, or the model-mode EOU drop each makes it fail.

```powershell
uv run python scripts\test_voice_binding.py
```

---

## See also

- [Configuration reference](configuration.md) — every environment variable
- [Architecture](architecture.md) — how a turn flows through the system
- [Channels](channels/README.md) — the delivery surfaces this binding sits under
