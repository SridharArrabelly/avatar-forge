"""Offline guards for isolated index creation and eventual search visibility."""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import setup_evaluation_index as setup


class Search:
    def __init__(self, responses):
        self.responses = iter(responses)

    def search(self, **kwargs):
        return next(self.responses)


def main() -> int:
    row = {"id": "synthetic", "source": "synthetic.docx", "content": "Exact source text", "chunk_index": 0}
    with patch.object(setup.time, "sleep") as sleep:
        result = setup.await_index_contents(Search([[], [row]]), [row])
        assert result == [row]
        sleep.assert_called_once_with(2)
    try:
        setup.await_index_contents(Search([[{**row, "content": "Different text"}]]), [row])
    except RuntimeError as error:
        assert "altered" in str(error)
    else:
        raise AssertionError("Content mismatch accepted")
    with patch.object(setup.time, "monotonic", side_effect=[0, 121]):
        try:
            setup.await_index_contents(Search([[]]), [row], timeout_s=120)
        except RuntimeError as error:
            assert "timed out" in str(error)
        else:
            raise AssertionError("Missing documents never timed out")
    print("PASS visibility wait is bounded and never hides altered evidence")

    cfg = {
        "AZURE_SEARCH_ENDPOINT": "https://example.invalid",
        "SEARCH_INDEX_NAME": "production-index", "PROJECT_ENDPOINT": "https://example.invalid/project",
        "EMBEDDING_DEPLOYMENT": "embedding",
    }
    for name in ("production-index", "knowledge-index"):
        argv = [
            "setup_evaluation_index.py", "--env-file", "unused", "--reference", "unused",
            "--output-dir", "unused", "--index-name", name,
        ]
        with patch.object(sys, "argv", argv), patch.object(setup, "dotenv_values", return_value=cfg), \
                patch.object(setup, "AzureCliCredential") as credential, contextlib.redirect_stderr(io.StringIO()):
            try:
                setup.main()
            except SystemExit as error:
                assert error.code == 2
            else:
                raise AssertionError("Non-evaluation index accepted")
            credential.assert_not_called()
    print("PASS production/non-evaluation index names are rejected before Azure authentication")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
