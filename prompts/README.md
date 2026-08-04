# prompts/

Single source of truth for every prompt the agent and the runtime use.
Centralising them here keeps prompt changes reviewable in PR diffs without
chasing string literals across the codebase.

## Layout

```
prompts/
├── README.md                          # this file
├── routing-test-questions.md          # shared checklist: does each question hit the right tool?
├── agent/                             # used when VOICE_BINDING=agent
│   ├── description.md                 # one-line agent description (UI / catalog)
│   └── instructions.md                # system instructions — the only agent prompt
└── realtime/                          # used when VOICE_BINDING=model
    └── instructions.md                # system instructions, gpt-realtime family
```

## Which prompt is used, and when

**The two bindings do not share a prompt.** They load different files, by
different mechanisms, at different times. That last column is the one that
catches people out.

| | agent mode | model mode |
| --- | --- | --- |
| selected by | `VOICE_BINDING=agent` (the default) | `VOICE_BINDING=model` |
| prompt file | `agent/instructions.md` | `realtime/instructions.md` |
| loaded by | `scripts/setup_foundry_agent.py` | `backend/voice/instructions.py` |
| **loaded when** | **agent-provisioning time** — baked into the stored agent definition | **runtime** — read from the image on first session, then cached |
| sent as | the agent's own stored instructions | `session.instructions`, prefilled every turn |

So in agent mode the running container never reads `prompts/` at all: it
references the agent by name and Foundry serves the stored instructions back.
In model mode the container reads the file directly and there is no agent.

### One agent prompt, loaded unconditionally

There used to be two agent prompts, `instructions-reasoning.md` and
`instructions-nonreasoning.md`, chosen from `AGENT_MODEL` by
`_model_supports_reasoning()`. With the shipped `AGENT_MODEL=gpt-5.4` only the
reasoning one was ever loaded, so the other drifted untested while every
measurement in this repo was taken against the file that shipped. **The selector
made an unmaintained path look supported**, which is worse than having one prompt.

Both the second file and the selection logic are gone. `create_agent()` now calls
`_load_prompt("agent", "instructions.md")` directly — no model check, no variants,
no fallback. Change the model and you get the same prompt; if that ever stops
working, re-tune the prompt rather than reintroduce a branch.

**The old name was also misleading.** It described the *model family*, not the
runtime setting — the production agent runs `gpt-5.4` with `reasoning.effort="none"`
(the deliberate default for conversational latency; see `AGENT_REASONING_EFFORT` in
[configuration.md](../docs/configuration.md)). So the "reasoning" prompt was always
running with reasoning switched **off**. `_model_supports_reasoning()` still exists,
but now only gates the `reasoning.effort` parameter, which is all it ever really
described.

The agent prompt carries the voice-first output rules, the silent meeting
catalogue contract, and the `bing_custom_search` query-style-by-intent block.

### A rejected draft, and what measuring it taught us

An alternative prompt (`instructions01.md`) once sat in **both** trees: a single
5,811-byte brief written to serve both bindings, on the theory that a shorter, less
ambiguous prompt would make tool decisions faster. Both copies have been **deleted**.
The measurements are kept here because they are the reason, and because they say
something durable about the two bindings.

**Agent mode** — a clone agent carrying only the prompt change, 5 interleaved rounds,
30 answers per arm:

| | `agent/instructions.md` | the draft |
| --- | --- | --- |
| routing | 30/30 | 30/30 — identical |
| answer latency | 6.90 s | 8.08 s (+17%) |
| answers leaking a `【n:m†source】` marker | 0/30 | **18/30** |

Those markers are Foundry's own citation annotations, and the avatar pronounces them
aloud character by character. No routing gain, 17% slower, plus that defect.

**Model mode** — the same method via `scripts/route_test_model.py`, driving a real
Voice Live model session, 5 interleaved rounds, 28 scored answers per arm:

