# prompts/

Single source of truth for every prompt the agent and the runtime use.
Centralising them here keeps prompt changes reviewable in PR diffs without
chasing string literals across the codebase.

## Layout

```
prompts/
├── README.md                          # this file
├── agent/                             # used when VOICE_BINDING=agent
│   ├── description.md                 # one-line agent description (UI / catalog)
│   ├── instructions.md                # system instructions — the only agent prompt
│   └── routing-test-questions.md      # manual checklist: does each question hit the right tool?
└── realtime/                          # used when VOICE_BINDING=model
    ├── instructions.md                # system instructions, gpt-realtime family
    └── instructions01.md              # DRAFT — loaded by nothing, see below
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

### `realtime/instructions01.md` is a draft — no code path loads it

`prompts/realtime/` still contains an `instructions01.md`: a 5,811-byte prompt
written to serve **both** bindings, on the theory that a shorter, less ambiguous
brief would make tool decisions faster. Nothing loads it —
`backend/voice/instructions.py` hardcodes `realtime/instructions.md`. It is a
checked-in draft, kept because it measures well on this binding (below).

An agent-tree copy also existed. It was measured against the live agent prompt — a
clone agent carrying only the prompt change, 5 interleaved rounds, 30 answers per
arm:

| | `agent/instructions.md` | the deleted agent draft |
| --- | --- | --- |
| routing | 30/30 | 30/30 — identical |
| answer latency | 6.90 s | 8.08 s (+17%) |
| answers leaking a `【n:m†source】` marker | 0/30 | **18/30** |

Those markers are Foundry's own citation annotations, and the avatar pronounces them
aloud character by character. The draft bought no routing improvement, cost 17%
latency and added that defect, so the agent copy was **deleted**.

That verdict is **agent-mode only, and it does not transfer** — measured, not assumed.
The markers come from the *managed* `azure_ai_search` / `bing_custom_search` tools;
model mode calls in-process Python returning `{meeting, date, extract}`, so no marker
of that shape exists to leak. That is why the realtime copy survives.

The realtime copy was then measured the same way, with `scripts/route_test_model.py`
driving a real Voice Live model session — 5 interleaved rounds, 28 scored answers per
arm:

| | `realtime/instructions.md` | `instructions01.md` |
| --- | --- | --- |
| routing | 16/28 (57%) | **26/28 (93%)** |
| failed to answer | 7/28 (25%) | **11/28 (39%)** |
| answers leaking a marker or URL | 0/28 | **0/28** |
| answer latency | 4.17 s | 4.47 s (+7%) |

**Both halves of that table matter.** The draft routes far better *and* answers less
often, because the two are the same fact: the live prompt sends public questions to the
minutes corpus, which usually has an answer, while the draft correctly sends them to
Web IQ, which often does not. In model mode the prompt is not the binding constraint —
**web retrieval is** (see issue #78). **Do not promote either copy on this evidence.**

To try either one, swap it over the filename its loader expects and restore
afterwards — there is deliberately no env override for the prompt path.

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
anywhere. It is the regression checklist to run *after* editing
`agent/instructions.md`, to confirm internal questions still route to
`azure_ai_search` and external ones to `bing_custom_search`.

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
