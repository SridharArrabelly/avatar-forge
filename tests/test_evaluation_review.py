"""Offline blinding and evidence-packet checks using synthetic content."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from prepare_evaluation_review import arm_mapping, blind_record
from summarize_agent_evaluation import aggregate, distribution


def main():
    mapping = arm_mapping(1)
    assert mapping == arm_mapping(1) and len(set(mapping.values())) == 8
    case = {
        "id": "case", "source": "original.docx", "group": "minutes", "answerable": True,
        "required_facts": [{"id": "fact", "evidence_quotes": ["The board approved the test."]}],
    }
    row = {
        "model": "gpt-5.4", "effort": "low", "stage": "agent", "run": 1, "case_id": "case",
        "status": "completed", "text": "The test was approved.", "tools": ["azure_ai_search_call"],
        "first_token_s": 123,
        "response": {"model": "gpt-5.4", "output": [
            {"type": "azure_ai_search_call", "arguments": '{"query":"test"}', "agent_reference": {"name": "model-name"}},
            {"type": "azure_ai_search_call_output", "output": json.dumps({"documents": [
                {"id": "chunk", "content": "The board approved the test.", "title": "Original"},
            ]})},
        ]},
    }
    documents = {}
    blinded = blind_record(row, "arm-C", case, {"chunk": {"source": "original.docx"}}, documents)
    blob = json.dumps(blinded)
    assert "gpt-5.4" not in blob and "first_token_s" not in blob and "effort" not in blob
    assert blinded["retrieval"]["complete"] is True
    assert len(documents) == 1 and len(blinded["evidence_ids"]) == 1
    oracle = blind_record({**row, "stage": "oracle", "tools": [], "response": {"output": []}}, "arm-C", case, {}, {})
    assert oracle["retrieval"]["status"] == "fixed_source_context"
    values = distribution([1.0, 2.0, 3.0, 100.0])
    assert values["median"] == 2.5 and values["p95"] == 100.0
    assert distribution([])["n"] == 0
    timed = {
        **row, "group": "minutes", "first_token_s": 1.0, "completion_s": 2.0,
        "usage": {"total_tokens": 10}, "tool_events": [], "routing_ok": True,
        "attempt_errors": [], "cohort": "legacy",
    }
    stats = aggregate([timed], {"case": case}, {"chunk": {"source": "original.docx"}})
    assert stats["observable_retrieval_complete"] == 1 and stats["routing_correct"] == 1
    oracle_stats = aggregate([{**timed, "stage": "oracle"}], {"case": case}, {})
    assert oracle_stats["routing_scored"] == 0 and oracle_stats["observable_retrieval_total"] == 0
    print("PASS review blinding hides configuration/timing and retains exact evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
