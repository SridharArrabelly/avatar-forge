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
    uv run python scripts/rename_avatar.py Nuru --model Sakura  # also switch character
    uv run python scripts/rename_avatar.py Simone --check-only  # verify only
    uv run python scripts/rename_avatar.py Nuru -e staging      # non-default azd env

The persona name and the Speech character are **separate knobs**: you can run the
``Simone`` character and call her ``Nuru``. Renaming therefore leaves
``PHOTO_AVATAR_NAME`` alone unless ``--model`` says otherwise -- it is a model id
from a fixed catalogue, and an unknown value renders nothing at all, silently.

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

from backend.avatar_identity import avatar_type  # noqa: E402

# The variable a rename writes. AVATAR_DISPLAY_NAME is the branding knob and it
# outranks every other input to the resolver.
#
# PHOTO_AVATAR_NAME is deliberately NOT here. It is a Speech *model id* from a
# fixed catalogue, not a label: builders.py lowercases it into the character sent
# to Speech, so pointing it at an arbitrary persona name renders nothing at all.
# Branding and model are separate knobs by design -- you can run the Simone
# character and call her Nuru. Change the model only via --model, which is
# validated against the catalogue.
# tests/test_avatar_identity.py pins this against the resolver's real inputs.
RENAME_VARS = ("AVATAR_DISPLAY_NAME",)

GREEN, RED, YEL, DIM, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(m: str) -> None:
    print(f"  {GREEN}PASS{RST}  {m}")


def bad(m: str) -> None:
    print(f"  {RED}FAIL{RST}  {m}")


def warn(m: str) -> None:
    print(f"  {YEL}WARN{RST}  {m}")


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


def valid_photo_characters() -> set[str]:
    """The photo avatars Speech actually offers, read from the UI's own picker.

    Parsed out of frontend/index.html rather than duplicated here, so this check
    cannot drift from the list a user can pick in the app.
    """
    html = (REPO / "frontend" / "index.html").read_text(encoding="utf-8", errors="replace")
    block = re.search(r'<select id="photoAvatarName">(.*?)</select>', html, re.S)
    return set(re.findall(r'value="([^"]+)"', block.group(1))) if block else set()


def _flag(env: dict[str, str], key: str) -> bool:
    return (env.get(key, "") or "").strip().lower() in ("true", "1", "yes", "on")


