"""build_query: does site scoping render the operators Web IQ actually documents?

No Azure, no network. Guards the shape of the query string, including the
exclusion form that the previous implementation rendered as `site:-host`.
"""
import sys

sys.path.insert(0, ".")

from backend.voice.tools import (  # noqa: E402
    build_query,
    is_nonprod_host,
    strip_scope_operators,
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


print("build_query")

check("no domains leaves the query untouched",
      build_query("q", []), "q")

check("single include",
      build_query("q", ["mtn.com"]), "q (site:mtn.com)")

check("multiple includes are OR-ed",
      build_query("q", ["mtn.com", "jse.co.za"]),
      "q (site:mtn.com OR site:jse.co.za)")

check("a full URL is reduced to its host",
      build_query("q", ["https://www.mtn.com/investors"]),
      "q (site:www.mtn.com)")

# The bug this test exists for: `-domain` used to render as `site:-domain`,
# a nonsense hostname that silently matched nothing.
check("exclusion renders as -site:, not site:-",
      build_query("q", ["-wikipedia.org"]), "q -site:wikipedia.org")

check("includes and excludes combine",
      build_query("q", ["mtn.com", "-wikipedia.org"]),
      "q (site:mtn.com) -site:wikipedia.org")

check("excluded full URL is reduced to its host",
      build_query("q", ["-https://en.wikipedia.org/wiki/X"]),
      "q -site:en.wikipedia.org")

check("whitespace around entries is ignored",
      build_query("q", ["  mtn.com  ", " -wikipedia.org "]),
      "q (site:mtn.com) -site:wikipedia.org")

check("empty and bare-dash entries are dropped",
      build_query("q", ["mtn.com", "", "-"]), "q (site:mtn.com)")

# --- scoping the model wrote itself is stripped before ours is applied -------
# Observed live: the model ignores the schema and writes its own operators,
# so the wire query carried two overlapping scopes.
print("\nstrip_scope_operators")

check("colon-less 'site x.com' (the form actually observed)",
      strip_scope_operators("MTN Group CFO name site mtn.com"),
      "MTN Group CFO name")

check("plain site: operator",
      strip_scope_operators("MTN CFO site:mtn.com"), "MTN CFO")

check("negated site: operator",
      strip_scope_operators("MTN CFO -site:wikipedia.org"), "MTN CFO")

check("an OR-group leaves no empty parens behind",
      strip_scope_operators("MTN CFO (site:mtn.com OR site:jse.co.za)"),
      "MTN CFO")

check("a dangling OR is cleaned up",
      strip_scope_operators("MTN CFO site:mtn.com OR site:jse.co.za"),
      "MTN CFO")

# Guards against over-matching: these are ordinary words, not operators.
check("the word 'site' alone is left alone",
      strip_scope_operators("site visit report"), "site visit report")

check("'website' is not mistaken for an operator",
      strip_scope_operators("MTN website redesign"), "MTN website redesign")

check("a query of nothing but operators falls back to the original",
      strip_scope_operators("site:mtn.com"), "site:mtn.com")

check("build_query strips the model's scoping before adding ours",
      build_query("MTN CFO site mtn.com", ["mtn.com", "jse.co.za"]),
      "MTN CFO (site:mtn.com OR site:jse.co.za)")

# --- staging mirrors of allowed domains -------------------------------------
# `site:sashares.co.za` matches every subdomain, so dev.sashares.co.za was
# admitted and ranked FIRST for "MTN Group share price" — quoting R211.74 from
# 2026-06-05 while production carried R204.97 from 2026-07-31.
print("\nis_nonprod_host")

ALLOWED = ["mtn.com", "sashares.co.za", "jse.co.za", "itweb.co.za"]

check("the staging mirror that caused this is rejected",
      is_nonprod_host("dev.sashares.co.za", ALLOWED), True)

check("the production host is kept",
      is_nonprod_host("sashares.co.za", ALLOWED), False)

check("www is a production host",
      is_nonprod_host("www.mtn.com", ALLOWED), False)

check("a legitimate subdomain is kept (JSE SENS filings)",
      is_nonprod_host("senspdf.jse.co.za", ALLOWED), False)

check("staging under a different allowed domain",
      is_nonprod_host("staging.mtn.com", ALLOWED), True)

check("www in front of a staging label is still staging",
      is_nonprod_host("www.uat.mtn.com", ALLOWED), True)

check("a non-allowed host is not our business here",
      is_nonprod_host("dev.example.com", ALLOWED), False)

# The reason the check is anchored to the allow-list rather than to label
# shapes: judging by the leftmost label alone would reject this outright.
check("an allowed domain whose own name is a nonprod word is kept",
      is_nonprod_host("test.co.za", ["test.co.za"]), False)

check("a full URL is accepted as input",
      is_nonprod_host("https://dev.sashares.co.za/mtn", ALLOWED), True)

print()
if FAILED:
    print(f"{FAILED} check(s) failed")
    sys.exit(1)
print("all checks passed")
