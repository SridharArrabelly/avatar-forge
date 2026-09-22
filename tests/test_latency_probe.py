"""latency probe: is it genuinely inert, and is it wired to the right edges?

`backend/voice/latency_trace.py` states two blind spots plainly: its clock
starts at speech_stopped, "NOT microphone capture time", and "WebRTC avatar
playback ... not observable here". Those are the first and last segments a
person in the room actually experiences, so the browser probe is the only
instrument that can close them.

A measurement tool that changes the thing it measures is worse than no tool,
and one wired to the wrong edge is worse still. This guards both:

  * default OFF - no env, no query string, no storage key means no probe, so
    a production page cannot start sampling because someone shipped a default.
  * every mark guarded - probeMark/probeStartMic return before doing any work
    when the probe is off, so the disabled path costs nothing per frame.
  * wired to the five edges the measurement is defined over. If a hook is
    dropped the probe still runs and still prints, it just silently reports
    null for that span - which is exactly the kind of quiet wrong number this
    whole exercise exists to avoid.

Static analysis of the shipped frontend. No browser, no Azure, no network.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "frontend" / "app.js"

FAILED = 0


def check(label: str, got, want) -> None:
    global FAILED
    ok = got == want
    if not ok:
        FAILED += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         got:  {got!r}")
        print(f"         want: {want!r}")


def main() -> int:
    src = APP.read_text(encoding="utf-8")

    print("probe is off unless explicitly asked for")
    # The only two switches, and neither may default to on.
    check("reads ?probe=1", "get('probe') === '1'" in src, True)
    check("reads localStorage avatarProbe",
          "getItem('avatarProbe') === '1'" in src, True)
    # A bare `|| true`, or a default of '1', would arm it for every visitor.
    check("no truthy fallback on the switch",
          bool(re.search(r"PROBE_ON\s*=\s*true", src)), False)
    check("resolver fails closed",
          bool(re.search(r"catch\s*\(e\)\s*\{\s*return false;\s*\}", src)), True)

    print("\ndisabled probe does no work")
    for fn in ("probeMark", "probeStartMic"):
        body = src.split(f"function {fn}(", 1)[1]
        first = body.split("\n", 2)[1].strip() + " " + body.split("\n", 3)[2].strip()
        check(f"{fn} returns early when off", "!PROBE_ON" in first, True)

    print("\nwired to the five edges the spans are defined over")
    for mark, edge in (
        ("probeMark('speech_stopped')", "end of the VAD window"),
        ("probeMark('transcript')", "recognition finalised"),
        ("probeMark('first_text')", "first answer token"),
        ("probeMark('speaking')", "avatar actually speaking"),
    ):
        check(f"{edge}", mark in src, True)
    # Two independent sources for the last edge: the service's own speaking
    # event, and audio energy as the fallback when it sends none.
    check("speaking has a data-channel source",
          src.count("probeMark('speaking')") >= 2, True)
    check("mic sampling starts with capture", "probeStartMic();" in src, True)
    check("mic sampling stops with capture", "probeStopMic();" in src, True)

    print("\nreports honestly")
    # A missing edge must report null, never a span measured from zero.
    check("absent edge yields null",
          "(a && b ? Math.round(b - a) : null)" in src, True)
    check("emits one record per turn", src.count("'[PROBE] '") == 1, True)

    print()
    if FAILED:
        print(f"FAILED {FAILED} check(s)")
        return 1
    print("All latency-probe checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
