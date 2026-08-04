"""Choose which channel to deploy and which brain answers, and record both in
the azd environment.

This is the first command anyone runs. It answers "where do I start?" by turning
two choices into (a) the azd env flags the templates read, and (b) a numbered,
ordered list of every remaining step — including the ones a human has to do.

The two questions are independent:

    channel  (DEPLOY_PROFILE)  where people reach the avatar   web / teams-tab /
                                                               in-call / in-call-browser
    brain    (VOICE_BINDING)   what answers                    agent / model

    uv run python scripts/set_profile.py                             # interactive
    uv run python scripts/set_profile.py --profile web --binding agent   # CI
    uv run python scripts/set_profile.py --show                      # current plan

Both are deliberately stored in the azd env rather than asked for at deploy
time: `azd up` must stay non-interactive so it works in CI and on re-deploys.
The menu is convenience over the flags, never a substitute for them.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

from channels import (
    BINDING_ORDER,
    BINDINGS,
    BOLD,
    CYAN,
    DIM,
    GREEN,
    PROFILE_MANAGED_FLAGS,
    PROFILE_ORDER,
    PROFILES,
    RESET,
    YELLOW,
    get_profile,
    render_steps,
)


def _azd() -> str:
    exe = shutil.which("azd") or shutil.which("azd.exe")
    if not exe:
        print(f"{YELLOW}azd not found on PATH.{RESET} Install it: https://aka.ms/azd-install")
        sys.exit(2)
    return exe


def _azd_env_set(name: str, value: str) -> bool:
    res = subprocess.run(
        [_azd(), "env", "set", name, value], capture_output=True, text=True, check=False
    )
    if res.returncode != 0:
        print(f"{YELLOW}WARN{RESET}  could not set {name}: {(res.stderr or res.stdout).strip()}")
        return False
    return True


def _azd_env_values() -> dict[str, str]:
    res = subprocess.run(
        [_azd(), "env", "get-values"], capture_output=True, text=True, check=False
    )
    if res.returncode != 0:
        return {}
    values: dict[str, str] = {}
    for line in res.stdout.splitlines():
        if "=" not in line:
            continue
        key, _, raw = line.partition("=")
        values[key.strip()] = raw.strip().strip('"')
    return values


def _choose() -> str:
    print()
    print(f"{BOLD}Which channel do you want to deploy?{RESET}")
    print(f"{DIM}  The first three build on each other. The last is a rival to the third — the{RESET}")
    print(f"{DIM}  same in-call avatar, reached a different way. You can re-run this later.{RESET}")
    print()
    for i, key in enumerate(PROFILE_ORDER, start=1):
        p = PROFILES[key]
        admin = any(s.who == "admin" for s in p.steps)
        badge = f"{YELLOW}needs an administrator{RESET}" if admin else f"{GREEN}no administrator needed{RESET}"
        print(f"  {i}. {BOLD}{p.title}{RESET}  {DIM}(channels {p.channels}){RESET}")
        print(f"     {p.summary}")
        print(f"     {badge}")
        print()

    while True:
        raw = input(f"Enter 1-{len(PROFILE_ORDER)} (default 1): ").strip() or "1"
        if raw.isdigit() and 1 <= int(raw) <= len(PROFILE_ORDER):
            return PROFILE_ORDER[int(raw) - 1]
        if raw.lower() in PROFILES:
            return raw.lower()
        print(f"{YELLOW}Not a valid choice.{RESET}")


def _choose_binding(current: str) -> str:
    print()
    print(f"{BOLD}Which brain should answer?{RESET}")
    print(f"{DIM}  Independent of the channel — every channel works with either. "
          f"Change it later by re-running this and redeploying.{RESET}")
    print()
    for i, key in enumerate(BINDING_ORDER, start=1):
        b = BINDINGS[key]
        marker = f"  {GREEN}(current){RESET}" if key == current else ""
        print(f"  {i}. {BOLD}{b.title}{RESET}  {DIM}(VOICE_BINDING={b.key}){RESET}{marker}")
        print(f"     {b.summary}")
        print(f"     {DIM}{b.tradeoff}{RESET}")
        print()

    while True:
        raw = input(f"Enter 1-{len(BINDING_ORDER)} (default 1): ").strip() or "1"
        if raw.isdigit() and 1 <= int(raw) <= len(BINDING_ORDER):
            return BINDING_ORDER[int(raw) - 1]
        if raw.lower() in BINDINGS:
            return raw.lower()
        print(f"{YELLOW}Not a valid choice.{RESET}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", choices=PROFILE_ORDER, help="Set this profile without prompting.")
    ap.add_argument(
        "--binding",
        choices=BINDING_ORDER,
        help="Set VOICE_BINDING without prompting (agent or model).",
    )
    ap.add_argument("--show", action="store_true", help="Print the current profile's plan and exit.")
    args = ap.parse_args()

    env = _azd_env_values()
    current = env.get("DEPLOY_PROFILE", "")

    if args.show:
        profile = get_profile(current or None)
        print(render_steps(profile))
        return 0

    key = args.profile or _choose()
    profile = PROFILES[key]

    if not _azd_env_set("DEPLOY_PROFILE", key):
        print(f"{YELLOW}Is an azd environment selected?{RESET} Run `azd env new <name>` first.")
        return 2

    # The profile is the source of truth. Write the flags it wants AND reset every
    # other managed flag, so switching profiles cannot leave the previous one's
    # infrastructure quietly switched on — moving from the media bot to the browser
    # guest must not keep paying for a Windows VM the new profile never asked for.
    changed: list[str] = []
    if current and current != key:
        changed.append(f"DEPLOY_PROFILE={key}")
    for name, off in PROFILE_MANAGED_FLAGS.items():
        want = profile.flags.get(name, off)
        # Unset and explicitly-off mean the same thing to infra, so only report a
        # difference that actually changes what gets deployed.
        if (env.get(name, "").strip() or off) != want:
            changed.append(f"{name}={want}")
        _azd_env_set(name, want)

    # Second question: which brain. Only prompted when not supplied, so
    # `--profile X --binding Y` stays fully non-interactive for CI.
    binding = args.binding or _choose_binding(env.get("VOICE_BINDING", "agent"))
    _azd_env_set("VOICE_BINDING", binding)

    print()
    print(f"{GREEN}Profile set to '{key}'.{RESET}")
    if profile.flags:
        flags = ", ".join(f"{k}={v}" for k, v in profile.flags.items())
        print(f"{DIM}  Set for you: {flags}{RESET}")
    reset = [
        n
        for n, off in PROFILE_MANAGED_FLAGS.items()
        if n not in profile.flags and (env.get(n, "").strip() or off) != off
    ]
    if reset:
        print(f"{DIM}  Reset to off (not part of this profile): {', '.join(reset)}{RESET}")
    print(f"{GREEN}Voice binding set to '{binding}' ({BINDINGS[binding].title}).{RESET}")

    missing = [r for r in profile.requires if not env.get(r.name) and not r.optional]
    if missing:
        print()
        print(f"{BOLD}This profile still needs {len(missing)} value(s) from you:{RESET}")
        for r in missing:
            print(f"  {YELLOW}{r.name}{RESET}")
            print(f"    {DIM}{r.how}{RESET}")
        print()
        print(f"{DIM}  Set each with:  azd env set <NAME> <value>{RESET}")

    print(render_steps(profile))

    # Greenfield and upgrade need different commands, and getting that wrong is the
    # classic way to be told SUCCESS while nothing you changed actually shipped.
    # SERVICE_APP_URI is a provision output, so its absence means nothing exists yet.
    if not env.get("SERVICE_APP_URI"):
        print(f"{CYAN}Next:{RESET} uv run python scripts/preflight.py")
        print(f"{DIM}       then `azd up` — one command provisions and deploys all of the above.{RESET}")
    elif changed:
        print(f"{BOLD}This environment is already deployed, and you just changed what it deploys:{RESET}")
        for c in changed:
            print(f"  {YELLOW}{c}{RESET}")
        print()
        print(f"{CYAN}Next:{RESET} uv run python scripts/preflight.py")
        print(f"       azd provision   {DIM}# these arrive as container-app env vars from Bicep,{RESET}")
        print(f"       azd deploy      {DIM}# so a deploy on its own would never see them{RESET}")
        print()
        print(
            f"{YELLOW}Run both.{RESET} {DIM}`azd provision` alone reverts the container app to the "
            f"placeholder image from Bicep, and still reports success.{RESET}"
        )
    else:
        print(f"{CYAN}Nothing to re-provision{RESET} — this environment already matches the profile.")
        print(f"{DIM}  Ship code changes with `azd deploy`.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
