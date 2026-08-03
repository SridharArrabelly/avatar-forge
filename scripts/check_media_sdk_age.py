"""Fail when the pinned Graph real-time-media SDK is too old to be supported.

Microsoft requires an application-hosted media bot to run "either the newest
available version of the NuGet package, or a version that isn't more than three
months old", and states that "older versions of the library are deprecated and
don't work after a few months":

    https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/requirements-considerations-application-hosted-media-bots

That makes this the rare failure that is **time-based, not code-based**. Nothing
in the repo changes, no test goes red, and the bot simply stops working in a
meeting one day. "It worked last week" is not evidence against it, so a dated
check is the only thing that can catch it before a live call does.

Exit codes:
    0  pin is inside the window
    1  pin is stale -> upgrade before deploying
    2  could not determine (no network, unexpected NuGet payload)

Usage:
    uv run python scripts/check_media_sdk_age.py
    uv run python scripts/check_media_sdk_age.py --max-age-days 60
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSPROJ = ROOT / "meeting-bot" / "MeetingBot.csproj"

# The package that carries the native media stack, and the one Microsoft's
# three-month rule is written about.
PACKAGE = "Microsoft.Graph.Communications.Calls.Media"

# Microsoft says "not more than three months old". 90 days is that rule; the
# default is deliberately not padded, because the padding is what expires.
DEFAULT_MAX_AGE_DAYS = 90

# The four Graph Communications packages are compiled against each other and
# must move together, so a version skew is its own defect.
ALIGNED_PACKAGES = (
    "Microsoft.Graph.Communications.Calls",
    "Microsoft.Graph.Communications.Calls.Media",
    "Microsoft.Graph.Communications.Client",
    "Microsoft.Graph.Communications.Common",
)


def pinned_versions(csproj_text: str) -> dict[str, str]:
    """Extract every PackageReference version from a csproj.

    Attribute order is not guaranteed by MSBuild, so match them independently
    rather than assuming Include always precedes Version.
    """
    found: dict[str, str] = {}
    for element in re.findall(r"<PackageReference\b[^>]*/?>", csproj_text):
        include = re.search(r'Include\s*=\s*"([^"]+)"', element)
        version = re.search(r'Version\s*=\s*"([^"]+)"', element)
        if include and version:
            found[include.group(1)] = version.group(1)
    return found


def published_at(package: str, version: str, timeout: int = 30) -> datetime:
    """Return the publish timestamp of one version, from the NuGet v3 index."""
    url = f"https://api.nuget.org/v3/registration5-gz-semver2/{package.lower()}/index.json"
    request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            import gzip

            raw = gzip.decompress(raw)
        index = json.loads(raw)

    for page in index.get("items", []):
        # Paged registrations inline their leaves only sometimes; fetch when not.
        leaves = page.get("items")
        if leaves is None:
            with urllib.request.urlopen(page["@id"], timeout=timeout) as sub:
                leaves = json.loads(sub.read()).get("items", [])
        for leaf in leaves:
            entry = leaf.get("catalogEntry", {})
            if entry.get("version") == version:
                return datetime.fromisoformat(
                    entry["published"].replace("Z", "+00:00")
                )

    raise LookupError(f"{package} {version} not found on NuGet")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    args = parser.parse_args()

    if not CSPROJ.exists():
        print(f"FAIL  {CSPROJ} not found")
        return 2

    pins = pinned_versions(CSPROJ.read_text(encoding="utf-8"))
    version = pins.get(PACKAGE)
    if not version:
        print(f"FAIL  no PackageReference for {PACKAGE} in {CSPROJ.name}")
        return 2

    # A skew here means the managed wrappers were compiled against different
    # builds of each other, which surfaces at runtime rather than at compile.
    aligned = {p: pins.get(p) for p in ALIGNED_PACKAGES if p in pins}
    if len(set(aligned.values())) > 1:
        print("FAIL  Graph Communications packages are not version-aligned:")
        for name, pinned in sorted(aligned.items()):
            print(f"        {name} = {pinned}")
        return 1

    try:
        released = published_at(PACKAGE, version)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"SKIP  could not reach NuGet ({exc}) - age not verified")
        return 2
    except (LookupError, KeyError, ValueError) as exc:
        print(f"SKIP  could not read NuGet metadata ({exc}) - age not verified")
        return 2

    age = (datetime.now(timezone.utc) - released).days
    print(f"      {PACKAGE}")
    print(f"      pinned  {version}")
    print(f"      published {released.date()}  ({age} days ago)")

    if age > args.max_age_days:
        print()
        print(f"FAIL  pin is {age} days old, limit is {args.max_age_days}.")
        print("      Microsoft deprecates older builds and they stop working in")
        print("      meetings with no code change. Bump all four Graph")
        print("      Communications packages together in meeting-bot/MeetingBot.csproj.")
        print("      https://www.nuget.org/packages/Microsoft.Graph.Communications.Calls.Media/")
        return 1

    print(f"OK    within the {args.max_age_days}-day support window")
    return 0


if __name__ == "__main__":
    sys.exit(main())
