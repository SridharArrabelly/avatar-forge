"""Paced agent-mode matrix using isolated copies of the live agent definition.

Runs LIVE paid inference and hosted Search/Bing calls. Never updates the source
agent. Saves every answer and response privately; pass an output directory outside
the repository. Temporary benchmark agents are deleted after each configuration.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential, get_bearer_token_provider
from dotenv import dotenv_values
from openai import APIConnectionError, APIStatusError, OpenAI

import bench_routing_agent as bench


class ResponseFailure(RuntimeError):
    def __init__(self, detail: dict):
        super().__init__(json.dumps(detail))
        self.detail = detail


class Pacer:
    def __init__(self, interval: float, budget: int, reserve: int):
        self.interval = interval
        self.budget = budget
        self.reserve = reserve
        self.calls: deque[tuple[float, int]] = deque()
        self.last_start = -float("inf")

    def wait(self) -> None:
        while True:
            now = time.monotonic()
            while self.calls and now - self.calls[0][0] >= 61:
                self.calls.popleft()
            spacing = self.last_start + self.interval - now
            capacity = 0.0
            if self.calls and sum(tokens for _, tokens in self.calls) + self.reserve > self.budget:
                capacity = self.calls[0][0] + 61 - now
            delay = max(spacing, capacity)
            if delay <= 0:
                break
            time.sleep(delay)
        self.last_start = time.monotonic()
        self.calls.append((self.last_start, self.reserve))

    def account(self, tokens: int) -> None:
        started, reserved = self.calls[-1]
        self.calls[-1] = (started, max(tokens, reserved))
        self.reserve = max(self.reserve, min(tokens, self.budget))


def stream_turn(client: OpenAI, catalog: str, question: str) -> dict:
    started = time.perf_counter()
    first = None
    text = []
    tools = []
    final = None
    with client.responses.create(
        stream=True, tool_choice="auto", parallel_tool_calls=True,
        input=[
            {"type": "message", "role": "system", "content": catalog},
            {"type": "message", "role": "user", "content": question},
        ],
    ) as stream:
        for event in stream:
            if event.type == "response.output_text.delta":
                if first is None and event.delta:
                    first = time.perf_counter() - started
                text.append(event.delta)
            elif event.type == "response.output_item.done":
                item = event.item
                if item.type.endswith("_call") and item.type != "function_call":
                    tools.append(item.type)
            elif event.type == "response.completed":
                final = event.response.model_dump(mode="json", warnings=False)
            elif event.type in ("response.failed", "response.incomplete", "error"):
                raise ResponseFailure(event.model_dump(mode="json", warnings=False))
    if final is None:
        raise ResponseFailure({"code": "missing_completed_event"})
    if final.get("status") != "completed":
        raise ResponseFailure({"code": "non_completed_response", "response": final})
    return {
        "tools": tools, "text": "".join(text).strip(),
        "first_token_s": first, "completion_s": time.perf_counter() - started,
        "usage": final.get("usage"), "response": final,
    }


def error_details(error: Exception) -> tuple[dict, bool, float]:
    if isinstance(error, APIStatusError):
        status = error.status_code
        headers = error.response.headers
        delay = float(headers.get("retry-after-ms", "0")) / 1000
        retry_after = headers.get("retry-after", "")
        if retry_after.replace(".", "", 1).isdigit():
            delay = max(delay, float(retry_after))
        return {"status": status, "body": error.body}, status in (408, 409, 429) or status >= 500, delay
    if isinstance(error, APIConnectionError):
        return {"type": type(error).__name__, "message": str(error)}, True, 0
    if isinstance(error, ResponseFailure):
        blob = str(error).lower()
        retryable = any(code in blob for code in ("rate_limit", "too many requests", "server_error", "temporarily"))
        return error.detail, retryable, 0
    raise error


def summarize(records: list[dict]) -> dict:
    completed = [record for record in records if record["status"] == "completed"]
    historical = [record for record in records if record["group"] in ("minutes", "web")]

    def latency(key: str) -> dict:
        values = [record[key] for record in completed if record[key] is not None]
        return {
            "n": len(values), "mean": sum(values) / len(values) if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None,
        }

    return {
        "attempted_turns": len(records), "completed": len(completed),
        "errors": len(records) - len(completed),
        "routing_correct": sum(record.get("routing_ok", False) for record in records),
        "historical10_correct": sum(record.get("routing_ok", False) for record in historical),
        "historical10_total": len(historical),
        "by_group": {
            group: {
                "correct": sum(record.get("routing_ok", False) for record in records if record["group"] == group),
                "total": sum(record["group"] == group for record in records),
            }
            for group in ("minutes", "policies", "web")
        },
        "first_token_s": latency("first_token_s"),
        "completion_s": latency("completion_s"),
        "retries": sum(len(record["attempt_errors"]) for record in records),
        "total_tokens": sum((record.get("usage") or {}).get("total_tokens", 0) for record in completed),
    }


def benchmark_definition(source: dict, model: str, effort: str, breadth: int) -> dict:
    modified = copy.deepcopy(source)
    modified["model"] = model
    modified["reasoning"] = {"effort": effort}
    types = {tool["type"] for tool in modified["tools"]}
    if not {"azure_ai_search", "bing_custom_search_preview"} <= types:
        raise RuntimeError("Both hosted Search and Bing tools are required")
    for tool in modified["tools"]:
        if tool["type"] == "azure_ai_search":
            for index in tool["azure_ai_search"]["indexes"]:
                index["top_k"] = breadth
        if tool["type"] == "bing_custom_search_preview":
            for search in tool["bing_custom_search_preview"]["search_configurations"]:
                search["count"] = breadth
    return modified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", required=True, help="Existing model deployment for benchmark copies; does not change the live agent.")
    parser.add_argument("--efforts", nargs="+", choices=("none", "low", "medium"), default=["none", "low"])
    parser.add_argument("--breadths", nargs="+", type=int, choices=(5, 8), default=[5, 8])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--token-budget", type=int, default=240000)
    parser.add_argument("--reserve-tokens", type=int, default=60000)
    parser.add_argument("--question-limit", type=int, default=15, help="Use 1 for a pilot; 15 for the scored core suite.")
    parser.add_argument("--groups", nargs="+", choices=("minutes", "policies", "web"), default=["minutes", "policies", "web"])
    parser.add_argument("--resume", action="store_true", help="Reuse completed turns with the same source definition; allows narrowing groups.")
    args = parser.parse_args()
    if args.runs < 1 or not 1 <= args.question_limit <= 15:
        parser.error("runs must be positive and question-limit must be between 1 and 15")
    if args.interval < 0 or not 0 < args.reserve_tokens <= args.token_budget:
        parser.error("interval must be nonnegative and reserve must fit the positive token budget")
    output = args.output_dir.resolve()
    if output.is_relative_to(bench.ROOT.resolve()):
        parser.error("output-dir must be outside the repository (answers may contain private corpus text)")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()) and not args.resume:
        parser.error("output-dir must be empty; existing benchmark evidence will not be overwritten")
    cfg = dotenv_values(args.env_file)
    required = ("PROJECT_ENDPOINT", "AGENT_NAME", "AZURE_SEARCH_ENDPOINT", "SEARCH_INDEX_NAME")
    for key in required:
        if not cfg.get(key):
            parser.error(f"Missing {key} in env-file")
        os.environ[key] = cfg[key]
    questions = [entry for entry in bench.CORE if bench.group_of(entry[0]) in args.groups][:args.question_limit]
    pacer = Pacer(args.interval, args.token_budget, args.reserve_tokens)
    all_records = []
    summaries = {}
    with AzureCliCredential(process_timeout=120) as credential, AIProjectClient(
        endpoint=cfg["PROJECT_ENDPOINT"], credential=credential,
    ) as project:
        source = project.agents.get(cfg["AGENT_NAME"])
        version = project.agents.get_version(cfg["AGENT_NAME"], source.versions.latest.version)
        definition = version.definition.as_dict()
        source_fingerprint = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
        if args.resume:
            previous_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            if previous_manifest["model"] != args.model:
                raise RuntimeError("Benchmark model changed: use a new output directory")
            previous = json.loads((output / "source-definition.json").read_text(encoding="utf-8"))
            if previous != definition:
                raise RuntimeError("Source definition changed: cannot combine this run with previous results")
        catalog = bench.tfa._fetch_catalog(credential=credential)
        if not catalog:
            raise RuntimeError("No meeting catalogue: refusing a non-comparable benchmark")
        date = datetime.now(timezone.utc)
        catalog = (
            "[SILENT REFERENCE DATA - do not speak or volunteer this data.]\n"
            f"TODAY: {date.strftime('%A, %d %B %Y')} (UTC).\n\n{catalog}"
        )
        manifest = {
            "started_at": date.isoformat(), "environment": args.env_file.parent.name,
            "project_endpoint": cfg["PROJECT_ENDPOINT"], "source_agent": cfg["AGENT_NAME"],
            "source_version": version.version, "source_definition_sha256": source_fingerprint,
            "instructions_sha256": hashlib.sha256(definition["instructions"].encode()).hexdigest(),
            "model": args.model, "runs": args.runs, "questions": questions,
            "interval_s": args.interval, "token_budget_per_61s": args.token_budget,
            "reserved_tokens_per_attempt": args.reserve_tokens,
        }
        manifest["groups"] = args.groups
        if args.resume:
            manifest["resumed_at"] = date.isoformat()
            original_catalog = (output / "catalog.txt").read_text(encoding="utf-8")
            if original_catalog != catalog:
                raise RuntimeError("Catalogue or date changed: cannot resume the same comparison")
        manifest_name = f"resume-{date.strftime('%Y%m%dT%H%M%S')}.json" if args.resume else "manifest.json"
        (output / manifest_name).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (output / "source-definition.json").write_text(json.dumps(definition, indent=2), encoding="utf-8")
        (output / "catalog.txt").write_text(catalog, encoding="utf-8")
        provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")
        for effort in args.efforts:
            for breadth in args.breadths:
                label = f"{effort}_k{breadth}"
                name = f"bench-routing-{label.replace('_', '-')}-{uuid.uuid4().hex[:8]}"
                modified = benchmark_definition(definition, args.model, effort, breadth)
                (output / f"{label}-definition.json").write_text(json.dumps(modified, indent=2), encoding="utf-8")
                log_path = output / f"{label}.jsonl"
                records = []
                completed_keys = set()
                if args.resume and log_path.exists():
                    selected = {question for question, _ in questions}
                    for line in log_path.read_text(encoding="utf-8").splitlines():
                        record = json.loads(line)
                        if record["question"] not in selected or record["run"] > args.runs:
                            continue
                        if record["effort"] != effort or record["breadth"] != breadth:
                            raise RuntimeError("Existing result configuration does not match")
                        key = (record["run"], record["question"])
                        if record["status"] == "completed":
                            if key in completed_keys:
                                raise RuntimeError("Duplicate completed turn in saved evidence")
                            completed_keys.add(key)
                            records.append(record)
                    all_records.extend(records)
                if len(records) == len(questions) * args.runs:
                    summaries[label] = summarize(records)
                    (output / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
                    print(f"REUSED {label}: {len(records)} completed selected turns", flush=True)
                    continue
                print(f"START {args.model} {label}: {len(questions)} questions x {args.runs}; isolated agent {name}", flush=True)
                created = project.agents.create_version(name, body={"definition": modified})
                (output / f"{label}-agent.json").write_text(json.dumps({"name": name, "version": created.version}), encoding="utf-8")
                try:
                    live_copy = project.agents.get_version(name, created.version).definition.as_dict()
                    if live_copy != modified:
                        raise RuntimeError("Benchmark agent definition readback differs from the requested configuration")
                    with OpenAI(
                        base_url=f"{cfg['PROJECT_ENDPOINT'].rstrip('/')}/agents/{name}/endpoint/protocols/openai",
                        api_key=provider(), default_query={"api-version": "v1"},
                        max_retries=0, timeout=180,
                    ) as client, log_path.open("a" if args.resume else "w", encoding="utf-8", buffering=1) as log:
                        for run in range(1, args.runs + 1):
                            for question, expected in questions:
                                if (run, question) in completed_keys:
                                    continue
                                number = bench.CORE.index((question, expected)) + 1
                                record = {
                                    "label": label, "model": args.model, "effort": effort, "breadth": breadth,
                                    "run": run, "question_number": number, "question": question,
                                    "expected": expected, "group": bench.group_of(question),
                                    "attempt_errors": [], "status": "error",
                                }
                                wall = time.perf_counter()
                                for attempt in range(1, 7):
                                    client.api_key = provider()
                                    pacer.wait()
                                    try:
                                        result = stream_turn(client, catalog, question)
                                    except (APIStatusError, APIConnectionError, ResponseFailure) as error:
                                        detail, retryable, retry_after = error_details(error)
                                        record["attempt_errors"].append({"attempt": attempt, "error": detail})
                                        print(f"RETRY {label} run={run} Q{number} attempt={attempt} retryable={retryable}: {str(detail)[:200]}", flush=True)
                                        if not retryable or attempt == 6:
                                            break
                                        time.sleep(max(65 * attempt, retry_after + 1))
                                    else:
                                        record.update(result, status="completed")
                                        primary = bench.classify(result["tools"][0]) if result["tools"] else "none"
                                        record.update(primary=primary, routing_ok=primary == expected)
                                        pacer.account((result.get("usage") or {}).get("total_tokens", 0))
                                        break
                                record["wall_s_including_pacing_and_retries"] = time.perf_counter() - wall
                                log.write(json.dumps(record, ensure_ascii=False) + "\n")
                                records.append(record)
                                all_records.append(record)
                                print(f"{label} run={run} Q{number:02} {record['status']} route={record.get('routing_ok')} first={record.get('first_token_s')} total={record.get('completion_s')}", flush=True)
                                if record["status"] != "completed":
                                    raise RuntimeError(f"Unrecovered API failure at {label} run={run} Q{number}; evidence saved, matrix stopped")
                finally:
                    project.agents.delete(name)
                    print(f"DELETED isolated agent {name}", flush=True)
                summaries[label] = summarize(records)
                (output / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
                print(f"SUMMARY {label} {json.dumps(summaries[label])}", flush=True)
        current = project.agents.get(cfg["AGENT_NAME"])
        current_definition = current.versions.latest.definition.as_dict()
        unchanged = current.versions.latest.version == version.version and current_definition == definition
        (output / "source-unchanged.json").write_text(json.dumps({"unchanged": unchanged, "version": current.versions.latest.version}), encoding="utf-8")
        if not unchanged:
            raise RuntimeError("Source agent changed during the experiment (this script never writes it)")
    print(f"DONE {len(all_records)} turns. Source agent unchanged. Evidence: {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
