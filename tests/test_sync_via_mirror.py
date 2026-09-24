"""sync_via_mirror + the uv_build backend: can the venv be built without PyPI?

No Azure, no network, no uv invoked. Pins the two properties that keep a
mirror-only network working without breaking everyone else:

* the mirror reaches only ``uv pip`` (never ``uv export``/``uv sync``/``uv lock``),
  so ``uv.lock`` keeps its PyPI URLs and the image build keeps working;
* the project builds with uv's in-process backend, so rebuilding it after a
  ``pyproject.toml`` change downloads nothing.
"""
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sync_via_mirror as svm  # noqa: E402

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


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


print("mirror discovery")
with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    glob_ = write(t / "global" / "pip.ini", "[global]\nindex-url = https://global.example/simple/\n")
    user = write(t / "user" / "pip.ini", "[global]\nindex-url = https://user.example/simple/\n")
    empty = write(t / "empty" / "pip.ini", "[global]\ntimeout = 60\n")
    missing = t / "nope" / "pip.ini"

    check("the highest-precedence file wins (user over global)",
          svm.find_mirror(None, {}, [glob_, user])[0], "https://user.example/simple/")
    check("a file without index-url does not mask a lower one",
          svm.find_mirror(None, {}, [glob_, empty, missing])[0], "https://global.example/simple/")
    check("PIP_INDEX_URL beats every file",
          svm.find_mirror(None, {"PIP_INDEX_URL": "https://env.example/simple/"}, [glob_, user])[0],
          "https://env.example/simple/")
    check("--index-url beats PIP_INDEX_URL",
          svm.find_mirror("https://cli.example/simple/", {"PIP_INDEX_URL": "https://env.example/"}, [user])[0],
          "https://cli.example/simple/")
    check("no configuration means no mirror, not PyPI by default",
          svm.find_mirror(None, {}, [empty, missing]), (None, ""))

    both = write(t / "both" / "pip.ini",
                 "[global]\nindex-url = https://g.example/\n[install]\nindex_url = https://i.example/\n")
    check("[install] overrides [global], underscore spelling accepted",
          svm.index_url_from_file(both), "https://i.example/")
    check("a %-sign in a token is not treated as interpolation",
          svm.index_url_from_file(write(t / "pct" / "pip.ini", "[global]\nindex-url = https://u:a%2Fb@m.example/\n")),
          "https://u:a%2Fb@m.example/")

print("pip config locations")
win = svm.pip_config_files({"PROGRAMDATA": r"C:\ProgramData", "APPDATA": r"C:\Users\u\AppData\Roaming",
                            "USERPROFILE": r"C:\Users\u", "PIP_CONFIG_FILE": r"C:\x\pip.ini"}, "win32")
check("Windows: ProgramData first (IT-managed, lowest), PIP_CONFIG_FILE last",
      (win[0], win[-1]), (Path(r"C:\ProgramData") / "pip" / "pip.ini", Path(r"C:\x\pip.ini")))
check("Windows: %APPDATA% overrides the legacy %USERPROFILE% file",
      win.index(Path(r"C:\Users\u\AppData\Roaming") / "pip" / "pip.ini")
      > win.index(Path(r"C:\Users\u") / "pip" / "pip.ini"), True)
linux = svm.pip_config_files({"HOME": "/home/u"}, "linux")
check("Linux: /etc before the user's ~/.config file",
      linux.index(Path("/etc/pip.conf")) < linux.index(Path("/home/u/.config/pip/pip.conf")), True)
check("PIP_CONFIG_FILE=os.devnull loads no config files, as in pip",
      [svm.pip_config_files({"PIP_CONFIG_FILE": null, "PROGRAMDATA": r"C:\ProgramData"}, "win32")
       for null in ("nul", "/dev/null")], [[], []])

print("redaction")
check("credentials are never printed",
      svm.redact("https://user:s3cret@feed.example/simple/"), "https://***@feed.example/simple/")
check("a URL without credentials is unchanged",
      svm.redact("https://feed.example/simple/"), "https://feed.example/simple/")

print("the plan never lets the mirror near uv.lock")
MIRROR = "https://mirror.example/simple/"
sel = svm.selection_args(["cosmos"], False, True)
steps = svm.plan("uv", MIRROR, Path("req.txt"), Path("py"), sel)
verbs = [" ".join(s[1:3]) if s[1] == "pip" else s[1] for s in steps]
check("export, pip sync, pip install, sync - in that order", verbs,
      ["export", "pip sync", "pip install", "sync"])
check("only the two `uv pip` steps see the mirror",
      [MIRROR in s for s in steps], [False, True, True, False])
check("no step can re-lock or upgrade",
      [a for s in steps for a in s if a in ("lock", "--upgrade", "-U", "--upgrade-package", "--refresh")], [])
check("no index option reaches export or sync",
      [a for s in (steps[0], steps[3]) for a in s if "index" in a], [])
check("export refuses a stale lock instead of resolving",
      "--locked" in steps[0] and "--frozen" not in steps[0], True)
check("the final check is offline and locked",
      ("--locked" in steps[3], "--offline" in steps[3]), (True, True))
