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
package called "Avatar" while the running app called itself something else. It
now asks ``backend.avatar_identity`` for the same persona name every other
surface uses, so an unbranded deployment running the "Simone" avatar gets a
"Simone" package rather than an "Avatar" one.

What it pins:

* flag matrix -> staticTabs / bots / supportsCalling / configurableTabs
* name resolution order: ``--name`` > ``TEAMS_APP_NAME`` > resolved persona name
  (``AVATAR_DISPLAY_NAME``, else the active avatar model)
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
    "MEETING_BOT_APP_ID",
    "AVATAR_DISPLAY_NAME",
    "AVATAR_NAME",
    "CUSTOM_AVATAR_NAME",
    "PHOTO_AVATAR_NAME",
    "IS_CUSTOM_AVATAR",
    "IS_PHOTO_AVATAR",
    "TEAMS_ENABLE_CALLING",
    "TEAMS_ENABLE_COMPANION",
    # Scopes the output filename. Stripped like the rest so a developer's selected
    # azd environment cannot leak in and make the expected filename machine-dependent.
    "AZURE_ENV_NAME",
)

HOST = ["--hostname", "example.azurecontainerapps.io"]
BOT = "11111111-2222-3333-4444-555555555555"

_failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        _failures.append(label)


def build(argv: list[str], env: dict[str, str] | None = None,
          azd: dict[str, str] | None = None) -> tuple[dict, list[str]]:
    """Build a package with a clean environment; return (manifest, zip entries).

    ``azd`` pins what the builder sees as the selected azd environment. It
    defaults to empty so tests stay hermetic — without pinning, the builder would
    shell out to the developer's real azd environment and the expected names and
    hostnames would change from machine to machine.
    """
    saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
    try:
        os.environ.update(env or {})
        # Reload per call so argparse defaults are re-read from the environment.
        spec = importlib.util.spec_from_file_location("build_package", _SCRIPT)
        bp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bp)
        bp._AZD_VALUES = dict(azd or {})
        with tempfile.TemporaryDirectory() as tmp:
            bp.BUILD_DIR = tmp
            rc = bp.main(argv)
            if rc != 0:
                raise SystemExit(f"builder returned {rc}")
            # The filename is derived from the azd environment, so discover it
            # rather than reasserting the naming rule here — check_package_name
            # covers that separately.
            produced = sorted(Path(tmp).glob("*.zip"))
            if len(produced) != 1:
                raise SystemExit(f"expected exactly one zip in {tmp}, got {produced}")
            with zipfile.ZipFile(produced[0]) as z:
                return json.loads(z.read("manifest.json")), z.namelist()
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def load_builder():
    """Fresh module instance — argparse defaults are read at import time."""
    spec = importlib.util.spec_from_file_location("build_package", _SCRIPT)
    bp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bp)
    return bp


