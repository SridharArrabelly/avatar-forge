"""Offline documentation checks — no network, no Azure, no deploy.

Two failure modes that have both bitten this repo and that neither a build nor a
test suite would otherwise catch:

1. **Broken relative links.** Docs are heavily cross-linked and files get renamed
   (channels were renumbered D->C, E->D once already). A dead link is invisible
   until a reader hits it.

2. **Mermaid phantom nodes.** Deleting a node definition but leaving an edge that
   references it does not raise an error — mermaid silently renders an empty box,
   and even `mermaid.parse()` accepts it. The only way to notice is to render and
   look, or to check structurally as we do here.

Run:  uv run python scripts/test_docs.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)")
BLOCK = re.compile(r"```mermaid\n(.*?)```", re.S)
# A node definition is an id immediately followed by a shape bracket. It can
# appear anywhere on a line, including on both sides of an edge.
DEF = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s*[\[\(\{]")
SUBGRAPH = re.compile(r"^\s*subgraph\s+([A-Za-z][A-Za-z0-9_]*)")
EDGE = re.compile(
    r"([A-Za-z][A-Za-z0-9_]*)\s*(?:<-->|-\.->|-->|---|<--|-\.-)\s*"
    r'(?:\|[^|]*\|\s*)?(?:"[^"]*"\s*(?:-->|-\.->)?\s*)?([A-Za-z][A-Za-z0-9_]*)'
)
KEYWORDS = {
    "flowchart", "graph", "subgraph", "end", "direction", "classDef", "class",
    "style", "LR", "TB", "TD", "RL", "BT",
}

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


def main() -> int:
    files = _tracked_markdown()
    links = check_links(files)
    blocks = check_mermaid(files)

    print(f"checked {links} relative links and {blocks} mermaid blocks across {len(files)} files")
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
