#!/usr/bin/env python3
"""Build a sideloadable Microsoft Teams app package.

Substitutes the templated values in ``manifest.template.json`` and zips the
resulting ``manifest.json`` together with the two icons **at the zip root**
(Teams requires a flat archive — no nested folders).

Stdlib only — this is a pure-Python repo (uv) with no Node toolchain.

Usage:
    # After `azd up` — the hostname is read from the azd environment:
    uv run python teams/build_package.py

    # Or state it explicitly (no azd needed):
    uv run python teams/build_package.py --hostname my-app.azurecontainerapps.io
    # or via env (PowerShell):
    $env:TEAMS_HOSTNAME = "my-app.azurecontainerapps.io"
    uv run python teams/build_package.py

Inputs (precedence: CLI flag > process env / .env > selected azd environment):
    --hostname / TEAMS_HOSTNAME      Bare ACA hostname, no scheme/path/port. When
                                     omitted, falls back to the selected azd
                                     environment's SERVICE_APP_URI (scheme stripped),
                                     so the command above works unmodified straight
                                     after a deploy. An explicit value is still
                                     validated strictly — passing a URL is an error,
                                     because a scheme in validDomains breaks the
                                     manifest and silently failing to notice is worse
                                     than being told.
    --version  / TEAMS_APP_VERSION   Optional. Manifest version (default 1.0.0).
    --app-id   / TEAMS_APP_ID        Optional. Stable GUID. Defaults to a deterministic
                                     uuid5 derived from the hostname so rebuilds match.
    --bot-id   / TEAMS_BOT_ID        Optional. Azure Bot / Entra app GUID. Falls back to the
                                     azd env's MEETING_BOT_APP_ID (the channel C calling bot).
                                     When neither is set the build is tab-only (channel B) —
                                     the additive `bots` entry is dropped so the Tab package
                                     always builds.
    --name     / TEAMS_APP_NAME      Optional. Assistant persona / display name shown in Teams.
                                     Falls back to the app's resolved persona name — the
                                     AVATAR_DISPLAY_NAME knob, or, when that is unset, the
                                     friendly name of the active avatar model (a "Simone"
                                     avatar gives a "Simone" package). Last resort "Avatar".
                                     Those avatar variables are read from the azd environment
                                     as well, so a package built from a deployed environment is
                                     named to match what the avatar calls itself, without
                                     setting a second variable. The full name + description are
                                     derived from it. See backend/avatar_identity.py for the rule.

Output:
    teams/build/teams-<azd-env-name>.zip
    (falls back to teams/build/teams.zip when no azd environment is
    selected, e.g. an explicit --hostname build)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from collections.abc import Mapping
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "manifest.template.json")

# Repo root on sys.path so the package name comes from the SAME persona rule the
# running app uses (backend/avatar_identity.py) rather than a second copy of it.
sys.path.insert(0, os.path.dirname(HERE))

from backend.avatar_identity import resolve_avatar_display_name  # noqa: E402

# Brand assets live in the repo-root canonical folder (assets/brand) so the web
# app, Teams package, and meeting bot all derive from a single source of truth.
ICONS_DIR = os.path.join(os.path.dirname(HERE), "assets", "brand")
BUILD_DIR = os.path.join(HERE, "build")
PACKAGE_STEM = "teams"

# A fixed namespace so uuid5(hostname) is stable across machines/runs.
_APP_ID_NAMESPACE = uuid.UUID("6f6c1d2e-7a4b-5c8d-9e0f-1a2b3c4d5e6f")

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$", re.IGNORECASE)


_AZD_VALUES: dict[str, str] | None = None


def _azd_env_values() -> dict[str, str]:
    """Values from the selected azd environment, or ``{}`` when unavailable.

    azd is deliberately NOT a hard dependency: a missing azd, an unselected
    environment or a pre-deploy run all yield ``{}`` so the caller falls back to
    its normal behaviour. Returns ``{}`` rather than raising for the same reason —
    an unavailable optional convenience must not become a stack trace.
    """
    global _AZD_VALUES
    if _AZD_VALUES is not None:
        return _AZD_VALUES
    _AZD_VALUES = {}
    exe = shutil.which("azd") or shutil.which("azd.exe")
    if not exe:
        return _AZD_VALUES
    try:
        res = subprocess.run(
            [exe, "env", "get-values"], capture_output=True, text=True, check=False, timeout=120
        )
    except (OSError, subprocess.SubprocessError):
        return _AZD_VALUES
    if res.returncode != 0:
        return _AZD_VALUES
    for line in res.stdout.splitlines():
        key, sep, raw = line.partition("=")
        if sep:
            _AZD_VALUES[key.strip()] = raw.strip().strip('"')
    return _AZD_VALUES


def _effective_env() -> dict[str, str]:
    """azd environment values overlaid with any non-empty process env.

    Same precedence as ``scripts/preflight.py::_config()``. It matters here
    because the package's *branding* is derived from the avatar model variables,
    and those live in the azd environment — not in the shell. Reading only
    ``os.environ`` produced a package named "Lisa" (the unset-everything default)
    for a deployment that calls itself "Simone", which is the very persona
    mismatch ``backend/avatar_identity.py`` exists to prevent. An explicit shell
    or ``.env`` value still wins, so local overrides keep working.
    """
    values = dict(_azd_env_values())
    for key, val in os.environ.items():
        if val:
            values[key] = val
    return values


def _hostname_from_env(values: Mapping[str, str]) -> str:
    """Bare hostname derived from the deployed app's ``SERVICE_APP_URI``.

    A FALLBACK only, used when no hostname was supplied. It exists so the command
    the post-deploy step plan prints (``build_package.py`` with no arguments) does
    what its own description claims — read the host from your azd environment —
    instead of exiting with "hostname is required".
    """
    uri = (values.get("SERVICE_APP_URI") or "").strip()
    if not uri:
        return ""
    # validDomains needs a bare host; urlsplit also lowercases and drops any port.
    return urlsplit(uri if "://" in uri else f"https://{uri}").hostname or ""


def _normalize_hostname(raw: str) -> str:
    """Reject scheme/path/port; return a bare, validated hostname for validDomains."""
    host = (raw or "").strip()
    if not host:
        sys.exit(
            "error: hostname is required.\n"
            "  Deployed already? Select the environment (azd env select <name>) and re-run —\n"
            "  the host is read from SERVICE_APP_URI.\n"
            "  Otherwise pass it: --hostname my-app.azurecontainerapps.io (or set TEAMS_HOSTNAME)."
        )
    if "://" in host:
        sys.exit(f"error: hostname must not include a scheme: {host!r} (use the bare host, e.g. my-app.azurecontainerapps.io)")
    if "/" in host:
        sys.exit(f"error: hostname must not include a path/slash: {host!r}")
    if ":" in host:
        sys.exit(f"error: hostname must not include a port: {host!r} (Teams validDomains is a bare host)")
    if not _HOSTNAME_RE.match(host):
        sys.exit(f"error: {host!r} does not look like a valid DNS hostname")
    return host.lower()


def _resolve_app_id(raw: str | None, hostname: str) -> str:
    if raw:
        try:
            return str(uuid.UUID(raw))
        except ValueError:
            sys.exit(f"error: --app-id must be a valid GUID, got {raw!r}")
    return str(uuid.uuid5(_APP_ID_NAMESPACE, hostname))


def _resolve_bot_id(raw: str | None) -> str:
    """Validate the bot id (the Azure Bot / Entra app GUID) used in the manifest.

    Optional: when omitted, the build produces a **tab-only** package (the
    channel B behaviour) by dropping the ``bots`` entry — the bot is purely
    additive and must never gate the always-working Tab. When supplied it must
    be the Microsoft App ID (GUID) of the Azure Bot registration (issue #53).
    """
    bot = (raw or "").strip()
    if not bot:
        return ""
    try:
        return str(uuid.UUID(bot))
    except ValueError:
        sys.exit(f"error: --bot-id must be a valid GUID, got {bot!r}")


def _package_filename(values: Mapping[str, str]) -> str:
    """Zip name scoped to the azd environment it was built from.

    A package is not a neutral artefact: the manifest bakes in that deployment's
    hostname, and the app id is a uuid5 OF that hostname, so two environments
    produce two genuinely *different* Teams apps that can be installed side by
    side. Under one fixed filename, `azd env select` followed by a rebuild
    silently overwrote the previous environment's package, and nothing on disk
    said which deployment a given zip pointed at — so sideloading the wrong one
    aimed Teams at another environment's host with no visible clue.

    Falls back to the bare stem when no azd environment is selected, keeping the
    documented no-azd path (`--hostname ...`) exactly as it was.
    """
    raw = (values.get("AZURE_ENV_NAME") or "").strip()
    # Defensive: an env name reaches a filesystem path here, so allow only safe
    # characters and refuse to let it climb out of the build directory.
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")[:60]
    return f"{PACKAGE_STEM}-{slug}.zip" if slug else f"{PACKAGE_STEM}.zip"


def _json_inner(s: str) -> str:
    """JSON-escape a string for safe substitution inside a JSON string literal."""
    return json.dumps(s)[1:-1]


def _env_flag(name: str) -> bool:
    """Truthy-ish parse of an env var ("1"/"true"/"yes"/"on")."""
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _resolve_names(raw_name: str | None, raw_full: str | None) -> dict[str, str]:
    """Derive the manifest name/description fields from the persona name.

    The display name is the assistant's brand/persona (e.g. "Nuru"), kept
    deliberately separate from the avatar-model binding (CUSTOM_AVATAR_NAME).
    Enforces the Teams v1.17 length limits (short name 30, full name 100,
    short description 80, full description 4000).
    """
    name = (raw_name or "").strip() or "Avatar"
    full = (raw_full or "").strip() or f"{name} — Azure Voice Live Avatar"
    desc_short = f"Chat with {name}, a real-time voice avatar."
    desc_full = (
        f"{name} brings the Azure Voice Live avatar experience to Microsoft Teams. "
        "Ask questions in chat and get grounded answers with sources, or open the "
        "personal tab to talk with a real-time, lip-synced avatar. Microphone access "
        "is required for the live avatar conversation."
    )
    limits = {"name": (name, 30), "full name": (full, 100),
              "short description": (desc_short, 80), "full description": (desc_full, 4000)}
    for label, (value, cap) in limits.items():
        if not value:
            sys.exit(f"error: manifest {label} must not be empty")
        if len(value) > cap:
            sys.exit(f"error: manifest {label} exceeds {cap} chars ({len(value)}): {value!r}")
    return {
        "APP_NAME": name,
        "APP_FULL_NAME": full,
        "APP_DESC_SHORT": desc_short,
        "APP_DESC_FULL": desc_full,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Teams app package.")
    parser.add_argument("--hostname", default=os.getenv("TEAMS_HOSTNAME"))
    parser.add_argument("--version", default=os.getenv("TEAMS_APP_VERSION", "1.0.0"))
    parser.add_argument("--app-id", default=os.getenv("TEAMS_APP_ID"))
    parser.add_argument("--bot-id", default=None)
    parser.add_argument("--name", default=None)
    parser.add_argument("--full-name", default=os.getenv("TEAMS_APP_FULL_NAME"))
    parser.add_argument(
        "--enable-companion",
        action="store_true",
        default=_env_flag("TEAMS_ENABLE_COMPANION"),
        help="Include the optional channel C meeting control panel (configurableTabs). "
        "Off by default — the package is then identical to the tab-only build.",
    )
    parser.add_argument(
        "--enable-calling",
        action="store_true",
        default=_env_flag("TEAMS_ENABLE_CALLING"),
        help="Mark the bot as a Teams calling bot (supportsCalling=true) for the "
        "channel C (#27) in-call media bot. Off by default. Requires a --bot-id and a "
        "tenant policy that allows calling bots in meetings.",
    )
    args = parser.parse_args(argv)

    # The deployed environment supplies the baseline, so a package built after
    # `azd up` inherits that deployment's host AND branding. Explicit input wins.
    env = _effective_env()

    supplied = (args.hostname or env.get("TEAMS_HOSTNAME") or "").strip()
    hostname_source = "--hostname/TEAMS_HOSTNAME" if supplied else "azd env SERVICE_APP_URI"
    hostname = _normalize_hostname(supplied or _hostname_from_env(env))
    app_id = _resolve_app_id(args.app_id, hostname)
    bot_id = _resolve_bot_id(
        args.bot_id or env.get("TEAMS_BOT_ID") or env.get("MEETING_BOT_APP_ID")
    )
    names = _resolve_names(
        args.name or env.get("TEAMS_APP_NAME") or resolve_avatar_display_name(env),
        args.full_name,
    )
    version = args.version.strip()

    with open(TEMPLATE, "r", encoding="utf-8") as f:
        manifest_text = f.read()

    manifest_text = (
        manifest_text
        .replace("{{HOSTNAME}}", hostname)
        .replace("{{VERSION}}", version)
        .replace("{{APP_ID}}", app_id)
        .replace("{{APP_NAME}}", _json_inner(names["APP_NAME"]))
        .replace("{{APP_FULL_NAME}}", _json_inner(names["APP_FULL_NAME"]))
        .replace("{{APP_DESC_SHORT}}", _json_inner(names["APP_DESC_SHORT"]))
        .replace("{{APP_DESC_FULL}}", _json_inner(names["APP_DESC_FULL"]))
        # When building tab-only (no bot id), substitute a throwaway GUID so the
        # template parses; the whole ``bots`` entry is dropped right after.
        .replace("{{BOT_ID}}", bot_id or "00000000-0000-0000-0000-000000000000")
    )

    # Fail fast if any placeholder slipped through or the result is not valid JSON.
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", manifest_text)
    if leftover:
        sys.exit(f"error: unsubstituted placeholders remain: {sorted(set(leftover))}")
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as e:
        sys.exit(f"error: rendered manifest is not valid JSON: {e}")

    # Tab-only build: drop the additive bot so the package matches channel B and
    # never gates the always-working Tab. The bot is opt-in via --bot-id.
    if not bot_id:
        manifest.pop("bots", None)

    # Channel C (#27): mark the bot as a calling bot so it can join meeting media.
    # Opt-in — default leaves supportsCalling=false.
    if bot_id and args.enable_calling:
        for bot in manifest.get("bots", []):
            bot["supportsCalling"] = True

    # The channel C meeting control panel (configurableTabs) is opt-in. When not
    # enabled the entry is dropped so the package is byte-for-byte the tab-only
    # shape — the optional Companion never gates the always-working Tab/bot.
    if not args.enable_companion:
        manifest.pop("configurableTabs", None)

    # Defensive: validDomains entries must stay scheme/path free.
    for d in manifest.get("validDomains", []):
        if "://" in d or "/" in d:
            sys.exit(f"error: validDomains entry must be a bare host: {d!r}")

    color = os.path.join(ICONS_DIR, "color.png")
    outline = os.path.join(ICONS_DIR, "outline.png")
    for p in (color, outline):
        if not os.path.isfile(p):
            sys.exit(f"error: missing icon {p}")

    os.makedirs(BUILD_DIR, exist_ok=True)
    output_zip = os.path.join(BUILD_DIR, _package_filename(env))
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        # Names are written at the archive root (no folder prefixes).
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.write(color, "color.png")
        zf.write(outline, "outline.png")

    print(f"Built {output_zip}")
    print(f"  name:     {names['APP_NAME']}")
    print(f"  env:      {env.get('AZURE_ENV_NAME') or '(no azd environment selected)'}")
    print(f"  hostname: {hostname}  (from {hostname_source})")
    print(f"  version:  {version}")
    print(f"  app id:   {app_id}")
    print(f"  bot id:   {bot_id or '(none — tab-only package)'}")
    print(f"  companion: {'included (meeting control panel)' if args.enable_companion else '(not included)'}")
    if not bot_id:
        calling = "(no bot in this package)"
    elif args.enable_calling:
        calling = "enabled (supportsCalling=true)"
    else:
        calling = "(not enabled)"
    print(f"  calling:   {calling}")
    print("Sideload it in Teams via: Apps -> Manage your apps -> Upload an app -> Upload a custom app")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