def built_filename(argv: list[str], env: dict[str, str] | None = None,
                   azd: dict[str, str] | None = None) -> str:
    """Name of the zip a real build writes — not just what the naming rule returns."""
    saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
    try:
        os.environ.update(env or {})
        bp = load_builder()
        bp._AZD_VALUES = dict(azd or {})
        with tempfile.TemporaryDirectory() as tmp:
            bp.BUILD_DIR = tmp
            if bp.main(argv) != 0:
                raise SystemExit("builder returned non-zero")
            produced = sorted(p.name for p in Path(tmp).glob("*.zip"))
            if len(produced) != 1:
                raise SystemExit(f"expected exactly one zip, got {produced}")
            return produced[0]
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
        ("   + bot, no calling", HOST + ["--bot-id", BOT], {}, (True, True, False, False)),
        ("D  + calling", HOST + ["--bot-id", BOT, "--enable-calling"], {}, (True, True, True, False)),
        ("D  + companion", HOST + ["--bot-id", BOT, "--enable-companion"], {}, (True, True, False, True)),
    ]
    for label, argv, env, want in cases:
        check(label, surfaces(build(argv, env)[0]), want)

    print("\n   bot id falls back to the azd env (channel C's calling bot)")
    # The bicep TEAMS_BOT_ID output is gone, so MEETING_BOT_APP_ID is the value a
    # channel C deploy actually leaves in the azd env. Without this fallback the
    # documented `build_package.py --enable-calling` produces a tab-only package
    # and silently drops the bot the operator just registered.
    m, _ = build(HOST, {"MEETING_BOT_APP_ID": BOT})
    check("MEETING_BOT_APP_ID -> bots entry", surfaces(m)[1], True)
    m, _ = build(HOST, {"TEAMS_BOT_ID": BOT})
    check("TEAMS_BOT_ID still honoured", surfaces(m)[1], True)
    m, _ = build(HOST + ["--bot-id", BOT], {"MEETING_BOT_APP_ID": "not-a-guid"})
    check("explicit --bot-id wins over env", m["bots"][0]["botId"], BOT)

    print("\n   env flags are equivalent to the CLI flags")
    m, _ = build(HOST + ["--bot-id", BOT], {"TEAMS_ENABLE_CALLING": "true"})
    check("TEAMS_ENABLE_CALLING=true", surfaces(m)[2], True)
    m, _ = build(HOST + ["--bot-id", BOT], {"TEAMS_ENABLE_COMPANION": "1"})
    check("TEAMS_ENABLE_COMPANION=1", surfaces(m)[3], True)

    print("\n2. Name resolution: --name > TEAMS_APP_NAME > resolved persona name")
    # The persona name itself is AVATAR_DISPLAY_NAME, else the active avatar
    # model's friendly name (backend/avatar_identity.py, pinned by
    # scripts/test_avatar_identity.py). What matters here is that the builder
    # asks for it instead of keeping its own fallback: a package must never be
    # called "Avatar" while the running app calls itself "Simone".
    name_cases = [
        ("neither set -> default avatar model's name", HOST, {}, "Lisa"),
        ("AVATAR_DISPLAY_NAME only", HOST, {"AVATAR_DISPLAY_NAME": "Nuru"}, "Nuru"),
        ("photo avatar, nothing branded", HOST,
         {"IS_PHOTO_AVATAR": "true", "PHOTO_AVATAR_NAME": "Simone"}, "Simone"),
        ("custom avatar, nothing branded", HOST,
         {"IS_CUSTOM_AVATAR": "true", "CUSTOM_AVATAR_NAME": "Nuru"}, "Nuru"),
        ("knob beats the avatar model", HOST,
         {"AVATAR_DISPLAY_NAME": "Ada", "IS_PHOTO_AVATAR": "true",
          "PHOTO_AVATAR_NAME": "Simone"}, "Ada"),
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

    print("\n6. The deployed azd environment supplies the baseline")
    # Regression: the builder used to read only os.environ, so a package built
    # against a deployment whose avatar is "Simone" was named "Lisa" (the
    # everything-unset default) and demanded a --hostname the step plan never
    # passed. Both inputs live in the azd environment, not the shell.
    DEPLOYED = {
        "SERVICE_APP_URI": "https://ca-deployed.example.azurecontainerapps.io",
        "IS_PHOTO_AVATAR": "true",
        "PHOTO_AVATAR_NAME": "Simone",
        "AVATAR_NAME": "Lisa-casual-sitting",  # inert: the photo gate is on
        "AVATAR_DISPLAY_NAME": "",
    }
    m, _ = build([], azd=DEPLOYED)
    check("no args -> host from SERVICE_APP_URI",
          m["validDomains"][0], "ca-deployed.example.azurecontainerapps.io")
    check("no args -> name from deployed avatar", m["name"]["short"], "Simone")

    m, _ = build([], azd={**DEPLOYED, "SERVICE_APP_URI": "https://Host.Example.COM:443/app"})
    check("scheme/port/path stripped from SERVICE_APP_URI",
          m["validDomains"][0], "host.example.com")

    print("\n   explicit input still wins over the deployed environment")
    m, _ = build(HOST, azd=DEPLOYED)
    check("--hostname wins", m["validDomains"][0], "example.azurecontainerapps.io")
    m, _ = build([], {"AVATAR_DISPLAY_NAME": "Nuru"}, azd=DEPLOYED)
    check("process env wins over azd", m["name"]["short"], "Nuru")
    m, _ = build(["--name", "Explicit"], azd=DEPLOYED)
    check("--name wins", m["name"]["short"], "Explicit")

    print("\n   no azd environment -> unchanged behaviour")
    try:
        build([])
        check("bare build without azd still errors", False, True)
    except SystemExit:
        check("bare build without azd still errors", True, True)

    print("\n7. The package filename is scoped to the azd environment")
    # A package is not a neutral artefact: the manifest bakes in the deployment's
    # hostname and the app id is a uuid5 OF that hostname, so two environments are
    # two different Teams apps. Under one fixed filename a rebuild after
    # `azd env select` silently overwrote the other environment's package, and
    # nothing on disk said which deployment a zip pointed at.
    bp = load_builder()
    name_rules = [
        ("env name -> suffixed", {"AZURE_ENV_NAME": "avatar-agent-mode"},
         "avatar-forge-teams-avatar-agent-mode.zip"),
        ("no env name -> bare stem", {}, "avatar-forge-teams.zip"),
        ("blank env name -> bare stem", {"AZURE_ENV_NAME": "   "},
         "avatar-forge-teams.zip"),
        ("path separators cannot escape the build dir",
         {"AZURE_ENV_NAME": "../../etc/passwd"}, "avatar-forge-teams-etc-passwd.zip"),
        ("spaces and unsafe chars collapse", {"AZURE_ENV_NAME": "my env (2)!"},
         "avatar-forge-teams-my-env-2.zip"),
    ]
    for label, values, want in name_rules:
        check(label, bp._package_filename(values), want)

    DEPLOY = {"SERVICE_APP_URI": "https://x.example.com"}
    check("a real build uses the derived name",
          built_filename([], azd={**DEPLOY, "AZURE_ENV_NAME": "avatar-model-mode"}),
          "avatar-forge-teams-avatar-model-mode.zip")
    check("two environments cannot overwrite each other",
          built_filename([], azd={**DEPLOY, "AZURE_ENV_NAME": "one"})
          != built_filename([], azd={**DEPLOY, "AZURE_ENV_NAME": "two"}), True)
    check("no azd env -> documented fallback name",
          built_filename(HOST), "avatar-forge-teams.zip")

    print()
    if _failures:
        print(f"FAILED: {_failures}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
