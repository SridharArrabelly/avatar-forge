"""Offline check: the Teams package builder produces the right manifest.

Needs **no Azure resources and no credentials** — it builds real packages into a
temporary directory and reads the manifests back out. Runs in about a second.

Why it exists: which channel a package serves is decided entirely by flags
(``--bot-id``, ``--enable-calling``, ``--enable-companion``), and the differences
are invisible until Teams rejects the upload. One combination in particular is a
trap: a manifest carrying a ``bots`` entry for a bot that is not registered and
Teams-channel-enabled in the tenant fails to install, so channel B must be able
to build a package with no ``bots`` entry at all.

The naming half pins a defect the docs had already promised was fixed: the
builder read only ``TEAMS_APP_NAME``, so a deployment branded via
``AVATAR_DISPLAY_NAME`` — the single branding knob everywhere else — produced a
package called "Avatar" while the running app called itself something else.

What it pins:

* flag matrix -> staticTabs / bots / supportsCalling / configurableTabs
* name resolution order: ``--name`` > ``TEAMS_APP_NAME`` > ``AVATAR_DISPLAY_NAME``
* the derived full name and descriptions follow the resolved name
* hostname validation rejects a scheme, a path, a port and a non-hostname
* the archive is flat (Teams rejects nested folders)

Run from the repo root:

    uv run python scripts/test_build_package.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "teams" / "build_package.py"

_ENV_KEYS = (
    "TEAMS_HOSTNAME",
    "TEAMS_APP_NAME",
    "TEAMS_APP_FULL_NAME",
    "TEAMS_APP_VERSION",
    "TEAMS_APP_ID",
    "TEAMS_BOT_ID",
    "AVATAR_DISPLAY_NAME",
    "TEAMS_ENABLE_CALLING",
    "TEAMS_ENABLE_COMPANION",
)

HOST = ["--hostname", "example.azurecontainerapps.io"]
BOT = "11111111-2222-3333-4444-555555555555"

_failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        _failures.append(label)


def build(argv: list[str], env: dict[str, str] | None = None) -> tuple[dict, list[str]]:
    """Build a package with a clean environment; return (manifest, zip entries)."""
    saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
    try:
        os.environ.update(env or {})
        # Reload per call so argparse defaults are re-read from the environment.
        spec = importlib.util.spec_from_file_location("build_package", _SCRIPT)
        bp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bp)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pkg.zip"
            bp.BUILD_DIR = tmp
            bp.OUTPUT_ZIP = str(out)
            rc = bp.main(argv)
            if rc != 0:
                raise SystemExit(f"builder returned {rc}")
            with zipfile.ZipFile(out) as z:
                return json.loads(z.read("manifest.json")), z.namelist()
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def surfaces(manifest: dict) -> tuple[bool, bool, object, bool]:
    bots = manifest.get("bots") or []
    return (
        bool(manifest.get("staticTabs")),
        bool(bots),
        bots[0].get("supportsCalling") if bots else None,
        bool(manifest.get("configurableTabs")),
    )


def main() -> int:
    print("1. Flag matrix -> which channel's surfaces the package carries")
    cases = [
        ("B  tab only", HOST, {}, (True, False, None, False)),
        ("C  + chat bot", HOST + ["--bot-id", BOT], {}, (True, True, False, False)),
        ("D  + calling", HOST + ["--bot-id", BOT, "--enable-calling"], {}, (True, True, True, False)),
        ("D  + companion", HOST + ["--bot-id", BOT, "--enable-companion"], {}, (True, True, False, True)),
    ]
    for label, argv, env, want in cases:
        check(label, surfaces(build(argv, env)[0]), want)

    print("\n   env flags are equivalent to the CLI flags")
    m, _ = build(HOST + ["--bot-id", BOT], {"TEAMS_ENABLE_CALLING": "true"})
    check("TEAMS_ENABLE_CALLING=true", surfaces(m)[2], True)
    m, _ = build(HOST + ["--bot-id", BOT], {"TEAMS_ENABLE_COMPANION": "1"})
    check("TEAMS_ENABLE_COMPANION=1", surfaces(m)[3], True)

    print("\n2. Name resolution: --name > TEAMS_APP_NAME > AVATAR_DISPLAY_NAME")
    name_cases = [
        ("neither set -> default", HOST, {}, "Avatar"),
        ("AVATAR_DISPLAY_NAME only", HOST, {"AVATAR_DISPLAY_NAME": "Nuru"}, "Nuru"),
        ("TEAMS_APP_NAME only", HOST, {"TEAMS_APP_NAME": "Legacy"}, "Legacy"),
        ("both -> TEAMS_APP_NAME wins", HOST,
         {"TEAMS_APP_NAME": "Legacy", "AVATAR_DISPLAY_NAME": "Nuru"}, "Legacy"),
        ("--name beats both", HOST + ["--name", "Cli"],
         {"TEAMS_APP_NAME": "Legacy", "AVATAR_DISPLAY_NAME": "Nuru"}, "Cli"),
    ]
    for label, argv, env, want in name_cases:
        m, _ = build(argv, env)
        check(label, m["name"]["short"], want)

    print("\n   derived fields follow the resolved name")
    m, _ = build(HOST, {"AVATAR_DISPLAY_NAME": "Nuru"})
    check("full name derives", m["name"]["full"].startswith("Nuru"), True)
    check("short description derives", "Nuru" in m["description"]["short"], True)

    print("\n3. Hostname validation rejects what Teams cannot use in validDomains")
    for label, host in [
        ("scheme", "https://example.azurecontainerapps.io"),
        ("path", "example.azurecontainerapps.io/app"),
        ("port", "example.azurecontainerapps.io:443"),
        ("not a hostname", "not a host"),
        ("empty", ""),
    ]:
        try:
            build(["--hostname", host])
            check(f"rejects {label}", False, True)
        except SystemExit:
            check(f"rejects {label}", True, True)

    print("\n4. The archive is flat — Teams rejects nested folders")
    _m, entries = build(HOST)
    check("entries", sorted(entries), ["color.png", "manifest.json", "outline.png"])
    check("no nested paths", any("/" in e for e in entries), False)

    print("\n5. The app id is deterministic, so rebuilds install as an upgrade")
    a, _ = build(HOST)
    b, _ = build(HOST + ["--version", "2.0.0"])
    check("same hostname -> same id", a["id"], b["id"])
    c, _ = build(["--hostname", "other.azurecontainerapps.io"])
    check("different hostname -> different id", c["id"] != a["id"], True)

    print()
    if _failures:
        print(f"FAILED: {_failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
