"""thinking cue: does the on-screen wait indicator ever claim work it isn't doing?

Issue #75: the cue was armed on every response and always read "Looking through
the records…", so "how are you?" produced a retrieval claim that never happened.

The fix is layered, because how much the platform tells us depends on the
binding:

  * model binding — our own tools raise real function calls, so the cue is exact.
  * agent binding (the shipped default) — the Foundry agent runs AI Search / Web
    Search inside its own thread and Voice Live relays nothing: no function call,
    no output item, no *.in_progress event. Confirmed against a live session.
    There the cue is PREDICTED from the user's question and the browser only
    promotes it to a caption once the turn has outrun a no-retrieval reply.

So this guards two things: that the mapping from a tool name to a caption is
right, and that a question with no retrieval signal predicts nothing at all.

No Azure, no network.
"""
import re
import sys
from pathlib import Path

# Anchor on this file, not the working directory, so the suite runs from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.voice.event_handlers import (  # noqa: E402
    _classify_question,
    _retrieval_cue_name,
)

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


print("retrieval cue names (model binding + any managed event that does arrive)")

# Our own function names, which is all model binding ever produces.
check("search_minutes -> records", _retrieval_cue_name("search_minutes"), "search_minutes")
check("search_web -> web", _retrieval_cue_name("search_web"), "search_web")

# Managed item/event names, matched on substrings because the wire spelling
# varies by tool and by API version.
check("file_search_call -> records", _retrieval_cue_name("file_search_call"), "search_minutes")
check("azure_ai_search_call -> records",
      _retrieval_cue_name("azure_ai_search_call"), "search_minutes")
check("web_search_call -> web", _retrieval_cue_name("web_search_call"), "search_web")
check("bing_custom_search_call -> web",
      _retrieval_cue_name("bing_custom_search_call"), "search_web")
# "web_search" also contains "search", so ordering inside the matcher matters.
check("web wins over the generic search fallback",
      _retrieval_cue_name("response.web_search_call.in_progress"), "search_web")

# Enums stringify as "ItemType.MESSAGE", so matching has to go through .value.
from azure.ai.voicelive.models import ItemType, ServerEventType  # noqa: E402

check("ItemType.MESSAGE is not a retrieval", _retrieval_cue_name(ItemType.MESSAGE), None)
check("file-search enum resolves through .value",
      _retrieval_cue_name(ServerEventType.RESPONSE_FILE_SEARCH_CALL_IN_PROGRESS),
      "search_minutes")
check("nothing to go on", _retrieval_cue_name(None), None)
check("a plain message is not a retrieval", _retrieval_cue_name("message"), None)


print("predicting the cue from the question (agent binding)")

# The regression the issue was filed for: chit-chat must predict nothing.
check("a greeting predicts nothing",
      _classify_question("Hey Simone, how are you?"), None)
check("small talk predicts nothing",
      _classify_question("Tell me a joke about penguins"), None)

check("a minutes question predicts records",
      _classify_question("What was discussed in the last meeting?"), "search_minutes")
check("action items predict records",
      _classify_question("What are the action items from the board meeting?"),
      "search_minutes")
check("a price/today question predicts web",
      _classify_question("What is MTH Group share price today?"), "search_web")
check("a news question predicts web",
      _classify_question("Any news on the merger today?"), "search_web")

# A question can carry both; the meeting corpus is the more specific claim.
check("records outranks web when both appear",
      _classify_question("What did we agree about the share price in the last meeting?"),
      "search_minutes")

# Short anaphoric follow-ups drop the subject, so they inherit rather than
# falling back to an unnamed wait.
check("a short follow-up inherits the previous prediction",
      _classify_question("I mean February 2026.", "search_minutes"), "search_minutes")
check("a long marker-less question does NOT inherit",
      _classify_question(
          "Could you please explain that in a much simpler way for me",
          "search_minutes",
      ), None)
check("nothing to inherit stays unnamed",
      _classify_question("I mean February 2026.", None), None)
check("empty transcript predicts nothing", _classify_question(""), None)


print("browser contract")

