"""Install ``uv.lock`` through a PyPI mirror, without rewriting ``uv.lock``.

For networks that block ``files.pythonhosted.org`` and allow only an approved
package mirror (the usual corporate setup, where IT points pip at the mirror in
``pip.ini``/``pip.conf``). There, a plain ``uv sync`` fails with a TLS
``HandshakeFailure`` or a connect error as soon as it needs a wheel it has not
cached, because:

* uv does not read pip's configuration, and
* ``uv.lock`` pins every artifact to its ``files.pythonhosted.org`` URL. uv has
  no way to fetch those through a mirror. Pointing uv's own index at the mirror
  (``uv sync --index-url``, ``UV_DEFAULT_INDEX``) re-resolves and **rewrites
  every URL in the lock**, which then breaks the image build for everyone else.

This script installs exactly what the lock pins, through the mirror:

1. ``uv export --locked`` — the lock as a hash-pinned requirements file. The
   lock is only read; if it is out of date with ``pyproject.toml`` this stops.
2. ``uv pip sync`` from the mirror — the same files, verified against the lock's
   hashes, so a mirror cannot substitute a different artifact.
3. ``uv pip install -e .`` — the project itself, with no dependencies.
4. ``uv sync --locked --offline`` — proof the venv now matches the lock.

Afterwards ``uv run`` and ``uv sync`` find nothing to install, so they work
offline until the lock changes again. Re-run this after a pull that changes
``uv.lock``.

The mirror is, in order: ``--index-url``, ``PIP_INDEX_URL``, then the
``index-url`` from pip's config files (the ones IT manages). It changes only the
local virtual environment; it never touches Azure or ``uv.lock``.

Usage (``--no-project`` stops ``uv run`` itself from syncing first)::

    uv run --no-project python scripts/sync_via_mirror.py
    uv run --no-project python scripts/sync_via_mirror.py --extra cosmos
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]


def pip_config_files(environ: dict[str, str] | None = None,
                     platform: str | None = None) -> list[Path]:
    """pip's config files from lowest to highest precedence (global, user, PIP_CONFIG_FILE)."""
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    files: list[Path] = []
    if plat == "win32":
        if env.get("PROGRAMDATA"):
            files.append(Path(env["PROGRAMDATA"]) / "pip" / "pip.ini")
        files.append(home / "pip" / "pip.ini")
        if env.get("APPDATA"):
            files.append(Path(env["APPDATA"]) / "pip" / "pip.ini")
    else:
        if plat == "darwin":
            files.append(Path("/Library/Application Support/pip/pip.conf"))
        for d in reversed(env.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":")):
            if d:
                files.append(Path(d) / "pip" / "pip.conf")
        files.append(Path("/etc/pip.conf"))
        files.append(home / ".pip" / "pip.conf")
        if plat == "darwin":
            files.append(home / "Library" / "Application Support" / "pip" / "pip.conf")
        files.append(Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "pip" / "pip.conf")
    if env.get("PIP_CONFIG_FILE"):
        files.append(Path(env["PIP_CONFIG_FILE"]))
    return files


def index_url_from_file(path: Path) -> str | None:
    """``index-url`` from one pip config file; ``[install]`` overrides ``[global]``."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error, UnicodeDecodeError):
        return None
    for section in ("install", "global"):
        if parser.has_section(section):
            for key in ("index-url", "index_url"):
                value = parser.get(section, key, fallback="").strip()
                if value:
                    return value
    return None


def find_mirror(cli_value: str | None, environ: dict[str, str] | None = None,
                files: list[Path] | None = None) -> tuple[str | None, str]:
    """The mirror URL and where it came from."""
    env = os.environ if environ is None else environ
    if cli_value:
        return cli_value, "--index-url"
    if env.get("PIP_INDEX_URL"):
        return env["PIP_INDEX_URL"], "PIP_INDEX_URL"
    for path in reversed(pip_config_files(env) if files is None else files):
        if path.is_file():
            value = index_url_from_file(path)
            if value:
                return value, str(path)
    return None, ""


def redact(url: str) -> str:
    """The URL with any ``user:token@`` removed, for printing."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc="***@" + parts.netloc.rsplit("@", 1)[1]))


