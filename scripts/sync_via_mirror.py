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
offline until the lock changes again. Extras already in the venv (``cosmos``)
are kept.

A successful run also installs git hooks (skip with ``--no-hooks``), so it is a
one-time step per clone: git then runs this script after every checkout,
merge/pull and rebase that changes ``uv.lock`` or ``pyproject.toml``. The hook
first tries an offline ``uv sync --inexact`` (about 0.1 s when the venv already
matches) and goes to the mirror only when that cannot finish; with no mirror
configured it runs a normal ``uv sync``. In hook mode it never removes packages
and never fails the git command. ``SYNC_VIA_MIRROR_SKIP=1`` skips it for one
command; ``--uninstall-hooks`` removes it.

Nothing here is specific to one company or mirror: it uses whatever index pip is
configured with (Artifactory, Nexus, Azure Artifacts, devpi, ...). On a network
where PyPI is reachable none of this is needed; plain ``uv sync`` works.

The mirror is, in order: ``--index-url``, ``PIP_INDEX_URL``, then the
``index-url`` from pip's config files (the ones IT manages). It changes only the
local virtual environment; it never touches Azure or ``uv.lock``.

Usage (``--no-project`` stops ``uv run`` itself from syncing first)::

    uv run --no-project python scripts/sync_via_mirror.py
    uv run --no-project python scripts/sync_via_mirror.py --extra cosmos
    uv run --no-project python scripts/sync_via_mirror.py --uninstall-hooks
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]

# The installed git hooks call this script only while the checked-out copy
# carries this exact line, so branches from before (or after) a change to the
# hook interface skip quietly instead of failing on unknown arguments. Bump it
# when `--hook` changes incompatibly.
HOOK_PROTOCOL = 1
HOOK_MARKER = "# avatar-forge: sync_via_mirror hook"
HOOK_NAMES = ("post-checkout", "post-merge", "post-rewrite")
SKIP_ENV = "SYNC_VIA_MIRROR_SKIP"
WATCHED = "uv.lock pyproject.toml"


def pip_config_files(environ: dict[str, str] | None = None,
                     platform: str | None = None) -> list[Path]:
    """pip's config files from lowest to highest precedence (global, user, PIP_CONFIG_FILE)."""
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    if env.get("PIP_CONFIG_FILE") in ("nul", "NUL", "/dev/null", os.devnull):
        return []  # pip's documented switch for "load no config files"
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


def project_venv(environ: dict[str, str] | None = None) -> Path:
    """The venv uv sync uses: ``UV_PROJECT_ENVIRONMENT`` (relative to the repo) or ``.venv``."""
    env = os.environ if environ is None else environ
    venv = Path(env.get("UV_PROJECT_ENVIRONMENT") or ROOT / ".venv")
    return venv if venv.is_absolute() else ROOT / venv


def normalize(name: str) -> str:
    """PEP 503 name normalization, so ``azure_cosmos`` and ``Azure.Cosmos`` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_distributions(venv: Path) -> set[str]:
    """Normalized names of every distribution in the venv's site-packages."""
    names: set[str] = set()
    for site in (venv / "Lib" / "site-packages", *venv.glob("lib/python*/site-packages")):
        if site.is_dir():
            for info in site.glob("*.dist-info"):
                names.add(normalize(info.name.removesuffix(".dist-info").rsplit("-", 1)[0]))
    return names


_REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def installed_extras(pyproject: dict, installed: set[str]) -> list[str]:
    """Optional extras whose every unconditional requirement is already installed.

    A plain export would leave them out, and the exact ``uv pip sync`` would then
    uninstall them, so they are carried into the selection instead.
    """
    found = []
    for extra, requirements in pyproject.get("project", {}).get("optional-dependencies", {}).items():
        names = {normalize(m.group(1)) for r in requirements
                 if ";" not in r and (m := _REQUIREMENT_NAME.match(r))}
        if names and names <= installed:
            found.append(extra)
    return sorted(found)


