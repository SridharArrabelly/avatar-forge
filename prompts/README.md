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
│   └── instructions.md                # active system instructions, concise v4
└── realtime/                          # used when VOICE_BINDING=model
    └── instructions.md                # system instructions, gpt-realtime family
```

Everything here is prompt text or its documentation. The whole folder is copied into the container
image (`Dockerfile`), so anything that is *not* a prompt does not belong — the
historical routing regression checklist that used to sit here now lives in
[evaluation history](../docs/evaluation-history.md). The
[assistant evaluation guide](../docs/assistant-evaluation.md) defines the approved
retrieval-first scope for new work.

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
`_model_supports_reasoning()`. With the then-shipped `AGENT_MODEL=gpt-5.4` only the
reasoning one was ever loaded, so the other drifted untested while every
measurement in this repo was taken against the file that shipped. **The selector
made an unmaintained path look supported**, which is worse than having one prompt.

Both the second file and the selection logic are gone. `create_agent()` now calls
`_load_prompt("agent", "instructions.md")` directly — no model check, no variants,
no fallback. Change the model and you get the same prompt; if that ever stops
working, re-tune the prompt rather than reintroduce a branch.

**The old name was also misleading.** It described the *model family*, not the
runtime setting — the original production agent ran `gpt-5.4` with
`reasoning.effort="none"`. The current repository default is Terra with that same
explicit effort (see `AGENT_REASONING_EFFORT` in
[configuration.md](../docs/configuration.md)). So the "reasoning" prompt was always
running with reasoning switched **off**. `_model_supports_reasoning()` still exists,
but now only gates the `reasoning.effort` parameter, which is all it ever really
described.

The agent prompt carries voice-first output rules, the silent meeting catalogue
contract, meeting/public routing and financial-source/unit safeguards.

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

**Model mode** — the same method via `scripts/bench_routing_model.py`, driving a real
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
uv run python scripts/bench_routing_model.py --runs 5 --arms LIVE=prompts/realtime/instructions.md,TRIAL=<path>
```

There is deliberately no env override for the prompt path, so promoting one means
replacing the file its loader expects.

### Concise v4 instructions

[`agent/instructions.md`](agent/instructions.md) now contains the concise v4
instructions evaluated on Terra/none. All four supported placeholders remain;
onboarding expands once. Repeated routing/voice rules and source-specific rule
examples are removed. The prompt retains date resolution, cents/rand handling,
honest missing-evidence responses and the three-sentence/70-word default.

Financial queries target issuer financial statements or income statements
instead of only results highlights. An unsupported total must be acknowledged
before any separately labelled service-revenue fallback. This does not
guarantee that managed Bing supplies the requested total.

Each selected tool is limited to one call per turn, replacing the previous
refinement allowance. Both tools are allowed only for an explicitly requested
meeting/public comparison. This intentional behavior change passed a bounded
smoke gate, not a broad quality certification.

See [the v4 evaluation](../docs/terra-prompt-evaluation.md) for exact prompt
versions, failed intermediate trials, final checks and remaining limitations.
The loader still selects one prompt unconditionally; no model-specific prompt
selector or environment override was added. Existing deployments require the
instruction-only update described below, not a web-image redeployment.

### Sizes, because this is a latency-sensitive path

Rendered character counts with the display name Nuru, including shared
onboarding expansion:

| prompt | characters sent |
| --- | ---:|
| `realtime/instructions.md` | 4,467 |
| `agent/instructions.md` (v4) | 5,931 |

These are character counts, not exact model tokens. V4 is 68.5% smaller than the
18,855-character rendered agent prompt it replaces. Smaller instructions do not
by themselves prove lower latency; tool results, cache state and answer behavior
also matter. Both prompts describe meeting minutes and public information, but
they are evaluated on different tool/voice paths. Do not copy one into the other.

## Format

Plain Markdown. Loaded as UTF-8 with leading/trailing whitespace stripped;
headings, lists and inline code are passed to the model verbatim.

`{{AVATAR_NAME}}` is substituted in **both** trees with the resolved persona
name — `_apply_brand()` in `setup_foundry_agent.py`, and
`load_realtime_instructions()` at session start. It is the same value the stage
and the Teams package use, so the avatar never introduces itself as someone
else. Never hardcode a persona name in a prompt.

`{{ONBOARDING_GUIDANCE}}` expands the shared questions and spoken replies from
[`backend/onboarding.py`](../backend/onboarding.py) in both loaders. This is the
single place to edit the three introductory answers. It also supplies the
default tile questions; custom `SUGGESTED_PROMPTS` overrides remain supported.
The model still generates speech, but receives explicit answer guidance rather
than inferring its capabilities from general tool descriptions.

`realtime/instructions.md` additionally uses a `---` convention: **everything
above the first horizontal rule is commentary for whoever edits the file and is
stripped before sending**, so authoring notes cost no prefill. The agent prompt
has no such separator — every line in it is sent.

`description.md` is deliberately a single line; it is the agent's short
description in the Foundry catalog, not a document. It looks empty in an editor
preview. It is not.

Future prompts (per-tool routing rules, clarification templates, UI captions)
belong in subfolders here, e.g. `prompts/tools/<tool>.md`.

## Editing

**Model mode** — edit `realtime/instructions.md` or the shared onboarding module,
then deploy the web service. Both ship in the image and are read at runtime.

**Agent mode** — edit `agent/instructions.md`, then push a new agent version:

```powershell
uv run python scripts/setup_foundry_agent.py --env-file .azure\my-agent-env\.env --update-instructions
```

Use the existing agent environment's path. `--update-instructions` copies the
live definition and changes only instructions, preserving model, reasoning,
tools, connections, retrieval settings, description and metadata. It is
idempotent when the instructions already match, detects a changed latest
version before publication, and verifies readback. It requires an existing
prompt agent plus `PROJECT_ENDPOINT` and `AGENT_NAME`; it does not rebuild tools
from potentially stale local defaults. It cannot be combined with `--clone-from`.

> ⚠️ **`azd up` will not do this for you on an existing environment.** The
> postprovision hook creates the agent on *greenfield only*; against an already
> provisioned Foundry it prints `[brownfield] Skipping Foundry agent creation`
> and your prompt edit silently never reaches the agent. Re-running the script
> creates a new agent *version*, which is the supported update path. For shared
> onboarding edits, republish agent instructions **and** redeploy the web image
> in both environments so the tiles and model-mode instructions stay aligned.

Commit the prompt change in the same PR as any code that depends on it (tool
wiring, routing rules), and follow the
[assistant evaluation guide](../docs/assistant-evaluation.md) for validation.
Customer policy tests are excluded from all new evaluation; historical checklist
defaults are not the new-run plan. Model/voice comparisons require the explicit
retrieval-results review gate first.
