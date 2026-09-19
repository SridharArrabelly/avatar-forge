"""Isolated four-deployment evaluation; running this CLI makes PAID live calls.

Example (PowerShell, from the repository root)::

    python scripts\\bench_agent_evaluation.py --env-file C:\\private\\eval.env `
        --cases C:\\private\\cases.json --output-dir C:\\private\\new-evaluation

The env file must explicitly name AvatarAgentRetrievalEvalV1-dc53 and
eval-knowledge-sections-v1-dc53. Nothing updates the source agent or index.
Budgets are client-side token reservations, NOT deployment quota changes.
--budgets accepts a JSON object, for example '{"gpt-5.4":180000}'.
Budget environment variables are not read.

Each model owns one sequential worker, credential, renewable token provider and
Pacer. Oracle precedes agent within that worker; effort/case pairs are shuffled
per round, identically across models. All turns start fresh: no conversation or
previous_response_id is sent. There is no model fallback or promotion.
--continue-from reads a FINISHED run into a NEW empty output directory. Frozen
settings, case hashes, catalogue and source must match. Completed JSONL lines
are copied unchanged, never requested again; failed/cancelled records go into
prior-errors.jsonl. Attempts are cumulative across continuations (maximum six).
In-place resume is not supported. Existing evidence is never written.

Private output must be a new or empty directory outside this checkout. It holds
source/catalogue/case snapshots, exact schedules, per-model/stage-effort JSONL,
attempt evidence, source-invariance checks, and ownership-checked cleanup
receipts. Unknown case fields (including private grading rubrics) are discarded;
only catalogue, question, optional oracle_context, and the fixed oracle-only
system overlay enter response requests. The overlay is recorded verbatim in
manifests; stored source instructions are unchanged.

TTFT means first nonempty output_text delta. Tool timings are client-observed
output_item.added/done lifetimes, not independent measurements of server tool
execution. Server timestamps are recorded verbatim (null if absent). NO audio,
speech, or container latency is measured. The final server model/effort and token
usage must be present; a deployment may report a dated model version.
Summaries contain status/counts; raw timing observations are in JSONL, without
median/p95 aggregation.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import random
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, TextIO
from urllib.parse import urlsplit

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import (
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity import AzureCliCredential, get_bearer_token_provider
from dotenv import dotenv_values
from openai import APIConnectionError, APIError, APIStatusError, OpenAI

import bench_routing_matrix as matrix


ROOT = Path(__file__).resolve().parent.parent
SOURCE_AGENT = "AvatarAgentRetrievalEvalV1-dc53"
SOURCE_INDEX = "eval-knowledge-sections-v1-dc53"
ORACLE_SYSTEM_OVERLAY = (
    "Fixed-evidence evaluation: the supplied source passages are the evidence available for this turn. "
    "No tools are available. Answer from those passages; state when evidence is insufficient. "
    "Do not claim to have performed a search."
)
DEFAULT_BUDGETS = {
    "gpt-5.4": 180000,
    "gpt-5.4-mini": 300000,
    "gpt-5.6-luna": 200000,
    "gpt-5.6-terra": 200000,
}
STAGES = ("oracle", "agent")
MAX_ATTEMPTS = 6
NO_RETRIES = dict(retry_total=0, retry_connect=0, retry_read=0, retry_status=0)
TRANSIENT_CODES = {
    "rate_limit_exceeded", "rate_limit_error", "rate_limit", "too_many_requests",
    "server_error", "internal_server_error", "service_unavailable",
    "temporarily_unavailable", "timeout", "request_timeout",
}
STREAMED_PROVIDER_CODES = {"server_error", "internal_server_error", "rate_limit_exceeded"}
PERMANENT_PROVIDER_CODES = {
    "invalid_request", "invalid_request_error", "bad_request", "bad_request_error",
    "invalid_api_key", "authentication_error", "authorization_error", "permission_error",
    "permission_denied", "validation_error", "invalid_argument", "unauthorized", "forbidden",
    "model_not_found", "deployment_not_found", "unsupported_parameter",
}
STREAMED_PROVIDER_MESSAGE = (
    "The model deployment encountered an error processing your request. You can retry your request, "
    "or contact us through an Azure support request at: https://go.microsoft.com/fwlink/?linkid=2213926 "
    "if you continue to see this error."
)


class PeerStopped(RuntimeError):
    pass


class StreamFailure(matrix.ResponseFailure):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    ).hexdigest()


def as_dict(value: object) -> dict:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    return value.model_dump(mode="json", warnings=False)


def append_record(log: TextIO, record: dict) -> None:
    log.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    log.flush()
    os.fsync(log.fileno())


class Evidence:
    """Exclusive writes only; even concurrent invocations cannot replace evidence."""

    def __init__(self, directory: Path):
        self.root = directory.resolve()
        if self.root.is_relative_to(ROOT.resolve()):
            raise ValueError("output-dir must be outside the repository")
        if self.root.exists() and (not self.root.is_dir() or any(self.root.iterdir())):
            raise ValueError("output-dir must be empty; evidence will not be overwritten")
        self.root.mkdir(parents=True, exist_ok=True)
        self.write_json("claim.json", {"claimed_at": utc_now(), "pid": os.getpid()})

    def open_log(self, relative: str | Path) -> TextIO:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("x", encoding="utf-8", buffering=1, newline="")

    def write_text(self, relative: str | Path, value: str) -> None:
        with self.open_log(relative) as log:
            log.write(value)
            log.flush()
            os.fsync(log.fileno())

    def write_json(self, relative: str | Path, value: object) -> None:
        self.write_text(relative, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True, help="JSON list; unknown/rubric fields are never sent.")
    parser.add_argument("--output-dir", type=Path, required=True, help="New/empty PRIVATE directory outside the repository.")
    parser.add_argument("--continue-from", type=Path, help="Read-only finished run to continue; output-dir must be new/empty and disjoint.")
    parser.add_argument("--models", nargs="+", choices=tuple(DEFAULT_BUDGETS), default=list(DEFAULT_BUDGETS))
    parser.add_argument("--efforts", nargs="+", choices=("none", "low"), default=["none", "low"])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--stages", nargs="+", choices=STAGES, default=list(STAGES),
                        help="Always oracle before agent; oracle requires at least one case with nonempty oracle_context.")
    parser.add_argument("--workers", type=int, default=4, help="1-4 model workers; never concurrent requests to one model.")
    parser.add_argument("--case-ids", nargs="+", help="Pilot subset, retaining case-file order.")
    parser.add_argument("--budgets", default="{}", help="JSON model->safe client TPM overrides; not Azure quotas.")
    parser.add_argument("--interval", type=float, default=8)
    parser.add_argument("--reserve-tokens", type=int, default=30000)
    parser.add_argument("--seed", type=int, default=20260919)
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argument_parser()
    args = parser.parse_args(argv)
    for name in ("models", "efforts", "stages", "case_ids"):
        values = getattr(args, name)
        if values is not None and len(values) != len(set(values)):
            parser.error(f"--{name.replace('_', '-')} must not contain duplicates")
    if args.runs < 1 or not 1 <= args.workers <= 4:
        parser.error("runs must be positive and workers must be between 1 and 4")
    if not math.isfinite(args.interval) or args.interval < 0:
        parser.error("interval must be finite and nonnegative")
    try:
        overrides = json.loads(args.budgets)
        if not isinstance(overrides, dict) or any(
            key not in DEFAULT_BUDGETS or type(value) is not int or value <= 0
            for key, value in overrides.items()
        ):
            raise ValueError
    except (ValueError, TypeError):
        parser.error("budgets must be a JSON object of approved model names to positive integer TPM")
    args.budgets = {**DEFAULT_BUDGETS, **overrides}
    if not 0 < args.reserve_tokens <= min(args.budgets[model] for model in args.models):
        parser.error("reserve-tokens must be positive and fit every selected model's budget")
    args.stages = [stage for stage in STAGES if stage in args.stages]
    return args


def load_settings(path: Path) -> dict:
    if not path.is_file():
        raise ValueError("env-file does not exist")
    values = dotenv_values(path, interpolate=False)
    keys = ("PROJECT_ENDPOINT", "AGENT_NAME", "AZURE_SEARCH_ENDPOINT", "SEARCH_INDEX_NAME")
    if any(not isinstance(values.get(key), str) or not values[key].strip() for key in keys):
        raise ValueError("env-file must explicitly set " + ", ".join(keys))
    cfg = {key: values[key].strip() for key in keys}
    if cfg["AGENT_NAME"] != SOURCE_AGENT or cfg["SEARCH_INDEX_NAME"] != SOURCE_INDEX:
        raise ValueError(f"Only evaluation agent {SOURCE_AGENT} and index {SOURCE_INDEX} are allowed")
    for key in ("PROJECT_ENDPOINT", "AZURE_SEARCH_ENDPOINT"):
        url = urlsplit(cfg[key])
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError(f"{key} must be an HTTPS endpoint without credentials, query, or fragment")
        cfg[key] = cfg[key].rstrip("/")
    return cfg


def load_cases(path: Path, selected_ids: list[str] | None = None) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, list) or not data:
        raise ValueError("cases must be a nonempty JSON list")
    cases, seen = [], set()
    for position, entry in enumerate(data, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"Case {position} must be an object")
        if any(not isinstance(entry.get(key), str) or not entry[key].strip() for key in ("id", "question")):
            raise ValueError(f"Case {position} needs a nonempty string id and question")
        if entry["id"] in seen:
            raise ValueError(f"Duplicate case id at position {position}")
        seen.add(entry["id"])
        if (
            entry.get("group") not in ("minutes", "web")
            or entry.get("expected") not in ("internal", "external")
            or entry.get("cohort") not in ("legacy", "holdout", "negative")
            or type(entry.get("answerable")) is not bool
        ):
            raise ValueError(f"Case {position} has invalid group, expected, cohort, or answerable")
        context = entry.get("oracle_context")
        if context is not None and not isinstance(context, str):
            raise ValueError(f"Case {position} oracle_context must be a string or null")
        case = {key: entry[key] for key in ("id", "question", "group", "expected", "cohort", "answerable")}
        if context is not None and context.strip():
            case["oracle_context"] = context
        cases.append(case)
    if selected_ids is not None:
        if not set(selected_ids) <= seen:
            raise ValueError("case-ids contains an unknown id")
        cases = [case for case in cases if case["id"] in selected_ids]
    return cases, hashlib.sha256(raw).hexdigest()


def build_schedules(cases: list[dict], efforts: list[str], stages: list[str], runs: int, seed: int) -> dict:
    schedules = {}
    for stage in stages:
        eligible = [case for case in cases if stage != "oracle" or case.get("oracle_context")]
        if not eligible:
            raise ValueError(f"No eligible cases for {stage}; oracle requires nonempty oracle_context")
        schedule = []
        for run in range(1, runs + 1):
            round_seed = f"{seed}:{stage}:{run}"
            pairs = [(effort, case["id"]) for effort in efforts for case in eligible]
            random.Random(round_seed).shuffle(pairs)
            # A random permutation can still be two effort blocks. Break that
            # degeneracy without changing any effort/case pair or round.
            if len(efforts) > 1 and len(eligible) > 1:
                transitions = sum(a[0] != b[0] for a, b in zip(pairs, pairs[1:]))
                if transitions == 1:
                    boundary = next(i for i, pair in enumerate(pairs) if pair[0] != pairs[0][0])
                    pairs[0], pairs[boundary] = pairs[boundary], pairs[0]
            for effort, case_id in pairs:
                schedule.append({
                    "sequence": len(schedule) + 1, "run": run,
                    "round_seed": round_seed, "effort": effort, "case_id": case_id,
                })
        schedules[stage] = schedule
    return schedules


def frozen_configuration(args, cfg, cases, cases_sha256, schedules) -> dict:
    return {
        "schema_version": 1, "source_agent": SOURCE_AGENT, "source_index": SOURCE_INDEX,
        "project_endpoint": cfg["PROJECT_ENDPOINT"], "cases_sha256": cases_sha256,
        "selected_cases_sha256": fingerprint(cases), "selected_case_ids": [case["id"] for case in cases],
        "models": args.models, "efforts": args.efforts, "stages": args.stages, "runs": args.runs,
        "workers": min(args.workers, len(args.models)), "schedule_seed": args.seed, "schedule": schedules,
        "budgets_per_61s": {model: args.budgets[model] for model in args.models},
        "interval_s": args.interval, "reserve_tokens": args.reserve_tokens, "max_attempts": MAX_ATTEMPTS,
        "fresh_sessions": True, "audio_measured": False, "sdk_retries": 0,
        "timing": "Client-observed text deltas and output_item.added/done; no speech/audio measurement.",
        "oracle_system_overlay": ORACLE_SYSTEM_OVERLAY if "oracle" in args.stages else None,
    }


def turn_key(record: dict) -> tuple:
    if not isinstance(record, dict) or any(
        not isinstance(record.get(key), str) or not record[key]
        for key in ("model", "stage", "effort", "case_id")
    ) or type(record.get("run")) is not int or record["run"] < 1:
        raise ValueError("Invalid planned turn identity")
    return tuple(record[key] for key in ("model", "stage", "effort", "run", "case_id"))


def jsonl_lines(raw: bytes, label: str):
    if raw and not raw.endswith(b"\n"):
        raise ValueError(f"Truncated JSONL in {label}; refusing ambiguous continuation")
    for number, line in enumerate(raw.split(b"\n")[:-1], 1):
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError) as error:
            raise ValueError(f"Invalid JSONL in {label} at line {number}") from error
        if not isinstance(record, dict):
            raise ValueError(f"Non-object JSONL in {label} at line {number}")
        yield number, record, (line + b"\n").decode("utf-8")


def saved_error_retryable(detail: dict) -> bool:
    if detail.get("type") == "APIError":
        return provider_error_details(
            detail.get("message", ""), detail.get("body"), detail.get("status"),
            code=detail.get("code"), error_type=detail.get("error_type"),
        )[1]
    status = detail.get("status")
    if type(status) is int:
        return status in (408, 409, 429) or 500 <= status < 600
    if detail.get("type") in ("APIConnectionError", "APITimeoutError", "ServiceRequestError", "ServiceResponseError"):
        return True
    return failure_details(StreamFailure(detail))[1]


class Continuation:
    """Read-only snapshot; keeps exact successful lines separate from recovery history."""

    def __init__(self, directory: Path):
        self.root = directory.resolve()
        self.hashes: dict[str, str | None] = {}
        self.completed: dict[tuple, str] = {}
        self.failed: dict[tuple, dict] = {}
        self.failed_source_run_ids: dict[tuple, str] = {}
        self.records: dict[tuple, dict] = {}
        self.prior_error_lines: list[str] = []
        self.not_before: dict[str, float] = {}
        self.reserves: dict[str, int] = {}
        self.provenance: dict = {}

    def read(self, relative: str | Path, *, optional: bool = False) -> bytes | None:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Continuation artifacts must stay inside their source directory")
        key = str(Path(relative))
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            if not optional:
                raise
            self.hashes[key] = None
            return None
        self.hashes[key] = hashlib.sha256(raw).hexdigest()
        return raw

    def json(self, relative: str | Path):
        return json.loads(self.read(relative))

    def verify_unchanged(self) -> None:
        expected = dict(self.hashes)
        for relative, digest in expected.items():
            self.read(relative, optional=True)
            if self.hashes[relative] != digest:
                self.hashes = expected
                raise ValueError(f"Original continuation artifact changed: {relative}")
        self.hashes = expected

    def pending(self, model: str) -> dict:
        return {
            stage: [item for item in schedule if turn_key({**item, "model": model, "stage": stage}) not in self.completed]
            for stage, schedule in self.manifest["schedule"].items()
        }


def _load_continuation(args, cfg, cases, cases_sha256, schedules) -> Continuation:
    prior = Continuation(args.continue_from)
    output = args.output_dir.resolve()
    if (
        prior.root.is_relative_to(ROOT.resolve()) or not prior.root.is_dir()
        or output.is_relative_to(prior.root) or prior.root.is_relative_to(output)
    ):
        raise ValueError("continue-from must be an existing external directory disjoint from output-dir")
    prior.manifest = prior.json("manifest.json")
    frozen = frozen_configuration(args, cfg, cases, cases_sha256, schedules)
    for key, value in frozen.items():
        if key not in prior.manifest or fingerprint(prior.manifest[key]) != fingerprint(value):
            raise ValueError(f"Continuation frozen setting changed: {key}; repeat the original flags exactly")
    if not isinstance(prior.manifest.get("run_id"), str) or not prior.manifest["run_id"]:
        raise ValueError("Continuation run_id is missing")
    if fingerprint(prior.json("cases.json")) != frozen["selected_cases_sha256"]:
        raise ValueError("Continuation case snapshot differs from the original cases hash")
    definition = prior.json("source-definition.json")
    validate_baseline(definition)
    digest = fingerprint(definition)
    if (
        digest != prior.manifest.get("source_definition_sha256")
        or hashlib.sha256(definition["instructions"].encode("utf-8")).hexdigest() != prior.manifest.get("instructions_sha256")
    ):
        raise ValueError("Continuation source definition/instructions hash mismatch")
    prior.source = {"version": prior.manifest["source_version"], "definition": definition}
    prior.catalogue = prior.read("catalog.txt").decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if hashlib.sha256(prior.catalogue.encode("utf-8")).hexdigest() != prior.manifest.get("catalogue_sha256"):
        raise ValueError("Continuation catalogue hash mismatch")
    unchanged = prior.json("source-unchanged.json")
    if (
        unchanged.get("unchanged") is not True or unchanged.get("status") != "checked"
        or unchanged.get("source_version") != prior.source["version"] or unchanged.get("current_version") != prior.source["version"]
        or unchanged.get("source_definition_sha256") != digest or unchanged.get("current_definition_sha256") != digest
        or fingerprint(unchanged.get("current_definition")) != digest
    ):
        raise ValueError("Continuation requires a verified unchanged source version/full definition")
    cleanup, summary = prior.json("cleanup.json"), prior.json("summary.json")
    if (
        cleanup.get("status") != "completed" or summary.get("source_unchanged") is not True
        or summary.get("status") not in ("completed", "error")
        or set(cleanup.get("workers", {})) != set(args.models) or set(summary.get("workers", {})) != set(args.models)
    ):
        raise ValueError("Continuation requires finished workers, completed cleanup and source verification")
    expected = {
        turn_key({**item, "model": model, "stage": stage}): item
        for model in args.models for stage, schedule in schedules.items() for item in schedule
    }
    case_map = {case["id"]: case for case in cases}
    history = prior.read("prior-errors.jsonl", optional=True)
    inherited = prior.manifest.get("continuation")
    if inherited is not None:
        if not isinstance(inherited, dict) or history is None or hashlib.sha256(history).hexdigest() != inherited.get("prior_errors_sha256"):
            raise ValueError("Continuation prior-error history is missing or changed")
    elif history is not None:
        raise ValueError("Unexpected prior-error history without continuation provenance")
    latest_history = {}
    if history:
        seen_history = set()
        for _, wrapper, raw_line in jsonl_lines(history, "prior-errors.jsonl"):
            record = wrapper.get("record")
            if not isinstance(record, dict) or record.get("status") not in ("error", "cancelled"):
                raise ValueError("Invalid inherited prior-error record")
            identity = (wrapper.get("source_run_id"), turn_key(record))
            if identity in seen_history or identity[1] not in expected or not identity[0]:
                raise ValueError("Duplicate or unplanned inherited prior-error record")
            seen_history.add(identity)
            latest_history[identity[1]] = wrapper
            prior.prior_error_lines.append(raw_line)
    failed_count = cancelled_count = api_errors = 0
    for model in args.models:
        receipt = cleanup["workers"][model]
        if receipt.get("status") != "completed" or any(
            entry.get("cleanup") not in ("deleted", "already_absent") for entry in receipt.get("agents", [])
        ):
            raise ValueError(f"Previous cleanup is incomplete for {model}")
        worker = prior.json(Path(model) / "manifest.json")
        worker_frozen = {
            "model": model, "source_version": prior.source["version"], "source_definition_sha256": digest,
            "schedule": schedules, "client_token_budget_per_61s": args.budgets[model],
            "interval_s": args.interval, "reserve_tokens": args.reserve_tokens,
            "oracle_system_overlay": frozen["oracle_system_overlay"],
        }
        if any(key not in worker or fingerprint(worker[key]) != fingerprint(value) for key, value in worker_frozen.items()):
            raise ValueError(f"Continuation worker configuration changed: {model}")
        prior.reserves[model], prior.not_before[model] = args.reserve_tokens, 0
        completed = 0
        for stage in args.stages:
            for effort in args.efforts:
                relative = Path(model) / f"{stage}-{effort}.jsonl"
                raw = prior.read(relative, optional=True)
                entries = list(jsonl_lines(raw or b"", str(relative)))
                present = {turn_key(record) for _, record, _ in entries}
                # A peer may have stopped a continuation before this old failed
                # turn was retried. Its lifetime attempts still belong to it.
                entries.extend(
                    (0, wrapper["record"], None) for key, wrapper in latest_history.items()
                    if key[:3] == (model, stage, effort) and key not in present
                )
                for number, record, raw_line in entries:
                    try:
                        key = turn_key(record)
                    except (KeyError, TypeError) as error:
                        raise ValueError(f"Missing continuation turn identity in {relative}:{number}") from error
                    if key in prior.records or key not in expected or key[:3] != (model, stage, effort):
                        raise ValueError(f"Duplicate, unplanned or misfiled continuation turn in {relative}:{number}")
                    scheduled = expected[key]
                    metadata = {name: case_map[key[-1]][name] for name in ("question", "group", "expected", "cohort", "answerable")}
                    if any(fingerprint(record.get(name)) != fingerprint(value) for name, value in {**scheduled, **metadata}.items()):
                        raise ValueError(f"Continuation turn metadata/schedule mismatch in {relative}:{number}")
                    status = record.get("status")
                    errors = record.get("attempt_errors")
                    attempts = record.get("attempts", 0)
                    if (
                        status not in ("completed", "error", "cancelled") or not isinstance(errors, list)
                        or type(attempts) is not int or not 0 <= attempts <= MAX_ATTEMPTS
                        or attempts != len(errors) + (1 if status == "completed" else 0)
                        or any(not isinstance(error, dict) or error.get("attempt") != index or not isinstance(error.get("error"), dict)
                               for index, error in enumerate(errors, 1))
                    ):
                        raise ValueError(f"Ambiguous prior request attempts in {relative}:{number}")
                    previous_history = latest_history.get(key, {}).get("record")
                    if previous_history is not None and (
                        attempts < previous_history.get("attempts", 0)
                        or fingerprint(errors[:len(previous_history["attempt_errors"])]) != fingerprint(previous_history["attempt_errors"])
                    ):
                        raise ValueError(f"Continuation dropped prior attempts in {relative}:{number}")
                    try:
                        finished = datetime.fromisoformat(record["finished_at"])
                        if finished.tzinfo is None:
                            raise ValueError
                    except (KeyError, TypeError, ValueError) as error:
                        raise ValueError(f"Missing prior turn finish timestamp in {relative}:{number}") from error
                    prior.not_before[model] = max(prior.not_before[model], finished.timestamp() + 65)
                    usages = [record.get("usage"), (record.get("partial") or {}).get("usage")]
                    usages.extend((error.get("partial") or {}).get("usage") for error in errors)
                    for usage in usages:
                        if isinstance(usage, dict) and type(usage.get("total_tokens")) is int:
                            prior.reserves[model] = max(prior.reserves[model], min(usage["total_tokens"], args.budgets[model]))
                    prior.records[key] = record
                    api_errors += len(errors)
                    if status == "completed":
                        response = record.get("response") or {}
                        usage = record.get("usage")
                        if (
                            response.get("status") != "completed" or response.get("error") or response.get("incomplete_details")
                            or record.get("actual_effort") != effort or (response.get("reasoning") or {}).get("effort") != effort
                            or not isinstance(record.get("actual_model"), str)
                            or not re.fullmatch(re.escape(model) + r"(?:-\d{4}-\d{2}-\d{2})?", record["actual_model"])
                            or record["actual_model"] != response.get("model")
                            or not isinstance(usage, dict) or usage != response.get("usage")
                            or any(type(usage.get(field)) is not int or usage[field] < 0 for field in ("input_tokens", "output_tokens", "total_tokens"))
                            or usage["total_tokens"] < usage["input_tokens"] + usage["output_tokens"]
                        ):
                            raise ValueError(f"Completed prior turn has invalid completion/configuration in {relative}:{number}")
                        prior.completed[key] = raw_line
                        completed += 1
                    else:
                        if attempts >= MAX_ATTEMPTS or (errors and not saved_error_retryable(errors[-1]["error"])):
                            raise ValueError(f"Prior turn has exhausted attempts or a permanent error in {relative}:{number}")
                        prior.failed[key] = record
                        prior.failed_source_run_ids[key] = prior.manifest["run_id"] if raw_line is not None else latest_history[key]["source_run_id"]
                        failed_count += status == "error"
                        cancelled_count += status == "cancelled"
                        if raw_line is not None:
                            wrapper = {
                                "source_run_id": prior.manifest["run_id"], "source_directory": str(prior.root),
                                "source_file": str(relative), "source_line": number, "record": record,
                            }
                            prior.prior_error_lines.append(json.dumps(wrapper, ensure_ascii=False, allow_nan=False) + "\n")
                        if errors:
                            delay = errors[-1].get("retry_delay_s") or 0
                            if isinstance(delay, (int, float)) and math.isfinite(delay):
                                prior.not_before[model] = max(prior.not_before[model], finished.timestamp() + max(65 * attempts, delay))
        if (
            summary["workers"][model].get("completed_turns") != completed
            or summary["workers"][model].get("planned_turns") != sum(map(len, schedules.values()))
            or summary["workers"][model].get("status") not in ("completed", "error", "cancelled")
        ):
            raise ValueError(f"Continuation completed/planned counts disagree with JSONL for {model}")
    reused = {model: sum(key[0] == model for key in prior.completed) for model in args.models}
    remaining = {model: sum(map(len, prior.pending(model).values())) for model in args.models}
    prior.provenance = {
        "source_directory": str(prior.root), "source_run_id": prior.manifest["run_id"],
        "origin_run_id": (inherited or {}).get("origin_run_id", prior.manifest["run_id"]),
        "source_manifest_sha256": prior.hashes["manifest.json"], "source_artifact_sha256": dict(prior.hashes),
        "reused_completed_turns": sum(reused.values()), "reused_by_model": reused,
        "remaining_turns": sum(remaining.values()), "remaining_by_model": remaining, "planned_turns": len(expected),
        "prior_failed_records": failed_count, "prior_cancelled_records": cancelled_count,
        "prior_api_error_attempts": api_errors,
        "prior_request_attempts": sum(record.get("attempts", 0) for record in prior.records.values()),
        "prior_errors_records": len(prior.prior_error_lines),
        "prior_errors_sha256": hashlib.sha256("".join(prior.prior_error_lines).encode("utf-8")).hexdigest(),
    }
    prior.verify_unchanged()
    return prior


def load_continuation(args, cfg, cases, cases_sha256, schedules) -> Continuation:
    try:
        return _load_continuation(args, cfg, cases, cases_sha256, schedules)
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"Malformed continuation snapshot ({type(error).__name__})") from error


def validate_baseline(definition: dict) -> None:
    if definition.get("kind") != "prompt" or not isinstance(definition.get("instructions"), str):
        raise ValueError("Source must be a prompt agent with explicit instructions")
    tools = definition.get("tools")
    if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
        raise ValueError("Source tools must be explicit")
    search = [tool for tool in tools if tool.get("type") == "azure_ai_search"]
    indexes = search[0].get("azure_ai_search", {}).get("indexes") if len(search) == 1 else None
    if not isinstance(indexes, list) or len(indexes) != 1 or not isinstance(indexes[0], dict):
        raise ValueError("Baseline requires exactly one native Search tool and one index")
    index = indexes[0]
    if index.get("index_name") != SOURCE_INDEX or index.get("query_type") != "semantic" or type(index.get("top_k")) is not int or index["top_k"] != 5:
        raise ValueError("Baseline Search must target the evaluation index with query_type=semantic and top_k=5")
    if not any(tool.get("type") == "bing_custom_search_preview" for tool in tools):
        raise ValueError("Baseline must include its hosted Bing tool")


def evaluation_definition(source: dict, model: str, effort: str, stage: str) -> dict:
    definition = matrix.benchmark_definition(source, model, effort, breadth=None)
    definition["reasoning"] = {**copy.deepcopy(source.get("reasoning") or {}), "effort": effort}
    if stage == "oracle":
        definition.update(tools=[], tool_choice="none")
    elif stage != "agent":
        raise ValueError("Unknown stage")
    return definition


def response_request(catalogue: str, case: dict, stage: str, tool_choice: object = "auto") -> dict:
    messages = [{"type": "message", "role": "system", "content": catalogue}]
    request = {"stream": True, "tool_choice": tool_choice, "parallel_tool_calls": True, "input": messages}
    if stage == "oracle":
        if not case.get("oracle_context"):
            raise ValueError("Oracle case has no context")
        messages.append({"type": "message", "role": "system", "content": case["oracle_context"]})
        messages.append({"type": "message", "role": "system", "content": ORACLE_SYSTEM_OVERLAY})
        request.update(tools=[], tool_choice="none", parallel_tool_calls=False)
    elif stage != "agent":
        raise ValueError("Unknown stage")
    messages.append({"type": "message", "role": "user", "content": case["question"]})
    return request


def stream_turn(client: OpenAI, request: dict, model: str, effort: str) -> dict:
    started = time.perf_counter()
    first_token, completed_at, final, created = None, None, None, {}
    text, tools = [], []
    by_key = {}

    def snapshot() -> dict:
        response = final or created
        reasoning = response.get("reasoning") or {}
        return {
            "first_token_s": first_token, "completion_s": completed_at,
            "elapsed_s": time.perf_counter() - started,
            "first_tool_start_s": tools[0]["start_s"] if tools else None,
            "first_tool_end_s": tools[0]["end_s"] if tools else None,
            "tools": [tool["type"] for tool in tools], "tool_events": copy.deepcopy(tools),
            "text": "".join(text), "response": copy.deepcopy(response),
            "actual_model": response.get("model"), "actual_effort": reasoning.get("effort"),
            "usage": response.get("usage"),
            "server_created_at": response.get("created_at", created.get("created_at")),
            "server_completed_at": response.get("completed_at"),
        }

    try:
        with client.responses.create(**request) as stream:
            for event in stream:
                data = as_dict(event)
                kind = data.get("type")
                elapsed = time.perf_counter() - started
                if completed_at is not None:
                    raise StreamFailure({"code": "event_after_completed", "event": data})
                if kind == "response.output_text.delta":
                    delta = data["delta"]
                    if not isinstance(delta, str):
                        raise StreamFailure({"code": "invalid_text_delta"})
                    if delta and first_token is None:
                        first_token = elapsed
                    text.append(delta)
                elif kind == "response.created":
                    created = data["response"]
                elif kind in ("response.output_item.added", "response.output_item.done"):
                    item = data["item"]
                    item_type = item.get("type", "")
                    if not item_type.endswith("_call"):
                        continue
                    item_id = item.get("id")
                    index = data.get("output_index")
                    if not item_id and type(index) is not int:
                        raise StreamFailure({"code": "tool_identity_missing", "event": data})
                    key = ("index", index) if type(index) is int else ("id", item_id)
                    if kind.endswith(".added"):
                        if key in by_key or (item_id and any(tool["id"] == item_id for tool in tools)):
                            raise StreamFailure({"code": "duplicate_tool_start", "event": data})
                        tool = {
                            "id": item_id, "type": item_type, "output_index": index,
                            "start_s": elapsed, "end_s": None, "duration_s": None,
                            "added": item, "done": None,
                        }
                        tools.append(tool)
                        by_key[key] = tool
                    else:
                        tool = by_key.get(key)
                        if (
                            tool is None or tool["end_s"] is not None or tool["type"] != item_type
                            or (tool["id"] and item_id and tool["id"] != item_id)
                        ):
                            raise StreamFailure({"code": "unmatched_tool_end", "event": data})
                        tool.update(id=tool["id"] or item_id, end_s=elapsed, duration_s=elapsed - tool["start_s"], done=item)
                        if item.get("status") in ("failed", "incomplete", "cancelled") or item.get("error"):
                            raise StreamFailure({"code": "tool_failed", "event": data})
                elif kind == "response.completed":
                    final, completed_at = data["response"], elapsed
                elif kind in ("response.failed", "response.incomplete", "error", "response.error"):
                    final = data.get("response") or final
                    raise StreamFailure({"code": kind, "event": data})
        if final is None:
            raise StreamFailure({"code": "missing_completed_event"})
        if final.get("status") != "completed" or final.get("error") or final.get("incomplete_details"):
            raise StreamFailure({"code": "non_completed_response", "response": final})
        if any(tool["end_s"] is None for tool in tools):
            raise StreamFailure({"code": "unfinished_tool_items"})
        output = final.get("output")
        if not isinstance(output, list):
            raise StreamFailure({"code": "missing_response_output"})
        final_text, refusal = [], False
        for index, item in enumerate(output):
            if item.get("status") in ("failed", "incomplete", "in_progress", "cancelled") or item.get("error"):
                raise StreamFailure({"code": "unfinished_response_output", "item": item})
            if item.get("type", "").endswith("_call") and not any(
                tool["type"] == item["type"] and (
                    tool["id"] == item["id"] if item.get("id") else tool["output_index"] == index
                ) for tool in tools
            ):
                raise StreamFailure({"code": "missing_tool_events", "item": item})
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        final_text.append(content["text"])
                    elif content.get("type") == "refusal" and content.get("refusal"):
                        refusal = True
        if "".join(final_text) != "".join(text):
            raise StreamFailure({"code": "text_stream_mismatch"})
        if not "".join(final_text).strip() and not refusal:
            raise StreamFailure({"code": "no_assistant_output"})
        actual_model = final.get("model")
        if not isinstance(actual_model, str) or not re.fullmatch(re.escape(model) + r"(?:-\d{4}-\d{2}-\d{2})?", actual_model):
            raise StreamFailure({"code": "actual_model_mismatch", "actual_model": actual_model, "requested_model": model})
        if (final.get("reasoning") or {}).get("effort") != effort:
            raise StreamFailure({"code": "actual_effort_mismatch", "actual_reasoning": final.get("reasoning")})
        usage = final.get("usage")
        if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0 for key in ("input_tokens", "output_tokens", "total_tokens")):
            raise StreamFailure({"code": "missing_or_invalid_usage"})
        if usage["total_tokens"] < usage["input_tokens"] + usage["output_tokens"]:
            raise StreamFailure({"code": "inconsistent_usage_total"})
        if request["tool_choice"] == "none" and tools:
            raise StreamFailure({"code": "oracle_used_tools"})
        return snapshot()
    except Exception as error:
        error.partial = snapshot()
        raise


def retry_after_seconds(headers: object) -> float:
    headers = headers or {}
    delay = 0.0
    for key, scale in (("retry-after-ms", 0.001), ("x-ms-retry-after-ms", 0.001), ("retry-after", 1)):
        value = headers.get(key)
        if value is None:
            continue
        try:
            seconds = float(value) * scale
        except (ValueError, TypeError):
            if key != "retry-after":
                continue
            try:
                date = parsedate_to_datetime(value)
                seconds = (date - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(seconds):
            delay = max(delay, seconds)
    return delay


def provider_error_details(message: str, body: object, status=None, *, code=None, error_type=None) -> tuple[dict, bool]:
    payload = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    code = payload.get("code") or code
    error_type = payload.get("type") or error_type
    status = status if status is not None else payload.get("status_code", payload.get("status"))
    if status is None and isinstance(body, dict):
        status = body.get("status_code", body.get("status"))
    labels = [label for label in (code, error_type) if isinstance(label, str)]
    retryable = any(label in STREAMED_PROVIDER_CODES for label in labels) or (
        not code and message == STREAMED_PROVIDER_MESSAGE
    )
    numeric_status = int(status) if isinstance(status, str) and status.isdigit() else status
    if any(label in PERMANENT_PROVIDER_CODES for label in labels) or (
        type(numeric_status) is int and numeric_status not in (200, 408, 409, 429) and not 500 <= numeric_status < 600
    ):
        retryable = False
    return {
        "type": "APIError", "message": message, "code": code,
        "error_type": error_type, "body": body, "status": status,
    }, retryable


def failure_details(error: Exception) -> tuple[dict, bool, float]:
    if isinstance(error, (APIStatusError, APIConnectionError)):
        try:
            detail, retryable, delay = matrix.error_details(error)
        except (ValueError, TypeError):
            detail = {"status": error.status_code, "body": error.body}
            retryable, delay = error.status_code in (408, 409, 429) or 500 <= error.status_code < 600, 0
        if not math.isfinite(delay) or delay < 0:
            delay = 0
        if isinstance(error, APIStatusError) and error.status_code >= 600:
            retryable = False
        return detail, retryable, max(delay, retry_after_seconds(getattr(getattr(error, "response", None), "headers", None)))
    if isinstance(error, APIError):
        detail, retryable = provider_error_details(
            error.message, error.body, getattr(error, "status_code", None),
            code=getattr(error, "code", None), error_type=getattr(error, "type", None),
        )
        detail["type"] = type(error).__name__
        return detail, retryable, retry_after_seconds(getattr(getattr(error, "response", None), "headers", None))
    if isinstance(error, matrix.ResponseFailure):
        detail = error.detail
        # Inspect actual error codes, never arbitrary response/question prose.
        event = detail.get("event") or {}
        response = event.get("response") or detail.get("response") or {}
        payload = event.get("error") or response.get("error") or {}
        code = payload.get("code") if isinstance(payload, dict) else None
        code = code or event.get("code") or detail.get("code")
        return detail, code in TRANSIENT_CODES, 0
    if isinstance(error, HttpResponseError):
        status = error.status_code
        detail = {"type": type(error).__name__, "status": status, "message": str(error)}
        return detail, status in (408, 409, 429) or (status is not None and 500 <= status < 600), retry_after_seconds(getattr(error.response, "headers", None))
    if isinstance(error, (ServiceRequestError, ServiceResponseError)):
        return {"type": type(error).__name__, "message": str(error)}, True, 0
    return {"type": type(error).__name__, "message": str(error)}, False, 0


def check_stop(stop: threading.Event) -> None:
    if stop.is_set():
        raise PeerStopped("Another worker failed; no further requests will start")


def operation(label: str, action: Callable, log: TextIO, *, once: bool = False):
    """Explicit bounded retries for control-plane calls; creation is never retried."""
    for attempt in range(1, (1 if once else MAX_ATTEMPTS) + 1):
        try:
            return action()
        except ResourceNotFoundError:
            raise
        except Exception as error:
            detail, retryable, retry_after = failure_details(error)
            delay = max(65.0 * attempt, retry_after + 1)
            append_record(log, {
                "kind": "control_error", "operation": label, "attempt": attempt,
                "error": detail, "retryable": retryable, "at": utc_now(),
                "retry_delay_s": delay if retryable and not once and attempt < MAX_ATTEMPTS else None,
            })
            if not retryable or once or attempt == MAX_ATTEMPTS:
                raise
            time.sleep(delay)


def read_source(project, log: TextIO) -> dict:
    agent = operation("read_source", lambda: project.agents.get(SOURCE_AGENT), log)
    version = operation("read_source_version", lambda: project.agents.get_version(SOURCE_AGENT, agent.versions.latest.version), log)
    return {"version": version.version, "definition": copy.deepcopy(version.definition.as_dict())}


def fetch_catalogue(credential, cfg: dict) -> str:
    # The smoke helper takes settings from the environment and has no client
    # options. Adapt it only during single-threaded preflight, then restore it.
    smoke = matrix.bench.tfa
    original_client = smoke.SearchClient
    keys = ("AZURE_SEARCH_ENDPOINT", "SEARCH_INDEX_NAME")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ[key] = cfg[key]
        smoke.SearchClient = lambda **kwargs: original_client(**kwargs, **NO_RETRIES)
        catalogue = smoke._fetch_catalog(credential=credential)
    finally:
        smoke.SearchClient = original_client
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if not catalogue:
        raise RuntimeError("No meeting catalogue; refusing a non-comparable evaluation")
    return (
        "[SILENT REFERENCE DATA - do not speak or volunteer this data.]\n"
        f"TODAY: {datetime.now(timezone.utc).strftime('%A, %d %B %Y')} (UTC).\n\n{catalogue}"
    )


class AgentCopies:
    def __init__(self, project, evidence: Evidence, model: str, run_id: str, log: TextIO):
        self.project, self.evidence, self.model, self.run_id, self.log = project, evidence, model, run_id, log
        self.entries: list[dict] = []

    def lookup(self, name: str):
        try:
            return operation("lookup_copy", lambda: self.project.agents.get(name), self.log)
        except ResourceNotFoundError:
            return None

    def create(self, definition: dict, stage: str, effort: str) -> dict:
        model_label = self.model.replace(".", "-")
        name = f"bench-eval-{model_label}-{stage}-{effort}-{uuid.uuid4().hex[:20]}"
        if name == SOURCE_AGENT or self.lookup(name) is not None:
            raise RuntimeError("Temporary agent name already exists; refusing to create a version")
        metadata = {"evaluation_run_id": self.run_id, "evaluation_config": f"{self.model}:{stage}:{effort}"}
        entry = {
            "name": name, "stage": stage, "effort": effort, "metadata": metadata,
            "definition_sha256": fingerprint(definition), "version": None,
            "creation_confirmed": False, "cleanup": "pending",
        }
        self.entries.append(entry)
        prefix = Path(self.model) / f"{stage}-{effort}"
        self.evidence.write_json(f"{prefix}-definition.json", definition)
        append_record(self.log, {"kind": "create_intent", "at": utc_now(), **entry})
        # A timed-out create can have succeeded. Do not retry a non-idempotent
        # create_version; cleanup will reconcile its unique ownership tags.
        created = operation("create_copy", lambda: self.project.agents.create_version(
            name, body={"definition": definition, "metadata": metadata},
        ), self.log, once=True)
        entry.update(version=created.version, creation_confirmed=True)
        self.evidence.write_json(f"{prefix}-agent.json", entry)
        readback = operation("readback_copy", lambda: self.project.agents.get_version(name, created.version), self.log)
        actual = readback.definition.as_dict()
        self.evidence.write_json(f"{prefix}-readback.json", {"definition": actual, "metadata": readback.metadata})
        if readback.version != created.version or actual != definition or any(readback.metadata.get(key) != value for key, value in metadata.items()):
            raise RuntimeError("Temporary agent readback differs from the exact requested definition/ownership")
        return entry

    def cleanup(self) -> dict:
        evidence_errors = []
        for entry in reversed(self.entries):
            try:
                agent = self.lookup(entry["name"])
                if agent is None:
                    entry["cleanup"] = "already_absent"
                    continue
                latest = agent.versions.latest
                owned = operation("read_cleanup_owner", lambda: self.project.agents.get_version(entry["name"], latest.version), self.log)
                if (
                    entry["name"] == SOURCE_AGENT
                    or any(owned.metadata.get(key) != value for key, value in entry["metadata"].items())
                    or (entry["version"] is not None and entry["version"] != owned.version)
                ):
                    raise RuntimeError("Ownership/version changed; refusing to delete an unowned agent")
                if not entry["creation_confirmed"] and fingerprint(owned.definition.as_dict()) != entry["definition_sha256"]:
                    raise RuntimeError("Ambiguous create readback differs; refusing to delete")
                entry.update(version=owned.version, creation_confirmed=True)
                try:
                    operation("delete_copy", lambda: self.project.agents.delete(entry["name"]), self.log)
                except ResourceNotFoundError:
                    pass
                if self.lookup(entry["name"]) is not None:
                    raise RuntimeError("Deleted agent still exists")
                entry["cleanup"] = "deleted"
            except Exception as error:
                entry.update(cleanup="error", cleanup_error=failure_details(error)[0])
            finally:
                try:
                    append_record(self.log, {"kind": "cleanup", "at": utc_now(), **entry})
                except Exception as error:
                    evidence_errors.append(failure_details(error)[0])
        return {
            "status": "completed" if not evidence_errors and all(entry["cleanup"] in ("deleted", "already_absent") for entry in self.entries) else "error",
            "agents": self.entries, "evidence_errors": evidence_errors, "completed_at": utc_now(),
        }


def run_turn(client, pacer, catalogue, case, stage, model, entry, scheduled, log, events, stop, tool_choice,
             *, prior_record=None, prior_run_id=None):
    previous_attempts = (prior_record or {}).get("attempts", 0)
    record = {
        **scheduled, "model": model, "stage": stage, "agent_name": entry["name"], "agent_version": entry["version"],
        **{key: case[key] for key in ("question", "group", "expected", "cohort", "answerable")},
        "status": "error", "attempt_errors": copy.deepcopy((prior_record or {}).get("attempt_errors", [])),
        "attempts": previous_attempts, "prior_attempts": previous_attempts, "attempts_this_run": 0,
        "started_at": utc_now(),
    }
    if prior_record is not None:
        record["recovery"] = {
            "source_run_id": prior_run_id, "prior_status": prior_record["status"],
            "prior_attempts": previous_attempts, "prior_finished_at": prior_record["finished_at"],
            "prior_wall_s_including_pacing_and_retries": prior_record.get("wall_s_including_pacing_and_retries"),
            "prior_error_retryable_under_current_classifier": (
                saved_error_retryable(prior_record["attempt_errors"][-1]["error"]) if prior_record["attempt_errors"] else None
            ),
        }
    started = time.perf_counter()
    request = response_request(catalogue, case, stage, tool_choice)
    try:
        for attempt in range(previous_attempts + 1, MAX_ATTEMPTS + 1):
            check_stop(stop)
            pacer.wait()
            check_stop(stop)
            record["attempts"] = attempt
            record["attempts_this_run"] += 1
            try:
                result = stream_turn(client, request, model, scheduled["effort"])
            except Exception as error:
                detail, retryable, retry_after = failure_details(error)
                partial = getattr(error, "partial", {})
                usage = partial.get("usage") or {}
                if not isinstance(usage, dict):
                    usage = {}
                counts = {key: value for key, value in usage.items() if type(value) is int and value >= 0}
                pacer.account(max(counts.get("total_tokens", 0), counts.get("input_tokens", 0) + counts.get("output_tokens", 0)))
                delay = max(65.0 * attempt, retry_after + 1)
                failure = {
                    "attempt": attempt, "error": detail, "retryable": retryable,
                    "retry_delay_s": delay if retryable and attempt < MAX_ATTEMPTS else None,
                    "partial": partial, "at": utc_now(),
                }
                record["attempt_errors"].append(failure)
                append_record(events, {"kind": "turn_error", "stage": stage, **scheduled, **failure})
                if not retryable or attempt == MAX_ATTEMPTS:
                    record["partial"] = partial
                    break
                if stop.wait(delay):
                    raise PeerStopped("Another worker failed during retry backoff")
            else:
                pacer.account(result["usage"]["total_tokens"])
                primary = matrix.bench.classify(result["tools"][0]) if result["tools"] else "none"
                record.update(result, status="completed", primary=primary, routing_ok=(primary == case["expected"]) if stage == "agent" else None)
                break
    except Exception as error:
        record.update(status="cancelled" if isinstance(error, PeerStopped) else "error", error=failure_details(error)[0])
    finally:
        record["wall_s_including_pacing_and_retries"] = time.perf_counter() - started
        record["finished_at"] = utc_now()
        append_record(log, record)
    if record["status"] != "completed":
        failure = (PeerStopped if record["status"] == "cancelled" else RuntimeError)(
            f"Unrecovered turn at {model}/{stage}/{scheduled['sequence']}; evidence saved"
        )
        failure.record = record
        raise failure
    return record


def project_client(cfg, credential):
    return AIProjectClient(endpoint=cfg["PROJECT_ENDPOINT"], credential=credential, **NO_RETRIES)


def run_model(model, args, cfg, cases, source, catalogue, schedules, run_id, evidence, stop, continuation=None) -> dict:
    pending = continuation.pending(model) if continuation else schedules
    reused = {key: line for key, line in continuation.completed.items() if key[0] == model} if continuation else {}
    prior_records = [record for key, record in continuation.records.items() if key[0] == model] if continuation else []
    reserve = continuation.reserves[model] if continuation else args.reserve_tokens
    result = {
        "model": model, "status": "error", "completed_turns": len(reused),
        "planned_turns": sum(map(len, schedules.values())), "reused_completed_turns": len(reused),
        "new_completed_turns": 0, "new_request_attempts": 0, "new_api_error_attempts": 0,
        "prior_request_attempts": sum(record.get("attempts", 0) for record in prior_records),
        "prior_api_error_attempts": sum(len(record["attempt_errors"]) for record in prior_records),
    }
    cleanup = {"status": "completed", "agents": []}
    cases_by_id = {case["id"]: case for case in cases}
    evidence.write_json(Path(model) / "manifest.json", {
        "model": model, "source_version": source["version"], "source_definition_sha256": fingerprint(source["definition"]),
        "schedule": schedules, "client_token_budget_per_61s": args.budgets[model],
        "interval_s": args.interval, "reserve_tokens": args.reserve_tokens,
        "oracle_system_overlay": ORACLE_SYSTEM_OVERLAY if "oracle" in schedules else None,
        "continuation": {
            "source_run_id": continuation.manifest["run_id"], "reused_completed_turns": len(reused),
            "remaining_turns": sum(map(len, pending.values())), "inherited_reserve_tokens": reserve,
        } if continuation else None,
    })
    with ExitStack() as worker_files:
        events = worker_files.enter_context(evidence.open_log(Path(model) / "events.jsonl"))
        logs = {
            (stage, effort): worker_files.enter_context(evidence.open_log(Path(model) / f"{stage}-{effort}.jsonl"))
            for stage in args.stages for effort in args.efforts
        }
        try:
            for key, line in reused.items():
                logs[key[1:3]].write(line)
            for log in logs.values():
                log.flush()
                os.fsync(log.fileno())
            if any(pending.values()):
                check_stop(stop)
                with AzureCliCredential(process_timeout=120) as credential, project_client(cfg, credential) as project:
                    copies = AgentCopies(project, evidence, model, run_id, events)
                    cleanup = {"status": "unknown", "agents": copies.entries}
                    try:
                        if read_source(project, events) != source:
                            raise RuntimeError("Source version/full definition changed before worker start")
                        provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")
                        pacer = matrix.Pacer(args.interval, args.budgets[model], reserve)
                        if continuation:
                            delay = max(0, continuation.not_before[model] - time.time())
                            if delay and stop.wait(delay):
                                raise PeerStopped("Another worker failed during continuation cooldown")
                        for stage, schedule in pending.items():
                            if not schedule:
                                continue
                            check_stop(stop)
                            with ExitStack() as stack:
                                clients, agents = {}, {}
                                for effort in args.efforts:
                                    if not any(item["effort"] == effort for item in schedule):
                                        continue
                                    check_stop(stop)
                                    definition = evaluation_definition(source["definition"], model, effort, stage)
                                    agents[effort] = copies.create(definition, stage, effort)
                                    name = agents[effort]["name"]
                                    clients[effort] = stack.enter_context(OpenAI(
                                        base_url=f"{cfg['PROJECT_ENDPOINT']}/agents/{name}/endpoint/protocols/openai",
                                        api_key=provider, default_query={"api-version": "v1"}, max_retries=0, timeout=180,
                                    ))
                                for scheduled in schedule:
                                    check_stop(stop)
                                    effort, observed = scheduled["effort"], None
                                    key = turn_key({**scheduled, "model": model, "stage": stage})
                                    try:
                                        observed = run_turn(
                                            clients[effort], pacer, catalogue, cases_by_id[scheduled["case_id"]],
                                            stage, model, agents[effort], scheduled, logs[(stage, effort)], events, stop,
                                            source["definition"].get("tool_choice") or "auto",
                                            prior_record=continuation.failed.get(key) if continuation else None,
                                            prior_run_id=continuation.failed_source_run_ids.get(key) if continuation else None,
                                        )
                                    except Exception as error:
                                        observed = getattr(error, "record", None)
                                        raise
                                    finally:
                                        if observed is not None:
                                            result["new_request_attempts"] += observed["attempts_this_run"]
                                            result["new_api_error_attempts"] += len(observed["attempt_errors"]) - observed["prior_attempts"]
                                    result["completed_turns"] += 1
                                    result["new_completed_turns"] += 1
                            print(f"{model} {stage}: completed {len(schedule)} new turns", flush=True)
                    except BaseException:
                        stop.set()
                        raise
                    finally:
                        cleanup = copies.cleanup()
            if result["completed_turns"] != result["planned_turns"]:
                raise RuntimeError("Consolidated completed count does not match the frozen plan")
            result["status"] = "completed"
        except Exception as error:
            stop.set()
            result.update(status="cancelled" if isinstance(error, PeerStopped) else "error", error=failure_details(error)[0])
        finally:
            if cleanup["status"] != "completed":
                result["status"] = "error"
                stop.set()
            result["cleanup"] = cleanup
            evidence.write_json(Path(model) / "cleanup.json", cleanup)
            evidence.write_json(Path(model) / "summary.json", result)
    return result


def evaluate(args, cfg, cases, cases_sha256, evidence: Evidence, continuation=None) -> int:
    run_id, stop = uuid.uuid4().hex, threading.Event()
    schedules = build_schedules(cases, args.efforts, args.stages, args.runs, args.seed)
    if args.continue_from and continuation is None:
        continuation = load_continuation(args, cfg, cases, cases_sha256, schedules)
    if continuation:
        schedules = copy.deepcopy(continuation.manifest["schedule"])
    manifest = {
        **frozen_configuration(args, cfg, cases, cases_sha256, schedules),
        "run_id": run_id, "started_at": utc_now(),
    }
    if continuation:
        manifest["continuation"] = continuation.provenance
        evidence.write_json("continuation.json", {
            **continuation.provenance,
            "pending_schedule": {model: continuation.pending(model) for model in args.models},
        })
        evidence.write_text("prior-errors.jsonl", "".join(continuation.prior_error_lines))
    evidence.write_json("plan.json", manifest)
    evidence.write_json("cases.json", cases)
    results, source, root_error, workers_started = {}, None, None, False
    invariant = {"unchanged": None, "status": "not_checked"}
    with evidence.open_log("events.jsonl") as events:
        try:
            with AzureCliCredential(process_timeout=120) as credential, project_client(cfg, credential) as project:
                try:
                    source = read_source(project, events)
                    evidence.write_json("source-definition.json", source["definition"])
                    validate_baseline(source["definition"])
                    if continuation and fingerprint(source) != fingerprint(continuation.source):
                        raise ValueError("Live source version/full definition differs from the continuation snapshot")
                    catalogue = fetch_catalogue(credential, cfg)
                    if continuation:
                        if catalogue != continuation.catalogue:
                            raise ValueError("Live catalogue differs from the frozen continuation catalogue (including its date)")
                        continuation.verify_unchanged()
                        catalogue = continuation.catalogue
                    evidence.write_text("catalog.txt", catalogue)
                    manifest.update(
                        source_version=source["version"], source_definition_sha256=fingerprint(source["definition"]),
                        instructions_sha256=hashlib.sha256(source["definition"]["instructions"].encode("utf-8")).hexdigest(),
                        catalogue_sha256=hashlib.sha256(catalogue.encode("utf-8")).hexdigest(),
                    )
                    evidence.write_json("manifest.json", manifest)
                    active_models = []
                    for model in args.models:
                        if continuation and not any(continuation.pending(model).values()):
                            results[model] = run_model(
                                model, args, cfg, cases, source, catalogue, schedules, run_id, evidence, stop, continuation,
                            )
                        else:
                            active_models.append(model)
                    if active_models:
                        with ThreadPoolExecutor(max_workers=min(manifest["workers"], len(active_models)), thread_name_prefix="evaluation") as pool:
                            workers_started = True
                            futures = {
                                pool.submit(run_model, model, args, cfg, cases, source, catalogue, schedules, run_id, evidence, stop, continuation): model
                                for model in active_models
                            }
                            try:
                                for future in as_completed(futures):
                                    model = futures[future]
                                    try:
                                        results[model] = future.result()
                                    except Exception as error:
                                        stop.set()
                                        results[model] = {"model": model, "status": "error", "error": failure_details(error)[0], "cleanup": {"status": "unknown"}}
                            except BaseException:
                                stop.set()
                                raise
                finally:
                    if source is not None:
                        try:
                            current = read_source(project, events)
                            invariant = {
                                "status": "checked", "unchanged": current == source,
                                "source_version": source["version"], "current_version": current["version"],
                                "source_definition_sha256": fingerprint(source["definition"]),
                                "current_definition_sha256": fingerprint(current["definition"]),
                                "current_definition": current["definition"], "checked_at": utc_now(),
                            }
                        except Exception as error:
                            invariant = {"status": "error", "unchanged": None, "error": failure_details(error)[0]}
        except (Exception, KeyboardInterrupt) as error:
            stop.set()
            root_error = failure_details(error)[0]
    original_unchanged = None
    if continuation:
        try:
            continuation.verify_unchanged()
            original_unchanged = True
        except (ValueError, OSError) as error:
            original_unchanged = False
            root_error = root_error or failure_details(error)[0]
        evidence.write_json("continuation-source-unchanged.json", {
            "unchanged": original_unchanged, "source_directory": str(continuation.root),
            "source_run_id": continuation.manifest["run_id"], "source_artifact_sha256": continuation.provenance["source_artifact_sha256"],
            "checked_at": utc_now(),
        })
    ok = (
        root_error is None and invariant["unchanged"] is True and len(results) == len(args.models)
        and original_unchanged is not False
        and all(result["status"] == "completed" and result["cleanup"]["status"] == "completed" for result in results.values())
    )
    evidence.write_json("source-unchanged.json", invariant)
    evidence.write_json("cleanup.json", {
        "status": (
            "completed" if len(results) == len(args.models) and all(result.get("cleanup", {}).get("status") == "completed" for result in results.values())
            else "error" if workers_started else "not_started"
        ),
        "workers": {model: result.get("cleanup") for model, result in results.items()}, "completed_at": utc_now(),
    })
    evidence.write_json("summary.json", {
        "status": "completed" if ok else "error", "error": root_error,
        "source_unchanged": invariant["unchanged"], "workers": results, "finished_at": utc_now(),
        "audio_measured": False, "continuation_source_unchanged": original_unchanged,
        "planned_turns": len(args.models) * sum(map(len, schedules.values())),
        "completed_turns": sum(result.get("completed_turns", 0) for result in results.values()),
        "reused_completed_turns": sum(result.get("reused_completed_turns", 0) for result in results.values()),
        "new_completed_turns": sum(result.get("new_completed_turns", 0) for result in results.values()),
        "request_attempts": sum(result.get("prior_request_attempts", 0) + result.get("new_request_attempts", 0) for result in results.values()),
        "api_error_attempts": sum(result.get("prior_api_error_attempts", 0) + result.get("new_api_error_attempts", 0) for result in results.values()),
    })
    print(f"{'DONE' if ok else 'FAILED'}: private evidence saved in {evidence.root}", flush=True)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_settings(args.env_file)
        cases, cases_sha256 = load_cases(args.cases, args.case_ids)
        schedules = build_schedules(cases, args.efforts, args.stages, args.runs, args.seed)
        continuation = load_continuation(args, cfg, cases, cases_sha256, schedules) if args.continue_from else None
        evidence = Evidence(args.output_dir)
    except (ValueError, OSError) as error:
        argument_parser().error(str(error))
    return evaluate(args, cfg, cases, cases_sha256, evidence, continuation)


if __name__ == "__main__":
    sys.exit(main())