def detect_extras(venv: Path) -> list[str]:
    try:
        import tomllib
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError):
        return []
    return installed_extras(pyproject, installed_distributions(venv))


def plan(uv: str, mirror: str, requirements: Path, python: Path,
         selection: list[str], exact: bool = True) -> list[list[str]]:
    """The commands, in order. Only the two ``uv pip`` steps ever see the mirror.

    ``exact=False`` (hook mode) installs what is missing or at the wrong version
    and removes nothing, so packages you added by hand survive a pull.
    """
    install = ([uv, "pip", "sync", str(requirements)] if exact
               else [uv, "pip", "install", "--requirement", str(requirements), "--no-deps"])
    return [
        [uv, "export", "--locked", "--no-emit-project", "--format", "requirements-txt",
         "--quiet", "--output-file", str(requirements), *selection],
        [*install, "--python", str(python), "--index-url", mirror],
        [uv, "pip", "install", "--editable", str(ROOT), "--no-deps",
         "--python", str(python), "--index-url", mirror],
        [uv, "sync", "--locked", "--offline", *([] if exact else ["--inexact"]), *selection],
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


def lock_digest() -> str:
    return hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()


def sync(uv: str, mirror: str, venv: Path, selection: list[str], *,
         exact: bool = True, run=subprocess.run) -> int:
    """Run the plan into ``venv``. 0 on success; never leaves ``uv.lock`` changed unnoticed."""
    python = venv_python(venv)
    if not python.is_file():
        print(f"creating {venv}")
        if run([uv, "venv", str(venv)], cwd=ROOT).returncode:
            return 1
    before = lock_digest()
    with tempfile.TemporaryDirectory(prefix="sync-via-mirror-") as tmp:
        steps = plan(uv, mirror, Path(tmp) / "requirements.txt", python, selection, exact)
        for i, cmd in enumerate(steps):
            print("\n$ uv " + " ".join(redact(c) for c in cmd[1:]), flush=True)
            if run(cmd, cwd=ROOT).returncode:
                print(f"\nFAILED at step {i + 1} of {len(steps)}. {HINTS.get(i, '')}".rstrip(),
                      file=sys.stderr)
                return 1
    if lock_digest() != before:
        print("uv.lock changed, which this script must never do. Restore it with "
              "`git checkout uv.lock` and report this.", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------- git hooks

def hook_should_run(name: str, args: list[str]) -> bool:
    """Whether this git hook invocation can have changed the checked-out tree."""
    if name == "post-checkout":  # prev-HEAD new-HEAD flag; flag 0 = a file checkout
        return len(args) >= 3 and args[2] == "1" and args[0] != args[1]
    if name == "post-rewrite":  # "amend" leaves the working tree alone
        return args[:1] == ["rebase"]
    return name == "post-merge"


def fast_path(uv: str, selection: list[str]) -> list[str]:
    """Offline, and ``--inexact`` so it never removes what the lock does not list."""
    return [uv, "sync", "--locked", "--offline", "--inexact", "--quiet", *selection]


def direct_sync(uv: str, selection: list[str]) -> list[str]:
    """No mirror configured: PyPI should be reachable, so let uv fetch as usual."""
    return [uv, "sync", "--locked", "--inexact", "--quiet", *selection]


NO_MIRROR_HINT = ("If PyPI is blocked on this network, give pip your mirror once "
                  "(`pip config set global.index-url <mirror>/simple/` or PIP_INDEX_URL) "
                  "and the hooks will use it.")


def run_hook(name: str, args: list[str], environ: dict[str, str] | None = None, *,
             run=subprocess.run, uv: str | None = None,
             mirror_files: list[Path] | None = None) -> int:
    """Bring the venv back in line with ``uv.lock`` after git moved HEAD. Always 0."""
    env = os.environ if environ is None else environ
    if env.get(SKIP_ENV) or not hook_should_run(name, args):
        return 0
    tag = f"sync_via_mirror ({name} hook)"
    try:
        uv = uv or shutil.which("uv")
        if not uv:
            return 0
        venv = project_venv(env)
        selection = selection_args(detect_extras(venv), False, False)
        if run(fast_path(uv, selection), cwd=ROOT, capture_output=True, text=True).returncode == 0:
            return 0
        mirror, source = find_mirror(None, env, mirror_files)
        if not mirror:
            print(f"{tag}: the venv no longer matches uv.lock; running uv sync.", flush=True)
            if run(direct_sync(uv, selection), cwd=ROOT).returncode:
                print(f"{tag}: uv sync failed. {NO_MIRROR_HINT}", file=sys.stderr)
            return 0
        print(f"{tag}: the venv no longer matches uv.lock; installing the locked versions "
              f"through {redact(mirror)} (from {source}). {SKIP_ENV}=1 skips this.", flush=True)
        if sync(uv, mirror, venv, selection, exact=False, run=run) == 0:
            print(f"{tag}: done. The venv matches uv.lock.")
    except Exception as exc:  # a hook must never break or clutter the git command
        print(f"{tag}: skipped ({type(exc).__name__}: {exc})", file=sys.stderr)
    return 0


def shim(name: str) -> str:
    """The hook file. POSIX sh (git runs hooks with sh on Windows too), LF endings."""
    lines = [
        "#!/bin/sh",
        f"{HOOK_MARKER} (protocol {HOOK_PROTOCOL}).",
        "# Keeps .venv matching uv.lock through your pip mirror; never fails git.",
        "# Remove: uv run --no-project python scripts/sync_via_mirror.py --uninstall-hooks",
    ]
    if name == "post-rewrite":
        lines.append("cat >/dev/null  # git sends the rewritten commits on stdin")
    lines.append(f'[ -n "${SKIP_ENV}" ] && exit 0')
    if name == "post-checkout":
        lines += ['[ "$3" = 1 ] && [ "$1" != "$2" ] || exit 0',
                  f'git diff --quiet "$1" "$2" -- {WATCHED} 2>/dev/null && exit 0']
    elif name == "post-rewrite":
        lines += ['[ "$1" = rebase ] || exit 0',
                  f"git diff --quiet ORIG_HEAD HEAD -- {WATCHED} 2>/dev/null && exit 0"]
    else:
        lines.append(f"git diff --quiet ORIG_HEAD HEAD -- {WATCHED} 2>/dev/null && exit 0")
    lines += [
        "[ -f scripts/sync_via_mirror.py ] || exit 0",
        f'grep -q "^HOOK_PROTOCOL = {HOOK_PROTOCOL}" scripts/sync_via_mirror.py || exit 0',
        "command -v uv >/dev/null 2>&1 || exit 0",
        f'uv run --no-project --quiet python scripts/sync_via_mirror.py --hook {name} "$@" '
        "</dev/null || true",
        "exit 0",
    ]
    return "\n".join(lines) + "\n"


def hooks_dir() -> tuple[Path, Path]:
    """(where git looks for hooks, the clone's git dir). Honours ``core.hooksPath``;
    the hooks directory is shared by every worktree of the clone."""
    out = subprocess.run(["git", "rev-parse", "--git-path", "hooks", "--git-common-dir"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.splitlines()
    hooks, common = (Path(p) if Path(p).is_absolute() else ROOT / p for p in out[:2])
    return hooks, common


def private_hooks_dir(hooks: Path, common: Path) -> bool:
    """Whether the hooks directory belongs to this clone alone.

    A ``core.hooksPath`` elsewhere is either shared with other repositories (a
    global hooks folder) or tracked in the repo (husky); a sync must not write
    there unasked.
    """
    return hooks.resolve().is_relative_to(common.resolve())


def install_hooks(directory: Path) -> tuple[list[Path], list[Path]]:
    """Write the shims: (written or updated, not-ours). Idempotent; never overwrites another hook."""
    directory.mkdir(parents=True, exist_ok=True)
    written, foreign = [], []
    for name in HOOK_NAMES:
        path = directory / name
        body = shim(name).encode()
        if path.exists():
            current = path.read_bytes()
            if HOOK_MARKER.encode() not in current:
                foreign.append(path)
                continue
            if current == body:
                continue
        path.write_bytes(body)
        path.chmod(0o755)
        written.append(path)
    return written, foreign


def uninstall_hooks(directory: Path) -> list[Path]:
    removed = []
    for name in HOOK_NAMES:
        path = directory / name
        if path.is_file() and HOOK_MARKER in path.read_text(encoding="utf-8", errors="replace"):
            path.unlink()
            removed.append(path)
    return removed


def manage_hooks(install: bool, auto: bool = False) -> int:
    """``auto`` is the install after a manual sync: silent when there is nothing new to say."""
    try:
        directory, common = hooks_dir()
    except (OSError, ValueError, subprocess.CalledProcessError):
        if not auto:
            print("Not a git checkout (or git is not on PATH); no hooks to manage.", file=sys.stderr)
        return 2
    if not install:
        removed = uninstall_hooks(directory)
        print("\n".join(f"removed {p}" for p in removed) or f"no sync_via_mirror hooks in {directory}")
        return 0
    if auto and not private_hooks_dir(directory, common):
        print(f"Not installing git hooks automatically: core.hooksPath points at {directory}, "
              "which is outside this clone's .git. Run with --install-hooks to install them there.")
        return 0
    written, foreign = install_hooks(directory)
    for path in written:
        print(f"installed git hook {path}")
    for path in foreign:
        print(f"left alone {path}: it is not ours. To chain it, add this line to it:\n"
              f'  uv run --no-project --quiet python scripts/sync_via_mirror.py --hook {path.name} "$@" '
              "</dev/null || true", file=sys.stderr)
    if written or not auto:
        print("From now on, a checkout, pull or rebase that changes uv.lock or pyproject.toml "
              "brings the venv back in line by itself (offline when it can, otherwise through the "
              f"mirror). This covers every worktree of this clone. {SKIP_ENV}=1 skips it for one "
              "command; --uninstall-hooks removes it.")
    return 1 if foreign else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Our lines must interleave with the uv subprocesses' output, not trail it.
    sys.stdout.reconfigure(line_buffering=True)
    if argv[:1] == ["--hook"]:
        return run_hook(argv[1] if len(argv) > 1 else "", argv[2:])

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--index-url", help="Mirror simple-index URL (default: pip's configured index-url).")
    ap.add_argument("--extra", action="append", default=[], metavar="NAME",
                    help="Include an optional extra, as with uv sync --extra (repeatable). "
                         "Extras already installed are kept either way.")
    ap.add_argument("--all-extras", action="store_true", help="Include every optional extra.")
    ap.add_argument("--no-dev", action="store_true", help="Skip the dev dependency group.")
    hooks = ap.add_mutually_exclusive_group()
    hooks.add_argument("--no-hooks", action="store_true",
                       help="Do not install the git hooks that repeat this after checkout, pull and rebase.")
    hooks.add_argument("--install-hooks", action="store_true",
                       help="Only install those git hooks (a sync installs them anyway).")
    hooks.add_argument("--uninstall-hooks", action="store_true", help="Remove those git hooks.")
    args = ap.parse_args(argv)
    if args.install_hooks or args.uninstall_hooks:
        return manage_hooks(args.install_hooks)

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

    venv = project_venv()
    extras = args.extra
    if not args.all_extras:
        kept = [e for e in detect_extras(venv) if e not in extras]
        if kept:
            print(f"keeping installed extras: {', '.join(kept)}")
        extras = [*extras, *kept]
    if sync(uv, mirror, venv, selection_args(extras, args.all_extras, args.no_dev)):
        return 1
    print("\nThe venv matches uv.lock and uv.lock is unchanged. "
          "`uv run` and `uv sync` now work without PyPI until the lock changes.")
    if not args.no_hooks:
        manage_hooks(True, auto=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
