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
    uv run python scripts/rename_avatar.py Nuru --model Nuru --type custom-photo
    uv run python scripts/rename_avatar.py Simone --check-only  # verify only
    uv run python scripts/rename_avatar.py Nuru -e staging      # non-default azd env

The persona name, the Speech character and the modality are separate knobs:
``AVATAR_DISPLAY_NAME`` brands the assistant, ``AVATAR_MODEL`` selects the Speech
character and ``AVATAR_TYPE`` decides whether that character is looked up in the
prebuilt catalogue or in your own Speech resource. Switching to an avatar you
trained yourself means moving the last two together, so ``--model`` asks about
``--type`` rather than failing on a name it cannot find in the catalogue.

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

from backend.avatar_identity import AVATAR_TYPES, avatar_type  # noqa: E402

# The variables a rename can write. AVATAR_DISPLAY_NAME is the branding knob and
# it outranks every other input to the resolver; the other two only move when
# asked for, via --model and --type.
#
# Branding, character and modality are separate knobs by design. The character
# changes only via --model, validated against the catalogue.
# tests/test_avatar_identity.py pins this against the resolver's real inputs.
RENAME_VARS = ("AVATAR_DISPLAY_NAME", "AVATAR_MODEL", "AVATAR_TYPE")

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


def character_var(env: dict[str, str]) -> str:
    """Return the canonical variable that reaches Voice Live."""
    return "AVATAR_MODEL"


def uses_agent(env: dict[str, str]) -> bool:
    """True when this deployment answers through a Foundry agent.

    Model mode binds a realtime model directly, so there is no agent to re-brand
    and no agent prompt to verify. azure.yaml already skips
    ``setup_foundry_agent.py`` on that path; this mirrors it, because running it
    anyway fails and then reports a HALF APPLIED rename that never happened.
    """
    return (env.get("VOICE_BINDING") or "agent").strip().lower() != "model"


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


