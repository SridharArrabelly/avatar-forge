"""Verify native agent Search query-mode compatibility using a temporary agent.

This is a paid capability probe, not an answer-quality or voice benchmark.
It never modifies the source agent or the selected index.
"""
from __future__ import annotations

import argparse
import copy
import json
import time
import uuid
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential, get_bearer_token_provider
from dotenv import dotenv_values
from openai import APIError, OpenAI

from bench_retrieval import ROOT, dump
from retrieval_evaluation import native_chunk_map, score


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--index", required=True)
    parser.add_argument("--query-type", choices=("simple", "semantic", "vector_simple_hybrid", "vector_semantic_hybrid"), required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--top", type=int, choices=(5, 8), default=5)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--case-id")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if bool(args.reference) != bool(args.case_id):
        parser.error("--reference and --case-id must be supplied together")
    out = args.output_dir.resolve()
    if out.is_relative_to(ROOT):
        parser.error("Private output must be outside the repository")
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        parser.error("Output directory must be empty")
    case = originals = chunks = None
    if args.reference:
        cases = json.loads((args.reference / "cases.json").read_text(encoding="utf-8"))
        matching = [item for item in cases if item["id"] == args.case_id]
        if len(matching) != 1:
            parser.error("The reference must contain exactly one matching case")
        case = matching[0]
        audit = json.loads((args.reference / "source-audit.json").read_text(encoding="utf-8"))
        originals = {doc["source"]: doc for doc in audit["documents"]}
        chunks = {
            chunk["id"]: chunk
            for chunk in json.loads((args.reference / "indexed-chunks.json").read_text(encoding="utf-8"))
        }
    cfg = dotenv_values(args.env_file)
    name = f"probe-retrieval-{uuid.uuid4().hex[:10]}"
    with AzureCliCredential(process_timeout=120) as cred, AIProjectClient(
        endpoint=cfg["PROJECT_ENDPOINT"], credential=cred,
    ) as project:
        source = project.agents.get(cfg["AGENT_NAME"])
        tool = copy.deepcopy(next(
            item for item in source.versions.latest.definition.as_dict()["tools"]
            if item["type"] == "azure_ai_search"
        ))
        indexes = tool["azure_ai_search"]["indexes"]
        if len(indexes) != 1:
            raise RuntimeError("Probe requires one unambiguous source Search connection")
        indexes[0].update(index_name=args.index, query_type=args.query_type, top_k=args.top)
        definition = {
            "kind": "prompt", "model": "gpt-5.4", "reasoning": {"effort": "none"},
            "instructions": "This is a retrieval capability probe. Call Azure AI Search exactly once with the user's exact query. Do not rewrite it. Then reply only: Probe complete. Do not answer the underlying question.",
            "tools": [tool],
        }
        created = project.agents.create_version(name, body={"definition": definition})
        dump(out / "probe-agent.json", {"name": name, "version": created.version, "definition": definition})
        outcome = {"index": args.index, "query_type": args.query_type, "query": args.query, "documents": 0}
        try:
            actual = project.agents.get_version(name, created.version).definition.as_dict()
            if actual != definition:
                raise RuntimeError("Native probe definition readback differs from requested query mode")
            with OpenAI(
                base_url=f"{cfg['PROJECT_ENDPOINT'].rstrip('/')}/agents/{name}/endpoint/protocols/openai",
                api_key=get_bearer_token_provider(cred, "https://ai.azure.com/.default")(),
                default_query={"api-version": "v1"}, max_retries=0, timeout=120,
            ) as client:
                started = time.perf_counter()
                try:
                    response = client.responses.create(
                        input=args.query, tool_choice="required", parallel_tool_calls=False, max_output_tokens=128,
                    )
                except APIError as error:
                    outcome.update(passed=False, error_type=type(error).__name__, error=str(error))
                else:
                    data = response.model_dump(mode="json", warnings=False)
                    dump(out / "response.json", data)
                    outcome["status"] = data["status"]
                    if data.get("error"):
                        outcome["error"] = data["error"]
                    if data.get("incomplete_details"):
                        outcome["incomplete_details"] = data["incomplete_details"]
                    outcome["tool_statuses"] = [
                        {"type": item["type"], "status": item.get("status")}
                        for item in data["output"] if "_call" in item["type"]
                    ]
                    outcome["actual_queries"] = [
                        json.loads(item["arguments"]).get("query")
                        for item in data["output"] if item["type"] == "azure_ai_search_call"
                    ]
                    retrieved = []
                    for item in data["output"]:
                        if item["type"] == "azure_ai_search_call_output" and item.get("output"):
                            output = json.loads(item["output"])
                            outcome["documents"] += len(output.get("documents", []))
                            retrieved.extend(output.get("documents", []))
                            if output.get("error"):
                                outcome["tool_error"] = output["error"]
                    outcome["passed"] = data["status"] == "completed" and outcome["documents"] > 0
                    outcome["query_exact"] = outcome["actual_queries"] == [args.query]
                    outcome["passed"] = outcome["passed"] and outcome["query_exact"]
                    if args.reference:
                        delivered = native_chunk_map(retrieved, chunks, originals)
                        outcome["evidence_metrics"] = score(case, retrieved, delivered)
                        outcome["actual_tool_text_verified"] = True
                        outcome["passed"] = outcome["passed"] and outcome["evidence_metrics"]["complete_case"]
                outcome["total_probe_s_not_retrieval_latency"] = time.perf_counter() - started
        finally:
            project.agents.delete(name)
        unchanged = project.agents.get(cfg["AGENT_NAME"]).versions.latest.version == source.versions.latest.version
        outcome["source_version_unchanged"] = unchanged
        if not unchanged:
            outcome["passed"] = False
            outcome["source_changed_during_probe"] = True
        dump(out / "outcome.json", outcome)
    print(json.dumps(outcome, indent=2))
    return 0 if outcome["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
