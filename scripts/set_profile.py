"""Choose which channel to deploy and which brain answers, and record both in
the azd environment.

This is the first command anyone runs. It answers "where do I start?" by turning
the choices into (a) the azd env flags the templates read, and (b) a numbered,
ordered list of every remaining step — including the ones a human has to do.

The questions are independent:

    channel   (DEPLOY_PROFILE)  where people reach the avatar   web / teams-tab /
                                                                in-call-browser / in-call
    brain     (VOICE_BINDING)   what answers                    agent / model
    web tool  (AGENT_WEB_TOOL)  agent mode's web search         bing / webiq

The web tool is only asked for agent mode; model mode always uses Web IQ.

    uv run python scripts/set_profile.py                             # interactive
    uv run python scripts/set_profile.py --profile web --binding agent   # CI
    uv run python scripts/set_profile.py --profile web --binding agent --web-tool webiq
    uv run python scripts/set_profile.py --show                      # current plan

All are deliberately stored in the azd env rather than asked for at deploy
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
    DEFAULT_WEB_TOOL,
    DIM,
    GREEN,
    PROFILE_MANAGED_FLAGS,
    PROFILE_ORDER,
    PROFILES,
    RESET,
    WEB_TOOL_ORDER,
    WEB_TOOLS,
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


def _choose_web_tool(current: str) -> str:
    # Enter keeps what the environment already has, so re-running this to change
    # the channel cannot quietly swap a deployed agent's web tool.
    default = current if current in WEB_TOOLS else DEFAULT_WEB_TOOL
    default_n = WEB_TOOL_ORDER.index(default) + 1
    print()
    print(f"{BOLD}Which web search should the agent use?{RESET}")
    print(f"{DIM}  Agent mode only. Both search the same trusted sites (bingAllowedDomains). "
          f"Change it later by re-running this and redeploying.{RESET}")
    print()
    for i, key in enumerate(WEB_TOOL_ORDER, start=1):
        t = WEB_TOOLS[key]
        marker = f"  {GREEN}(current){RESET}" if key == current else ""
        print(f"  {i}. {BOLD}{t.title}{RESET}  {DIM}(AGENT_WEB_TOOL={t.key}){RESET}{marker}")
        print(f"     {t.summary}")
        print(f"     {DIM}{t.tradeoff}{RESET}")
        print()

    while True:
        raw = input(f"Enter 1-{len(WEB_TOOL_ORDER)} (default {default_n}): ").strip() or str(default_n)
        if raw.isdigit() and 1 <= int(raw) <= len(WEB_TOOL_ORDER):
            return WEB_TOOL_ORDER[int(raw) - 1]
        if raw.lower() in WEB_TOOLS:
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
    ap.add_argument(
        "--web-tool",
        choices=WEB_TOOL_ORDER,
        help=f"Set AGENT_WEB_TOOL without prompting (agent mode only; default {DEFAULT_WEB_TOOL}).",
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
    previous_binding = env.get("VOICE_BINDING", "").strip().lower() or "agent"
    binding = args.binding or _choose_binding(env.get("VOICE_BINDING", "agent"))
    _azd_env_set("VOICE_BINDING", binding)
    if binding != previous_binding:
        changed.append(f"VOICE_BINDING={binding}")

    # Third question, agent mode only: which web tool. Asked only in an
    # interactive run (no --binding), so existing CI invocations never block on
    # it; unanswered, the environment keeps whatever it already had.
    current_web_tool = env.get("AGENT_WEB_TOOL", "").strip().lower()
    web_tool = args.web_tool
    if not web_tool and binding == "agent" and not args.binding:
        web_tool = _choose_web_tool(current_web_tool)
    if web_tool:
        _azd_env_set("AGENT_WEB_TOOL", web_tool)
    effective_web_tool = web_tool or current_web_tool or DEFAULT_WEB_TOOL
    if binding == "agent" and effective_web_tool != (current_web_tool or DEFAULT_WEB_TOOL):
        changed.append(f"AGENT_WEB_TOOL={effective_web_tool}")

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
    if binding == "agent":
        tool = WEB_TOOLS.get(effective_web_tool)
        if tool:
            print(f"{GREEN}Agent web tool: '{effective_web_tool}' ({tool.title}).{RESET}")
        else:
            print(f"{YELLOW}AGENT_WEB_TOOL={effective_web_tool!r} is not a valid web tool, so "
                  f"preflight will stop.{RESET} {DIM}Re-run with --web-tool "
                  f"{' or '.join(WEB_TOOL_ORDER)}.{RESET}")
        if effective_web_tool == "webiq" and env.get("FOUNDRY_ACCOUNT_NAME", "").strip():
            print(f"{YELLOW}  webiq needs the Foundry account this template creates, and "
                  f"FOUNDRY_ACCOUNT_NAME is set, so preflight will stop.{RESET}")
            print(f"{DIM}  Re-run with --web-tool bing, or clear FOUNDRY_ACCOUNT_NAME.{RESET}")
    elif web_tool:
        print(f"{DIM}  AGENT_WEB_TOOL={web_tool} recorded, but it only applies to agent mode; "
              f"model mode always uses Web IQ.{RESET}")

    uses_web_iq = binding == "model" or effective_web_tool == "webiq"
    if uses_web_iq and not env.get("WEBIQ_API_KEY", "").strip():
        print(f"{DIM}  Web IQ needs a key unless the app's identity is bound in the Web IQ "
              f"portal: azd env set WEBIQ_API_KEY <key>{RESET}")

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
        if f"AGENT_WEB_TOOL={effective_web_tool}" in changed:
            print(
                f"{DIM}Provisioning publishes a new version of the live agent with the new web "
                f"tool. An existing Bing account is not deleted and keeps billing; delete it "
                f"yourself once you will not switch back.{RESET}"
            )
    else:
        print(f"{CYAN}Nothing to re-provision{RESET} — this environment already matches the profile.")
        print(f"{DIM}  Ship code changes with `azd deploy`.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
