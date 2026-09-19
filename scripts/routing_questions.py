"""The shared routing question set and classifier — imported, never run.

No prefix, because this is a **library** (see ``README.md``: ``setup_``/``grant_``/
``smoke_``/``bench_`` all answer "what does running this cost me?"; this one costs
nothing because there is nothing to run). Both routing benchmarks import it:

* ``bench_routing_agent.py``  — agent binding, hosted Foundry tools
* ``bench_routing_model.py``  — model binding, in-process FunctionTools

so the two bindings are scored against **identical** questions and cannot silently
diverge.

This module deliberately imports nothing beyond the standard library. It used to
live inside ``bench_routing_agent.py``, which meant the model-mode harness had to
exec that whole module — and transitively ``smoke_foundry_agent.py``, pulling in
``azure.identity``, ``azure.search.documents`` and ``openai`` — just to read a list
of strings. Keep it dependency-free so importing the questions stays free.

The prose rationale for *why* each question is in the set lives in
``docs/testing-routing.md``. That file is the commentary; this file is the data.
Update both together.
"""
from __future__ import annotations

# (question, expected) where expected in {"internal", "external"}
#
# The core set is three groups of five. Note what the groups do and do NOT test:
#
#   minutes  + policies  -> BOTH expect "internal", because both corpora sit
#                           behind the SAME hosted tool (azure_ai_search) in the
#                           SAME index. So these five policy questions do NOT
#                           test corpus separation - that is retrieval-level and
#                           is measured separately. What they DO test is that a
#                           policy question does not LEAK TO THE WEB TOOL, which
#                           is a real and previously-shipped failure: a prompt
#                           that says "only meeting minutes are internal" sends
#                           "what is our gift policy" straight to Bing, which
#                           does not hold MTN's internal policies.
#   web                  -> expects "external".
MINUTES = [
    ("What did we decide about dividends in the last board meeting?", "internal"),
    ("What were the action items from the February 2026 board meeting?", "internal"),
    ("Who attended the October 2025 board meeting?", "internal"),
    ("Summarise the customer experience discussion from the October 2025 board meeting.", "internal"),
    ("What strategy did the board agree in the 15 September 2023 meeting?", "internal"),
]

# Ordered by how strongly the surface form pulls towards the web tool, so a
# partial pass still says something: Q1 is the canonical phrasing, Q3 sounds
# like a question about general law rather than an MTN rule.
POLICIES = [
    ("What is our gift policy?", "internal"),
    ("What is the maximum value of a gift I can accept from a supplier?", "internal"),
    ("Who owns a patent created by one of our employees?", "internal"),
    ("Am I eligible for a study bursary?", "internal"),
    ("What does our responsible betting policy say about data breaches?", "internal"),
]

WEB = [
    ("Who is MTN's Group CFO?", "external"),
    ("What was MTN's FY2025 revenue?", "external"),
    ("What is MTN's share price today?", "external"),
    ("What is Vodacom doing in fintech?", "external"),
    ("What is MTN's Ambition 2025?", "external"),
]

CORE = MINUTES + POLICIES + WEB

# The discriminating set. Every one of these is a case where the *surface form*
# of the question pulls the wrong way, so they separate a prompt that states a
# rule from one that merely lists examples. The core set above is saturated at
# 30/30, so it can only catch a regression - improvement has to show up here.
#
# Sourced from "Boundary / edge cases" in docs/testing-routing.md, where they
# were recorded as manual-only.
BOUNDARY = [
    # Public governance facts, despite the word "board".
    ("Who is on MTN's board?", "external"),
    ("Who chairs the board?", "external"),
    # "our / we / MTN's" must not force an internal lookup.
    ("What's our revenue?", "external"),
    # A date alone must not force internal; only meeting/minutes framing does.
    ("What is MTN's share price on 31 March?", "external"),
    # Genuinely both - internal first, then public. Scored on the FIRST tool.
    ("Compare what the board discussed on fintech with Airtel's public strategy.",
     "internal"),
    # Purely public: superficially parallel to the one above, but no internal side.
    ("Compare MTN and Airtel fintech.", "external"),
]

TIERS = {"core": CORE, "boundary": BOUNDARY, "all": CORE + BOUNDARY}

# Reporting only. The question tuples stay 2-wide on purpose: bench_routing_model.py
# unpacks them as ``for idx, (q, expected) in enumerate(questions)``, so widening
# the tuple would break the model-mode harness. Grouping therefore lives beside the
# data, not inside it.
GROUPS = {"minutes": MINUTES, "policies": POLICIES, "web": WEB}
_GROUP_OF = {q: name for name, qs in GROUPS.items() for q, _ in qs}

# Retrieval-quality questions are deliberately NOT here. They route to the web
# tool unambiguously, so scoring them would award a guaranteed pass and inflate
# the routing number. They are manual, and live in docs/testing-routing.md.


def group_of(question: str) -> str:
    """Which core group a question belongs to; BOUNDARY questions fall through."""
    return _GROUP_OF.get(question, "boundary")


def classify(itype: str) -> str:
    t = itype.lower()
    # Agent mode fires hosted tools (azure_ai_search_call, bing_custom_search_
    # preview_call); model mode fires our in-process FunctionTools registered in
    # backend/voice/tools.py as search_minutes / search_web. Recognise both so
    # this harness can score either binding.
    if "azure" in t or "ai_search" in t or "minutes" in t:
        return "internal"
    if "bing" in t or "web" in t:
        return "external"
    return f"other:{itype}"
