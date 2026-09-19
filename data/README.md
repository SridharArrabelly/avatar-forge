# data/

Source corpus for the Azure AI Search index. The agent answers from these
documents at runtime via the AI Search index named in `SEARCH_INDEX_NAME`
(see top-level README → "Build the Azure AI Search index").

## What goes here

The default is **structured meeting DOCX files**, using
`CHUNKING_MODE=section` and `DOCUMENT_SCOPE=minutes`.
The supplied ten demo meetings produce **60 whole-section blocks**. Their
dated filenames and headings (Agenda, Key Discussion Points, Decisions Made,
Action Items and Next Steps) are validated. Unsupported structure, tables,
tracked changes and notes fail explicitly rather than silently losing evidence.

For legacy/general ingestion, explicitly set `CHUNKING_MODE=window` and
`DOCUMENT_SCOPE=all`. Subfolders are walked recursively. Supported extensions,
auto-detected by [scripts/setup_aisearch_index.py](../scripts/setup_aisearch_index.py):

- `.docx`
- `.pdf`
- `.md`, `.markdown`
- `.txt`

The window mode uses the configurable character window/overlap, historically
1,200/200. It does not have the same evidence boundaries as section mode.
To add another format to that path, register a reader in `READERS`.

`README.md` files are skipped — they are repo documentation, not corpus content.

## Folder layout decides `documentType`

The folder a file sits in sets its `documentType`, which is indexed as a
filterable field **and** prepended into every chunk's text (`Type: …`) so the
agent can tell the two corpora apart when it answers:

| location | `documentType` | contents |
| --- | --- | --- |
| `data/` | `MeetingMinutes` | board and executive meeting minutes |
| `data/policies/` | `Policy` | official policies, procedures, standards, codes |

So **put policy documents in `data/policies/`, not loose in `data/`** — a policy
dropped at the top level would be labelled and answered as if it were meeting
minutes. Meeting minutes keep the `Board Meeting – DD Month YYYY` filename
pattern, which is what `parse_meeting_date()` reads the date from; policies have
no date and are not listed in the meeting catalogue.

`DOCUMENT_SCOPE=minutes` excludes the designated policy folders before files
are opened; it does not infer document type from their contents. `all` includes
all supported corpus files. Neither option deletes records from an existing
index. To add another designated policy folder, extend `POLICY_DIRS`.

## `data/policies/` is deliberately NOT committed

This repository is **public**, and the policy corpus is real internal corporate
documentation. `data/policies/` is therefore in `.gitignore` and the files are
supplied locally.

The meeting minutes in `data/` **are** committed demo content. Customer policy
documents and questions are excluded from the current default and evaluation;
do not restore them just to reproduce historical tests. Any separately
authorized additional corpus requires an explicit ingestion scope and suitable
layout. Do not commit private documents or evaluation transcripts.

## (Re)build the index

```powershell
uv run python scripts/setup_aisearch_index.py

# Optional explicit settings and an ingestion receipt:
uv run python scripts\setup_aisearch_index.py --env-file .env.evaluation --manifest .azure\ingestion-receipt.json
```

Section indexes are immutable versions: reruns verify an identical corpus.
Choose a **new `SEARCH_INDEX_NAME`** for changed documents, layout or embedding
dimensions. Existing window indexes are not migrated or deleted automatically;
section mode rejects `RECREATE_INDEX=true`. The old window/all path remains
available explicitly, including its legacy rebuild option when deliberately needed.

The folder location is configurable via the `DATA_DIR` env var (default `./data`).