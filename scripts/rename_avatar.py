"""Rename the avatar persona (e.g. Simone -> Nuru) in one command, and prove it landed.

The rename is **not** a prompt edit. ``prompts/agent/instructions.md`` contains
``{{AVATAR_NAME}}`` and never a literal name, so the name is resolved from the
environment in three separate places -- and all three have to move together:

  1. the azd environment      so a later ``azd up`` cannot revert it
  2. the container app        so the stage name, tagline and wake phrase follow
  3. the Foundry agent        so she *says* the new name -- the prompt is
                              rendered and frozen into an agent version at push
                              time, so it does not pick up an env change

Miss (3) and the screen says Nuru while she introduces herself as Simone. That
one failure mode is the whole reason this script exists.

It deliberately does **not** run ``azd up``. ``az containerapp update
--set-env-vars`` merges into the live revision in about a minute and cannot
revert the deployed image, which a bare ``azd provision`` can.

Usage (from anywhere -- the repo is located from this file)::

    uv run python scripts/rename_avatar.py Nuru
    uv run python scripts/rename_avatar.py Nuru --model Nuru-v2  # Speech id differs
    uv run python scripts/rename_avatar.py Simone --check-only   # verify only
    uv run python scripts/rename_avatar.py Nuru -e staging       # non-default azd env

Exit 0 = every surface agrees on the new name. The last step cannot be
automated: open the app and ask "what is your name?".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# The variables a rename has to write. AVATAR_DISPLAY_NAME is the explicit
# branding knob; PHOTO_AVATAR_NAME moves with it so the derived fallback agrees
# too, and the name survives someone later clearing the knob.
# tests/test_avatar_identity.py pins these against the resolver's real inputs.
RENAME_VARS = ("AVATAR_DISPLAY_NAME", "PHOTO_AVATAR_NAME")

GREEN, RED, YEL, DIM, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(m: str) -> None:
    print(f"  {GREEN}PASS{RST}  {m}")


def bad(m: str) -> None:
    print(f"  {RED}FAIL{RST}  {m}")


def info(m: str) -> None:
    print(f"  {DIM}{m}{RST}")


def run(args: list[str], **kw) -> subprocess.CompletedProcess:
    """Run an external command.

    Resolves the executable via shutil.which because on Windows ``az`` and
    ``azd`` are ``.cmd`` shims and subprocess does not apply PATHEXT -- a bare
    ["az", ...] raises FileNotFoundError.
    """
    exe = shutil.which(args[0])
    if not exe:
        raise SystemExit(f"{args[0]} not found on PATH")
    return subprocess.run([exe, *args[1:]], capture_output=True, text=True, **kw)


def azd_env(env_name: str | None) -> dict[str, str]:
    """Read the azd environment. With no name, azd resolves its own default."""
    args = ["azd", "env", "get-values"]
    if env_name:
        args += ["-e", env_name]
    proc = run(args, cwd=REPO)
    if proc.returncode != 0:
        raise SystemExit(
            f"`{' '.join(args)}` failed:\n{(proc.stderr or proc.stdout).strip()}\n\n"
            "Deploy first (`azd up`), or pass -e with an existing environment name."
        )
    values = {}
    for line in proc.stdout.splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
        if m:
            values[m.group(1)] = m.group(2).strip('"')
    return values


def resolve_rg(env: dict[str, str], override: str | None) -> str:
    if override:
        return override
    # main.parameters.json maps resourceGroupName to ${AZURE_RESOURCE_GROUP_NAME},
    # but azd also writes AZURE_RESOURCE_GROUP; accept either.
    for key in ("AZURE_RESOURCE_GROUP_NAME", "AZURE_RESOURCE_GROUP"):
        value = (env.get(key) or "").strip()
        if value:
            return value
    raise SystemExit(
        "No resource group in the azd environment (AZURE_RESOURCE_GROUP_NAME). "
        "Deploy first, or pass --resource-group."
    )


def container_app_name(env: dict[str, str], rg: str) -> str:
    """Prefer azd's own record of the app; fall back to listing the group."""
    named = (env.get("SERVICE_APP_NAME") or "").strip()
    if named:
        return named
    apps = json.loads(run(["az", "containerapp", "list", "-g", rg, "-o", "json"]).stdout or "[]")
    if len(apps) == 1:
        return apps[0]["name"]
    if not apps:
        raise SystemExit(f"No container app found in {rg}. Has this environment been deployed?")
    raise SystemExit(
        f"{len(apps)} container apps in {rg} and SERVICE_APP_NAME is unset, so the "
        "target is ambiguous. Set it with `azd env set SERVICE_APP_NAME <name>`."
    )


