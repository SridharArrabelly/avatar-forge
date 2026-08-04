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
│   ├── instructions01.md              # DRAFT — loaded by nothing, see below
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

### `instructions01.md` is a draft — no code path loads it

Both trees contain an `instructions01.md`: one 5,811-byte prompt written to serve
**both** bindings, on the theory that a shorter, less ambiguous brief would make
tool decisions faster.

**Neither copy is loaded by anything.** `grep -rn instructions01` over the repo
returns no matches: `backend/voice/instructions.py` hardcodes
`realtime/instructions.md`, and `_load_agent_instructions()` only ever reaches for
the two `instructions-*.md` variants. They are checked-in drafts, not live prompts.

The agent copy has been measured against the live prompt — a clone agent carrying
only the prompt change, 5 interleaved rounds, 30 answers per arm:

| | `instructions-reasoning.md` | `instructions01.md` |
| --- | --- | --- |
| routing | 30/30 | 30/30 — identical |
| answer latency | 6.90 s | 8.08 s (+17%) |
| answers leaking a `【n:m†source】` marker | 0/30 | **18/30** |

Those markers are Foundry's own citation annotations, and the avatar pronounces
them aloud character by character. **Do not promote the agent copy as it stands** —
it buys no routing improvement, costs 17% latency, and adds that defect.

That verdict is **agent-mode only, and it does not transfer** — measured, not assumed.
The markers come from the *managed* `azure_ai_search` / `bing_custom_search` tools;
model mode calls in-process Python returning `{meeting, date, extract}`, so no marker
of that shape exists to leak.

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
