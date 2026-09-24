"""Private MODEL-mode Voice Live evaluation. Running this CLI makes PAID calls.

    uv run --no-sync python scripts\\bench_realtime_evaluation.py `
        --env-file C:\\private\\evaluation.env --cases C:\\private\\cases.json `
        --output-dir C:\\private\\new-run --case-ids Q1 W2 --runs 1 --stages live

Defaults: EXACT gpt-realtime-2.1 and gpt-realtime-2.1-mini, three rounds, oracle
then live, API 2026-04-10, en-ZA-LeahNeural, TEXT+AUDIO, no avatar/microphone/SR.
Preview requires --api-version 2026-06-01-preview. MAI/Phi are explicit optional
bindings, never aliases/fallbacks. Parent-reported, scoped MAI unavailability and
Phi structured-tool incompatibility are recorded separately in availability.json,
with null quality scores. They create NO default cohort or synthetic scored turn.

The explicit env file must supply AZURE_VOICELIVE_ENDPOINT, AZURE_SEARCH_ENDPOINT
and SEARCH_INDEX_NAME. Production Search is semantic BM25, no vectors, top-5,
12000-character full-section allowance. Production WebIQ must be enabled; no Bing,
stub, agent definition, project or azd lookup is used. Voice/Search authenticate
through async AzureCliCredential(process_timeout=120), never local managed
identity. Dotenv autoload is disabled before backend imports; relevant ambient
settings are cleared and only explicit settings are applied, in this process.
The harness and production tool functions execute LOCALLY from this checkout,
calling remote Voice Live/Search/WebIQ services, not the deployed application.
WebIQ publication/update/crawl metadata and notes pass through unchanged; the
runner never derives currentness or a live-quote timestamp from crawler dates.

Each attempt is a fresh session. Execution is globally serial, with seeded model
rotation, --interval 8 minimum start spacing and --token-budget 90000 per 61s
client window. --reserve-tokens 15000 reserves EACH attempt; reported usage from
ALL response.done events, including tool rounds and audio output, raises actual
charges and subsequent same-stage reservations. Charges remain for 61s after
attempt completion. Missing usage retains reservations, not fabricated usage.
Reported token totals smooth admission BETWEEN complete attempts (tokens * 60 /
budget), never the normal tool-followup inside a measured turn. Genuine reported
rate-limit resets/Retry-After remain enforced and their waits are explicitly traced.
These are conservative client admission estimates, NOT observed service quotas
or guaranteed token bounds. Overruns are charged, never fitted by truncating data.

Only question, frozen catalogue, and optional verbatim oracle_context enter
messages. Gold/scoring/expected/cohort fields NEVER enter requests. Oracle skips
cases without context and uses the fixed neutral overlay with tools disabled.
Only native function events execute tools; spoken JSON is NEVER parsed as a
call. Three calls/four response rounds maximum. A completed empty response still
fails. Tool errors cannot produce a fully successful turn. Only transient
failures retry, at most four total attempts, never with a different model/profile.
Default failure policy stops the run; --failure-policy continue-other-models
explicitly permits a partial matrix. There is no resume.

Output must be NEW/EMPTY and outside Git checkouts. Schema v1 artifacts:
manifest.json, availability.json, prompt.txt, catalogue.txt, tools.json, cases.json,
schedule.json; events.jsonl, attempts.jsonl, turns.jsonl, summary.json.
Events are snapshotted into a bounded in-memory buffer, then redacted and fsynced
at attempt boundaries, outside TTFA. An unclean process exit can lose in-flight
events; completed attempt/turn records remain durable. Buffer overflow fails the
attempt rather than silently losing evidence.
Run-observed unavailable bindings also have availability.jsonl entries. All
evidence is secret-redacted; full source/tool results remain private. PCM is
retained as decoded byte counts/chunk hashes, not playback files.

Primary latency: timings.final_answer_first_received_ms, from immediately before
user text submission to the first TEXT/AUDIO_TRANSCRIPT/PCM of the terminal,
completed, tool-free answer response. First-any timings can include spoken tool
preambles and MUST NOT be used as useful-answer latency. Each response retains
its channels, first timestamps, audio count and function/tool calls. TEXT and
AUDIO_TRANSCRIPT are never concatenated; answer_text prefers the final transcript.
Connection setup and response.create origins are separate. Client throttling is
traced; service-directed waits remain visible in end-to-end observations, while
discretionary admission pacing is outside the turn. No playback/first-audible or
container or deployed-app end-to-end latency is measured. Absent usage/rate
observations remain absent.

Diagnostic records additionally retain T0-T5, function-announcement/argument
readiness, response-create send boundaries, actual client pacing waits, and
in-memory trace-capture costs. Tool payloads, prompts and production behavior
are unchanged. Component sizes are characters/UTF-8 bytes, not claimed provider
token attribution.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import hashlib
import json
import logging
import math
import os
import random
import re
import sys
import time
import uuid
from collections import deque
from collections.abc import Mapping
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
MODELS = ("gpt-realtime-2.1", "gpt-realtime-2.1-mini")
# "gpt-realtime-2" is an explicit opt-in candidate only (must be passed via
# --models): it is NOT part of the default two-model roster in MODELS above,
# and adding it here does not change any default behavior.
MODEL_CHOICES = (*MODELS, "gpt-realtime-2", "mai-mm-realtime", "phi4-mm-realtime")
STAGES = ("oracle", "live")
MAX_ATTEMPTS, MAX_TOOL_CALLS, MAX_RESPONSE_ROUNDS = 4, 3, 4
MAX_BUFFERED_EVENTS = 20000
DOCUMENTED_GLOBAL_TPM, DOCUMENTED_CONNECTIONS_PER_MINUTE = 120000, 30
TOKEN_WINDOW_SECONDS = 61.0
UNAVAILABLE_CODES = {"invalid_model", "model_not_found", "deployment_not_found"}
ORACLE_SYSTEM_OVERLAY = (
    "Fixed-evidence evaluation: the supplied source passages are the evidence available for this turn. "
    "No tools are available. Answer from those passages; state when evidence is insufficient. "
    "Do not claim to have performed a search."
)
PRIOR_CAPABILITY_REPORTS = [
    {
        "model": "mai-mm-realtime", "status": "unavailable", "source": "parent_reported",
        "scope": "Parent's swedencentral MODEL resource; not a global availability claim",
        "api_versions": ["2026-04-10", "2026-06-01-preview"], "voice": "en-ZA-LeahNeural",
        "error": {"code": "invalid_model", "message": "Model mai-mm-realtime is not supported in this region."},
        "quality_evaluated": False, "quality_score": None,
    },
    {
        "model": "phi4-mm-realtime", "status": "incompatible", "source": "parent_reported",
        "scope": "Parent's MODEL resource and production structured-tool workflow",
        "api_versions": ["2026-04-10", "2026-06-01-preview"], "voice": "en-ZA-LeahNeural",
        "detail": "Spoken JSON instead of native function events under the production profile; synthetic probe also had empty terminal output.",
        "quality_evaluated": False, "quality_score": None,
    },
]
PUBLIC_ENV = (
    "AZURE_VOICELIVE_ENDPOINT", "AZURE_SEARCH_ENDPOINT", "SEARCH_INDEX_NAME",
    "SEARCH_SEMANTIC_CONFIG", "AI_SEARCH_QUERY_TYPE", "AI_SEARCH_TOP_K",
    "AI_SEARCH_SNIPPET_CHARS", "WEBIQ_BASE_URL", "TRUSTED_WEB_SITES", "WEBIQ_ALLOWED_DOMAINS",
    "WEBIQ_LANGUAGE", "WEBIQ_REGION", "AVATAR_DISPLAY_NAME", "AVATAR_MODEL", "AVATAR_TYPE",
)
ENV_DEFAULTS = {
    "SEARCH_SEMANTIC_CONFIG": "default-semantic", "AI_SEARCH_QUERY_TYPE": "semantic",
    "AI_SEARCH_TOP_K": "5", "AI_SEARCH_SNIPPET_CHARS": "12000",
    "WEBIQ_BASE_URL": "https://api.microsoft.ai/v3", "TRUSTED_WEB_SITES": "", "WEBIQ_ALLOWED_DOMAINS": "",
    "WEBIQ_LANGUAGE": "en", "WEBIQ_REGION": "ZA",
}
ENV_PREFIXES = (
    "AGENT_", "SEARCH_", "AI_SEARCH_", "AZURE_SEARCH_", "WEBIQ_", "VOICE",
    "AZURE_VOICELIVE_", "REALTIME_", "AVATAR_", "SR_", "BING_",
)
ENV_EXACT = {
    "PROJECT_ENDPOINT", "AZURE_TOKEN_CREDENTIALS", "AUTH_EXCLUDE_MANAGED_IDENTITY",
    "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID",
    "MEETING_CATALOG_TTL_S", "PYTHON_DOTENV_DISABLED",
}
SECRET_KEY = re.compile(
    r"(?i)(api[-_]?key|apikey|authorization|headers|password|secret|"
    r"access[-_]?token|refresh[-_]?token|connection[-_]?string|cookie)"
)
TRANSIENT_CODES = {
    "rate_limit_exceeded", "rate_limit_error", "too_many_requests", "server_error",
    "internal_server_error", "service_unavailable", "temporarily_unavailable",
    "timeout", "request_timeout",
}
PERMANENT_CODES = UNAVAILABLE_CODES | {
    "unsupported_parameter", "invalid_request", "invalid_request_error",
    "bad_request", "bad_request_error", "authentication_error", "permission_denied",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False
    ).encode("utf-8")).hexdigest()


def plain(value):
    if hasattr(value, "as_dict"):
        value = value.as_dict()
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, bytes):
        return {"byte_count": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = sorted({v for v in secrets if isinstance(v, str) and v}, key=len, reverse=True)

    def __call__(self, value):
        value = plain(value)
        if isinstance(value, dict):
            return {key: "[REDACTED]" if SECRET_KEY.search(key) else self(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
            value = re.sub(
                r"(?i)(api[-_]?key|apikey|access_token|client_secret|password|sig)"
                r"(\s*[=:]\s*)[^&\s,\"'}]+", r"\1\2[REDACTED]", value,
            )
        return value


def load_settings(path):
    from dotenv import dotenv_values

    if not path.is_file():
        raise ValueError("env-file does not exist")
    values = dotenv_values(path, interpolate=False)
    secrets = [v for k, v in {**os.environ, **values}.items() if SECRET_KEY.search(k)]
    settings = {**ENV_DEFAULTS, **{k: values[k] for k in PUBLIC_ENV if isinstance(values.get(k), str)}}
    for key in ("AZURE_VOICELIVE_ENDPOINT", "AZURE_SEARCH_ENDPOINT", "SEARCH_INDEX_NAME"):
        if not isinstance(values.get(key), str) or not values[key].strip():
            raise ValueError(f"env-file must explicitly set {key}")
        settings[key] = values[key].strip()
    for key in ("AZURE_VOICELIVE_ENDPOINT", "AZURE_SEARCH_ENDPOINT", "WEBIQ_BASE_URL"):
        url = urlsplit(settings[key])
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError(f"{key} must be HTTPS without credentials, query or fragment")
        settings[key] = settings[key].rstrip("/")
    for key in ("AI_SEARCH_QUERY_TYPE", "AI_SEARCH_TOP_K", "AI_SEARCH_SNIPPET_CHARS"):
        if settings[key] != ENV_DEFAULTS[key]:
            raise ValueError(f"{key} must match the fixed semantic top-5 full-section profile")
    if not settings["SEARCH_SEMANTIC_CONFIG"].strip():
        raise ValueError("SEARCH_SEMANTIC_CONFIG cannot be empty")
    if values.get("WEBIQ_API_KEY"):
        settings["WEBIQ_API_KEY"] = values["WEBIQ_API_KEY"]
    return settings, Redactor(secrets)


@contextmanager
def isolated_environment(settings):
    keys = {k for k in os.environ if k in ENV_EXACT or k.startswith(ENV_PREFIXES)}
    keys |= set(settings) | ENV_EXACT | {"VOICE_BINDING"}
    before = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ.update(settings)
        os.environ.update(PYTHON_DOTENV_DISABLED="1", VOICE_BINDING="model",
                          AZURE_TOKEN_CREDENTIALS="AzureCliCredential", AUTH_EXCLUDE_MANAGED_IDENTITY="true")
        yield
    finally:
        for key, previous in before.items():
            os.environ.pop(key, None)
            if previous is not None:
                os.environ[key] = previous


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for key in ("env-file", "cases", "output-dir"):
        parser.add_argument(f"--{key}", type=Path, required=True)
    parser.add_argument("--models", nargs="+", choices=MODEL_CHOICES, default=list(MODELS))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--stages", nargs="+", choices=STAGES, default=list(STAGES))
    parser.add_argument("--case-ids", nargs="+")
    parser.add_argument("--api-version", default="2026-04-10",
                        help="App parity; preview requires --api-version 2026-06-01-preview.")
    parser.add_argument("--voice", default="en-ZA-LeahNeural")
    parser.add_argument("--interval", type=float, default=8.0)
    parser.add_argument("--token-budget", type=int, default=90000)
    parser.add_argument("--reserve-tokens", type=int, default=15000)
    parser.add_argument("--seed", type=int, default=20260919)
    parser.add_argument("--setup-timeout", type=float, default=180.0)
    parser.add_argument("--turn-timeout", type=float, default=180.0)
    parser.add_argument("--tool-timeout", type=float, default=30.0)
    parser.add_argument("--max-output-tokens", type=int, default=1200)
    parser.add_argument("--failure-policy", choices=("stop", "continue-other-models"), default="stop")
    args = parser.parse_args(argv)
    for key in ("models", "stages", "case_ids"):
        value = getattr(args, key)
        if value and len(value) != len(set(value)):
            parser.error(f"{key} cannot contain duplicates")
    if args.runs < 1 or args.max_output_tokens < 1:
        parser.error("runs and max-output-tokens must be positive")
    if not 0 < args.reserve_tokens <= args.token_budget <= DOCUMENTED_GLOBAL_TPM:
        parser.error("Require 0 < reserve-tokens <= token-budget <= 120000 (documentation reference)")
    for key in ("interval", "setup_timeout", "turn_timeout", "tool_timeout"):
        value = getattr(args, key)
        if not math.isfinite(value) or value < 0 or (key != "interval" and value == 0):
            parser.error(f"Invalid {key}")
    if not args.api_version.strip() or not args.voice.strip():
        parser.error("api-version and voice cannot be empty")
    args.stages = [stage for stage in STAGES if stage in args.stages]
    return args


def load_cases(path, selected_ids=None):
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, list) or not data:
        raise ValueError("cases must be a nonempty JSON list")
    cases, seen = [], set()
    for entry in data:
        if not isinstance(entry, dict) or any(
            not isinstance(entry.get(k), str) or not entry[k].strip() for k in ("id", "question")
        ):
            raise ValueError("Each case requires nonempty id and question")
        if entry["id"] in seen:
            raise ValueError("Duplicate case id")
        seen.add(entry["id"])
        if (entry.get("group") not in ("minutes", "web")
                or entry.get("cohort") not in ("legacy", "holdout", "negative")
                or entry.get("expected") not in ("internal", "external")
                or type(entry.get("answerable")) is not bool):
            raise ValueError("Invalid case group/cohort/expected/answerable")
        context = entry.get("oracle_context")
        if context is not None and not isinstance(context, str):
            raise ValueError("oracle_context must be string or null")
        case = {k: entry[k] for k in ("id", "question", "group", "cohort", "expected", "answerable")}
        if context is not None and context.strip():
            case["oracle_context"] = context
        cases.append(case)
    if selected_ids is not None:
        if not set(selected_ids) <= seen:
            raise ValueError("Unknown case-ids")
        cases = [case for case in cases if case["id"] in selected_ids]
    return cases, hashlib.sha256(raw).hexdigest()


def build_schedule(cases, args):
    schedule = []
    for stage in args.stages:
        eligible = [c["id"] for c in cases if stage != "oracle" or c.get("oracle_context")]
        if not eligible:
            raise ValueError(f"No eligible cases for {stage}")
        for run in range(1, args.runs + 1):
            rng = random.Random(f"{args.seed}:{stage}:{run}")
            ids, models = eligible[:], args.models[:]
            rng.shuffle(ids)
            rng.shuffle(models)
            for index, case_id in enumerate(ids):
                offset = index % len(models)
                for model in models[offset:] + models[:offset]:
                    schedule.append(dict(stage=stage, run=run, case_id=case_id, model=model))
    return schedule


def session_config(runtime, stage, args):
    return {
        "modalities": ["text", "audio"],
        "voice": {"type": "azure-standard", "name": args.voice, "rate": "1.0"},
        "instructions": runtime.prompt, "tools": copy.deepcopy(runtime.tools) if stage == "live" else [],
        "tool_choice": "auto" if stage == "live" else "none", "parallel_tool_calls": False,
        "max_response_output_tokens": args.max_output_tokens, "output_audio_format": "pcm16",
        "turn_detection": None, "input_audio_transcription": None,
    }


def input_messages(catalogue, case, stage):
    messages = [("system", catalogue)]
    if stage == "oracle":
        if not case.get("oracle_context"):
            raise ValueError("Oracle requires source passages")
        messages += [("system", case["oracle_context"]), ("system", ORACLE_SYSTEM_OVERLAY)]
    elif stage != "live":
        raise ValueError("Unknown stage")
    messages.append(("user", case["question"]))
    return [{"role": role, "type": "message", "content": [{"type": "input_text", "text": text}]}
            for role, text in messages]


class Evidence:
    def __init__(self, directory, redact):
        self.root, self.redact, self.logs = directory.resolve(), redact, {}
        self.pending_events = []
        self.closed = False
        if self.root.is_relative_to(ROOT.resolve()) or any(
            (parent / ".git").exists() for parent in (self.root, *self.root.parents)
        ):
            raise ValueError("output-dir must be outside Git checkouts")
        if self.root.exists() and (not self.root.is_dir() or any(self.root.iterdir())):
            raise ValueError("output-dir must be new or empty")
        self.root.mkdir(parents=True, exist_ok=True)
        self.write_json("claim.json", {"created_at": utc_now(), "pid": os.getpid()})

    def _write(self, name, text):
        with (self.root / name).open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())

    def write_text(self, name, text):
        self._write(name, self.redact(text))

    def write_json(self, name, value):
        self._write(name, json.dumps(self.redact(value), indent=2, ensure_ascii=False, allow_nan=False) + "\n")

    def _append(self, kind, value):
        if kind not in self.logs:
            self.logs[kind] = (self.root / f"{kind}.jsonl").open("x", encoding="utf-8")
        stream = self.logs[kind]
        stream.write(json.dumps(self.redact(value), ensure_ascii=False, allow_nan=False) + "\n")
        return stream

    def flush_events(self):
        if not self.pending_events:
            return
        pending, self.pending_events = self.pending_events, []
        for value in pending:
            stream = self._append("events", value)
        stream.flush()
        os.fsync(stream.fileno())

    def emit(self, kind, value):
        if self.closed:
            raise RuntimeError("Evidence capture is closed")
        if kind == "events":
            if len(self.pending_events) >= MAX_BUFFERED_EVENTS:
                raise RuntimeError("Event buffer limit exceeded; this attempt has incomplete evidence")
            self.pending_events.append(copy.deepcopy(value))
            return
        self.flush_events()
        stream = self._append(kind, value)
        stream.flush()
        os.fsync(stream.fileno())

    def close(self):
        try:
            self.flush_events()
        finally:
            for stream in self.logs.values():
                stream.close()
            self.closed = True


def nonnegative_seconds(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else 0.0
    except (ValueError, TypeError):
        return 0.0


def retry_delay(payload):
    if not isinstance(payload, Mapping):
        return 0.0
    delay = 0.0
    normalized = {str(k).lower().replace("-", "_"): v for k, v in payload.items()}
    for name, value in normalized.items():
        if name in ("retry_after", "retry_after_seconds", "retryafter"):
            seconds = nonnegative_seconds(value)
            if not seconds and isinstance(value, str):
                try:
                    seconds = max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
            delay = max(delay, seconds)
        elif name in ("retry_after_ms", "x_ms_retry_after_ms"):
            delay = max(delay, nonnegative_seconds(value) / 1000)
        elif name in ("reset_seconds", "reset_after_seconds") and str(payload.get("remaining")) == "0":
            delay = max(delay, nonnegative_seconds(value))
        elif name.startswith("x_ratelimit_reset_") and str(normalized.get(name.replace("_reset_", "_remaining_"))) == "0":
            if isinstance(value, str) and re.fullmatch(r"(?:\d+(?:\.\d+)?(?:ms|s|m|h))+", value):
                delay = max(delay, sum(float(n) * {"ms": .001, "s": 1, "m": 60, "h": 3600}[unit]
                                       for n, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value)))
        elif isinstance(value, Mapping):
            delay = max(delay, retry_delay(value))
        elif isinstance(value, list):
            delay = max(delay, *(retry_delay(item) for item in value), 0.0)
    return delay


@dataclass
class TokenReservation:
    stage: str
    reserved: int
    charged: int
    expires_at: float
    reported_tokens: int = 0
    reported_responses: int = 0
    completed_responses: int = 0
    started_responses: int = 0

    def reconcile(self):
        unknown = max(self.started_responses, self.completed_responses) - self.reported_responses
        self.charged = max(self.reserved, self.reported_tokens + unknown * self.reserved)


class Pacer:
    """Async reservation/high-water policy, kept local to avoid agent imports."""

    def __init__(self, interval, budget=90000, reserve=15000):
        if (not math.isfinite(interval) or interval < 0 or type(budget) is not int
                or type(reserve) is not int or not 0 < reserve <= budget <= DOCUMENTED_GLOBAL_TPM):
            raise ValueError("Invalid client interval, budget or reservation")
        self.interval, self.budget = interval, budget
        self.reserves = {stage: reserve for stage in STAGES}
        self.last_start, self.blocked_until, self.token_due = None, 0.0, 0.0
        self.calls: deque[TokenReservation] = deque()
        self.active: TokenReservation | None = None

    def defer(self, seconds):
        self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)

    def response_started(self):
        if self.active is None:
            raise RuntimeError("Response without token reservation")
        self.active.started_responses += 1
        self.active.reconcile()

    def account_usage(self, usage):
        if self.active is None:
            raise RuntimeError("Usage without token reservation")
        self.active.completed_responses += 1
        total = usage.get("total_tokens") if isinstance(usage, Mapping) else None
        if type(total) is not int or total < 0:
            self.active.reconcile()
            return None
        self.active.reported_tokens += total
        self.active.reported_responses += 1
        self.active.reconcile()
        delay = total * 60 / self.budget
        self.token_due = max(self.token_due, time.monotonic()) + delay
        return {"reported_total_tokens": total, "reported_turn_tokens_so_far": self.active.reported_tokens,
                "client_delay_seconds": delay, "client_delay_scope": "between_attempts",
                "client_token_budget": self.budget}

    def reservation_snapshot(self):
        if self.active is None:
            raise RuntimeError("No active token reservation")
        call = self.active
        return {
            "client_token_budget": self.budget, "window_seconds": TOKEN_WINDOW_SECONDS,
            "stage": call.stage, "reserved_tokens": call.reserved, "client_charged_tokens": call.charged,
            "reported_tokens_sum": call.reported_tokens if call.reported_responses else None,
            "responses_started": call.started_responses, "responses_done": call.completed_responses,
            "responses_with_usage": call.reported_responses,
            "usage_complete": (call.started_responses > 0
                               and call.started_responses == call.completed_responses == call.reported_responses),
        }

    def finish_turn(self):
        if self.active is None:
            raise RuntimeError("Token reservation already finished")
        call = self.active
        call.reconcile()
        call.expires_at = time.monotonic() + TOKEN_WINDOW_SECONDS
        self.reserves[call.stage] = max(self.reserves[call.stage], min(call.reported_tokens, self.budget))
        note = {**self.reservation_snapshot(), "next_stage_reservation": self.reserves[call.stage]}
        self.active = None
        return note

    def response_delay(self):
        return max(0.0, self.blocked_until - time.monotonic())

    async def wait_response(self):
        delay = self.response_delay()
        if delay:
            await asyncio.sleep(delay)

    async def wait(self, stage="live"):
        if self.active is not None:
            raise RuntimeError("Previous reservation must finish before next session")
        if stage not in self.reserves:
            raise ValueError("Unknown reservation stage")
        reserve = self.reserves[stage]
        while True:
            now = time.monotonic()
            while self.calls and self.calls[0].expires_at <= now:
                self.calls.popleft()
            capacity_due = 0.0
            if self.calls and sum(call.charged for call in self.calls) + reserve > self.budget:
                capacity_due = self.calls[0].expires_at
            due = max(self.blocked_until, self.token_due, capacity_due,
                      self.last_start + self.interval if self.last_start is not None else 0)
            if due <= now:
                break
            await asyncio.sleep(due - now)
        self.last_start = time.monotonic()
        self.active = TokenReservation(stage, reserve, reserve, self.last_start + TOKEN_WINDOW_SECONDS)
        self.calls.append(self.active)
        return self.reservation_snapshot()


class TurnFailure(RuntimeError):
    def __init__(self, code, message, *, retryable=False, retry_after=0.0):
        super().__init__(message)
        self.code, self.retryable, self.retry_after = code, retryable, retry_after


def service_failure(payload):
    error = payload.get("error") or payload
    if not isinstance(error, dict):
        error = {"message": str(error)}
    code = str(error.get("code") or error.get("type") or "service_error")
    status = error.get("status") or error.get("status_code") or payload.get("status_code")
    return TurnFailure(code, str(error.get("message") or code),
                       retryable=code.lower() not in PERMANENT_CODES and (
                           code.lower() in TRANSIENT_CODES or str(status) in ("429", "500", "502", "503", "504")
                       ),
                       retry_after=retry_delay(payload))


def exception_details(exc, phase):
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    headers = getattr(exc, "headers", None) or getattr(getattr(exc, "response", None), "headers", None) or {}
    transient = isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in {
        "ClientConnectionError", "ClientConnectorError", "ServerDisconnectedError",
        "ServiceRequestError", "ServiceResponseError",
    } or str(status) in ("429", "500", "502", "503", "504")
    if isinstance(exc, TurnFailure):
        transient = exc.retryable
    if str(exc).startswith("Failed to parse message"):
        transient = False
    return {
        "code": getattr(exc, "code", None) or ("timeout" if isinstance(exc, TimeoutError) else type(exc).__name__),
        "message": str(exc), "phase": phase, "http_status": status, "retryable": bool(transient),
        "retry_after_seconds": max(getattr(exc, "retry_after", 0.0), retry_delay(headers)),
        "reported_rate_limits": {str(k).lower(): str(v) for k, v in headers.items()
                                if str(k).lower() in ("retry-after", "retry-after-ms", "x-ms-retry-after-ms")
                                or str(k).lower().startswith("x-ratelimit-")},
    }


@asynccontextmanager
async def production_runtime():
    from azure.ai.voicelive import models
    from azure.ai.voicelive.aio import connect
    from azure.identity.aio import AzureCliCredential
    from backend.voice import auth, catalog, functions, instructions, tools

    if any(value is not None for value in (
        auth._default_credential, catalog._search_client, tools._aad_credential, tools._web_client,
        catalog._cache[0], tools._web_probe,
    )):
        raise RuntimeError("Runner requires fresh production clients/cache in its own process")
    credential = auth._CachingCredentialWrapper(AzureCliCredential(process_timeout=120))
    auth._default_credential = credential
    try:
        tools._aad_credential = AzureCliCredential(process_timeout=120)
        schemas = await tools.build_realtime_tools()
        if {tool.get("name") for tool in schemas} != {"search_minutes", "search_web"}:
            raise ValueError("Both production Search and WebIQ tools must be available")
        catalogue = await catalog.get_meeting_catalog(force_refresh=True)
        if not catalogue:
            raise ValueError("A fresh production catalogue is required")
        yield SimpleNamespace(
            connect=connect, models=models, credential=credential,
            execute_function=functions.execute_function, prompt=instructions.load_realtime_instructions(),
            tools=plain(schemas), catalogue=catalogue,
        )
    finally:
        try:
            async with asyncio.timeout(10):
                await catalog.close_search_client()
        finally:
            try:
                async with asyncio.timeout(10):
                    await tools.close_web_client()
            finally:
                async with asyncio.timeout(10):
                    await auth.close_credential()


def decoded_audio(event):
    raw = getattr(event, "delta", None) if hasattr(event, "delta") else event.get("delta")
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)  # SDK attributes already decode the wire base64.
    if isinstance(raw, str):
        try:
            return base64.b64decode(raw, validate=True)
        except ValueError as exc:
            raise TurnFailure("invalid_audio", "Invalid base64 audio") from exc
    raise TurnFailure("invalid_audio", "Audio must be decoded bytes or base64 text")


def check_session_echo(session, requested):
    if not isinstance(session, dict):
        raise TurnFailure("invalid_session_echo", "session.updated omitted the session")
    for key in ("tool_choice", "parallel_tool_calls", "max_response_output_tokens", "output_audio_format",
                "turn_detection", "input_audio_transcription", "instructions"):
        if key in session and session[key] != requested[key]:
            raise TurnFailure("session_profile_mismatch", f"Server changed requested {key}")
    if "modalities" in session and set(session["modalities"]) != set(requested["modalities"]):
        raise TurnFailure("session_profile_mismatch", "Server changed modalities")
    if "tools" in session and {t.get("name") for t in session["tools"]} != {t.get("name") for t in requested["tools"]}:
        raise TurnFailure("session_profile_mismatch", "Server changed tools")
    if "voice" in session:
        voice = session["voice"]
        voice = voice if isinstance(voice, dict) else {"name": voice}
        for key in ("name", "type", "rate"):
            if key in voice and str(voice[key]) != str(requested["voice"][key]):
                raise TurnFailure("session_profile_mismatch", f"Server changed voice {key}")


class Channels:
    def __init__(self):
        self.parts = {"TEXT": {}, "AUDIO_TRANSCRIPT": {}}
        self.first = {"TEXT": None, "AUDIO_TRANSCRIPT": None, "PCM": None}
        self.audio_bytes = 0

    def text(self, channel, key, text, at, *, replace=False):
        if not isinstance(text, str):
            raise TurnFailure("invalid_text", "Text/transcript must be a string")
        if text and self.first[channel] is None:
            self.first[channel] = at
        self.parts[channel][key] = text if replace else self.parts[channel].get(key, "") + text

    def snapshot(self):
        return {"channels": {channel: "".join(parts[key] for key in sorted(parts))
                             for channel, parts in self.parts.items()},
                "first_received_ms": dict(self.first), "audio_bytes": self.audio_bytes}


def text_size(text):
    if not isinstance(text, str):
        return {"characters": None, "utf8_bytes": None}
    return {"characters": len(text), "utf8_bytes": len(text.encode("utf-8"))}


async def run_turn(runtime, settings, args, case, scheduled, attempt, emit, pacer):
    started, submitted, response_create_at = time.monotonic(), None, None
    first_any = {"TEXT": None, "AUDIO_TRANSCRIPT": None, "PCM": None}
    record = {
        "schema_version": 1, **scheduled, "attempt": attempt, "attempt_id": uuid.uuid4().hex,
        "execution_location": "local",
        "started_at": utc_now(), "status": "failed", "tool_calls": [], "responses": [],
        "session_events": [], "rate_limit_events": [], "response_done": [], "usage": [],
        "timings": {"final_answer_first_received_ms": dict(first_any)},
        "audio_bytes": 0, "answer_text": "", "answer_channel": None,
        "channels": {"TEXT": "", "AUDIO_TRANSCRIPT": ""}, "final_response_id": None,
        "preamble_response_ids": [], "final_answer_audio_bytes": 0,
        "token_reservation": pacer.reservation_snapshot(),
        "latency_trace": {
            "measurement_policy_version": 2,
            "T0_ms": 0.0, "T4_ms": None, "T5_ms": None,
            "function_events": [], "response_requests": [], "trace_capture_spans": [],
        },
    }
    config = session_config(runtime, scheduled["stage"], args)
    record["session_config"] = copy.deepcopy(config)
    connect_args = {"endpoint": settings["AZURE_VOICELIVE_ENDPOINT"],
                    "api_version": args.api_version, "model": scheduled["model"]}
    record["connect_args"] = connect_args
    phase, stack = "session_config", AsyncExitStack()

    def trace(kind, value, *, observed=None, observed_utc=None):
        observed = time.monotonic() if observed is None else observed
        capture_started = time.monotonic()
        emit("events", {"schema_version": 1, "attempt_id": record["attempt_id"], "kind": kind,
                        "received_at": observed_utc or utc_now(),
                        "attempt_elapsed_ms": (observed - started) * 1000,
                        "turn_elapsed_ms": (observed - submitted) * 1000 if submitted is not None else None,
                        "data": value})
        if submitted is not None:
            record["latency_trace"]["trace_capture_spans"].append({
                "kind": kind, "start_ms": (capture_started - submitted) * 1000,
                "end_ms": (time.monotonic() - submitted) * 1000,
            })

    async def receive(connection):
        try:
            event = await connection.recv()
            observed, observed_utc = time.monotonic(), utc_now()
        except (EOFError, StopAsyncIteration) as exc:
            raise TurnFailure("truncated_stream", "Connection ended before final completed answer") from exc
        obj, pcm = plain(event), None
        if obj.get("type") in ("response.audio.delta", "response.output_audio.delta"):
            try:
                pcm = decoded_audio(event)
            except TurnFailure:
                obj["delta"] = {"invalid_pcm": True, "sha256": fingerprint(obj.get("delta"))}
                trace("server_event", obj, observed=observed, observed_utc=observed_utc)
                raise
            obj["delta"] = {"pcm_bytes": len(pcm), "sha256": hashlib.sha256(pcm).hexdigest()}
        trace("server_event", obj, observed=observed, observed_utc=observed_utc)
        pacer.defer(retry_delay(obj))
        if "rate_limit" in str(obj.get("type", "")):
            record["rate_limit_events"].append(obj)
        if obj.get("type") in ("error", "response.error"):
            raise service_failure(obj)
        return obj, pcm, observed

    async def send_item(connection, item, *, user_submission=False):
        nonlocal submitted
        trace("conversation.item.create", plain(item))
        if user_submission:
            submitted, record["submitted_at"] = time.monotonic(), utc_now()
        send_started = time.monotonic()
        await connection.conversation.item.create(item=item)
        sent = time.monotonic()
        timing = {
            "send_started_ms": (send_started - submitted) * 1000,
            "send_completed_ms": (sent - submitted) * 1000,
        } if submitted is not None else None
        if user_submission:
            record["latency_trace"]["user_item_send"] = timing
        return timing

    async def create_response(connection):
        nonlocal response_create_at
        requested = time.monotonic()
        delay = pacer.response_delay()
        request = {
            "round": len(record["responses"]) + 1,
            "requested_ms": (requested - submitted) * 1000,
            "planned_wait_ms": delay * 1000,
            "token_due_ms": max(0.0, pacer.token_due - requested) * 1000,
            "service_defer_due_ms": max(0.0, pacer.blocked_until - requested) * 1000,
        }
        if delay:
            trace("client_pacing", {
                "before": "response.create", "wait_seconds": delay,
                "reason": "service_directed",
            })
        wait_started = time.monotonic()
        await pacer.wait_response()
        request.update(
            wait_started_ms=(wait_started - submitted) * 1000,
            wait_completed_ms=(time.monotonic() - submitted) * 1000,
        )
        trace("response.create", {})
        response_create_at = time.monotonic()
        record["timings"].setdefault("initial_response_create_ms", (response_create_at - submitted) * 1000)
        pacer.response_started()
        request["send_started_ms"] = (response_create_at - submitted) * 1000
        await connection.response.create()
        request["send_completed_ms"] = (time.monotonic() - submitted) * 1000
        record["latency_trace"]["response_requests"].append(request)

    try:
        sdk_session = runtime.models.RequestSession(**config)
        record["session_config"] = plain(sdk_session)
        phase = "connect"
        async with asyncio.timeout(args.setup_timeout):
            trace("connect", connect_args)
            connection_started = time.monotonic()
            connection = await stack.enter_async_context(runtime.connect(**connect_args, credential=runtime.credential))
            record["timings"]["connection_setup_ms"] = (time.monotonic() - connection_started) * 1000
            phase = "session_setup"
            trace("session.update", record["session_config"])
            await connection.session.update(session=sdk_session)
            while True:
                event, _, _ = await receive(connection)
                if event.get("type") in ("session.created", "session.updated"):
                    record["session_events"].append(event)
                if event.get("type") == "session.updated":
                    check_session_echo(event.get("session"), config)
                    break
            record["timings"]["session_ready_ms"] = (time.monotonic() - started) * 1000
        phase = "turn"
        async with asyncio.timeout(args.turn_timeout):
            for message in input_messages(runtime.catalogue, case, scheduled["stage"]):
                item_type = runtime.models.UserMessageItem if message["role"] == "user" else runtime.models.SystemMessageItem
                await send_item(connection, item_type(content=[
                    runtime.models.InputTextContentPart(text=part["text"]) for part in message["content"]
                ]), user_submission=message["role"] == "user")
            await create_response(connection)
            seen_calls = set()
            for round_number in range(1, MAX_RESPONSE_ROUNDS + 1):
                channels, pending = Channels(), {}
                current = {"round": round_number, "response_id": None, "status": "streaming",
                           "response_create_ms": (response_create_at - submitted) * 1000,
                           "is_final_answer": False, "function_calls": [], "tool_calls": [],
                           "first_from_response_create_ms": dict(channels.first), **channels.snapshot()}
                record["responses"].append(current)
                while True:
                    event, pcm, observed = await receive(connection)
                    at, kind = (observed - submitted) * 1000, event.get("type", "")
                    event_rid = (event.get("response", {}).get("id")
                                 if kind in ("response.created", "response.done") else event.get("response_id"))
                    if event_rid and current["response_id"] and event_rid != current["response_id"]:
                        raise TurnFailure("response_id_mismatch", "Refusing mixed response content/timing")
                    if event_rid:
                        current["response_id"] = event_rid
                    if kind in ("session.created", "session.updated"):
                        record["session_events"].append(event)
                        if kind == "session.updated":
                            check_session_echo(event.get("session"), config)
                    if pcm:
                        channels.audio_bytes += len(pcm)
                        record["audio_bytes"] += len(pcm)
                        if channels.first["PCM"] is None:
                            channels.first["PCM"] = at
                    key = (event.get("output_index", 0), event.get("content_index", 0))
                    for marker, channel in (("text", "TEXT"), ("audio_transcript", "AUDIO_TRANSCRIPT")):
                        if kind in (f"response.{marker}.delta", f"response.output_{marker}.delta"):
                            channels.text(channel, key, event.get("delta", ""), at)
                        elif kind in (f"response.{marker}.done", f"response.output_{marker}.done"):
                            channels.text(channel, key, event.get("text" if marker == "text" else "transcript", ""), at, replace=True)
                    if kind in ("response.output_item.added", "response.output_item.done", "conversation.item.created"):
                        item = event.get("item", {})
                        if item.get("type") == "function_call" and not (
                            kind == "conversation.item.created" and item.get("call_id") in seen_calls
                        ):
                            record["latency_trace"]["function_events"].append({
                                "round": round_number, "event": kind, "received_ms": at,
                                "item_id": item.get("id"), "call_id": item.get("call_id"),
                            })
                            identity = item.get("id") or item.get("call_id")
                            pending[identity] = {**pending.get(identity, {}), **item}
                    if kind in ("response.function_call_arguments.delta", "response.function_call_arguments.done"):
                        if kind.endswith(".done"):
                            record["latency_trace"]["function_events"].append({
                                "round": round_number, "event": kind, "received_ms": at,
                                "item_id": event.get("item_id"), "call_id": event.get("call_id"),
                            })
                        identity = event.get("item_id") or event.get("call_id")
                        item = pending.setdefault(identity, {})
                        for field in ("call_id", "name"):
                            if event.get(field):
                                item[field] = event[field]
                        item["arguments"] = (event.get("arguments", "") if kind.endswith(".done")
                                             else item.get("arguments", "") + event.get("delta", ""))
                    if kind == "response.done":
                        current["response_done_ms"] = at
                        response = event.get("response", {})
                        record["response_done"].append(response)
                        current["status"] = response.get("status")
                        if response.get("usage") is not None:
                            record["usage"].append({"response_id": response.get("id"), "usage": response["usage"]})
                        current["usage_pacing"] = pacer.account_usage(response.get("usage"))
                        if response.get("status") != "completed":
                            if response.get("status") == "failed":
                                raise service_failure(response.get("status_details") or response)
                            raise TurnFailure("incomplete_response", f"Response status: {response.get('status')}")
                        if response.get("error") or response.get("incomplete_details") or (
                            isinstance(response.get("status_details"), dict) and response["status_details"].get("error")
                        ):
                            raise TurnFailure("inconsistent_response", "Completed response also reported error/incomplete details")
                        for oi, item in enumerate(response.get("output") or []):
                            if item.get("type") == "function_call":
                                identity = item.get("id") or item.get("call_id")
                                pending[identity] = {**pending.get(identity, {}), **item}
                            if item.get("type") == "message" and item.get("role") == "assistant":
                                if item.get("status") not in (None, "completed"):
                                    raise TurnFailure("incomplete_message", "Assistant message was not completed")
                                for ci, part in enumerate(item.get("content") or []):
                                    for field, channel in (("text", "TEXT"), ("transcript", "AUDIO_TRANSCRIPT")):
                                        if part.get(field) is not None:
                                            channels.text(channel, (oi, ci), part[field], at, replace=True)
                    current.update(channels.snapshot())
                    current["function_calls"] = copy.deepcopy(list(pending.values()))
                    current["first_from_response_create_ms"] = {
                        channel: first - current["response_create_ms"] if first is not None else None
                        for channel, first in channels.first.items()
                    }
                    for channel, first in channels.first.items():
                        if first_any[channel] is None and first is not None:
                            first_any[channel] = first
                    if kind == "response.done":
                        break
                calls = {}
                for item in pending.values():
                    if not item.get("call_id") or not item.get("name"):
                        raise TurnFailure("invalid_function_call", "Function omitted call_id/name")
                    calls[item["call_id"]] = item
                current["function_calls"] = list(calls.values())
                if calls:
                    if channels.audio_bytes or any(current["channels"].values()):
                        record["preamble_response_ids"].append(current["response_id"])
                    if scheduled["stage"] != "live":
                        raise TurnFailure("oracle_tool_call", "Oracle emitted a native function call")
                    if len(seen_calls) + len(calls) > MAX_TOOL_CALLS or round_number == MAX_RESPONSE_ROUNDS:
                        raise TurnFailure("tool_limit", "Maximum three calls/four response rounds exceeded")
                    for call_id, item in calls.items():
                        if call_id in seen_calls:
                            raise TurnFailure("duplicate_function_call", "Function call_id was reused")
                        seen_calls.add(call_id)
                        call = {"call_id": call_id, "name": item["name"], "arguments": item.get("arguments", ""),
                                "response_id": current["response_id"], "status": "running",
                                "execution_location": "local", "response_round": round_number}
                        markers = [
                            marker for marker in record["latency_trace"]["function_events"]
                            if marker["round"] == round_number and (
                                marker["call_id"] == call_id
                                or (item.get("id") and marker["item_id"] == item["id"])
                            )
                        ]
                        call["timing_stages"] = {
                            "function_announced_ms": next((
                                marker["received_ms"] for marker in markers
                                if marker["event"] in ("response.output_item.added", "conversation.item.created")
                            ), None),
                            "arguments_ready_ms": next((
                                marker["received_ms"] for marker in markers
                                if marker["event"] == "response.function_call_arguments.done"
                            ), None),
                            "call_response_done_ms": current["response_done_ms"],
                        }
                        record["tool_calls"].append(call)
                        current["tool_calls"].append(call)
                        trace("tool_start", call)
                        tool_started = time.monotonic()
                        call["timing_stages"]["T1_ms"] = (tool_started - submitted) * 1000
                        try:
                            arguments = json.loads(call["arguments"])
                            if not isinstance(arguments, dict) or not isinstance(arguments.get("query"), str) or not arguments["query"].strip():
                                raise ValueError("Function requires a nonempty string query")
                            call["query"] = arguments["query"]
                            if call["name"] not in {"search_minutes", "search_web"}:
                                result = {"error": f"Unknown function: {call['name']}"}
                            else:
                                async with asyncio.timeout(args.tool_timeout):
                                    result = await runtime.execute_function(call["name"], call["arguments"])
                            if not isinstance(result, dict):
                                raise ValueError("Production function returned non-object result")
                            call["result"] = result
                            truncated = call["name"] == "search_minutes" and any(
                                part.get("truncated") for part in result.get("passages", []) if isinstance(part, dict)
                            )
                            call["status"] = "error" if result.get("error") or truncated else "completed"
                            if truncated:
                                call["error"] = {"code": "truncated_retrieval", "message": "Full sections were not returned"}
                        except Exception as exc:
                            call["status"], call["error"] = "error", exception_details(exc, "tool")
                            pacer.defer(call["error"]["retry_after_seconds"])
                            call["result"] = {"error": f"{type(exc).__name__}: {exc}"}
                        finally:
                            tool_finished = time.monotonic()
                            call["duration_ms"] = (tool_finished - tool_started) * 1000
                            call["timing_stages"]["T2_ms"] = (tool_finished - submitted) * 1000
                            if call["status"] == "running":
                                call["status"] = "cancelled"
                            trace("tool_result", call)
                        sent = await send_item(connection, runtime.models.FunctionCallOutputItem(
                            call_id=call_id, output=json.dumps(call["result"], ensure_ascii=False)
                        ))
                        call["timing_stages"]["result_send_started_ms"] = sent["send_started_ms"]
                        call["timing_stages"]["T3_ms"] = sent["send_completed_ms"]
                    await create_response(connection)
                    continue
                if not response.get("output"):
                    raise TurnFailure("empty_response", "Completed terminal response had no output")
                if not any(item.get("type") == "message" and item.get("role") == "assistant"
                           for item in response["output"]):
                    raise TurnFailure("missing_final_message", "Final response omitted assistant message")
                preferred = "AUDIO_TRANSCRIPT" if current["channels"]["AUDIO_TRANSCRIPT"].strip() else "TEXT"
                answer = current["channels"][preferred]
                if not answer.strip() or not channels.audio_bytes:
                    raise TurnFailure("missing_answer_channel", "TEXT+AUDIO requires final text/transcript and nonempty PCM")
                current["is_final_answer"] = True
                record.update(channels=current["channels"], answer_channel=preferred, answer_text=answer,
                              final_response_id=current["response_id"], final_answer_audio_bytes=channels.audio_bytes)
                record["timings"]["final_answer_first_received_ms"] = dict(current["first_received_ms"])
                record["timings"]["final_first_received_ms"] = dict(current["first_received_ms"])
                record["timings"]["final_first_from_response_create_ms"] = dict(current["first_from_response_create_ms"])
                record["latency_trace"]["T4_ms"] = current["first_received_ms"]["AUDIO_TRANSCRIPT"]
                record["latency_trace"]["T5_ms"] = current["first_received_ms"]["PCM"]
                record["status"] = ("completed_with_tool_error" if any(
                    call["status"] != "completed" for call in record["tool_calls"]
                ) else "completed")
                break
    except asyncio.CancelledError:
        record.update(status="cancelled", error={"code": "cancelled", "phase": phase, "retryable": False})
        raise
    except Exception as exc:
        record["error"] = exception_details(exc, phase)
        if record["error"]["code"] in UNAVAILABLE_CODES:
            record.update(status="unavailable", quality_evaluated=False, quality_score=None)
        pacer.defer(record["error"]["retry_after_seconds"])
    finally:
        try:
            async with asyncio.timeout(10):
                await stack.aclose()
        except Exception as exc:
            record.update(status="failed", cleanup_error=exception_details(exc, "connection_close"))
        record["timings"]["first_any_received_ms"] = dict(first_any)
        record["timings"]["first_received_ms"] = dict(first_any)
        initial = record["timings"].get("initial_response_create_ms")
        record["timings"]["first_from_response_create_ms"] = {
            channel: value - initial if value is not None and initial is not None else None
            for channel, value in first_any.items()
        }
        record["timings"]["attempt_total_ms"] = (time.monotonic() - started) * 1000
        record["finished_at"], record["token_accounting"] = utc_now(), pacer.finish_turn()
        record["latency_trace"]["service_wait_observed"] = any(
            request["planned_wait_ms"] > 0
            for request in record["latency_trace"]["response_requests"]
        )
        record["context_sizes"] = {
            "measurement": "Observed text sizes, not exact provider token attribution",
            "prior_user_turns": 0,
            "system_prompt": text_size(runtime.prompt),
            "catalogue": text_size(runtime.catalogue),
            "tool_schemas_json": text_size(json.dumps(record["session_config"]["tools"], ensure_ascii=False)),
            "user_question": text_size(case["question"]),
            "oracle_context": text_size(case.get("oracle_context", "") if scheduled["stage"] == "oracle" else ""),
            "tool_calls": [
                {"call_id": call["call_id"], "name": call["name"],
                 "arguments": text_size(call["arguments"]),
                 "result_json": text_size(json.dumps(call.get("result"), ensure_ascii=False))}
                for call in record["tool_calls"]
            ],
        }
        emit("attempts", record)
        if record.get("error", {}).get("code") in UNAVAILABLE_CODES:
            emit("availability", {"schema_version": 1, "source": "run_observed", "status": "unavailable",
                                  "model": scheduled["model"], "api_version": args.api_version,
                                  "attempt_id": record["attempt_id"], "error": record["error"],
                                  "quality_evaluated": False, "quality_score": None})
    return record


def freeze(evidence, settings, args, cases, raw_hash, schedule, runtime):
    source_paths = [
        "scripts\\bench_realtime_evaluation.py", "backend\\voice\\instructions.py",
        "backend\\voice\\tools.py", "backend\\voice\\functions.py", "backend\\voice\\catalog.py",
        "backend\\voice\\auth.py", "backend\\avatar_identity.py", "backend\\onboarding.py",
        "prompts\\realtime\\instructions.md",
    ]
    manifest = {
        "schema_version": 1, "measurement_policy_version": 2,
        "created_at": utc_now(), "binding": "model",
        "execution_location": "local", "tool_execution_location": "local",
        "measurement_scope": "Local checkout benchmark calling remote Voice Live/Search/WebIQ; not the deployed application",
        "web_result_date_policy": "Preserve publication/update/crawl fields and notes verbatim; infer no currentness or live-quote timestamp",
        "config": {k: settings[k] for k in PUBLIC_ENV if k in settings},
        "credential": "Async AzureCliCredential(process_timeout=120), cached for Voice/Search",
        "web_grounding": "Production WebIQ, no Bing", "models": args.models, "stages": args.stages,
        "runs": args.runs, "case_ids": [case["id"] for case in cases], "seed": args.seed,
        "failure_policy": args.failure_policy, "interval_seconds": args.interval,
        "prior_capability_reports_file": "availability.json",
        "documented_limits_reference": {
            "global_tpm": DOCUMENTED_GLOBAL_TPM, "new_connections_per_minute": DOCUMENTED_CONNECTIONS_PER_MINUTE,
            "source": "User-supplied Voice Live documentation; NOT an observed quota",
        },
        "client_token_policy": {
            "global_budget": args.token_budget, "initial_reservation": args.reserve_tokens,
            "window_seconds": TOKEN_WINDOW_SECONDS, "expiry": "61 seconds after attempt completion",
            "accounting": "Sum ALL response.done usage including tool rounds; charge at least reservation",
            "adaptive": "Raise next same-stage reservation to largest reported turn total, capped at budget",
            "missing_usage": "Retain one reservation per unreported/unfinished response; not observed tokens",
            "response_pacing": "Only genuine reported service resets/Retry-After; no token smoothing inside a turn",
            "admission_pacing": "Reported total_tokens * 60 / client budget applied between complete attempts",
            "overruns": "Charge excess and wait; never truncate data, change profile, or substitute models",
        },
        "max_attempts": MAX_ATTEMPTS, "max_tool_calls": MAX_TOOL_CALLS, "max_response_rounds": MAX_RESPONSE_ROUNDS,
        "setup_timeout_seconds": args.setup_timeout, "turn_timeout_seconds": args.turn_timeout,
        "tool_timeout_seconds": args.tool_timeout, "api_version": args.api_version,
        "sdk_version": version("azure-ai-voicelive"),
        "session_profiles": {stage: plain(runtime.models.RequestSession(**session_config(runtime, stage, args)))
                             for stage in args.stages},
        "prompt_sha256": fingerprint(runtime.prompt), "tools_sha256": fingerprint(runtime.tools),
        "catalogue_sha256": fingerprint(runtime.catalogue), "cases_file_sha256": raw_hash,
        "selected_cases_sha256": fingerprint(cases), "schedule_sha256": fingerprint(schedule),
        "oracle_system_overlay": ORACLE_SYSTEM_OVERLAY,
        "oracle_skipped_case_ids": [case["id"] for case in cases if not case.get("oracle_context")],
        "source_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in source_paths},
        "timing": {
            "origin": "Immediately before user conversation.item.create; connection/setup excluded",
            "primary_latency_field": "timings.final_answer_first_received_ms",
            "final_answer_first_received_ms": "First TEXT/AUDIO_TRANSCRIPT/PCM in terminal completed tool-free response, from submission",
            "first_any_received_ms": "May contain tool preamble; NOT useful-answer latency",
            "response_origins": "Each response also records first_from_response_create_ms",
            "preambles": "Kept in responses and preamble_response_ids; not in answer_text",
            "latency_trace": {
                "T0": "Immediately before user item send; zero origin",
                "T1": "Tool execution begins after arguments and response.done; includes argument validation",
                "T2": "Tool execution and result validation end",
                "T3": "SDK await for function output send completes; not a server acknowledgement",
                "T4": "First terminal answer AUDIO_TRANSCRIPT observed by SDK client",
                "T5": "First terminal answer PCM observed by SDK client",
                "client_waits": "response_requests labels service waits; discretionary smoothing is between attempts",
                "observer_overhead": "trace_capture_spans measures in-memory event snapshot cost; file flush is after the attempt",
            },
            "not_measured": ["playback", "first audible latency", "avatar", "microphone", "SR",
                             "container latency", "deployed-app end-to-end latency"],
        },
        "audio_storage": "Decoded byte counts and chunk SHA256, no playback files",
        "evidence_capture": {
            "mode": "buffered_per_attempt", "max_buffered_events": MAX_BUFFERED_EVENTS,
            "durability": "Events and attempt record fsynced after each attempt; unclean exit may lose in-flight events",
            "overflow": "Fail the attempt; never silently drop and count it as complete",
        },
    }
    evidence.write_json("manifest.json", manifest)
    for name, text in (("prompt.txt", runtime.prompt), ("catalogue.txt", runtime.catalogue)):
        evidence.write_text(name, text)
    for name, value in (("tools.json", runtime.tools), ("cases.json", cases), ("schedule.json", schedule)):
        evidence.write_json(name, value)


async def run_matrix(runtime, settings, args, cases, schedule, evidence, summary):
    pacer = Pacer(args.interval, args.token_budget, args.reserve_tokens)
    stopped, unavailable = set(), set()
    for key in ("turns", "attempts", "completed", "failed", "unavailable", "skipped"):
        summary.setdefault(key, 0)
    summary.setdefault("stopped_models", [])
    summary.setdefault("unavailable_models", [])
    by_id = {case["id"]: case for case in cases}
    for scheduled in schedule:
        if scheduled["model"] in stopped:
            continue
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await pacer.wait(scheduled["stage"])
            record = await run_turn(runtime, settings, args, by_id[scheduled["case_id"]],
                                    scheduled, attempt, evidence.emit, pacer)
            summary["attempts"] += 1
            if (record["status"] != "failed" or not record.get("error", {}).get("retryable")
                    or attempt == MAX_ATTEMPTS or record.get("cleanup_error")):
                break
            pacer.defer(max(2 ** attempt, record["error"]["retry_after_seconds"]))
        evidence.emit("turns", record)
        summary["turns"] += 1
        if record["status"] == "completed":
            summary["completed"] += 1
        else:
            if record["status"] == "unavailable":
                summary["unavailable"] += 1
                unavailable.add(scheduled["model"])
                summary["unavailable_models"] = sorted(unavailable)
            else:
                summary["failed"] += 1
            stopped.add(scheduled["model"])
            summary["stopped_models"] = sorted(stopped)
            if args.failure_policy == "stop":
                break
    summary["skipped"] = len(schedule) - summary["turns"]
    summary["status"] = "completed" if summary["completed"] == len(schedule) else "partial"


async def execute(args, settings, redact, cases, raw_hash, schedule):
    evidence = Evidence(args.output_dir, redact)
    summary = dict(schema_version=1, execution_location="local", status="failed", planned=len(schedule), turns=0, attempts=0,
                   completed=0, failed=0, unavailable=0, unavailable_models=[], skipped=0, stopped_models=[])
    try:
        evidence.write_json("availability.json", {
            "schema_version": 1, "reports": PRIOR_CAPABILITY_REPORTS,
            "note": "Scoped parent reports, not new observations or scored/scheduled evaluation turns",
        })
        async with AsyncExitStack() as stack:
            async with asyncio.timeout(args.setup_timeout):
                runtime = await stack.enter_async_context(production_runtime())
            freeze(evidence, settings, args, cases, raw_hash, schedule, runtime)
            await run_matrix(runtime, settings, args, cases, schedule, evidence, summary)
    except asyncio.CancelledError:
        summary["status"] = "cancelled"
        raise
    except Exception as exc:
        summary.update(status="failed", error=exception_details(exc, "runner"))
    finally:
        summary["finished_at"], summary["skipped"] = utc_now(), summary["planned"] - summary["turns"]
        try:
            evidence.write_json("summary.json", summary)
        finally:
            evidence.close()
    return summary


def main(argv=None):
    args = parse_args(argv)
    previous, previous_logging = os.environ.get("PYTHON_DOTENV_DISABLED"), logging.root.manager.disable
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    try:
        settings, redact = load_settings(args.env_file)
        cases, raw_hash = load_cases(args.cases, args.case_ids)
        schedule = build_schedule(cases, args)
        sys.path.insert(0, str(ROOT))
        logging.disable(logging.CRITICAL)  # Only redacted evidence is a diagnostic channel.
        with isolated_environment(settings):
            summary = asyncio.run(execute(args, settings, redact, cases, raw_hash, schedule))
        print(json.dumps({k: summary[k] for k in (
            "execution_location", "status", "planned", "completed", "failed", "unavailable", "skipped"
        )}))
        return 0 if summary["status"] == "completed" else 1
    except (ValueError, OSError):
        print("Configuration/output rejected; check explicit settings and use a new private directory.", file=sys.stderr)
        return 2
    finally:
        logging.disable(previous_logging)
        if previous is None:
            os.environ.pop("PYTHON_DOTENV_DISABLED", None)
        else:
            os.environ["PYTHON_DOTENV_DISABLED"] = previous


if __name__ == "__main__":
    raise SystemExit(main())