check("export and the final check select the same extras/groups",
      (steps[0][-len(sel):], steps[3][-len(sel):]), (sel, sel))
check("the project is installed editable with no dependencies",
      ("--editable" in steps[2], "--no-deps" in steps[2]), (True, True))
check("--all-extras replaces individual extras",
      svm.selection_args(["cosmos"], True, False), ["--all-extras"])

print("packaging builds in-process")
pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
build = pyproject["build-system"]
check("backend is uv_build", build["build-backend"], "uv_build")
req = build["requires"]
check("one requirement, uv_build bounded to a single minor",
      (len(req), req[0].startswith("uv_build>=") and ",<" in req[0]), (1, True))
lo, hi = req[0].removeprefix("uv_build>=").split(",<")
lo_v, hi_v = [int(x) for x in lo.split(".")[:2]], [int(x) for x in hi.split(".")[:2]]
check("the upper bound is the next minor", hi_v, [lo_v[0], lo_v[1] + 1])
bb = pyproject.get("tool", {}).get("uv", {}).get("build-backend", {})
check("the flat `backend` package is the module", (bb.get("module-name"), bb.get("module-root")), ("backend", ""))
check("no stale setuptools configuration", "setuptools" in pyproject.get("tool", {}), False)
check("the console script still points into backend",
      pyproject["project"]["scripts"].get("avatar-forge"), "backend.main:run")

dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
copy_backend = next(i for i, line in enumerate(dockerfile) if line.startswith("COPY backend/"))
project_sync = next(i for i, line in enumerate(dockerfile)
                    if line.startswith("RUN uv sync") and "--no-install-project" not in line)
check("the image copies backend/ before the sync that builds the project",
      copy_backend < project_sync, True)

print("extras already installed are kept")
PY = {"project": {"optional-dependencies": {
    "cosmos": ["azure-cosmos>=4.17.1"],
    "two": ["Foo.Bar[x]>=1", "baz_qux; python_version >= '3.0'"],
    "missing": ["not-installed>=1"],
    "empty": [],
}}}
check("names compare PEP 503-normalized; marker-guarded requirements are not required",
      svm.installed_extras(PY, {"azure-cosmos", "foo-bar"}), ["cosmos", "two"])
check("an extra with any unconditional requirement missing is not kept",
      svm.installed_extras(PY, {"foo-bar"}), ["two"])
check("no optional-dependencies table means no extras",
      svm.installed_extras({"project": {}}, {"azure-cosmos"}), [])
with tempfile.TemporaryDirectory() as tmp:
    venv = Path(tmp)
    site = venv / "Lib" / "site-packages"
    (site / "azure_cosmos-4.17.1.dist-info").mkdir(parents=True)
    (site / "Foo.Bar-2.0.dist-info").mkdir()
    (site / "not_a_dist").mkdir()
    posix = venv / "lib" / "python3.12" / "site-packages" / "httpx-0.28.1.dist-info"
    posix.mkdir(parents=True)
    check("site-packages names are read from dist-info, Windows and POSIX layouts",
          svm.installed_distributions(venv), {"azure-cosmos", "foo-bar", "httpx"})
check("a missing venv has no distributions", svm.installed_distributions(Path(tmp) / "gone"), set())

print("which git events run the hook")
A, B = "a" * 40, "b" * 40
for label, name, args, want in [
    ("branch checkout", "post-checkout", [A, B, "1"], True),
    ("new worktree (null previous HEAD)", "post-checkout", ["0" * 40, B, "1"], True),
    ("file checkout (flag 0)", "post-checkout", [A, B, "0"], False),
    ("checkout -b at the same commit", "post-checkout", [A, A, "1"], False),
    ("malformed post-checkout", "post-checkout", [A], False),
    ("merge / pull", "post-merge", ["0"], True),
    ("rebase / pull --rebase", "post-rewrite", ["rebase"], True),
    ("commit --amend", "post-rewrite", ["amend"], False),
    ("any other hook", "pre-commit", [], False),
]:
    check(f"{label}: {'runs' if want else 'skips'}", svm.hook_should_run(name, args), want)


class Runner:
    """Records commands instead of running them; returns the scripted exit codes in order."""

    def __init__(self, *codes: int):
        self.codes, self.calls = list(codes), []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        return type("Done", (), {"returncode": self.codes.pop(0) if self.codes else 0})()


def hook(runner, name="post-merge", args=("0",), env=None, files=()):
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        svm.venv_python(venv).parent.mkdir(parents=True)
        svm.venv_python(venv).write_bytes(b"")
        (venv / "Lib" / "site-packages" / "azure_cosmos-4.17.1.dist-info").mkdir(parents=True)
        environ = {"UV_PROJECT_ENVIRONMENT": str(venv), **(env or {})}
        return svm.run_hook(name, list(args), environ, run=runner, uv="uv", mirror_files=list(files))


print("hook: offline first, mirror only when needed")
r = Runner(0)
check("hook always returns 0", hook(r), 0)
check("venv already matches: one offline call, nothing else", len(r.calls), 1)
fast = r.calls[0]
check("the fast path is locked, offline, inexact, and keeps the installed extra",
      all(a in fast for a in ("--locked", "--offline", "--inexact")) and fast[-2:] == ["--extra", "cosmos"],
      True)