def project_endpoint(env: dict[str, str]) -> str:
    for key in ("PROJECT_ENDPOINT", "AZURE_AI_PROJECT_ENDPOINT", "FOUNDRY_PROJECT_ENDPOINT"):
        value = (env.get(key) or "").strip()
        if value:
            return value
    raise SystemExit("No Foundry project endpoint in the azd environment (PROJECT_ENDPOINT).")


def live_agent_instructions(env: dict[str, str]) -> tuple[str, str]:
    """Return (version, instructions) for the agent version currently deployed."""
    from azure.ai.projects import AIProjectClient
    from azure.identity import AzureCliCredential

    client = AIProjectClient(
        endpoint=project_endpoint(env),
        # The default 10s process timeout expires when az is throttled.
        credential=AzureCliCredential(process_timeout=90),
    )
    name = env.get("AGENT_NAME", "AvatarAgent")
    # list_versions returns DESCENDING; take the numeric max rather than the first.
    latest = max(client.agents.list_versions(agent_name=name), key=lambda v: int(v.version))
    definition = latest.definition
    text = getattr(definition, "instructions", None) or definition.get("instructions", "")
    return str(latest.version), str(text)


def push_agent(env_name: str | None, overrides: dict[str, str]) -> int:
    """Re-render the prompt with the new name and publish a new agent version."""
    env = os.environ.copy()
    # azd_env() returns a dict without touching os.environ, and the child script
    # reads the process environment, so hydrate it explicitly.
    env.update(azd_env(env_name))
    env.update(overrides)
    proc = subprocess.run(
        [sys.executable, "scripts/setup_foundry_agent.py"],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
    )
    for line in (proc.stdout or "").splitlines()[-12:]:
        info(line)
    if proc.returncode not in (0, 3):
        for line in (proc.stderr or "").splitlines()[-15:]:
            info(line)
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="Rename the avatar persona everywhere, then verify.")
    ap.add_argument("name", help='New persona name, e.g. "Nuru"')
    ap.add_argument("--model", help="Speech photo-avatar id, if it differs from the name")
    ap.add_argument("--check-only", action="store_true", help="Verify only; change nothing")
    ap.add_argument("-e", "--env", help="azd environment name (default: azd's own default)")
    ap.add_argument("--resource-group", help="Override the resource group from the azd env")
    a = ap.parse_args()

    display = a.name.strip()
    model = (a.model or display).strip()
    if not display:
        raise SystemExit("The new name cannot be empty.")
    overrides = dict(zip(RENAME_VARS, (display, model)))

    print(f"\n{'=' * 74}\nRENAME AVATAR -> {display!r}   (photo-avatar id {model!r})\n{'=' * 74}")
    print(f"repo: {REPO}")

    env = azd_env(a.env)
    env_name = a.env or env.get("AZURE_ENV_NAME", "(default)")
    rg = resolve_rg(env, a.resource_group)
    print(f"azd env: {env_name}    resource group: {rg}\n")
    print(f"current: PHOTO_AVATAR_NAME={env.get('PHOTO_AVATAR_NAME')!r}  "
          f"AVATAR_DISPLAY_NAME={env.get('AVATAR_DISPLAY_NAME')!r}\n")

    if not a.check_only:
        # --- 1. azd env: keeps infra-as-code truthful so `azd up` cannot revert it
        print("1. azd environment")
        for k, v in overrides.items():
            args = ["azd", "env", "set", k, v]
            if a.env:
                args += ["-e", a.env]
            r = run(args, cwd=REPO)
            (ok if r.returncode == 0 else bad)(f"{k}={v}")

        # --- 2. container app: stage name, tagline and wake phrase read these at
        #     runtime. --set-env-vars merges, so unrelated variables are preserved.
        print("\n2. container app (new revision, ~1 min)")
        ca = container_app_name(env, rg)
        r = run(["az", "containerapp", "update", "-g", rg, "-n", ca,
                 "--set-env-vars", f"AVATAR_DISPLAY_NAME={display}",
                 f"PHOTO_AVATAR_NAME={model}", "-o", "none"])
        (ok if r.returncode == 0 else bad)(f"{ca} env updated")
        if r.returncode != 0:
            info((r.stderr or "")[:400])

        # --- 3. Foundry agent: the prompt is rendered and FROZEN at push time,
        #     so without this she keeps introducing herself by the old name.
        print("\n3. Foundry agent (re-render prompt, new version)")
        rc = push_agent(a.env, overrides)
        (ok if rc in (0, 3) else bad)(f"setup_foundry_agent.py exit {rc}"
                                      + (" (degraded: web tool off)" if rc == 3 else ""))

    # --- verification: read every surface back, independently
    print(f"\n{'=' * 74}\nVERIFY\n{'=' * 74}")
    failures = 0
    env = azd_env(a.env)

    # Compare the RESOLVED name, not the raw variables: an empty
    # AVATAR_DISPLAY_NAME is legitimate when the name derives from
    # PHOTO_AVATAR_NAME, so asserting on the raw var would call a correct
    # configuration broken. resolve_avatar_display_name is the same function the
    # app and the deploy script use, so this asks the question the app asks.
    from backend.avatar_identity import resolve_avatar_display_name

    def check_surface(label: str, surface_env: dict[str, str]) -> int:
        got = resolve_avatar_display_name(surface_env)
        raw = (f"AVATAR_DISPLAY_NAME={surface_env.get('AVATAR_DISPLAY_NAME', '')!r} "
               f"PHOTO_AVATAR_NAME={surface_env.get('PHOTO_AVATAR_NAME', '')!r}")
        if got == display:
            ok(f"{label} resolves to {got!r}")
            info(f"       {raw}")
            return 0
        bad(f"{label} resolves to {got!r}, expected {display!r}")
        info(f"       {raw}")
        return 1

    failures += check_surface("azd env      ", env)

    ca = container_app_name(env, rg)
    app = json.loads(run(["az", "containerapp", "show", "-g", rg, "-n", ca, "-o", "json"]).stdout)
    live_env = {
        e["name"]: e.get("value", "")
        for e in app["properties"]["template"]["containers"][0].get("env", [])
    }
    failures += check_surface("container app", live_env)

    version, text = live_agent_instructions(env)
    first = text.splitlines()[0] if text else "<empty>"
    if text.count(display) >= 2 and first.startswith(f"You are {display},"):
        ok(f"agent v{version} says {display!r} ({text.count(display)}x) -- {first[:58]}...")
    else:
        bad(f"agent v{version} first line: {first[:70]}")
        failures += 1
    if "{{AVATAR_NAME}}" in text:
        bad("agent prompt still contains an unresolved {{AVATAR_NAME}} placeholder")
        failures += 1
    else:
        ok("no unresolved placeholders in the deployed prompt")

    print()
    if failures:
        print(f"{RED}{failures} check(s) FAILED{RST} -- the surfaces disagree on the name.\n")
        return 1
    print(f"{GREEN}All surfaces agree: the avatar is {display}.{RST}")
    print(f"{YEL}Last step is human: open the app and ask 'what is your name?'{RST}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
