"""Offline documentation checks — no network, no Azure, no deploy.

Four failure modes that have all bitten this repo and that neither a build nor a
test suite would otherwise catch:

1. **Broken relative links.** Docs are heavily cross-linked and files get renamed
   (channels were renumbered D->C, E->D once already). A dead link is invisible
   until a reader hits it.

2. **Mermaid phantom nodes.** Deleting a node definition but leaving an edge that
   references it does not raise an error — mermaid silently renders an empty box,
   and even `mermaid.parse()` accepts it. The only way to notice is to render and
   look, or to check structurally as we do here.

3. **Region lists drifting from the code.** The supported-region sets were stated
   in three separate docs, and one of them silently grew a region the code never
   had (`South Central US`), so the docs contradicted `preflight.py` — which is
   what actually gates a deploy. Prose cannot be trusted to stay in sync with a
   constant; it has to be pinned. Any doc that names regions is now checked
   against `preflight.py`, the authoritative copy.

4. **Counts quoted for MTN's trusted-site list drifting from the list itself.** Same
   one-fact-many-copies shape as (3): the list grew from 7 entries to 17 while three
   separate sentences went on saying 7. The list is the `azd env set TRUSTED_WEB_SITES`
   example in docs/configuration.md, so that line is the source of truth, parsed the
   way the app parses it, and the prose is checked against it.

Run:  uv run python tests/test_docs.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from preflight import AVATAR_REGIONS, VOICELIVE_REGIONS  # noqa: E402

from backend import trusted_sites  # noqa: E402

LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)")
BLOCK = re.compile(r"```mermaid\n(.*?)```", re.S)
# A node definition is an id immediately followed by a shape bracket. It can
# appear anywhere on a line, including on both sides of an edge.
DEF = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s*[\[\(\{]")
SUBGRAPH = re.compile(r"^\s*subgraph\s+([A-Za-z][A-Za-z0-9_]*)")
# Prose that states how many entries MTN's list has. Both phrasings the docs actually
# use are listed, and at least one match is REQUIRED (see check_bing_allowlist) so that
# rewording the prose fails loudly instead of silently disabling the check.
ALLOWLIST_COUNT = re.compile(
    r"\*{0,2}(\d+)\s+(?:path-scoped[^.\n]{0,40}?entries|entries with boost levels)"
)
# Prose stating how many BARE HOSTS Web IQ reduces that list to. Same required-match
# rule as ALLOWLIST_COUNT: this number drifts the moment an entry is added on a host
# that is not already covered.
DERIVED_HOST_COUNT = re.compile(r"\*{0,2}(\d+)\s+(?:bare\s+)?hosts")
# MTN's list: the `azd env set` line in the code block after this marker.
MTN_SITES_DOC = "docs/configuration.md"
MTN_SITES = re.compile(
    r"<!-- mtn-trusted-sites\b[^>]*-->\s*```\w*\n\s*azd env set TRUSTED_WEB_SITES \"([^\"]*)\""
)
EDGE = re.compile(
    r"([A-Za-z][A-Za-z0-9_]*)\s*(?:<-->|-\.->|-->|---|<--|-\.-)\s*"
    r'(?:\|[^|]*\|\s*)?(?:"[^"]*"\s*(?:-->|-\.->)?\s*)?([A-Za-z][A-Za-z0-9_]*)'
)
KEYWORDS = {
    "flowchart", "graph", "subgraph", "end", "direction", "classDef", "class",
    "style", "LR", "TB", "TD", "RL", "BT",
}

# Azure regions a supported-regions list could plausibly gain by mistake. This is
# deliberately broader than what the docs name today, because the point is to catch a
# region being ADDED — which is exactly how `South Central US` appeared in
# development.md while the code never had it. Slug -> portal display name.
REGION_VOCABULARY = {
    "eastus": "East US",
    "eastus2": "East US 2",
    "westus": "West US",
    "westus2": "West US 2",
    "westus3": "West US 3",
    "centralus": "Central US",
    "southcentralus": "South Central US",
    "northcentralus": "North Central US",
    "westcentralus": "West Central US",
    "canadacentral": "Canada Central",
    "canadaeast": "Canada East",
    "brazilsouth": "Brazil South",
    "northeurope": "North Europe",
    "westeurope": "West Europe",
    "swedencentral": "Sweden Central",
    "uksouth": "UK South",
    "ukwest": "UK West",
    "francecentral": "France Central",
    "germanywestcentral": "Germany West Central",
    "norwayeast": "Norway East",
    "switzerlandnorth": "Switzerland North",
    "polandcentral": "Poland Central",
    "italynorth": "Italy North",
    "southafricanorth": "South Africa North",
    "uaenorth": "UAE North",
    "centralindia": "Central India",
    "southindia": "South India",
    "eastasia": "East Asia",
    "southeastasia": "Southeast Asia",
    "japaneast": "Japan East",
    "japanwest": "Japan West",
    "koreacentral": "Korea Central",
    "australiaeast": "Australia East",
    "australiasoutheast": "Australia Southeast",
}

# Regions the docs may name even though they support neither feature.
# `southafricanorth` is the worked example of a primary region that forces
# FOUNDRY_LOCATION to be split out to a supported one.
EXAMPLE_REGIONS = {"southafricanorth"}

failures: list[str] = []


def _working_markdown() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return sorted({path for path in out.stdout.split("\0") if path and (ROOT / path).is_file()})


def check_links(files: list[str]) -> int:
    checked = 0
    for rel in files:
        path = ROOT / rel
        for match in LINK.finditer(path.read_text(encoding="utf-8")):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            checked += 1
            if not (path.parent / target).resolve().exists():
                failures.append(f"broken link: {rel} -> {target}")
    return checked


def check_mermaid(files: list[str]) -> int:
    blocks = 0
    for rel in files:
        path = ROOT / rel
        for index, match in enumerate(BLOCK.finditer(path.read_text(encoding="utf-8")), start=1):
            body = match.group(1)
            blocks += 1
            defined: set[str] = set()
            used: set[str] = set()
            for line in body.splitlines():
                for found in DEF.finditer(line):
                    if found.group(1) not in KEYWORDS:
                        defined.add(found.group(1))
                subgraph = SUBGRAPH.match(line)
                if subgraph:
                    defined.add(subgraph.group(1))
            for line in body.splitlines():
                if line.strip().startswith(("subgraph", "%%")):
                    continue
                for edge in EDGE.finditer(line):
                    used.update(node for node in edge.groups() if node not in KEYWORDS)
            for phantom in sorted(used - defined):
                failures.append(
                    f"phantom mermaid node: {rel} block {index}: "
                    f"'{phantom}' is used in an edge but never defined"
                )
    return blocks


def check_regions(files: list[str]) -> int:
    """Every Azure region a doc names must be one the code actually supports.

    `preflight.py` is the authoritative copy — it is what gates a real deploy — so
    the docs are checked against it rather than against each other.
    """
    allowed = VOICELIVE_REGIONS | AVATAR_REGIONS | EXAMPLE_REGIONS
    # Longest display name first, blanking each match, so `Central US` cannot also
    # match inside `South Central US` and report two regions for one mention.
    ordered = sorted(REGION_VOCABULARY.items(), key=lambda item: -len(item[1]))
    mentions = 0
    for rel in files:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for slug, display in ordered:
            # `\b` already stops `eastus` matching inside `eastus2`. The display
            # names need the lookahead so `West US` does not match `West US 2`.
            slug_pattern = rf"\b{slug}\b"
            display_pattern = rf"\b{re.escape(display)}\b(?!\s*\d)"
            if not (re.search(slug_pattern, text) or re.search(display_pattern, text)):
                continue
            text = re.sub(display_pattern, "", text)
            mentions += 1
            if slug not in allowed:
                failures.append(
                    f"unsupported region in docs: {rel} names '{display}' ({slug}), "
                    f"which is in neither VOICELIVE_REGIONS nor AVATAR_REGIONS in "
                    f"scripts/preflight.py. Add it there, or to EXAMPLE_REGIONS if it "
                    f"is deliberately an unsupported example."
                )
    return mentions


def check_bing_allowlist(files: list[str]) -> int:
    """Any count the docs quote for MTN's trusted-site list must match the list.

    Guards two numbers, both functions of the same list: the entries Bing deploys, and
    the bare hosts Web IQ reduces them to. The list is the documented
    `azd env set TRUSTED_WEB_SITES` example, parsed by backend/trusted_sites.py, which
    tests/test_trusted_web_sites.py pins to main.bicep's parser.

    This guard exists because the list grew from 7 entries to 17 while three separate
    sentences went on saying 7 — the same one-fact-many-copies drift that put a region
    in the docs the code never supported.
    """
    match = MTN_SITES.search((ROOT / MTN_SITES_DOC).read_text(encoding="utf-8"))
    if not match:
        failures.append(
            f"could not find the `<!-- mtn-trusted-sites -->` example in {MTN_SITES_DOC} "
            "— update MTN_SITES in tests/test_docs.py"
        )
        return 0
    raw = match.group(1)
    sites = trusted_sites.entries(raw)
    hosts = trusted_sites.hosts(raw)
    unusable = [item.strip() for item in raw.split(",") if not item.strip() or item.strip().startswith("-")]
    if unusable or any(not trusted_sites.host(site) for site, _ in sites):
        failures.append(f"MTN's list in {MTN_SITES_DOC} has blank, skipped or malformed entries")

    mentions = 0
    for rel in files:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for found in ALLOWLIST_COUNT.finditer(text):
            mentions += 1
            claimed = int(found.group(1))
            if claimed != len(sites):
                failures.append(
                    f"stale allow-list count in {rel}: says {claimed} entries, but MTN's "
                    f"list in {MTN_SITES_DOC} has {len(sites)}"
                )

    if not mentions:
        failures.append(
            "no doc states the size of MTN's list any more. If the wording changed, "
            "update ALLOWLIST_COUNT in tests/test_docs.py — otherwise this check "
            "silently stops guarding anything."
        )

    host_mentions = 0
    for rel in files:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for found in DERIVED_HOST_COUNT.finditer(text):
            host_mentions += 1
            claimed = int(found.group(1))
            if claimed != len(hosts):
                failures.append(
                    f"stale derived-host count in {rel}: says {claimed} hosts, but MTN's "
                    f"list in {MTN_SITES_DOC} reduces to {len(hosts)} ({', '.join(hosts)})"
                )

    if not host_mentions:
        failures.append(
            "no doc states the derived Web IQ host count any more. If the wording "
            "changed, update DERIVED_HOST_COUNT in tests/test_docs.py — otherwise "
            "this check silently stops guarding anything."
        )

    return mentions + host_mentions


def main() -> int:
    files = _working_markdown()
    links = check_links(files)
    blocks = check_mermaid(files)
    regions = check_regions(files)
    allowlist = check_bing_allowlist(files)

    print(
        f"checked {links} relative links, {blocks} mermaid blocks, "
        f"{regions} region mentions and {allowlist} allow-list counts "
        f"across {len(files)} files"
    )
    if failures:
        print()
        for failure in failures:
            print(f"  FAIL  {failure}")
        print(f"\n{len(failures)} problem(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
