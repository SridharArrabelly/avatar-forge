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