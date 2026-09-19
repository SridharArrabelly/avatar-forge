"""Pure, offline-testable helpers for deriving benchmark-grading metrics from
``turns.jsonl`` records produced by ``bench_realtime_evaluation.py``.

These helpers exist to make timing/token/routing derivations independently
testable and to prevent the channel-confusion bug caught in review: the
"final answer received" timing dict carries three distinct channel keys
(``TEXT``, ``AUDIO_TRANSCRIPT``, ``PCM``) and they must never be conflated.

- ``AUDIO_TRANSCRIPT`` is the spoken-word *transcript* text timing — useful,
  but it is NOT the audio (PCM) receipt time.
- ``PCM`` is the actual synthesized-audio byte receipt timing — this is what
  "audio latency" must mean.
- ``TEXT`` is the text-channel timing (null for audio-modality sessions).

Likewise, "first-any" (which may include a pre-tool spoken preamble) must be
read from the turn-level ``timings.first_any_received_ms`` dict (covering the
whole turn/session), never from ``responses[0]`` alone, since a turn can have
multiple response rounds and the first round is not necessarily the first
receipt event of any kind.
"""

from __future__ import annotations

import math
from typing import Iterable, Mapping, Sequence

# Canonical channel labels. Do not rename without updating all call sites --
# the whole point of these constants is to make "audio" mean PCM and nothing
# else.
CHANNEL_TEXT = "TEXT"
CHANNEL_TRANSCRIPT = "AUDIO_TRANSCRIPT"
CHANNEL_AUDIO = "PCM"


def select_timing(timings_block: Mapping[str, object] | None, channel: str) -> float | None:
    """Return the millisecond value for ``channel`` from a turn's timings
    sub-dict (e.g. ``turn["timings"]["final_answer_first_received_ms"]``),
    or ``None`` if the block/channel is missing or null.

    This never falls back to a different channel: callers must be explicit
    about which channel they want (transcript vs audio vs text).
    """
    if not timings_block:
        return None
    value = timings_block.get(channel)
    if value is None:
        return None
    return float(value)


def final_answer_transcript_ms(turn: Mapping[str, object]) -> float | None:
    """First-received timing (ms) of the terminal answer's spoken
    *transcript* text. This is NOT audio latency."""
    timings = turn.get("timings") or {}
    return select_timing(timings.get("final_answer_first_received_ms"), CHANNEL_TRANSCRIPT)


def final_answer_audio_ms(turn: Mapping[str, object]) -> float | None:
    """First-received timing (ms) of the terminal answer's actual PCM audio
    bytes. This is the only correct meaning of "final answer audio
    latency"."""
    timings = turn.get("timings") or {}
    return select_timing(timings.get("final_answer_first_received_ms"), CHANNEL_AUDIO)


def first_any_audio_ms(turn: Mapping[str, object]) -> float | None:
    """First-received timing (ms) of ANY audio byte across the whole turn
    (including any pre-tool spoken preamble round), read from the turn-level
    ``timings.first_any_received_ms`` block -- never from
    ``responses[0]`` in isolation, which only reflects the first response
    round and silently drops turns whose first round was not the first
    receipt event."""
    timings = turn.get("timings") or {}
    return select_timing(timings.get("first_any_received_ms"), CHANNEL_AUDIO)


def has_preamble(turn: Mapping[str, object]) -> bool:
    """True if the turn produced any pre-final-answer spoken preamble
    response (tracked by the runner in ``preamble_response_ids``)."""
    ids = turn.get("preamble_response_ids") or []
    return len(ids) > 0


def reported_tokens_sum(turn: Mapping[str, object]) -> int:
    """Total reported tokens for the whole turn/attempt, across every
    response/tool round -- NOT just the terminal response's own usage.

    Uses ``token_accounting.reported_tokens_sum``, which the runner already
    accumulates across rounds; grading code must not re-derive this from a
    single terminal ``usage_pacing`` entry, which only reflects the last
    round.
    """
    accounting = turn.get("token_accounting") or {}
    return int(accounting.get("reported_tokens_sum", 0) or 0)


def terminal_round_tokens(turn: Mapping[str, object]) -> int | None:
    """Tokens reported for only the terminal (final-answer) response round,
    kept as a distinct, separately labelled subset -- not a substitute for
    ``reported_tokens_sum``."""
    responses = turn.get("responses") or []
    if not responses:
        return None
    final = responses[-1]
    usage = final.get("usage_pacing") or {}
    value = usage.get("reported_total_tokens")
    return int(value) if value is not None else None


def nearest_rank_percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile (no interpolation), matching the convention
    used elsewhere in this project's agent-mode study so numbers are
    comparable across evaluation tracks.

    For a sorted sample of size n, the nearest-rank index for percentile p
    (0 < p <= 100) is ``ceil(p / 100 * n)`` (1-based), clamped to
    ``[1, n]``.
    """
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    rank = max(1, min(n, math.ceil(pct / 100.0 * n)))
    return ordered[rank - 1]


def median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def tool_calls_made(turn: Mapping[str, object]) -> int:
    return len(turn.get("tool_calls") or [])


def oracle_tools_disabled_compliant(turn: Mapping[str, object]) -> bool:
    """Oracle-stage turns must make zero tool calls (tools are disabled by
    design in that stage); this is a compliance check, not a "routing"
    quality judgment, and must never be pooled with live-stage routing
    correctness."""
    return tool_calls_made(turn) == 0


def group_by(turns: Iterable[Mapping[str, object]], *keys: str):
    buckets: dict[tuple, list[Mapping[str, object]]] = {}
    for turn in turns:
        bucket_key = tuple(turn.get(k) for k in keys)
        buckets.setdefault(bucket_key, []).append(turn)
    return buckets
