"""Private, source-grounded retrieval audit. Reads Azure; never changes an index.

The audit subcommand proves source/extraction/chunk correspondence before the run
subcommand compares Azure AI Search query modes. Outputs must stay outside git.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from azure.identity import AzureCliCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.models import VectorizableTextQuery
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrieval_evaluation import build_cases, locate_chunk, normalize, query_for, read_original, score


def dump(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def audit(cfg: dict, data_dir: Path, output: Path) -> None:
    import setup_aisearch_index as ingest

    paths = sorted(data_dir.glob("Board Meeting*.docx"))
    if not paths:
        raise ValueError("No original meeting DOCX files found (policy subdirectories are never scanned)")
    documents = [read_original(path) for path in paths]
    by_source = {doc["source"]: doc for doc in documents}
    expected = {}
    for path, document in zip(paths, documents):
        extracted = ingest.read_docx(path)
        if normalize(extracted) != document["text"]:
            raise ValueError(f"{path.name}: production extraction differs from independent XML extraction")
        dt = ingest.parse_meeting_date(path.stem)
        if dt is None:
            raise ValueError(f"{path.name}: production date extraction failed")
        title = ingest.display_document_title(path.stem, ingest.DOC_TYPE_MINUTES)
        prefix = f"[Document: {title} | Type: MeetingMinutes | Meeting Date: {dt.strftime('%d %B %Y')}]\n\n"
        for number, text in enumerate(ingest.chunk_text(extracted, 1200, 200)):
            key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{path.name}:{number}"))
            expected[key] = {"content": prefix + text, "source": path.name, "chunk_index": number}
    with AzureCliCredential(process_timeout=120) as credential:
        with SearchIndexClient(cfg["AZURE_SEARCH_ENDPOINT"], credential) as indexes:
            index = indexes.get_index(cfg["SEARCH_INDEX_NAME"])
        with SearchClient(cfg["AZURE_SEARCH_ENDPOINT"], cfg["SEARCH_INDEX_NAME"], credential) as search:
            rows = list(search.search(
                search_text="*", select=["id", "title", "source", "documentType", "meeting_date", "chunk_index", "content"],
                top=1000, include_total_count=True,
            ))
    actual = {row["id"]: row for row in rows}
    failures = []
    if set(actual) != set(expected):
        failures.append({"missing_chunks": sorted(set(expected) - set(actual)), "unexpected_chunks": sorted(set(actual) - set(expected))})
    chunk_map = {}
    for key, row in actual.items():
        if row["source"] not in by_source or row.get("documentType") != "MeetingMinutes":
            failures.append({"id": key, "error": "non-minutes or unexpected document"})
            continue
        document = by_source[row["source"]]
        if str(row.get("meeting_date", ""))[:10] != document["meeting_date"]:
            failures.append({"id": key, "error": "meeting date mismatch"})
        if key in expected and any(row[field] != expected[key][field] for field in ("content", "source", "chunk_index")):
            failures.append({"id": key, "error": "indexed content/source/chunk position differs from production extraction"})
        try:
            start, end = locate_chunk(row["content"], document)
        except ValueError as error:
            failures.append({"id": key, "error": str(error)})
        else:
            chunk_map[key] = {"source": row["source"], "start": start, "end": end, "chunk_index": row["chunk_index"]}
    cases = build_cases(documents)
    if not failures:
        for case in cases:
            reference = score(case, [{"id": key} for key in chunk_map], chunk_map)
            if not reference["complete_case"]:
                failures.append({"case": case["id"], "error": "required original evidence is not completely covered by any indexed chunks"})
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "index": cfg["SEARCH_INDEX_NAME"], "search_endpoint": cfg["AZURE_SEARCH_ENDPOINT"],
        "documents": documents, "chunk_size": 1200, "chunk_overlap": 200,
        "source_count": len(documents), "expected_chunks": len(expected), "actual_chunks": len(actual),
        "cases": len(cases), "splits": dict(Counter(case["split"] for case in cases)),
        "passed": not failures, "failures": failures,
    }
    dump(output / "source-audit.json", manifest)
    dump(output / "index-definition.json", index.as_dict())
    dump(output / "indexed-chunks.json", [{k: v for k, v in row.items() if not k.startswith("@")} for row in rows])
    dump(output / "chunk-map.json", chunk_map)
    dump(output / "cases.json", cases)
    print(json.dumps({key: manifest[key] for key in ("source_count", "expected_chunks", "actual_chunks", "cases", "splits", "passed", "failures")}, indent=2))
    if failures:
        raise RuntimeError("Source/index audit failed. Do not run retrieval/model scoring until resolved.")


def run(cfg: dict, args, output: Path) -> None:
    source_audit = json.loads((args.reference / "source-audit.json").read_text(encoding="utf-8"))
    if not source_audit["passed"]:
        raise ValueError("Source audit must pass before retrieval evaluation")
    if source_audit["search_endpoint"] != cfg["AZURE_SEARCH_ENDPOINT"]:
        raise ValueError("Reference search service differs from selected environment")
    cases_file = args.reference / "cases.json"
    cases = [case for case in json.loads(cases_file.read_text(encoding="utf-8")) if case["split"] == args.split]
    chunk_map = json.loads((args.reference / "chunk-map.json").read_text(encoding="utf-8"))
    audited_chunks = {
        chunk["id"]: chunk
        for chunk in json.loads((args.reference / "indexed-chunks.json").read_text(encoding="utf-8"))
    }
    if not cases:
        raise ValueError("No cases in selected split")
    metadata = {
        "started_at": datetime.now(timezone.utc).isoformat(), "index": args.index or cfg["SEARCH_INDEX_NAME"],
        "reference_sha256": hashlib.sha256(cases_file.read_bytes()).hexdigest(),
        "split": args.split, "runs": args.runs, "modes": args.modes, "tops": args.tops,
        "vector_k": 50, "semantic_config": args.semantic_config, "date_filter": args.date_filter,
        "interval": args.interval, "cases": len(cases), "order_seed": args.seed,
        "query_style": args.query_style,
    }
    dump(output / "run-manifest.json", metadata)
    records = []
    last_call = 0.0
    with AzureCliCredential(process_timeout=120) as credential, SearchClient(
        cfg["AZURE_SEARCH_ENDPOINT"], metadata["index"], credential, retry_total=0,
    ) as client, (output / "retrieval.jsonl").open("w", encoding="utf-8", buffering=1) as log:
        with SearchIndexClient(cfg["AZURE_SEARCH_ENDPOINT"], credential) as indexes:
            live_definition = indexes.get_index(metadata["index"]).as_dict()
        expected_definition = json.loads((args.reference / "index-definition.json").read_text(encoding="utf-8"))
        serialized_definition = json.loads(json.dumps(live_definition, default=str))
        if serialized_definition != expected_definition:
            raise RuntimeError("Index definition drifted from the audited reference")
        metadata["index_definition_sha256"] = hashlib.sha256(
            json.dumps(live_definition, sort_keys=True, default=str).encode()
        ).hexdigest()
        dump(output / "run-manifest.json", metadata)
        warmup_start = time.perf_counter()
        list(client.search(search_text="*", top=1, select=["id"]))
        dump(output / "warmup.json", {"authentication_connection_and_lookup_s": time.perf_counter() - warmup_start})
        rng = random.Random(args.seed)
        for repeat in range(1, args.runs + 1):
            for case in cases:
                query = query_for(case, args.query_style)
                arms = [(mode, top) for mode in args.modes for top in args.tops]
                rng.shuffle(arms)
                for mode, top in arms:
                    params = {
                        "search_text": query, "top": top,
                        "select": ["id", "title", "source", "meeting_date", "content", "chunk_index"],
                    }
                    if mode in ("hybrid", "semantic"):
                        params["vector_queries"] = [VectorizableTextQuery(text=query, k_nearest_neighbors=50, fields="content_vector")]
                    if mode in ("semantic", "semantic_keyword"):
                        params.update(query_type="semantic", semantic_configuration_name=args.semantic_config)
                    if args.date_filter:
                        params["filter"] = f"meeting_date eq {case['meeting_date']}T00:00:00Z"
                        if mode in ("hybrid", "semantic"):
                            params["vector_filter_mode"] = "preFilter"
                    time.sleep(max(0, last_call + args.interval - time.monotonic()))
                    last_call = time.monotonic()
                    headers = []

                    def capture_headers(response):
                        headers.append({
                            "request_id": response.http_response.headers.get("request-id"),
                            "server_elapsed_ms": response.http_response.headers.get("elapsed-time"),
                        })

                    started = time.perf_counter()
                    hits = list(client.search(**params, raw_response_hook=capture_headers))
                    elapsed = time.perf_counter() - started
                    for hit in hits:
                        original = audited_chunks.get(hit["id"])
                        if original is None or any(hit[field] != original[field] for field in ("source", "content", "chunk_index")):
                            raise RuntimeError("Index content drifted from the source audit; start a new reference version")
                    record = {
                        "case_id": case["id"], "run": repeat, "mode": mode, "top": top,
                        "query": query, "filter": params.get("filter"), "elapsed_s": elapsed,
                        "requests": headers, "hits": hits, "metrics": score(case, hits, chunk_map),
                    }
                    log.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    records.append(record)
            print(f"Completed round {repeat}: {len(records)} retrievals", flush=True)
    summary = {}
    for mode in args.modes:
        for top in args.tops:
            selected = [r for r in records if r["mode"] == mode and r["top"] == top]
            summary[f"{mode}_k{top}"] = {
                "requests": len(selected),
                "complete_cases": sum(r["metrics"]["complete_case"] for r in selected),
                "complete_units": sum(r["metrics"]["complete_evidence_units"] for r in selected),
                "required_units": sum(r["metrics"]["required_evidence_units"] for r in selected),
                "mean_char_coverage": statistics.mean(r["metrics"]["evidence_char_coverage"] for r in selected),
                "wrong_source_hits": sum(r["metrics"]["wrong_source_hits"] for r in selected),
                "hits": sum(r["metrics"]["retrieved_hits"] for r in selected),
                "mean_returned_content_chars": statistics.mean(sum(len(h["content"]) for h in r["hits"]) for r in selected),
                "latency_mean": statistics.mean(r["elapsed_s"] for r in selected),
                "latency_median": statistics.median(r["elapsed_s"] for r in selected),
                "latency_p95": sorted(r["elapsed_s"] for r in selected)[math.ceil(len(selected) * 0.95) - 1],
                "latency_max": max(r["elapsed_s"] for r in selected),
                "reranked_requests": sum(any(h.get("@search.reranker_score") is not None for h in r["hits"]) for r in selected),
            }
    dump(output / "summary.json", summary)
    print(json.dumps(summary, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("audit", "run"))
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--split", choices=("development", "held_out", "regression"), default="development")
    parser.add_argument("--modes", nargs="+", choices=("keyword", "hybrid", "semantic", "semantic_keyword"), default=["hybrid", "semantic"])
    parser.add_argument("--tops", nargs="+", type=int, choices=(5, 8), default=[5, 8])
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=20260918)
    parser.add_argument("--query-style", choices=("canonical", "natural"), default="canonical")
    parser.add_argument("--semantic-config", default="default-semantic")
    parser.add_argument("--index")
    parser.add_argument("--date-filter", action="store_true", help="Oracle-date diagnostic only; does not prove native agent dynamic filtering.")
    args = parser.parse_args()
    if args.runs < 1 or args.interval < 0:
        parser.error("runs must be positive; interval cannot be negative")
    if args.action == "run" and args.reference is None:
        parser.error("--reference is required for run")
    output = args.output_dir.resolve()
    if output.is_relative_to(ROOT):
        parser.error("Private outputs must be outside the repository")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("Use an empty output directory; existing evidence is never overwritten")
    cfg = dotenv_values(args.env_file)
    for key in ("AZURE_SEARCH_ENDPOINT", "SEARCH_INDEX_NAME"):
        if not cfg.get(key):
            parser.error(f"Missing {key}")
    if args.action == "audit":
        audit(cfg, args.data_dir, output)
    else:
        run(cfg, args, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