app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
acs_js = (ROOT / "frontend" / "acs-join.js").read_text(encoding="utf-8")
cue_js = (ROOT / "frontend" / "thinking-cue.js").read_text(encoding="utf-8")
index_html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
acs_html = (ROOT / "frontend" / "acs-join.html").read_text(encoding="utf-8")
style_css = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")

# The whole point of the fix: a prediction must not become a caption until the
# turn has run longer than a no-retrieval reply takes (~1.1-1.5s to first token).
m = re.search(r"PREDICT_MS:\s*(\d+)", cue_js)
check("a promotion delay is defined", bool(m), True)
if m:
    check("promotion waits out a no-retrieval turn", int(m.group(1)) >= 1600, True)

# response_created carries the prediction; without it the cue can never be named
# in agent binding.
check("response_created passes the prediction through",
      "startThinking(msg.expectedTool)" in app_js, True)
# A real tool event must win over a guess, which is what keeps model binding exact.
check("a real tool event is marked authoritative",
      "thinkingAuthoritative = true" in app_js, True)

# The neutral phase is dots with no words, so it makes no claim at all.
check("the pill starts dots-only", 'class="avatar-thinking dots-only"' in index_html, True)
check("the neutral label is empty",
      'id="avatarThinkingText" class="avatar-thinking-text"></div>' in index_html, True)
check("dots-only hides the label", ".avatar-thinking.dots-only .avatar-thinking-text" in style_css, True)

dots = re.search(r'class="avatar-thinking-dots">(.*?)</div>', index_html)
check("the pill renders 8 dots",
      dots.group(1).count("<span>") if dots else 0, 8)
# Each dot needs its own delay or they blink in unison instead of travelling.
check("every dot after the first is staggered",
      all(f".avatar-thinking-dots span:nth-child({i})" in style_css for i in range(2, 9)),
      True)
# Once there are words the caption's own trailing ellipsis is the "still going"
# beat; leading dots in front of it just read as clutter.
check("dots are hidden once a caption is showing",
      ".avatar-thinking:not(.dots-only) .avatar-thinking-dots" in style_css, True)


print("one source of truth for the copy")

# The web stage (channel A/B) and the meeting tile (channel C) render the cue in
# completely different ways, but they must never disagree about what it SAYS.
# Rather than checking two copies match, there is only one copy — so the test is
# that neither renderer has quietly grown its own.
CAPTIONS = ("Checking the records…", "Searching the web…", "Still working, nearly there…")
for text in CAPTIONS:
    check(f"{text!r} is defined in thinking-cue.js", text in cue_js, True)
    check(f"{text!r} is not duplicated in app.js", text in app_js, False)
    check(f"{text!r} is not duplicated in acs-join.js", text in acs_js, False)
    # The trailing ellipsis is load-bearing now that the animated dots are
    # hidden during the caption phase.
    check(f"{text!r} ends in an ellipsis", text.endswith("…"), True)

# Both renderers read the shared object rather than their own constants.
for name in ("SLOW_CAPTION", "SLOW_MS", "MAX_MS", "PREDICT_MS"):
    check(f"thinking-cue.js exports {name}", f"{name}" in cue_js, True)
    check(f"app.js reads CUE.{name}", f"CUE.{name}" in app_js, True)
    check(f"acs-join.js reads CUE.{name}", f"CUE.{name}" in acs_js, True)

# It has to load before the renderers, on both pages. index.html loads app.js as
# a classic script, so the shared file must be classic too (module scripts are
# deferred and would arrive too late).
check("thinking-cue.js is a classic script",
      "type=\"module\"" not in cue_js and "export " not in cue_js, True)
for page, html, after in (
    ("index.html", index_html, "app.js"),
    ("acs-join.html", acs_html, "acs-join.js"),
):
    i_cue = html.find("thinking-cue.js")
    i_app = html.find(after)
    check(f"{page} loads thinking-cue.js", i_cue != -1, True)
    check(f"{page} loads it before {after}", i_cue != -1 and i_cue < i_app, True)

print()
if FAILED:
    print(f"{FAILED} check(s) FAILED")
    sys.exit(1)
print("All checks passed.")
