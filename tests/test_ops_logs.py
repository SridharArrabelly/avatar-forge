"""ops logs: does conversation content ever reach operational telemetry?

Container stdout is collected into Log Analytics, and will reach Application
Insights once telemetry ships. Those stores are operational — broad access,
export, dashboards, a retention window chosen for debugging. Conversation
content logged there bypasses every control the audit trail applies, including
``ENABLE_AUDIT=false``, which a deployment may have chosen precisely because it
does not want conversations recorded.

Two layers are guarded here:

  * the helpers, behaviourally — a fingerprint must not contain the text, and
    must still distinguish one question from another;
  * every ``logger``/``print`` call in ``backend/``, statically — because the
    real risk is not the lines fixed today, it is the next line someone adds.

The static sweep is what caught a tenth leak that a hand audit had missed, so
it is doing real work rather than restating the fix.

No Azure, no network.
"""
import asyncio
import ast
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.logsafe import fingerprint, keys_only  # noqa: E402
from backend.voice.functions import execute_function  # noqa: E402

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


SECRET = "What was decided about the Henderson acquisition in February?"

print()
print("fingerprint carries the shape, never the words")

fp = fingerprint(SECRET)
check("no word of the question survives",
      any(w in fp for w in ("Henderson", "acquisition", "February", "decided")),
      False)
check("length is preserved", fp.startswith(f"len={len(SECRET)}"), True)
check("same text fingerprints alike", fingerprint(SECRET), fp)
check("different text fingerprints differently",
      fingerprint("What was decided about the Henderson acquisition in March?") == fp,
      False)
# These run on real transcripts, which are routinely empty or absent when a
# turn is barged in on. A helper that raises there would turn a privacy fix
# into an outage.
check("empty is handled", fingerprint(""), "len=0 fp=empty")
check("none is handled", fingerprint(None), "len=0 fp=none")


print()
print("keys_only describes shape, not values")

ko = keys_only('{"query": "Henderson acquisition"}')
check("argument names survive", ko, "keys=['query']")
check("argument values do not", "Henderson" in ko, False)
check("malformed JSON degrades to a fingerprint",
      keys_only("{not json: Henderson").startswith("len="), True)
check("malformed JSON leaks nothing", "Henderson" in keys_only("{nope: Henderson"),
      False)
check("empty payload", keys_only("{}"), "keys=[]")


print()
print("the real dispatch path logs no content")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


# Malformed arguments are the case that used to log the raw payload verbatim.
# Driving the real function keeps this honest: it asserts what the shipped code
# emits, not what a reimplementation of it would.
cap = _Capture()
root = logging.getLogger()
root.addHandler(cap)
root.setLevel(logging.DEBUG)
try:
    asyncio.run(execute_function("no_such_tool", '{"query": "' + SECRET + '"'))
finally:
    root.removeHandler(cap)

emitted = " | ".join(cap.lines)
check("something was actually logged (the test would be vacuous otherwise)",
      len(cap.lines) > 0, True)
check("the question does not appear in the logs", SECRET in emitted, False)
check("no fragment of it appears either",
      any(w in emitted for w in ("Henderson", "acquisition", "February")), False)


print()
print("no logging call in backend/ interpolates conversation content")

# Names that hold user speech, model answers, or retrieved passages at some
# point in this codebase. Matching on the *name* is deliberately blunt: a new
# log line that interpolates one of these is guilty until allowlisted, which is
# the behaviour that catches the leak nobody remembered to look for.
CONTENT_NAMES = {
    "query", "transcript", "utterance", "raw", "arguments", "passage",
    "passages", "answer", "reply", "snippet", "content", "user_text",
    "assistant_text", "question",
}

# Passing content through one of these is the whole point of the fix, so a call
# to them is a sanitisation boundary and the scan stops there. `len` earns its
# place for the same reason: a count of retrieved passages is exactly the kind
# of operational signal these logs should keep.
SANITISERS = {"fingerprint", "keys_only", "len"}

LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}

# Reviewed and safe. Each entry is a deliberate exception with a stated reason,
# so the list stays short and every addition is an explicit decision.
ALLOWLIST = {
    # Built from ACS_WAKE_PHRASES config, not from anything the user said.
    ("backend/acs/bridge.py", "wake-phrase hint"),
}


def _is_log_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "print"
    return isinstance(func, ast.Attribute) and func.attr in LOG_METHODS


def _content_names(node: ast.AST) -> set:
    """Content-bearing names reachable in this expression, skipping sanitisers.

    Written as an explicit walk rather than ``ast.walk`` because it has to stop
    descending at ``fingerprint(query)`` — otherwise the very fix being verified
    would register as a violation.
    """
    found = set()

    def visit(n: ast.AST) -> None:
        if isinstance(n, ast.Call):
            name = n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
            if name in SANITISERS:
                return
        if isinstance(n, ast.Name) and n.id in CONTENT_NAMES:
            found.add(n.id)
        elif isinstance(n, ast.Attribute) and n.attr in CONTENT_NAMES:
            found.add(n.attr)
        for child in ast.iter_child_nodes(n):
            visit(child)

    visit(node)
    return found


def _literal_text(node: ast.AST) -> str:
    return " ".join(
        n.value for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )


def scan(source: str, rel: str) -> list:
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not _is_log_call(node):
            continue
        names = set()
        for arg in list(node.args) + [k.value for k in node.keywords]:
            names |= _content_names(arg)
        if not names:
            continue
        literals = _literal_text(node)
        if any(f == rel and marker in literals for f, marker in ALLOWLIST):
            continue
        hits.append(f"{rel}:{node.lineno}  {sorted(names)}")
    return hits


offenders = []
for path in sorted((ROOT / "backend").rglob("*.py")):
    offenders += scan(path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix())

if offenders:
    print("         content-bearing log lines found:")
    for o in offenders:
        print(f"           {o}")
check("no log call interpolates a content variable", offenders, [])

# Guard the guard. The first version of this sweep was line-based and silently
# missed every multi-line logger call — which is most of the ones being fixed —
# so it passed while the leak was still there. These pin the two properties
# that failure depended on.
check("a single-line leak is detected",
      len(scan('logger.info(f"q={query!r}")', "x.py")), 1)
check("a multi-line leak is detected",
      len(scan('logger.info(\n    f"n={n}  q={query!r}"\n)', "x.py")), 1)
check("an attribute leak is detected",
      len(scan('logger.info(f"{args_done.arguments}")', "x.py")), 1)
check("a sanitised call is not flagged",
      scan('logger.info(f"[{fingerprint(query)}]")', "x.py"), [])
check("a count of content is not flagged",
      scan('logger.info(f"n={len(passages)}")', "x.py"), [])
check("but the content beside the count still is",
      len(scan('logger.info(f"n={len(passages)} q={query}")', "x.py")), 1)
check("an ordinary log line is not flagged",
      scan('logger.info(f"took {elapsed_ms:.0f}ms n={len(results)}")', "x.py"), [])


print()
if FAILED:
    print(f"{FAILED} check(s) FAILED")
    sys.exit(1)
print("All checks passed.")
