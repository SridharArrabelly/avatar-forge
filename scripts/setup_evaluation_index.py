"""Create a NEW isolated paragraph/section-aware minutes index for evaluation.

Never updates an existing index or changes an agent/connection. Embedding calls
are paid and paced against the current 50k TPM embedding allocation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureCliCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import SearchFieldDataType, SimpleField
from dotenv import dotenv_values
from openai import AzureOpenAI

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench_retrieval import dump
from bench_routing_matrix import Pacer
from retrieval_evaluation import layout_chunks, locate_chunk, score
import setup_aisearch_index as ingest


def await_index_contents(client, chunks: list[dict], timeout_s: float = 120) -> list[dict]:
    expected = {chunk["id"]: chunk for chunk in chunks}
    deadline = time.monotonic() + timeout_s
    while True:
        rows = list(client.search(search_text="*", select=["id", "source", "content", "chunk_index"], top=1000))
        if any(
            row["id"] not in expected or any(row[key] != expected[row["id"]][key] for key in ("source", "content", "chunk_index"))
            for row in rows
        ):
            raise RuntimeError("Evaluation index contains unexpected or altered content")
        if len(rows) == len(expected):
            return rows
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Indexing visibility timed out: {len(rows)}/{len(expected)} chunks")
        print(f"Waiting for indexed documents to become searchable: {len(rows)}/{len(expected)}", flush=True)
        time.sleep(2)


def embed_chunks(chunks: list[dict], cfg: dict, credential) -> int:
    host = urlsplit(cfg["PROJECT_ENDPOINT"])
    pacer = Pacer(interval=1, budget=35000, reserve=6000)
    token_usage = 0
    with AzureOpenAI(
        azure_endpoint=f"{host.scheme}://{host.netloc}", api_version="2024-10-21",
        azure_ad_token_provider=get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default"),
        max_retries=0, timeout=120,
    ) as embeddings:
        for offset in range(0, len(chunks), 16):
            batch = chunks[offset:offset + 16]
            pacer.wait()
            result = embeddings.embeddings.create(
                model=cfg["EMBEDDING_DEPLOYMENT"], input=[chunk["content"] for chunk in batch],
            )
            if len(result.data) != len(batch):
                raise RuntimeError("Embedding response count mismatch")
            for item in result.data:
                batch[item.index]["content_vector"] = item.embedding
            pacer.account(result.usage.total_tokens)
            token_usage += result.usage.total_tokens
            print(f"Embedded {offset + len(batch)}/{len(chunks)} paragraphs", flush=True)
    return token_usage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--index-name", required=True)
    parser.add_argument("--layout", choices=("paragraph", "section"), default="paragraph")
    parser.add_argument("--verify-existing", action="store_true", help="Read-only recovery using this output directory's created-index receipt; never repeats embeddings or uploads.")
    args = parser.parse_args()
    cfg = dotenv_values(args.env_file)
    for key in ("AZURE_SEARCH_ENDPOINT", "SEARCH_INDEX_NAME", "PROJECT_ENDPOINT", "EMBEDDING_DEPLOYMENT"):
        if not cfg.get(key):
            parser.error(f"Missing {key}")
    if not args.index_name.startswith("eval-") or args.index_name == cfg["SEARCH_INDEX_NAME"]:
        parser.error("Only a new eval-* index is allowed; production indexes cannot be targeted")
    output = args.output_dir.resolve()
    if output.is_relative_to(ROOT):
        parser.error("Private evidence must remain outside the repository")
    output.mkdir(parents=True, exist_ok=True)
    receipt = None
    if args.verify_existing:
        receipt = json.loads((output / "created-index.json").read_text(encoding="utf-8"))
        if receipt["index"] != args.index_name:
            parser.error("Created-index receipt does not match requested index")
        if receipt.get("layout", "paragraph") != args.layout:
            parser.error("Recovery must use the recorded layout")
    elif any(output.iterdir()):
        parser.error("Output directory must be empty")
    audit = json.loads((args.reference / "source-audit.json").read_text(encoding="utf-8"))
    if not audit["passed"] or audit["search_endpoint"] != cfg["AZURE_SEARCH_ENDPOINT"]:
        parser.error("A passing source audit of the same service is required")
    for doc in audit["documents"]:
        path = ROOT / "data" / doc["source"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != doc["sha256"]:
            raise RuntimeError("Original documents changed after reference freeze")
    chunks = layout_chunks(audit["documents"], args.layout)
    by_source = {doc["source"]: doc for doc in audit["documents"]}
    chunk_map = {}
    for chunk in chunks:
        start, end = locate_chunk(chunk["content"], by_source[chunk["source"]])
        chunk_map[chunk["id"]] = {"source": chunk["source"], "start": start, "end": end, "chunk_index": chunk["chunk_index"]}
    cases = json.loads((args.reference / "cases.json").read_text(encoding="utf-8"))
    for case in cases:
        if not score(case, [{"id": key} for key in chunk_map], chunk_map)["complete_case"]:
            raise RuntimeError("Proposed layout loses required original evidence")
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with AzureCliCredential(process_timeout=120) as credential, SearchIndexClient(
        cfg["AZURE_SEARCH_ENDPOINT"], credential,
    ) as indexes:
        try:
            indexes.get_index(args.index_name)
        except ResourceNotFoundError:
            if args.verify_existing:
                raise RuntimeError("The recorded evaluation index no longer exists")
        else:
            if not args.verify_existing:
                raise RuntimeError("Index already exists; refusing to change it")
        if receipt is not None:
            token_usage = receipt["embedding_tokens"]
        else:
            token_usage = embed_chunks(chunks, cfg, credential)
            dimensions = {len(chunk["content_vector"]) for chunk in chunks}
            if len(dimensions) != 1:
                raise RuntimeError("Embedding dimensions disagree")
            index = ingest.build_index(args.index_name, {
                "project_endpoint": cfg["PROJECT_ENDPOINT"],
                "embed_deployment": cfg["EMBEDDING_DEPLOYMENT"], "embed_dim": dimensions.pop(),
                "chunking_mode": "window",
            })
            index.fields.extend([
                SimpleField(name="section", type=SearchFieldDataType.String, filterable=True),
                SimpleField(name="source_sha256", type=SearchFieldDataType.String, filterable=True),
                SimpleField(name="source_paragraph", type=SearchFieldDataType.Int32, filterable=True),
            ])
            index.semantic_search.default_configuration_name = ingest.SEMANTIC_CONFIG
            indexes.create_index(index)
            dump(output / "created-index.json", {"index": args.index_name, "layout": args.layout, "embedding_tokens": token_usage})
        with SearchClient(cfg["AZURE_SEARCH_ENDPOINT"], args.index_name, credential) as client:
            if not args.verify_existing:
                for offset in range(0, len(chunks), 100):
                    results = client.upload_documents(chunks[offset:offset + 100])
                    failures = [{"key": item.key, "error": item.error_message} for item in results if not item.succeeded]
                    if failures:
                        dump(output / "upload-failures.json", failures)
                        raise RuntimeError("Evaluation index upload failed; production was not modified")
            indexed = await_index_contents(client, chunks)
        live = indexes.get_index(args.index_name)
        if live.semantic_search.default_configuration_name != ingest.SEMANTIC_CONFIG:
            raise RuntimeError("Default semantic configuration was not retained")
    audit.update({
        "index": args.index_name, "layout": f"section-aware-whole-{args.layout}-v1",
        "expected_chunks": len(chunks), "actual_chunks": len(indexed),
        "chunk_size": None, "chunk_overlap": 0, "embedding_tokens": token_usage,
    })
    dump(output / "source-audit.json", audit)
    dump(output / "index-definition.json", live.as_dict())
    dump(output / "chunk-map.json", chunk_map)
    dump(output / "indexed-chunks.json", [{key: value for key, value in chunk.items() if key != "content_vector"} for chunk in chunks])
    shutil.copyfile(args.reference / "cases.json", output / "cases.json")
    print(f"Verified {args.index_name}: {len(chunks)} {args.layout} blocks; {token_usage} embedding tokens. Production unchanged.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
