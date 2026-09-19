"""Build blinded quality-review packets and separate machine evidence metrics.

Reads private benchmark artifacts only. Does not contact models or Azure.
Model/effort mappings and latency stay outside the blinded review packets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

from evaluation_scoring import retrieval_coverage, search_documents


MODELS = ("gpt-5.4", "gpt-5.4-mini", "gpt-5.6-luna", "gpt-5.6-terra")


def arm_mapping(seed: int) -> dict[tuple[str, str], str]:
    configurations = [(model, effort) for model in MODELS for effort in ("none", "low")]
    random.Random(seed).shuffle(configurations)
    return {config: f"arm-{chr(65 + number)}" for number, config in enumerate(configurations)}


def blind_record(row: dict, arm: str, case: dict, chunks: dict, documents: dict) -> dict:
    identity = f"{arm}:{row['stage']}:{row['run']}:{row['case_id']}"
    record = {
        "id": identity, "arm": arm, "stage": row["stage"], "run": row["run"],
        "case_id": row["case_id"], "status": row["status"],
        "answer": row.get("text", ""), "tools": row.get("tools", []),
        "evidence_ids": [], "queries": [], "citations": [],
    }
    if row["status"] != "completed":
        record["failure"] = "The inference attempt did not complete; do not grade as a successful answer."
        return record
    response = row["response"]
    for item in response.get("output", []):
        if item.get("type") in ("azure_ai_search_call", "bing_custom_search_preview_call"):
            arguments = item.get("arguments")
            record["queries"].append({
                "tool": item["type"],
                "arguments": json.loads(arguments) if isinstance(arguments, str) else arguments,
            })
        if item.get("type") == "message":
            for content in item.get("content") or []:
                for citation in content.get("annotations") or []:
                    if citation.get("type") == "url_citation":
                        record["citations"].append({
                            key: citation[key] for key in ("url", "title") if key in citation
                        })
    if row["stage"] == "agent":
        record["retrieval"] = retrieval_coverage(case, response, chunks)
        for hit in search_documents(response):
            text = hit.get("content", "")
            key = hashlib.sha256((str(hit.get("id")) + "\0" + text).encode()).hexdigest()
            stored = chunks.get(hit.get("id"), {})
            documents[key] = {
                "source": stored.get("source"), "title": hit.get("title"),
                "text": text, "source_id_known": bool(stored),
            }
            record["evidence_ids"].append(key)
    else:
        if row.get("tools"):
            raise ValueError("Oracle response unexpectedly used a tool")
        record["retrieval"] = {"status": "fixed_source_context", "complete": None}
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--stage", choices=("oracle", "agent"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260919)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS), help="Completed model subsets can be reviewed while other workers continue.")
    parser.add_argument("--partial", action="store_true", help="Review completed turns of a stopped/error run; missing and failed turns remain explicitly ungraded.")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if output.is_relative_to(Path(__file__).resolve().parent.parent):
        parser.error("Review packets contain private evidence and must remain outside git")
    if args.runs < 1:
        parser.error("runs must be positive")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    by_case = {case["id"]: case for case in cases}
    selected_ids = {c["id"] for c in cases if args.stage == "agent" or c.get("oracle_context")}
    expected = {
        (model, effort, run, case_id)
        for model in args.models for effort in ("none", "low")
        for run in range(1, args.runs + 1) for case_id in selected_ids
    }
    rows = []
    if args.partial:
        run_summary = json.loads((args.run_dir / "summary.json").read_text(encoding="utf-8"))
        if run_summary.get("status") != "error":
            parser.error("--partial requires a stopped run with an error summary")
    for model in args.models:
        for effort in ("none", "low"):
            path = args.run_dir / model / f"{args.stage}-{effort}.jsonl"
            if args.partial and not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            if not text.endswith("\n"):
                raise ValueError(f"Evidence file is still being written or truncated: {path.name}")
            rows.extend(json.loads(line) for line in text.splitlines())
    all_keys = {(row["model"], row["effort"], row["run"], row["case_id"]) for row in rows}
    if len(all_keys) != len(rows) or not all_keys <= expected:
        raise ValueError("Unexpected or duplicate turn in evidence")
    if args.partial:
        rows = [row for row in rows if row["status"] == "completed"]
    actual = {(row["model"], row["effort"], row["run"], row["case_id"]) for row in rows}
    if not args.partial and (actual != expected or len(rows) != len(expected)):
        raise ValueError(f"Stage incomplete/duplicated: {len(actual)}/{len(expected)} expected unique turns")
    if output.exists() and any(output.iterdir()):
        parser.error("Review output must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    chunks = {
        chunk["id"]: chunk
        for chunk in json.loads((args.reference / "indexed-chunks.json").read_text(encoding="utf-8"))
    }
    mapping = arm_mapping(args.seed)
    mapping_rows = [{"arm": arm, "model": model, "effort": effort} for (model, effort), arm in mapping.items()]
    (output / "PRIVATE-arm-mapping.json").write_text(json.dumps(mapping_rows, indent=2), encoding="utf-8")
    for group in ("minutes", "web"):
        documents = {}
        blinded = [
            blind_record(row, mapping[row["model"], row["effort"]], by_case[row["case_id"]], chunks, documents)
            for row in rows if row["group"] == group
        ]
        blinded.sort(key=lambda item: (item["arm"], item["case_id"], item["run"]))
        packet = {
            "stage": args.stage,
            "partial_cohort": args.partial,
            "instructions": "Apply the frozen rubric. Do not infer quality from a successful tool call. Report factual correctness/completeness separately from available-evidence support and style; unverified is not proven false.",
            "cases": [case for case in cases if case["group"] == group and case["id"] in selected_ids],
            "documents": documents, "records": blinded,
        }
        (output / f"{group}-review.json").write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
        print(f"{args.stage} {group}: {len(blinded)} blinded responses; {len(documents)} deduplicated evidence passages")
    (output / "manifest.json").write_text(json.dumps({
        "stage": args.stage, "turns": len(rows), "seed": args.seed,
        "partial_cohort": args.partial, "expected_turns": len(expected),
        "not_yet_graded": [
            {"arm": mapping[model, effort], "run": run, "case_id": case_id}
            for model, effort, run, case_id in sorted(expected - actual)
        ],
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "run_dir": str(args.run_dir.resolve()),
    }, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
