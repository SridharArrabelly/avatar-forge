"""Measure what the audit trail costs the turn it is recording (#30).

Capture runs inside ``handle_event``, on the event loop that carries audio, so
anything synchronous it does is added directly to the turn. The question is not
"is the writer fast" but "how much is charged to the turn before the writer is
even involved".

Three arms, matching switches that actually exist:

    off      ENABLE_AUDIT=false                  every entry point inert
    capture  ENABLE_AUDIT=true AUDIT_SINK=none   record built and queued, nothing stored
    file     ENABLE_AUDIT=true AUDIT_SINK=file   as above, plus a real sink draining

``capture - off`` is what capture charges the turn. ``file - capture`` shows
whether a draining sink leaks back onto the turn path; by design it must not,
since submit() is non-blocking and the writer is a background task.

Each arm runs in its own subprocess, for two reasons. ENABLE_AUDIT is read at
import time, so arms cannot share an interpreter. And ``backend/config.py`` calls
``load_dotenv(override=True)``, which means a local .env would *override* the
arm being tested and silently invalidate the whole run -- so children are given a
working directory where no .env is discoverable, and every arm then asserts that
the configuration it actually resolved is the one intended.

    uv run --no-sync python scripts/bench_audit_latency.py
    uv run --no-sync python scripts/bench_audit_latency.py --arm capture -n 5000

Offline: touches no Azure resource and costs nothing. It measures the synchronous
cost on the turn, which is precisely the part a deployed A/B cannot separate from
network jitter. Run this first to learn whether a live A/B could detect anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# expected (ENABLE_AUDIT, AUDIT_SINK, is_enabled) per arm
ARMS = {
    "off": ({"ENABLE_AUDIT": "false", "AUDIT_SINK": "none"}, False),
    "capture": ({"ENABLE_AUDIT": "true", "AUDIT_SINK": "none"}, True),
    "file": ({"ENABLE_AUDIT": "true", "AUDIT_SINK": "file"}, True),
}

# A real retrieval turn: spoken question, a tool result holding several
# passages, a spoken answer. Size matters -- records.py caps tool payloads at
# AUDIT_TOOL_PAYLOAD_MAX_KB, and truncation is itself work.
QUESTION = "what did the board decide about the capital expenditure budget"
ANSWER = (
    "The board approved the capital expenditure budget for the next financial "
    "year, with the reservation that any single item above five million rand "
    "returns to the committee for a second review before it is committed. "
) * 2
PASSAGE = (
    "Minutes of the finance committee. The capital expenditure budget was "
    "tabled and discussed at length, with particular attention to the phasing "
    "of the infrastructure programme across the two outer years. "
)
TOOL_RESULT = {"results": [{"id": f"doc-{i}", "content": PASSAGE * 3,
                            "score": 0.9 - i * 0.05} for i in range(6)]}
TOOL_ARGS = {"query": "capital expenditure budget board decision", "top": 6}


class FakeHandler:
    """Shaped like VoiceSessionHandler as far as the audit API reaches."""

    model_binding = False
    client_id = "bench-client"
    voice_session_id = "bench-session"
    audit_channel = "web"
    audit_agent_name = "AvatarAgent"


def percentile(ordered, pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted sequence."""
    if not ordered:
        return 0.0
    k = (len(ordered) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


async def run_arm(iterations: int, warmup: int) -> dict:
    from backend import config
    import backend.audit as audit
    from backend.logsafe import fingerprint, keys_only

    await audit.init_audit()

    def one_turn(handler) -> None:
        """Exactly the calls handle_event makes, in the order it makes them."""
        audit.start_turn(handler)
        audit.set_conversation(handler, "conv_bench", "resp_bench")
        audit.record_user_text(handler, QUESTION, "item_bench")
        audit.record_tool(handler, "search_minutes", TOOL_ARGS, TOOL_RESULT, 42.0)
        audit.record_assistant_text(handler, ANSWER)
        audit.finish_turn(handler, status="completed",
                          output_types=["ItemType.MESSAGE"])

    for _ in range(warmup):
        one_turn(FakeHandler())
        await asyncio.sleep(0)

    samples = []
    for _ in range(iterations):
        handler = FakeHandler()
        t0 = time.perf_counter_ns()
        one_turn(handler)
        samples.append((time.perf_counter_ns() - t0) / 1000.0)  # microseconds
        # Yield outside the timed region so the background writer is scheduled.
        await asyncio.sleep(0)

    # The sanitiser cost, which is always on -- ENABLE_AUDIT does not gate it.
    args_json = json.dumps(TOOL_ARGS)
    fp_samples = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        fingerprint(QUESTION)
        keys_only(args_json)
        fp_samples.append((time.perf_counter_ns() - t0) / 1000.0)

    stats = dict(audit.stats())
    enabled = audit.is_enabled()
    await audit.shutdown_audit()

    samples.sort()
    fp_samples.sort()
    return {
        # Echoed back so the parent can prove the arm was not overridden.
        "resolved_enable_audit": config.ENABLE_AUDIT,
        "resolved_sink": config.AUDIT_SINK,
        "is_enabled": enabled,
        "n": iterations,
        "median_us": statistics.median(samples),
        "mean_us": statistics.fmean(samples),
        "p95_us": percentile(samples, 0.95),
        "p99_us": percentile(samples, 0.99),
        "max_us": samples[-1],
        "fingerprint_median_us": statistics.median(fp_samples),
        "stats": stats,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Measure audit capture cost per turn.")
    ap.add_argument("--arm", choices=sorted(ARMS))
    ap.add_argument("-n", "--iterations", type=int, default=2000)
    ap.add_argument("--warmup", type=int, default=200)
    args = ap.parse_args()

    if args.arm:
        print("__RESULT__" + json.dumps(asyncio.run(
            run_arm(args.iterations, args.warmup))))
        return 0

    print(f"audit capture cost per turn — {args.iterations} turns per arm, "
          f"{args.warmup} warmup\n")

    workdir = Path(tempfile.mkdtemp(prefix="audit-bench-"))
    results, failures = {}, []
    try:
        for arm, (env, expect_enabled) in ARMS.items():
            child = dict(os.environ)
            child.update(env)
            child["PYTHONPATH"] = str(ROOT)
            # The writer batches up to 50 records with a 2s window, then redacts
            # and renders them, so it drains far slower than this loop produces.
            # Production never sees that: turns are seconds apart and the queue
            # sits near-empty. Size the queue to the run so capture always
            # measures the enqueue path rather than the drop path.
            child["AUDIT_QUEUE_MAX"] = str(args.iterations + args.warmup + 1000)
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--arm", arm,
                 "-n", str(args.iterations), "--warmup", str(args.warmup)],
                capture_output=True, text=True, env=child,
                # No .env is discoverable from here, so load_dotenv(override=True)
                # cannot quietly replace the arm under test.
                cwd=str(workdir),
            )
            line = next((ln for ln in proc.stdout.splitlines()
                         if ln.startswith("__RESULT__")), None)
            if line is None:
                print(f"  {arm}: FAILED to produce a result")
                print(proc.stdout[-2000:])
                print(proc.stderr[-2000:])
                return 1
            r = json.loads(line[len("__RESULT__"):])
            results[arm] = r

            # Guard: prove the arm is the configuration actually measured.
            want_enable = env["ENABLE_AUDIT"] == "true"
            if r["resolved_enable_audit"] is not want_enable:
                failures.append(
                    f"{arm}: ENABLE_AUDIT resolved to {r['resolved_enable_audit']},"
                    f" expected {want_enable} — a .env overrode the arm")
            if r["resolved_sink"] != env["AUDIT_SINK"]:
                failures.append(
                    f"{arm}: AUDIT_SINK resolved to {r['resolved_sink']!r},"
                    f" expected {env['AUDIT_SINK']!r}")
            if r["is_enabled"] is not expect_enabled:
                failures.append(
                    f"{arm}: is_enabled() is {r['is_enabled']},"
                    f" expected {expect_enabled}")
            if expect_enabled:
                want = args.iterations + args.warmup
                stats = r["stats"]
                got = stats.get("submitted", 0)
                dropped = stats.get("dropped", 0)
                if got + dropped < want:
                    failures.append(
                        f"{arm}: {got} submitted + {dropped} dropped < {want} turns")
                if dropped:
                    failures.append(
                        f"{arm}: {dropped} records dropped — the queue saturated, "
                        f"so the timings past that point measure the drop path")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if failures:
        print("ARM VERIFICATION FAILED — measurements are meaningless:\n")
        for f in failures:
            print(f"  {f}")
        return 1

    header = (f"{'arm':>9}  {'sink':>7}  {'median':>11}  {'mean':>11}  "
              f"{'p95':>11}  {'p99':>11}")
    print(header)
    print("-" * len(header))
    for arm, r in results.items():
        print(f"{arm:>9}  {r['resolved_sink']:>7}  {r['median_us']:>9.2f}us  "
              f"{r['mean_us']:>9.2f}us  {r['p95_us']:>9.2f}us  "
              f"{r['p99_us']:>9.2f}us")

    off = results["off"]["median_us"]
    print("\ncost charged to the turn, against ENABLE_AUDIT=false:")
    for arm in ("capture", "file"):
        d = results[arm]["median_us"] - off
        print(f"  {arm:>7}  {d:+9.2f}us  ({d / 1000:+.4f}ms) per turn")

    fp = results["off"]["fingerprint_median_us"]
    print(f"\nlog sanitiser, always on (ENABLE_AUDIT does not gate it): "
          f"{fp:.2f}us per turn")

    # What it would take to see this in a deployed A/B. Two-sample comparison,
    # 80% power at 5% significance, needs roughly 16*sigma^2/delta^2 per arm.
    delta = results["file"]["median_us"] - off
    print("\nturns needed per arm to detect this end to end:")
    print(f"{'turn-latency jitter (sd)':>26}   {'turns per arm':>16}")
    for sigma_ms in (5, 20, 50):
        sigma_us = sigma_ms * 1000.0
        n = 16.0 * sigma_us ** 2 / (delta ** 2) if delta > 0 else float("inf")
        print(f"{f'{sigma_ms} ms':>26}   {n:>16,.0f}")
    print("\nA Voice Live turn is hundreds of milliseconds end to end and its")
    print("variance is dominated by the network. The capture cost is four to five")
    print("orders of magnitude below that, so a deployed A/B would be measuring")
    print("jitter, not audit. That is the useful result: it says do not run one.")
    print("\nWhat a live run should still prove is correctness, not latency —")
    print("that Cosmos accepts writes under managed identity, and that the")
    print("agent-mode reconciler recovers tool I/O (scripts/smoke_audit_conversation.py).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
