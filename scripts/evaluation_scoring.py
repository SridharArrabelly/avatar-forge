"""Evidence availability checks, separate from human/source-based answer grading.

Exact reference quotes must come from original documents. The checks here do not
infer that a model's answer is correct merely because the passages were present.
"""
from __future__ import annotations

import json
import re


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def search_documents(response: dict) -> list[dict]:
    documents = []
    for item in response.get("output", []):
        if item.get("type") != "azure_ai_search_call_output":
            continue
        output = item.get("output")
        if output is None or output == "":
            continue
        body = json.loads(output) if isinstance(output, str) else output
        if not isinstance(body, dict) or not isinstance(body.get("documents", []), list):
            raise ValueError("Unrecognized AI Search output; evidence cannot be scored")
        documents.extend(body.get("documents", []))
    return documents


def retrieval_coverage(case: dict, response: dict, chunks: dict[str, dict]) -> dict:
    if case["group"] == "web":
        return {"status": "hidden_bing_evidence", "complete": None}
    if not case.get("answerable", True):
        return {"status": "negative_case_no_recall_target", "complete": None}
    hits = search_documents(response)
    known = []
    unknown = []
    for hit in hits:
        stored = chunks.get(hit.get("id"))
        if stored is None:
            unknown.append(hit.get("id"))
            continue
        if stored["source"] == case["source"]:
            known.append(normalized(hit.get("content", "")))
    facts = []
    for fact in case["required_facts"]:
        quotes = [normalized(text) for text in fact.get("evidence_quotes", [])]
        if not quotes:
            raise ValueError(f"Required fact {fact['id']} has no frozen evidence")
        found = [any(quote in passage for passage in known) for quote in quotes]
        rule = fact.get("evidence_match", "all")
        if rule not in ("all", "any"):
            raise ValueError(f"Invalid evidence_match for {fact['id']}")
        facts.append({
            "id": fact["id"], "quotes_present": sum(found), "quotes_required": len(quotes),
            "covered": all(found) if rule == "all" else any(found),
        })
    if not facts:
        raise ValueError("Answerable internal case has no required facts")
    return {
        "status": "complete" if all(fact["covered"] for fact in facts) else "incomplete",
        "complete": all(fact["covered"] for fact in facts),
        "covered_facts": sum(fact["covered"] for fact in facts), "required_facts": len(facts),
        "returned_documents": len(hits), "matching_source_documents": len(known),
        "unrecognized_document_ids": unknown, "facts": facts,
    }


def oracle_coverage(case: dict) -> dict:
    context = normalized(case.get("oracle_context", ""))
    if not context:
        return {"status": "not_supplied", "complete": None}
    if not case.get("answerable", True):
        return {"status": "negative_control", "complete": None}
    facts = []
    for fact in case["required_facts"]:
        quotes = [normalized(value) for value in fact.get("evidence_quotes", [])]
        if not quotes:
            raise ValueError(f"Oracle fact {fact['id']} has no evidence quote")
        present = [quote in context for quote in quotes]
        rule = fact.get("evidence_match", "all")
        if rule not in ("all", "any"):
            raise ValueError(f"Invalid evidence_match for {fact['id']}")
        facts.append(all(present) if rule == "all" else any(present))
    return {"status": "complete" if facts and all(facts) else "incomplete", "complete": bool(facts) and all(facts)}