def selection_args(extras: list[str], all_extras: bool, no_dev: bool) -> list[str]:
    """The dependency selection, passed identically to export and the final check."""
    args: list[str] = ["--all-extras"] if all_extras else [a for e in extras for a in ("--extra", e)]
    if no_dev:
        args.append("--no-dev")
    return args


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def plan(uv: str, mirror: str, requirements: Path, python: Path,
         selection: list[str]) -> list[list[str]]:
    """The commands, in order. Only the two ``uv pip`` steps ever see the mirror."""
    return [
        [uv, "export", "--locked", "--no-emit-project", "--format", "requirements-txt",
         "--quiet", "--output-file", str(requirements), *selection],
        [uv, "pip", "sync", str(requirements), "--python", str(python), "--index-url", mirror],
        [uv, "pip", "install", "--editable", str(ROOT), "--no-deps",
         "--python", str(python), "--index-url", mirror],
        [uv, "sync", "--locked", "--offline", *selection],
    ]


HINTS = {
    0: "uv.lock is out of date with pyproject.toml. Re-lock where PyPI is reachable "
       "(or in ACR), commit uv.lock, then run this again. Locking through the mirror "
       "would write mirror URLs into the lock.",
    1: "If a pinned version is missing, the mirror has not caught up with PyPI yet "
       "(new releases can lag by a day). Retry later, or pin that dependency to a "
       "version the mirror has and re-lock.",
    3: "The venv still differs from the lock. Run with the same --extra/--no-dev "
       "you use for uv sync.",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--index-url", help="Mirror simple-index URL (default: pip's configured index-url).")
    ap.add_argument("--extra", action="append", default=[], metavar="NAME",
                    help="Include an optional extra, as with uv sync --extra (repeatable).")
    ap.add_argument("--all-extras", action="store_true", help="Include every optional extra.")
    ap.add_argument("--no-dev", action="store_true", help="Skip the dev dependency group.")
    args = ap.parse_args(argv)
    # Our lines must interleave with the uv subprocesses' output, not trail it.
    sys.stdout.reconfigure(line_buffering=True)

    uv = shutil.which("uv")
    if not uv:
        print("uv is not on PATH. Install it first: https://docs.astral.sh/uv/", file=sys.stderr)
        return 2
    mirror, source = find_mirror(args.index_url)
    if not mirror:
        print("No mirror found (no --index-url, PIP_INDEX_URL or pip index-url). "
              "If PyPI is reachable, plain `uv sync` is all you need.", file=sys.stderr)
        return 2
    print(f"mirror: {redact(mirror)}  (from {source})")

    venv = Path(os.environ.get("UV_PROJECT_ENVIRONMENT") or ROOT / ".venv")
    if not venv.is_absolute():
        venv = ROOT / venv
    python = venv_python(venv)
    if not python.is_file():
        print(f"creating {venv}")
        if subprocess.run([uv, "venv", str(venv)], cwd=ROOT).returncode:
            return 1

    lock = ROOT / "uv.lock"
    before = hashlib.sha256(lock.read_bytes()).hexdigest()
    selection = selection_args(args.extra, args.all_extras, args.no_dev)
    with tempfile.TemporaryDirectory(prefix="sync-via-mirror-") as tmp:
        steps = plan(uv, mirror, Path(tmp) / "requirements.txt", python, selection)
        for i, cmd in enumerate(steps):
            print("\n$ uv " + " ".join(redact(c) for c in cmd[1:]), flush=True)
            if subprocess.run(cmd, cwd=ROOT).returncode:
                print(f"\nFAILED at step {i + 1} of {len(steps)}. {HINTS.get(i, '')}".rstrip(),
                      file=sys.stderr)
                return 1

    if hashlib.sha256(lock.read_bytes()).hexdigest() != before:
        print("uv.lock changed, which this script must never do. Restore it with "
              "`git checkout uv.lock` and report this.", file=sys.stderr)
        return 1
    print("\nThe venv matches uv.lock and uv.lock is unchanged. "
          "`uv run` and `uv sync` now work without PyPI until the lock changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
