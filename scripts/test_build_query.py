"""build_query: does site scoping render the operators Web IQ actually documents?

No Azure, no network. Guards the shape of the query string, including the
exclusion form that the previous implementation rendered as `site:-host`.
"""
import sys

sys.path.insert(0, ".")

from backend.voice.tools import build_query  # noqa: E402

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

print()
if FAILED:
    print(f"{FAILED} check(s) failed")
    sys.exit(1)
print("all checks passed")
