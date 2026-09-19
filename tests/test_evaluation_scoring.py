"""Offline evidence-availability tests; no answer or model accuracy is inferred."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from evaluation_scoring import oracle_coverage, retrieval_coverage


def response(hits):
    return {"output": [{"type": "azure_ai_search_call_output", "output": json.dumps({"documents": hits})}]}


def main():
    case = {
        "id": "case", "group": "minutes", "source": "right.docx", "answerable": True,
        "required_facts": [{"id": "f1", "evidence_quotes": ["Alice approved the test.", "Bob must review it."]}],
        "oracle_context": "Alice approved the test.\nBob must review it.",
    }
    chunks = {"right": {"source": "right.docx"}, "wrong": {"source": "other-meeting.docx"}}
    assert oracle_coverage(case)["complete"] is True
    assert retrieval_coverage(case, response([{"id": "right", "content": case["oracle_context"]}]), chunks)["complete"] is True
    assert retrieval_coverage(case, response([{"id": "right", "content": "Alice approved the test."}]), chunks)["complete"] is False
    assert retrieval_coverage(case, response([{"id": "wrong", "content": case["oracle_context"]}]), chunks)["complete"] is False
    assert retrieval_coverage(case, response([]), chunks)["complete"] is False
    assert retrieval_coverage({**case, "group": "web"}, response([]), chunks)["complete"] is None
    assert retrieval_coverage({**case, "answerable": False}, response([]), chunks)["complete"] is None
    incomplete = retrieval_coverage(case, response([{"id": "unknown", "content": case["oracle_context"]}]), chunks)
    assert incomplete["complete"] is False and incomplete["unrecognized_document_ids"] == ["unknown"]
    alternatives = {**case, "required_facts": [{"id": "f1", "evidence_quotes": ["Absent", "Alice approved the test."], "evidence_match": "any"}]}
    assert retrieval_coverage(alternatives, response([{"id": "right", "content": "Alice approved the test."}]), chunks)["complete"] is True
    print("PASS exact delivered evidence, source identity, alternatives, and unknown web/negative outcomes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
