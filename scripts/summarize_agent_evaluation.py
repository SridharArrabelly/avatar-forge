"""Aggregate private agent-evaluation timings and observable evidence availability."""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter
from pathlib import Path

from evaluation_scoring import retrieval_coverage
from prepare_evaluation_review import MODELS


def distribution(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "median": None, "p95": None, "mean": None, "max": None}
    ordered = sorted(values)
    return {
        "n": len(values), "median": statistics.median(values),
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "mean": statistics.mean(values), "max": max(values),
    }


def aggregate(rows: list[dict], cases: dict, chunks: dict) -> dict:
    completed = [row for row in rows if row["status"] == "completed"]
    evidence = [
        retrieval_coverage(cases[row["case_id"]], row["response"], chunks)
        for row in completed if row["stage"] == "agent"
    ]
    observable = [item for item in evidence if item["complete"] is not None]
    groups = {}
    for group in ("minutes", "web"):
        selected = [row for row in completed if row["group"] == group]
        groups[group] = {
            "completed": len(selected),
            "first_token_s": distribution([row["first_token_s"] for row in selected if row["first_token_s"] is not None]),
        }
    return {
        "turns": len(rows), "completed": len(completed), "errors": len(rows) - len(completed),
        "retry_attempts": sum(len(row.get("attempt_errors", [])) for row in rows),
        "routing_correct": sum(row.get("routing_ok") is True for row in completed if row["stage"] == "agent"),
        "routing_scored": sum(row.get("routing_ok") is not None for row in completed if row["stage"] == "agent"),
        "first_token_s": distribution([row["first_token_s"] for row in completed if row["first_token_s"] is not None]),
        "completion_s": distribution([row["completion_s"] for row in completed]),
        "server_window_s": distribution([
            row["server_completed_at"] - row["server_created_at"]
            for row in completed
            if row.get("server_created_at") is not None and row.get("server_completed_at") is not None
        ]),
        "client_tool_lifetime_s": distribution([
            event["duration_s"] for row in completed for event in row["tool_events"]
            if event.get("duration_s") is not None
        ]),
        "first_text_before_last_tool_end": sum(
            row["first_token_s"] is not None
            and any(event.get("end_s") is not None and row["first_token_s"] < event["end_s"] for event in row["tool_events"])
            for row in completed
        ),
        "tool_call_counts": dict(Counter(len(row["tools"]) for row in completed)),
        "tool_status_counts": dict(Counter(
            item.get("status", "not_reported")
            for row in completed for item in row["response"].get("output", [])
            if item.get("type", "").endswith("_call")
        )),
        "internal_turns_with_web_calls": sum(
            row["stage"] == "agent" and row["group"] == "minutes"
            and any("bing" in tool.lower() or "web" in tool.lower() for tool in row["tools"])
            for row in completed
        ),
        "web_turns_with_internal_calls": sum(
            row["stage"] == "agent" and row["group"] == "web"
            and any("azure_ai_search" in tool.lower() for tool in row["tools"])
            for row in completed
        ),
        "total_tokens": sum(row["usage"]["total_tokens"] for row in completed),
        "reasoning_tokens": sum((row["usage"].get("output_tokens_details") or {}).get("reasoning_tokens", 0) for row in completed),
        "cached_input_tokens": sum((row["usage"].get("input_tokens_details") or {}).get("cached_tokens", 0) for row in completed),
        "cache_write_tokens": sum((row["usage"].get("input_tokens_details") or {}).get("cache_write_tokens", 0) for row in completed),
        "observable_retrieval_complete": sum(item["complete"] for item in observable),
        "observable_retrieval_total": len(observable),
        "by_group": groups,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parent.parent):
        parser.error("Detailed summaries must remain outside the repository")
    cases = {c["id"]: c for c in json.loads(args.cases.read_text(encoding="utf-8"))}
    chunks = {c["id"]: c for c in json.loads((args.reference / "indexed-chunks.json").read_text(encoding="utf-8"))}
    summary = {}
    for model in MODELS:
        for stage in ("oracle", "agent"):
            for effort in ("none", "low"):
                path = args.run_dir / model / f"{stage}-{effort}.jsonl"
                if not path.exists():
                    continue
                text = path.read_text(encoding="utf-8")
                if text and not text.endswith("\n"):
                    raise ValueError(f"Partial record encountered; retry after the current write: {path}")
                rows = [json.loads(line) for line in text.splitlines()]
                if len({(row["run"], row["case_id"]) for row in rows}) != len(rows):
                    raise ValueError("Duplicate case/round result")
                key = f"{model}/{stage}/{effort}"
                result = aggregate(rows, cases, chunks)
                result["by_cohort"] = {
                    cohort: aggregate([row for row in rows if row["cohort"] == cohort], cases, chunks)
                    for cohort in ("legacy", "holdout", "negative")
                }
                summary[key] = result
                print(
                    f"{key}: n={result['completed']} "
                    f"TTFT median={result['first_token_s']['median']} p95={result['first_token_s']['p95']} "
                    f"evidence={result['observable_retrieval_complete']}/{result['observable_retrieval_total']}",
                    flush=True,
                )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
