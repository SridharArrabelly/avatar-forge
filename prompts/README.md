# prompts/

Single source of truth for every prompt the Foundry agent and runtime
use. Centralising them here keeps prompt changes reviewable in PR diffs
without chasing string literals across the codebase.

## Layout

```
prompts/
├── README.md                          # this file
└── agent/
    ├── description.md                 # one-line agent description (UI / catalog)
    ├── instructions-nonreasoning.md   # full system instructions, gpt-4.x / gpt-4o family
    ├── instructions-reasoning.md      # full system instructions, o-series / gpt-5 family
    └── routing-test-questions.md      # manual checklist: does each question hit the right tool?
```

`description.md` is deliberately a single line — it is the agent's short
description shown in the Foundry catalog, not a document. It looks empty in
an editor preview; it is not.

`routing-test-questions.md` is the only file here that is **not** pushed to the
agent. It is the regression checklist you run *after* editing either
`instructions-*.md`, to confirm internal questions still route to
`azure_ai_search` and external ones to `bing_custom_search`.

`setup_foundry_agent.py` selects ONE of the two `instructions-*.md` files
at agent-provisioning time based on `AGENT_MODEL` — reasoning models
(o-series, gpt-5) get the deliberate / multi-step prompt; everything
else (gpt-4.x, gpt-4o) gets the literal / hard-rule prompt. Same
predicate that gates the `reasoning.effort` parameter, so the prompt
and the model capability stay in lock-step. Falls back to the
non-reasoning variant if the reasoning file is missing.

Both variants share: voice-first output rules, the silent meeting
catalogue contract, and the `bing_custom_search` query-style-by-intent
block. They differ on tool-selection rigidity (hard rules vs softer
principles), max calls per turn (one vs up to three), and whether
exhaustive "X → tool" examples are spelled out.

Future prompts (per-tool routing rules, category-specific instructions,
clarification templates, UI captions) belong in subfolders under
`prompts/`, e.g. `prompts/tools/<tool>.md` or `prompts/ui/<surface>.md`.

## Format

Plain Markdown text. No templating, no variable substitution — runtime
context (catalogue, TODAY, etc.) is injected as a separate system
message at session start by the backend, not woven into these files.

Files are loaded as UTF-8 with leading/trailing whitespace stripped.
Headings, lists, and inline code formatting are passed through to the
model verbatim.

## Loading

`scripts/setup_foundry_agent.py` reads these at agent-provisioning
time via a small `_load_prompt()` helper resolved relative to the repo
root. The runtime backend (`backend/`) does NOT read this folder —
it references the agent by name and lets Foundry serve the stored
instructions back.

## Editing

Edit the `.md` file, run `uv run python scripts/setup_foundry_agent.py`
to push a new agent version, and commit the prompt change in the same
PR as any code that depends on it (tool wiring, routing rules, etc.).
