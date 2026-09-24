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

print()
if FAILED:
    print(f"{FAILED} check(s) failed")
    sys.exit(1)
print("all checks passed")