def resolve_custom_type(model: str, display: str, valid: set[str], current_type: str) -> str:
    """Decide what an unrecognised ``--model`` means: custom avatar, or a typo?

    Only the prebuilt catalogue can be checked locally, so a name outside it is
    either a model trained in the caller's own Speech resource or a mistake.
    Guessing either way is wrong. Assume custom and a typo ships an avatar that
    never renders; assume typo and a custom avatar cannot be selected by this
    script at all -- which is the state that made people set AVATAR_TYPE by hand
    and then discover the container app had not moved with it.

    So ask, and on a yes return the custom type to write. Non-interactive callers
    get the old hard failure, with the flag that resolves it.
    """
    chosen = "custom-video" if current_type.endswith("-video") else "custom-photo"
    lead = (f"\n{model!r} is not one of the avatars Speech offers out of the box, and "
            f"AVATAR_TYPE is {current_type!r}.\nThat combination renders nothing -- but it "
            f"is exactly what you would expect if you\ntrained {model!r} yourself in your "
            f"Speech resource.\n")
    hint = (f"\nIf it was a typo, the prebuilt characters are:\n\n  {', '.join(sorted(valid))}\n\n"
            f"To brand her {display!r} while keeping the current character, omit --model.\n")

    print(lead)
    reply: str | None = None
    if sys.stdin.isatty():
        try:
            reply = input(f"  Is {model!r} a custom avatar in your Speech resource? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            # isatty() can report a terminal with nothing attached to read from --
            # piped shells and CI runners both do it -- so a bare input() here
            # dies with a traceback on the one path that most needs to explain
            # itself. Fall through to the explicit-flag advice instead.
            print()

    if reply is None:
        raise SystemExit(
            f"\nIf it is a custom avatar, say so explicitly:\n\n"
            f"  uv run python scripts/rename_avatar.py {display} --model {model} "
            f"--type {chosen}\n"
            + hint
        )
    if reply.strip().lower() not in ("y", "yes"):
        raise SystemExit(hint)
    info(f"AVATAR_TYPE {current_type!r} -> {chosen!r} (pass --type to choose the other)")
    return chosen


def main() -> int:
    ap = argparse.ArgumentParser(description="Rename the avatar persona everywhere, then verify.")
    ap.add_argument("name", help='New persona name, e.g. "Nuru"')
    ap.add_argument("--model", help="Also switch the Speech photo-avatar character "
                                    "(validated); omit to keep the current one")
    ap.add_argument("--type", choices=AVATAR_TYPES,
                    help="Also switch AVATAR_TYPE, e.g. custom-photo when moving to an "
                         "avatar you trained yourself. Asked for interactively when "
                         "--model names something outside the prebuilt catalogue.")
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
    print(f"azd env: {env_name}    resource group: {rg}")

    overrides = {"AVATAR_DISPLAY_NAME": display}
    model = None
    char_var = write_var = character_var(env)

    # The type has to settle BEFORE the character is validated. A custom model is
    # legitimately absent from the prebuilt catalogue, so checking the new name
    # against a stale AVATAR_TYPE rejects the very avatar being switched to.
    current_type = avatar_type(env)
    new_type = a.type or current_type
    if a.model:
        model = a.model.strip()
        valid = valid_photo_characters()
        unknown = bool(valid) and model not in valid
        if unknown and not new_type.startswith("custom-"):
            if a.check_only:
                warn(f"{model!r} is not a prebuilt character and AVATAR_TYPE is "
                     f"{new_type!r}, so the avatar would not render.")
            else:
                new_type = resolve_custom_type(model, display, valid, current_type)
        if unknown and new_type.startswith("custom-"):
            warn(f"{model!r} is not a prebuilt character. This is only valid if a model\n"
                 "        of that name exists in your Speech resource.")
        write_var = "AVATAR_MODEL"
        overrides[write_var] = model

    if new_type != current_type:
        # AVATAR_TYPE is what makes the backend mark the avatar customized and the
        # frontend request the photo modality, so it has to reach the same three
        # surfaces as the name. Writing it to the azd environment alone leaves the
        # running app on the old type until someone redeploys -- drift the VERIFY
        # block below already reports, and which this script should not create.
        overrides["AVATAR_TYPE"] = new_type

    current_model = env.get(char_var) or ""
    print(f"avatar type: AVATAR_TYPE={current_type!r}"
          + (f" -> {new_type!r}" if new_type != current_type else "  (unchanged)"))
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
        #     Model mode has no agent, so there is nothing to re-render.
        if not uses_agent(env):
            print("\n3. Foundry agent")
            info("skipped - VOICE_BINDING=model binds a realtime model directly, so the "
                 "persona\n        reaches Voice Live from the container app in step 2. "
                 "Nothing is frozen.")
        else:
            print("\n3. Foundry agent (re-render prompt, new version)")
            rc = push_agent(a.env, overrides)
            (ok if rc in (0, 3) else bad)(f"setup_foundry_agent.py exit {rc}"
                                          + (" (degraded: web tool off)" if rc == 3 else ""))
            if rc not in (0, 3):
                # Steps 1 and 2 already landed, so the deployment is now internally
                # inconsistent in exactly the way this script exists to prevent. Say so
                # here rather than leaving it to be inferred from the VERIFY table.
                print(f"\n{RED}The rename is HALF APPLIED.{RST} Steps 1 and 2 succeeded, so the "
                      f"stage will show {display!r}\nwhile she still introduces herself by the "
                      "old name. Fix the error above and re-run:\nthis script is idempotent, so "
                      "a second run completes the rename.\n")

    # --- verification: read every surface back, independently
    print(f"\n{'=' * 74}\nVERIFY\n{'=' * 74}")
    failures = 0
    env = azd_env(a.env)

    # Compare the resolved name, not the raw branding variable.
    from backend.avatar_identity import resolve_avatar_display_name

    def check_surface(label: str, surface_env: dict[str, str]) -> int:
        got = resolve_avatar_display_name(surface_env)
        raw = (f"AVATAR_DISPLAY_NAME={surface_env.get('AVATAR_DISPLAY_NAME', '')!r} "
               f"AVATAR_MODEL={surface_env.get('AVATAR_MODEL', '')!r}")
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
    identity_vars = ("AVATAR_DISPLAY_NAME", "AVATAR_TYPE", "AVATAR_MODEL")
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

    # Standard Speech characters are catalogued; custom models are checked by Voice Live.
    catalogue = valid_photo_characters()
    live_var = character_var(live_env)
    character = (live_env.get(live_var) or "").strip()
    if not character:
        ok("no speech character set (nothing to validate)")
    elif character in catalogue:
        ok(f"speech character {character!r} ({live_var}) is a real prebuilt avatar")
    elif avatar_type(live_env).startswith("custom-"):
        ok(f"speech character {character!r} ({live_var}) is a custom model")
    else:
        bad(f"speech character {character!r} ({live_var}) is not a prebuilt avatar and "
            "AVATAR_TYPE is not custom -- the avatar will not render")
        failures += 1

    if not uses_agent(env):
        # No agent means no frozen prompt, so the surface simply does not exist.
        # Saying so beats silently reporting one fewer check than agent mode.
        ok("no agent prompt to check - VOICE_BINDING=model")
    else:
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
