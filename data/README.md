# data/

Source corpus for the Azure AI Search index. The agent answers from these
documents at runtime via the AI Search index named in `SEARCH_INDEX_NAME`
(see top-level README → "Build the Azure AI Search index").

## What goes here

Drop files directly in this folder (subfolders are walked recursively).
Supported extensions, auto-detected by [scripts/setup_aisearch_index.py](../scripts/setup_aisearch_index.py):

- `.docx`
- `.pdf`
- `.md`, `.markdown`
- `.txt`

To add another format, register a reader in the `READERS` dict at the top of
that script.

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

To add another policy folder, extend `POLICY_DIRS` in that same script.

## (Re)build the index

```powershell
uv run python scripts/setup_aisearch_index.py

# or, to wipe and recreate (required after changing embedding model —
# vector dimensions are immutable on an existing index):
$env:RECREATE_INDEX = "true"
uv run python scripts/setup_aisearch_index.py
Remove-Item Env:\RECREATE_INDEX
```

The folder location is configurable via the `DATA_DIR` env var (default `./data`).