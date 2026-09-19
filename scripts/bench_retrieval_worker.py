"""Read-only retrieval worker for container exec; emits hashes, never passages.

The base64 JSON payload contains index/query settings and cases, not credentials
or gold answers. Run in the deployed environment with its managed identity.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import random
import time

from azure.identity import ManagedIdentityCredential
from azure.search.documents import SearchClient


def emit(kind: str, record: dict) -> None:
    print(f"{kind} {json.dumps(record)}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()
    cfg = json.loads(base64.b64decode(args.payload, validate=True))
    if not isinstance(cfg["cases"], list) or not 1 <= len(cfg["cases"]) <= 200:
        raise ValueError("Expected 1-200 retrieval cases")
    if not 1 <= cfg["runs"] <= 10 or cfg["top"] not in (5, 8) or cfg["interval"] < 0:
        raise ValueError("Invalid run, top-k or pacing settings")
    if not cfg["index"].startswith("eval-"):
        raise ValueError("Worker must explicitly target an evaluation index")
    rng = random.Random(cfg.get("seed", 20260918))
    with ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"]) as credential, SearchClient(
        os.environ["AZURE_SEARCH_ENDPOINT"], cfg["index"], credential,
        connection_timeout=10, read_timeout=30, retry_total=0,
    ) as client:
        started = time.perf_counter()
        list(client.search(search_text="*", top=1, select=["id"]))
        emit("EVAL_CONTEXT", {
            "platform": platform.platform(), "python": platform.python_version(),
            "container_app": os.getenv("CONTAINER_APP_NAME"), "revision": os.getenv("CONTAINER_APP_REVISION"),
            "configured_production_index": os.getenv("SEARCH_INDEX_NAME"),
            "evaluation_index": cfg["index"],
            "warmup_s": time.perf_counter() - started,
        })
        last_start = 0.0
        total = 0
        for run in range(1, cfg["runs"] + 1):
            for case in cfg["cases"]:
                modes = ["keyword", "semantic_keyword"]
                rng.shuffle(modes)
                for mode in modes:
                    params = {
                        "search_text": case["query"], "top": cfg["top"],
                        "select": ["id", "source", "content"],
                    }
                    if mode == "semantic_keyword":
                        params.update(query_type="semantic", semantic_configuration_name=cfg["semantic_config"])
                    headers = []

                    def record_headers(response):
                        headers.append({
                            "request_id": response.http_response.headers.get("request-id"),
                            "server_elapsed_ms": response.http_response.headers.get("elapsed-time"),
                        })

                    time.sleep(max(0, last_start + cfg["interval"] - time.monotonic()))
                    last_start = time.monotonic()
                    started = time.perf_counter()
                    hits = list(client.search(**params, raw_response_hook=record_headers))
                    elapsed = time.perf_counter() - started
                    if mode == "semantic_keyword" and any(hit.get("@search.reranker_score") is None for hit in hits):
                        raise RuntimeError("Semantic results lack reranker scores; refusing a mislabeled comparison")
                    emit("EVAL_RESULT", {
                        "run": run, "case_id": case["id"], "mode": mode,
                        "elapsed_s": elapsed, "requests": headers,
                        "hits": [{
                            "id": hit["id"], "source": hit["source"],
                            "content_sha256": hashlib.sha256(hit["content"].encode()).hexdigest(),
                            "content_chars": len(hit["content"]),
                            "score": hit.get("@search.score"),
                            "reranker_score": hit.get("@search.reranker_score"),
                        } for hit in hits],
                    })
                    total += 1
        emit("EVAL_DONE", {"requests": total})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