check("the fast path never sees an index", [a for a in fast if "index" in a], [])

r = Runner(1)
check("fast path fails + mirror: hook still returns 0",
      hook(r, env={"PIP_INDEX_URL": MIRROR}), 0)
verbs = [" ".join(c[1:3]) if c[1] == "pip" else c[1] for c in r.calls]
check("then export, pip install, editable install, sync", verbs,
      ["sync", "export", "pip install", "pip install", "sync"])
check("hook mode installs without removing anything (no `uv pip sync`)",
      ["sync" in c[1:3] and c[1] == "pip" for c in r.calls], [False] * 5)
check("only the two `uv pip` steps see the mirror", [MIRROR in c for c in r.calls],
      [False, False, True, True, False])
check("the closing check is inexact too", "--inexact" in r.calls[-1], True)
check("the extra reaches the export as well", r.calls[1][-2:], ["--extra", "cosmos"])

r = Runner(1, 0)
hook(r)
check("fast path fails + no mirror: a normal online uv sync, not an error",
      [(c[1], "--offline" in c, "--inexact" in c) for c in r.calls],
      [("sync", True, True), ("sync", False, True)])
check("with no mirror nothing sees an index", [a for c in r.calls for a in c if "index" in a], [])

for label, kwargs in [("SYNC_VIA_MIRROR_SKIP=1", {"env": {svm.SKIP_ENV: "1"}}),
                      ("a file checkout", {"name": "post-checkout", "args": (A, B, "0")}),
                      ("commit --amend", {"name": "post-rewrite", "args": ("amend",)})]:
    r = Runner()
    hook(r, **kwargs)
    check(f"{label}: nothing runs", r.calls, [])


def boom(cmd, **kwargs):
    raise OSError("uv vanished")


check("an unexpected error never reaches git", hook(boom), 0)

print("hook installation")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp) / "hooks"
    written, foreign = svm.install_hooks(d)
    check("installs post-checkout, post-merge and post-rewrite",
          sorted(p.name for p in written), sorted(svm.HOOK_NAMES))
    bodies = {n: (d / n).read_bytes() for n in svm.HOOK_NAMES}
    check("LF endings only (sh on Windows chokes on CRLF)",
          [b"\r" in b for b in bodies.values()], [False] * 3)
    check("each starts with a shebang and ends with exit 0",
          [b.startswith(b"#!/bin/sh\n") and b.endswith(b"exit 0\n") for b in bodies.values()], [True] * 3)
    check("each passes git's arguments to --hook with its own name, stdin closed, errors swallowed",
          [f'--hook {n} "$@" </dev/null || true'.encode() in b for n, b in bodies.items()], [True] * 3)
    check("only post-rewrite reads stdin (git sends it the rewritten commits)",
          [b"cat >/dev/null" in bodies[n] for n in svm.HOOK_NAMES], [False, False, True])
    check("each honours the skip variable and skips when uv.lock/pyproject.toml did not change",
          [f"${svm.SKIP_ENV}".encode() in b and b"-- uv.lock pyproject.toml" in b for b in bodies.values()],
          [True] * 3)
    check("installing again changes nothing",
          (svm.install_hooks(d), {n: (d / n).read_bytes() for n in svm.HOOK_NAMES}),
          (([], []), bodies))

    (d / "post-merge").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
    written, foreign = svm.install_hooks(d)
    check("someone else's hook is reported and left alone",
          ([p.name for p in foreign], (d / "post-merge").read_text(encoding="utf-8")),
          (["post-merge"], "#!/bin/sh\necho mine\n"))
    removed = svm.uninstall_hooks(d)
    check("uninstall removes only ours",
          (sorted(p.name for p in removed), sorted(p.name for p in d.iterdir())),
          (["post-checkout", "post-rewrite"], ["post-merge"]))

    git = Path(tmp) / "repo" / ".git"
    check("the clone's own .git/hooks may be written automatically",
          svm.private_hooks_dir(git / "hooks", git), True)
    check("a core.hooksPath in the working tree (husky) may not",
          svm.private_hooks_dir(git.parent / ".husky", git), False)
    check("nor a global core.hooksPath shared with other repositories",
          svm.private_hooks_dir(Path(tmp) / "global-hooks", git), False)

source_lines = (ROOT / "scripts" / "sync_via_mirror.py").read_text(encoding="utf-8").splitlines()
check("the shim's protocol grep matches exactly one line of the script",
      [line for line in source_lines if line.startswith(f"HOOK_PROTOCOL = {svm.HOOK_PROTOCOL}")],
      [f"HOOK_PROTOCOL = {svm.HOOK_PROTOCOL}"])
check("and the shim greps for that same protocol",
      f'grep -q "^HOOK_PROTOCOL = {svm.HOOK_PROTOCOL}"' in svm.shim("post-merge"), True)

print()
if FAILED:
    print(f"{FAILED} check(s) failed")
    sys.exit(1)
print("all checks passed")
