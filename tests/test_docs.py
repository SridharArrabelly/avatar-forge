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

4. **Counts quoted for the Bing allow-list drifting from `infra/main.bicep`.** Same
   one-fact-many-copies shape as (3): the list grew from 7 entries to 17 while three
   separate sentences went on saying 7. The bicep parameter is what actually deploys,
   so it is the source of truth and the prose is checked against it.

Run:  uv run python tests/test_docs.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from preflight import AVATAR_REGIONS, VOICELIVE_REGIONS  # noqa: E402

LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)")
BLOCK = re.compile(r"```mermaid\n(.*?)```", re.S)
# A node definition is an id immediately followed by a shape bracket. It can
# appear anywhere on a line, including on both sides of an edge.
DEF = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s*[\[\(\{]")
SUBGRAPH = re.compile(r"^\s*subgraph\s+([A-Za-z][A-Za-z0-9_]*)")
# Prose that states how many entries the Bing allow-list has. Both phrasings the docs
# actually use are listed, and at least one match is REQUIRED (see check_bing_allowlist)
# so that rewording the prose fails loudly instead of silently disabling the check.
ALLOWLIST_COUNT = re.compile(
    r"\*{0,2}(\d+)\s+(?:path-scoped[^.\n]{0,40}?entries|entries with boost levels)"
)
# Counts the `{ domain: ... }` rows inside the `bingAllowedDomains` array in main.bicep.
BICEP_DOMAIN = re.compile(r"^\s*\{\s*domain:", re.M)
# Prose stating how many BARE HOSTS the Web IQ allow-list derives to. Same required-match
# rule as ALLOWLIST_COUNT: this number is a function of bingAllowedDomains, so it drifts
# the moment an entry is added on a host that is not already covered.
DERIVED_HOST_COUNT = re.compile(r"\*{0,2}(\d+)\s+(?:bare\s+)?hosts")
# Pulls the URL out of each `domain: '...'` row, for the host derivation.
BICEP_DOMAIN_URL = re.compile(r"domain:\s*'([^']+)'")
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


def _tracked_markdown() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


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
    """Any count the docs quote for the Bing allow-list must match `infra/main.bicep`.

    Guards two numbers, both functions of the same array: the entry count agent mode
    deploys, and the bare-host count model mode's Web IQ list derives to.

    The bicep parameter is the thing that actually deploys, so it is the source of
    truth. This guard exists because the list grew from 7 entries to 17 while three
    separate sentences went on saying 7 — the same one-fact-many-copies drift that
    put a region in the docs the code never supported.
    """
    bicep = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
    array = re.search(
        r"param bingAllowedDomains array = \[(.*?)^\]", bicep, re.S | re.M
    )
    if not array:
        failures.append(
            "could not find `param bingAllowedDomains array = [...]` in "
            "infra/main.bicep — update the pattern in tests/test_docs.py"
        )
        return 0

    actual = len(BICEP_DOMAIN.findall(array.group(1)))
    mentions = 0
    for rel in files:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for match in ALLOWLIST_COUNT.finditer(text):
            mentions += 1
            claimed = int(match.group(1))
            if claimed != actual:
                failures.append(
                    f"stale allow-list count in {rel}: says {claimed} entries, but "
                    f"`bingAllowedDomains` in infra/main.bicep has {actual}"
                )

    if not mentions:
        failures.append(
            "no doc states the Bing allow-list size any more. If the wording changed, "
            "update ALLOWLIST_COUNT in tests/test_docs.py — otherwise this check "
            "silently stops guarding anything."
        )

    # Model mode's Web IQ list is derived from the same array (main.bicep strips each
    # entry to its bare host and de-duplicates), so the docs quote a second number that
    # can drift independently. Mirror the ARM expression exactly.
    hosts: list[str] = []
    for url in BICEP_DOMAIN_URL.findall(array.group(1)):
        host = url.replace("https://", "").replace("http://", "").split("/")[0]
        if host.startswith("www."):
            host = host[4:]
        if host not in hosts:
            hosts.append(host)

    host_mentions = 0
    for rel in files:
        text = (ROOT / rel).read_text(encoding="utf-8")
        for match in DERIVED_HOST_COUNT.finditer(text):
            host_mentions += 1
            claimed = int(match.group(1))
            if claimed != len(hosts):
                failures.append(
                    f"stale derived-host count in {rel}: says {claimed} hosts, but "
                    f"`bingAllowedDomains` in infra/main.bicep derives to "
                    f"{len(hosts)} ({', '.join(hosts)})"
                )

    if not host_mentions:
        failures.append(
            "no doc states the derived Web IQ host count any more. If the wording "
            "changed, update DERIVED_HOST_COUNT in tests/test_docs.py — otherwise "
            "this check silently stops guarding anything."
        )

    return mentions + host_mentions


def main() -> int:
    files = _tracked_markdown()
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