| | `realtime/instructions.md` | the draft |
| --- | --- | --- |
| routing | 16/28 (57%) | **26/28 (93%)** |
| failed to answer | 7/28 (25%) | **11/28 (39%)** |
| answers leaking a marker or URL | 0/28 | **0/28** |
| answer latency | 4.17 s | 4.47 s (+7%) |

Three things worth carrying forward:

1. **A prompt verdict does not transfer between bindings.** The citation leak that
   disqualified the draft in agent mode is impossible in model mode: the markers come
   from the *managed* `azure_ai_search` / `bing_custom_search` tools, whereas model mode
   calls in-process Python returning `{meeting, date, extract}`. Measure on the binding
   you intend to ship.
2. **Better routing is not automatically a better answer.** The draft routes far better
   *and* answers less often — the same fact stated twice. The live prompt sends public
   questions to the minutes corpus, which usually has an answer; the draft correctly
   sends them to Web IQ, which often does not.
3. **In model mode the prompt is not the binding constraint — web retrieval is**
   (see issue #78). No prompt edit fixes a snippet that lacks the figure.

To trial a replacement prompt, point the model-mode harness at it directly — it takes
arbitrary arms and needs no deployment:

```powershell
uv run python scripts/route_test_model.py --runs 5 --arms LIVE=prompts/realtime/instructions.md,TRIAL=<path>
```

There is deliberately no env override for the prompt path, so promoting one means
replacing the file its loader expects.

### Sizes, because this is a latency-sensitive path

Measured as the text actually sent, not the file on disk:

| prompt | sent | approx. tokens |
| --- | --- | --- |
| `realtime/instructions.md` | 4,106 chars | ~1,000 |
| `agent/instructions.md` | 15,006 chars | ~3,750 |

The realtime prompt is deliberately the smallest: it is prefilled on every model
turn, and a realtime model is tuned for immediacy rather than for following a
long procedural brief. The agent prompt is larger because it arbitrates
between two tools for a reasoning model. Do not copy one into the other.

## Format

Plain Markdown. Loaded as UTF-8 with leading/trailing whitespace stripped;
headings, lists and inline code are passed to the model verbatim.

`{{AVATAR_NAME}}` is substituted in **both** trees with the resolved persona
name — `_apply_brand()` in `setup_foundry_agent.py`, and
`load_realtime_instructions()` at session start. It is the same value the stage
and the Teams package use, so the avatar never introduces itself as someone
else. Never hardcode a persona name in a prompt.

`realtime/instructions.md` additionally uses a `---` convention: **everything
above the first horizontal rule is commentary for whoever edits the file and is
stripped before sending**, so authoring notes cost no prefill. The agent prompt
has no such separator — every line in it is sent.

`description.md` is deliberately a single line; it is the agent's short
description in the Foundry catalog, not a document. It looks empty in an editor
preview. It is not.

`routing-test-questions.md` is the only file here that is **never** sent
anywhere. It is the regression checklist, shared by both bindings, to run
*after* editing either `agent/instructions.md` or `realtime/instructions.md`,
confirming internal questions still route to the minutes tool and external ones
to the web tool.

Future prompts (per-tool routing rules, clarification templates, UI captions)
belong in subfolders here, e.g. `prompts/tools/<tool>.md`.

## Editing

**Model mode** — edit `realtime/instructions.md`, then `azd deploy`. The file
ships in the container image and is read at runtime, so a deploy is enough.

**Agent mode** — edit `agent/instructions.md`, then push a new agent version:

```powershell
uv run python scripts/setup_foundry_agent.py
```

> ⚠️ **`azd up` will not do this for you on an existing environment.** The
> postprovision hook creates the agent on *greenfield only*; against an already
> provisioned Foundry it prints `[brownfield] Skipping Foundry agent creation`
> and your prompt edit silently never reaches the agent. Re-running the script
> creates a new agent *version*, which is the supported update path.

Commit the prompt change in the same PR as any code that depends on it (tool
wiring, routing rules), and run `routing-test-questions.md` afterwards.
