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
│   ├── instructions-nonreasoning.md   # system instructions, gpt-4.x / gpt-4o family
│   ├── instructions-reasoning.md      # system instructions, o-series / gpt-5 family
│   └── routing-test-questions.md      # manual checklist: does each question hit the right tool?
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
| prompt file | `agent/instructions-{reasoning,nonreasoning}.md` | `realtime/instructions.md` |
| which variant | chosen from `AGENT_MODEL` — see below | only one file |
| loaded by | `scripts/setup_foundry_agent.py` | `backend/voice/instructions.py` |
| **loaded when** | **agent-provisioning time** — baked into the stored agent definition | **runtime** — read from the image on first session, then cached |
| sent as | the agent's own stored instructions | `session.instructions`, prefilled every turn |

So in agent mode the running container never reads `prompts/` at all: it
references the agent by name and Foundry serves the stored instructions back.
In model mode the container reads the file directly and there is no agent.

### The agent variant is picked from the model family

`_load_agent_instructions()` calls `_model_supports_reasoning(AGENT_MODEL)`:

* `o1*` / `o3*` / `o4*` and anything starting `gpt-5` → **`instructions-reasoning.md`**
  (deliberate, multi-step: softer principles, up to three tool calls per turn,
  one refined follow-up search).
* everything else — `gpt-4.1`, `gpt-4o`, `gpt-4` → **`instructions-nonreasoning.md`**
  (literal: hard rules, "EXACTLY ONE tool per turn", exhaustive `X → tool`
  examples).
* if the reasoning file is missing it falls back to the non-reasoning variant,
  so a partial checkout never bricks the agent.

This is the same predicate that gates the `reasoning.effort` parameter, so the
prompt and the model capability stay in lock-step. With the shipped default
`AGENT_MODEL=gpt-5.4`, the **reasoning** variant is the one in use.

Both agent variants share the voice-first output rules, the silent meeting
catalogue contract, and the `bing_custom_search` query-style-by-intent block.
They differ on tool-selection rigidity, calls allowed per turn, and whether the
`X → tool` examples are spelled out.

### Sizes, because this is a latency-sensitive path

Measured as the text actually sent, not the file on disk:

| prompt | sent | approx. tokens |
| --- | --- | --- |
| `realtime/instructions.md` | 4,106 chars | ~1,000 |
| `agent/instructions-reasoning.md` | 15,006 chars | ~3,750 |
| `agent/instructions-nonreasoning.md` | 18,074 chars | ~4,500 |

The realtime prompt is deliberately the smallest: it is prefilled on every model
turn, and a realtime model is tuned for immediacy rather than for following a
long procedural brief. The agent prompts are larger because they arbitrate
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
stripped before sending**, so authoring notes cost no prefill. The agent prompts
have no such separator — every line in them is sent.

`description.md` is deliberately a single line; it is the agent's short
description in the Foundry catalog, not a document. It looks empty in an editor
preview. It is not.

`routing-test-questions.md` is the only file here that is **never** sent
anywhere. It is the regression checklist to run *after* editing either
`instructions-*.md`, to confirm internal questions still route to
`azure_ai_search` and external ones to `bing_custom_search`.

Future prompts (per-tool routing rules, clarification templates, UI captions)
belong in subfolders here, e.g. `prompts/tools/<tool>.md`.

## Editing

**Model mode** — edit `realtime/instructions.md`, then `azd deploy`. The file
ships in the container image and is read at runtime, so a deploy is enough.

**Agent mode** — edit the `instructions-*.md` file, then push a new agent
version:

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