def character_var(env: dict[str, str]) -> str:
    """Which variable actually reaches Voice Live as the avatar character.

    Mirrors the precedence in frontend/app.js (startSession and
    updateAvatarScene), so this script validates the value the app really sends
    rather than a variable that happens to be inert in the current mode.
    """
    if (env.get("AVATAR_TYPE") or "").strip() or (env.get("AVATAR_MODEL") or "").strip():
        return "AVATAR_MODEL"
    # Preserve the legacy fallback exactly for environments that have not opted
    # into the canonical selector yet.
    if _flag(env, "IS_CUSTOM_AVATAR") and (env.get("CUSTOM_AVATAR_NAME") or "").strip():
        return "CUSTOM_AVATAR_NAME"
    if _flag(env, "IS_PHOTO_AVATAR"):
        return "PHOTO_AVATAR_NAME"
    return "AVATAR_NAME"


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
    ap.add_argument("--model", help="Also switch the Speech photo-avatar character "
                                    "(validated); omit to keep the current one")
    ap.add_argument("--check-only", action="store_true", help="Verify only; change nothing")
    ap.add_argument("-e", "--env", help="azd environment name (default: azd's own default)")
    ap.add_argument("--resource-group", help="Override the resource group from the azd env")
    a = ap.parse_args()

    display = a.name.strip()
    if not display:
        raise SystemExit("The new name cannot be empty.")

    print(f"\n{'=' * 74}\nRENAME AVATAR -> {display!r}\n{'=' * 74}")
    print(f"repo: {REPO}")

    env = azd_env(a.env)
    env_name = a.env or env.get("AZURE_ENV_NAME", "(default)")
    rg = resolve_rg(env, a.resource_group)

    # The Speech character is only touched when asked for explicitly. Defaulting
    # it to the persona name is what broke rendering: the character is a model
    # id, so "Nuru" is not a character Speech can draw unless it was trained.
    overrides = {"AVATAR_DISPLAY_NAME": display}
    model = None
    char_var = write_var = character_var(env)
    if a.model:
        model = a.model.strip()
        # Only the prebuilt catalogue can be checked locally. With IS_CUSTOM_AVATAR
        # on, the character is a model trained in your own Speech resource and any
        # name may be legitimate -- warn, but do not refuse.
        is_custom = avatar_type(env).startswith("custom-")
        valid = valid_photo_characters()
        if is_custom:
            if valid and model not in valid:
                warn(f"{model!r} is not a prebuilt character. IS_CUSTOM_AVATAR is on, so this\n"
                     "        is only valid if a model of that name exists in your Speech "
                     "resource.")
        elif valid and model not in valid:
            raise SystemExit(
                f"\n{model!r} is not a photo avatar that Speech offers, so the avatar would\n"
                f"silently fail to render. Valid characters:\n\n  {', '.join(sorted(valid))}\n\n"
                f"The persona name and the Speech character are separate knobs: to brand her\n"
                f"{display!r} while keeping the current character, just omit --model."
            )
        # In custom mode CUSTOM_AVATAR_NAME is the name that reaches Voice Live,
        # so that is what --model has to set -- including when it is still empty,
        # which is the "custom on, no custom name" silent-blank failure.
        write_var = "AVATAR_MODEL" if (env.get("AVATAR_TYPE") or "").strip() else (
            "CUSTOM_AVATAR_NAME" if is_custom else char_var
        )
        overrides[write_var] = model

    print(f"azd env: {env_name}    resource group: {rg}")
    current_model = env.get(char_var) or ""
    print(f"speech character: {char_var}={current_model!r}"
          + (f" -> {write_var}={model!r}" if model else "  (unchanged)"))
    print(f"current: AVATAR_DISPLAY_NAME={env.get('AVATAR_DISPLAY_NAME')!r}\n")

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
                 "--set-env-vars", *[f"{k}={v}" for k, v in overrides.items()],
                 "-o", "none"])
        (ok if r.returncode == 0 else bad)(f"{ca} env updated")
        if r.returncode != 0:
            info((r.stderr or "")[:400])

        # --- 3. Foundry agent: the prompt is rendered and FROZEN at push time,
        #     so without this she keeps introducing herself by the old name.
        print("\n3. Foundry agent (re-render prompt, new version)")
        rc = push_agent(a.env, overrides)
        (ok if rc in (0, 3) else bad)(f"setup_foundry_agent.py exit {rc}"
                                      + (" (degraded: web tool off)" if rc == 3 else ""))
        if rc not in (0, 3):
            # Steps 1 and 2 already landed, so the deployment is now internally
            # inconsistent in exactly the way this script exists to prevent. Say so
            # here rather than leaving it to be inferred from the VERIFY table.
            print(f"\n{RED}The rename is HALF APPLIED.{RST} Steps 1 and 2 succeeded, so the "
                  f"stage will show {display!r}\nwhile she still introduces herself by the old "
                  "name. Fix the error above and re-run:\nthis script is idempotent, so a "
                  "second run completes the rename.\n")

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

    # Drift between the two is the quiet killer: the container app is what runs
    # today, the azd environment is what the next `azd up` will impose. When they
    # disagree, a deploy silently reverts working configuration -- and a rename
    # that only reached one of them looks fine right up until then.
    identity_vars = ("AVATAR_DISPLAY_NAME", "PHOTO_AVATAR_NAME", "AVATAR_NAME",
                     "CUSTOM_AVATAR_NAME", "IS_PHOTO_AVATAR", "IS_CUSTOM_AVATAR")
    drift = [(k, env.get(k, ""), live_env.get(k, "")) for k in identity_vars
             if (env.get(k, "") or "") != (live_env.get(k, "") or "")]
    if drift:
        bad(f"azd env and container app disagree on {len(drift)} identity variable(s); "
            "the next `azd up` would revert the running app")
        for k, azd_value, live_value in drift:
            info(f"       {k}: azd={azd_value!r} vs container app={live_value!r}")
        failures += 1
    else:
        ok("azd env and container app agree on every identity variable")

    # The persona name is free text, but the Speech character is not: an unknown
    # value renders nothing, with no error anywhere in the app. Which variable
    # carries the character depends on the mode, so ask character_var rather than
    # reading PHOTO_AVATAR_NAME -- it is inert whenever a custom avatar is named.
    catalogue = valid_photo_characters()
    live_var = character_var(live_env)
    character = (live_env.get(live_var) or "").strip()
    live_custom = _flag(live_env, "IS_CUSTOM_AVATAR")
    if live_custom and not (live_env.get("CUSTOM_AVATAR_NAME") or "").strip():
        warn(f"IS_CUSTOM_AVATAR is on but CUSTOM_AVATAR_NAME is empty, "
             f"so the character falls back to {live_var}")
    if not character:
        ok("no speech character set (nothing to validate)")
    elif live_custom:
        if character in catalogue:
            # Cannot be settled locally: a custom-trained avatar may legitimately
            # share a prebuilt name, and the Speech resource has disableLocalAuth
            # so it cannot be listed. Warn loudly, but do not fail -- this shape
            # is only wrong if the avatar was never trained under that name.
            warn(f"{live_var}={character!r} is also a PREBUILT name while "
                 "IS_CUSTOM_AVATAR is on -- Voice Live looks it up in your own resource "
                 "only. If no custom avatar of that name was trained, every session "
                 "fails with avatar_verification_failed.")
        else:
            ok(f"speech character {character!r} ({live_var}) is custom -- not a prebuilt name")
    elif character in catalogue:
        ok(f"speech character {character!r} ({live_var}) is a real prebuilt avatar")
    else:
        bad(f"speech character {character!r} ({live_var}) is not a prebuilt avatar and "
            "IS_CUSTOM_AVATAR is off -- the avatar will not render")
        failures += 1

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
