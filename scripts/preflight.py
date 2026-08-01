"""Pre-deployment doctor for avatar-forge. Run this BEFORE `azd up`.

Two jobs:

1. **Block the silent failures.** Voice Live (preview) is only available in a
   handful of regions; deploy the Foundry account elsewhere and everything
   provisions cleanly, then the WebSocket closes ~2s later with no error event.
   Region and provider problems are cheap to fix here and expensive to fix after
   a twenty-minute deployment.
2. **Tell you what is still missing for the channel you chose**, including the
   steps automation cannot perform and who has to perform them. Most abandoned
   deployments are not caused by a hard problem — they are caused by discovering
   a directory-admin dependency at step 9 with no explanation.

Usage:
    uv run python scripts/preflight.py                       # uses the azd env
    uv run python scripts/preflight.py --profile in-call
    uv run python scripts/preflight.py --location eastus2 --voicelive-location eastus2
    uv run python scripts/preflight.py --steps-only          # just print the plan
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

from channels import (
    ADMIN,
    AFTER,
    BOLD,
    CYAN,
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    get_profile,
    render_steps,
)

# Voice Live (preview) supported regions as of 2026-06.
# Keep in sync with:
# https://learn.microsoft.com/azure/ai-services/speech-service/regions#voice-live
VOICELIVE_REGIONS = {
    "eastus2",
    "swedencentral",
    "southeastasia",
    "centralindia",
    "westus2",
}

# Avatar (TTS avatar / video sync) regions.
# https://learn.microsoft.com/azure/ai-services/speech-service/regions#text-to-speech
AVATAR_REGIONS = {
    "westus2",
    "westeurope",
    "southeastasia",
    "northeurope",
    "swedencentral",
    "eastus2",
}

BASE_PROVIDERS = ["Microsoft.CognitiveServices", "Microsoft.App", "Microsoft.Search", "Microsoft.Bing"]


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    warn_only: bool = False


def _az() -> str:
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        print(f"{RED}FAIL{RESET}  Azure CLI (`az`) not found on PATH.")
        sys.exit(2)
    return exe


def _run(args: list[str]) -> tuple[int, str, str]:
    res = subprocess.run([_az(), *args], capture_output=True, text=True, check=False)
    return res.returncode, res.stdout, res.stderr


def _print_target(cfg: dict[str, str]) -> None:
    """Announce WHICH environment/subscription is about to be deployed into.

    ``azd`` only prompts for an environment name when none exists; after that it
    silently reuses the default recorded in ``.azure/config.json``. On a machine
    with several environments or subscriptions that makes it easy to provision --
    or tear down -- the wrong one without ever being asked. Printing the target
    before any resource is touched turns a silent default into a visible one.

    Deliberately not a prompt: hooks also run under ``azd up --no-prompt`` and in
    CI, where blocking on stdin would hang the deploy.
    """
    env_name = cfg.get("AZURE_ENV_NAME", "") or "(unset)"
    sub_id = cfg.get("AZURE_SUBSCRIPTION_ID", "") or "(unset)"
    rg = cfg.get("AZURE_RESOURCE_GROUP") or cfg.get("AZURE_RESOURCE_GROUP_NAME")
    # main.parameters.json maps resourceGroupName to ${AZURE_RESOURCE_GROUP_NAME} with
    # no default, so when it is unset azd ASKS during `azd up`. Printing a guess here
    # would state as settled something the user has not chosen yet.
    rg_label = rg if rg else f"{DIM}azd will ask during `azd up` (suggests rg-{env_name}){RESET}"
    sub_label = sub_id
    if sub_id and sub_id != "(unset)":
        code, out, _ = _run(["account", "show", "--subscription", sub_id, "--query", "name", "-o", "tsv"])
        if code == 0 and out.strip():
            sub_label = f"{out.strip()} ({sub_id})"
    print(f"{BOLD}Deploying into{RESET}")
    print(f"  environment  : {BOLD}{env_name}{RESET}")
    print(f"  subscription : {sub_label}")
    print(f"  resource grp : {rg_label}")
    print(f"{DIM}  Not what you expected? azd env select <name>, or azd env new <name>{RESET}\n")


_AZD_ENV_ERROR = ""


def _azd_env_values() -> dict[str, str]:
    global _AZD_ENV_ERROR
    exe = shutil.which("azd") or shutil.which("azd.exe")
    if not exe:
        return {}
    res = subprocess.run([exe, "env", "get-values"], capture_output=True, text=True, check=False)
    if res.returncode != 0:
        # Record WHY. Returning {} silently makes every downstream check report a
        # missing value, so a broken or deleted environment surfaces as something
        # unrelated ("No location") whose suggested fix cannot work either.
        _AZD_ENV_ERROR = (res.stderr or res.stdout or "").strip() or (
            f"`azd env get-values` exited {res.returncode}"
        )
        return {}
    values: dict[str, str] = {}
    for line in res.stdout.splitlines():
        key, sep, raw = line.partition("=")
        if sep:
            values[key.strip()] = raw.strip().strip('"')
    return values


def _config() -> dict[str, str]:
    """azd env values, overlaid with any non-empty process env (hook context)."""
    values = _azd_env_values()
    for key, val in os.environ.items():
        if val:
            values[key] = val
    return values


# ── Tooling ──────────────────────────────────────────────────────────────────
def check_tool(name: str, url: str) -> CheckResult:
    found = shutil.which(name) or shutil.which(f"{name}.exe") or shutil.which(f"{name}.cmd")
    return CheckResult(
        f"`{name}` on PATH",
        bool(found),
        found or "not found",
        fix=f"        Install it: {url}",
    )


def check_login() -> CheckResult:
    code, out, err = _run(["account", "show", "-o", "json"])
    if code != 0 or not out.strip():
        return CheckResult("az login", False, err.strip() or "not signed in", fix="        az login")
    acct = json.loads(out)
    user = acct.get("user", {}).get("name", "?")
    return CheckResult("az login", True, f"{user} / sub {acct.get('name')}")


# ── Regions ──────────────────────────────────────────────────────────────────
def check_voicelive(location: str) -> CheckResult:
    ok = location in VOICELIVE_REGIONS
    return CheckResult(
        "Voice Live region",
        ok,
        f"`{location}` is supported"
        if ok
        else f"`{location}` is NOT a Voice Live region. Supported: {sorted(VOICELIVE_REGIONS)}",
        fix=(
            "        Put the Foundry account in a supported region and keep the rest where you want it:\n"
            "        azd env set FOUNDRY_LOCATION eastus2"
        ),
    )


def check_avatar(location: str) -> CheckResult:
    ok = location in AVATAR_REGIONS
    return CheckResult(
        "Avatar region",
        ok,
        f"`{location}` supports TTS avatar"
        if ok
        else f"`{location}` does NOT support TTS avatar. Supported: {sorted(AVATAR_REGIONS)}",
        fix="        azd env set FOUNDRY_LOCATION eastus2",
    )


def check_aiservices(location: str) -> CheckResult:
    code, out, err = _run(
        [
            "cognitiveservices", "account", "list-skus",
            "--location", location, "--kind", "AIServices", "-o", "json",
        ]
    )
    if code != 0 or not out.strip():
        return CheckResult("Foundry AIServices SKU", False, err.strip() or "no AIServices SKUs returned")
    skus = json.loads(out)
    if not [s for s in skus if s.get("name") == "S0"]:
        return CheckResult("Foundry AIServices SKU", False, f"no S0 SKU in {location}")
    return CheckResult("Foundry AIServices SKU", True, f"S0 available in {location}")


def check_provider_registered(provider: str) -> CheckResult:
    code, out, _ = _run(["provider", "show", "-n", provider, "--query", "registrationState", "-o", "tsv"])
    state = out.strip()
    return CheckResult(
        f"Provider {provider}",
        state == "Registered",
        state or "unknown",
        fix=f"        az provider register -n {provider}",
    )


# ── Profile inputs ───────────────────────────────────────────────────────────
def check_required_inputs(profile, cfg: dict[str, str]) -> list[CheckResult]:
    results = []
    for req in profile.requires:
        value = cfg.get(req.name, "")
        shown = "set" if (req.secret and value) else (value or "not set")
        if not value and req.optional:
            shown = "not set — using the deployment default"
        results.append(
            CheckResult(
                req.name,
                bool(value),
                shown,
                fix=f"        {req.how}\n        azd env set {req.name} <value>",
                warn_only=req.optional,
            )
        )
    return results


def check_dns_label(cfg: dict[str, str], location: str) -> CheckResult | None:
    label = cfg.get("MEETING_BOT_DNS_LABEL", "").strip()
    if not label:
        return None
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,61}[a-z0-9]", label):
        return CheckResult(
            "Meeting bot DNS label",
            False,
            f"'{label}' is not a valid label",
            fix="        Lower-case letters, digits and hyphens; must start with a letter.",
        )
    sub = cfg.get("AZURE_SUBSCRIPTION_ID", "").strip()
    if not sub:
        code, out, _ = _run(["account", "show", "--query", "id", "-o", "tsv"])
        sub = out.strip() if code == 0 else ""
    if not sub:
        return CheckResult(
            "Meeting bot DNS label", True, f"'{label}' (availability not checked)", warn_only=True
        )
    url = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/providers/Microsoft.Network/locations/{location}/CheckDnsNameAvailability"
    )
    # Pass the query string via --url-parameters rather than embedding it: `az` is
    # a .cmd shim on Windows, so an `&` inside the URL is swallowed by cmd.exe and
    # the request silently loses its api-version.
    code, out, _ = _run(
        [
            "rest", "--method", "get", "--url", url,
            "--url-parameters", f"domainNameLabel={label}", "api-version=2023-09-01",
        ]
    )
    if code != 0 or not out.strip():
        return CheckResult(
            "Meeting bot DNS label", True, f"'{label}' (availability not checked)", warn_only=True
        )
    available = json.loads(out).get("available", True)
    return CheckResult(
        "Meeting bot DNS label",
        bool(available),
        f"'{label}.{location}.cloudapp.azure.com' is {'available' if available else 'TAKEN'}",
        fix="        Pick another: azd env set MEETING_BOT_DNS_LABEL <label>",
    )


def check_vm_password(cfg: dict[str, str]) -> CheckResult | None:
    pwd = cfg.get("MEETING_BOT_ADMIN_PASSWORD", "")
    if not pwd:
        return None
    classes = sum(bool(re.search(p, pwd)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    ok = 12 <= len(pwd) <= 123 and classes >= 3
    return CheckResult(
        "Windows VM password complexity",
        ok,
        "meets Azure requirements" if ok else "too weak — Azure rejects it at deploy time",
        fix="        12-123 characters using at least 3 of: lower, upper, digit, symbol.",
    )


def _azd_env_set(var: str, value: str) -> bool:
    """Persist a value into the azd environment. False if azd could not store it."""
    exe = shutil.which("azd") or shutil.which("azd.exe")
    if not exe:
        return False
    res = subprocess.run(
        [exe, "env", "set", var, value], capture_output=True, text=True, check=False
    )
    return res.returncode == 0


def _settle_subscription(cfg: dict[str, str]) -> str:
    """Choose the target subscription here rather than leaving it to `azd up`.

    azd resolves subscription before everything else, so an unset one is the FIRST
    thing it stops for. Settling it here also keeps preflight honest: the provider
    and quota checks below run against a subscription, and if that is not the one
    the deploy uses, a green preflight says nothing about the deploy.
    """
    existing = (cfg.get("AZURE_SUBSCRIPTION_ID") or "").strip()
    if existing or not sys.stdin.isatty():
        return existing
    code, out, _ = _run(["account", "show", "--query", "[id,name]", "-o", "tsv"])
    if code != 0 or not out.strip():
        return ""  # not signed in to az -- nothing to suggest, let azd ask
    parts = [p.strip() for p in out.strip().split("\t")]
    default = parts[0]
    name = parts[1] if len(parts) > 1 else ""
    print(f"{BOLD}No subscription set for this environment yet.{RESET}")
    print(f"{DIM}  Signed in to az as: {name or default}{RESET}")
    try:
        answer = input(f"Subscription id [{default}]: ").strip() or default
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    if _azd_env_set("AZURE_SUBSCRIPTION_ID", answer):
        print(f"{GREEN}  Saved: azd env set AZURE_SUBSCRIPTION_ID {answer}{RESET}\n")
    else:
        print(f"{YELLOW}  Could not save it; using it for this run only.{RESET}\n")
    return answer


def _settle_resource_group(cfg: dict[str, str]) -> str:
    """Choose the resource group here so `azd up` does not stop to ask for it.

    main.parameters.json maps resourceGroupName to ${AZURE_RESOURCE_GROUP_NAME}
    with no default, so azd prompts whenever it is unset. That is the last
    interactive stop between a green preflight and a finished deploy.
    """
    existing = (
        cfg.get("AZURE_RESOURCE_GROUP") or cfg.get("AZURE_RESOURCE_GROUP_NAME") or ""
    ).strip()
    if existing or not sys.stdin.isatty():
        return existing
    env_name = (cfg.get("AZURE_ENV_NAME") or "").strip()
    default = f"rg-{env_name}" if env_name else ""
    print(f"{BOLD}No resource group set for this environment yet.{RESET}")
    print(f"{DIM}  Everything this profile deploys lands in it. It is created if absent.{RESET}")
    try:
        prompt = f"Resource group [{default}]: " if default else "Resource group: "
        answer = (input(prompt).strip() or default).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    if not answer:
        return ""
    if _azd_env_set("AZURE_RESOURCE_GROUP_NAME", answer):
        print(f"{GREEN}  Saved: azd env set AZURE_RESOURCE_GROUP_NAME {answer}{RESET}\n")
    else:
        print(f"{YELLOW}  Could not save it; using it for this run only.{RESET}\n")
    return answer


def _prompt_for_location() -> str:
    """Ask for a region when the environment has none, and persist the answer.

    A freshly created azd environment holds only AZURE_ENV_NAME: azd does not
    collect a location until `azd up`. Preflight deliberately runs BEFORE that,
    so on a new environment it needed a value nothing had supplied yet and simply
    failed -- for anyone following the printed step plan in order.

    Only offered interactively. As the preprovision hook there is no TTY, and by
    then azd has already recorded a location, so this never runs there.
    """
    if not sys.stdin.isatty():
        return ""
    supported = sorted(VOICELIVE_REGIONS & AVATAR_REGIONS)
    default = "swedencentral" if "swedencentral" in supported else supported[0]
    print(f"{BOLD}No region set for this environment yet.{RESET}")
    print(f"{DIM}  azd asks for one during `azd up`, but preflight runs first.{RESET}")
    print(f"{DIM}  Supports both Voice Live and the avatar: {', '.join(supported)}{RESET}")
    try:
        answer = input(f"Region [{default}]: ").strip() or default
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    exe = shutil.which("azd") or shutil.which("azd.exe")
    if exe:
        if _azd_env_set("AZURE_LOCATION", answer):
            print(f"{GREEN}  Saved: azd env set AZURE_LOCATION {answer}{RESET}\n")
        else:
            print(f"{YELLOW}  Could not save it; using it for this run only.{RESET}\n")
    return answer


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--location", default=None, help="Main azd location. Defaults to AZURE_LOCATION.")
    ap.add_argument(
        "--voicelive-location",
        default=None,
        help="Foundry/Voice Live region. Defaults to FOUNDRY_LOCATION then --location.",
    )
    ap.add_argument("--profile", default=None, help="Channel profile. Defaults to DEPLOY_PROFILE.")
    ap.add_argument("--steps-only", action="store_true", help="Print the step plan and exit.")
    ap.add_argument(
        "--remaining",
        action="store_true",
        help="Print only the post-deployment steps and exit (used by the postprovision hook).",
    )
    args = ap.parse_args()

    cfg = _config()
    profile = get_profile(args.profile or cfg.get("DEPLOY_PROFILE"))

    if args.remaining:
        print(render_steps(profile, phases=(AFTER,)))
        return 0

    if args.steps_only:
        print(render_steps(profile))
        return 0

    if _AZD_ENV_ERROR:
        print(f"{RED}FAIL{RESET}  Could not read the azd environment, so none of its values are visible.")
        for line in _AZD_ENV_ERROR.splitlines():
            if line.strip():
                print(f"{DIM}        azd: {line.strip()}{RESET}")
        print()
        print("        Every value you set with `azd env set` lives in that environment.")
        print("        This means .azure/config.json still names an environment whose")
        print("        folder is no longer there — deleted, renamed, or never created on")
        print("        this machine (a fresh clone has no .azure/ at all).")
        print()
        print(f"        {BOLD}azd env list{RESET}              # what still exists")
        print(f"        {BOLD}azd env new <name>{RESET}        # start a fresh one, then re-set your values")
        print(f"        {BOLD}azd env select <name>{RESET}     # point at an existing one")
        return 2

    # Settle the whole deploy target here -- subscription, region, resource group --
    # so `azd up` has nothing left to stop and ask for. azd resolves them in this
    # order, and each one it cannot find is an interactive halt partway through a
    # deploy. Each is skipped when already set or when there is no TTY (the hook).
    sub = _settle_subscription(cfg)
    if sub:
        cfg["AZURE_SUBSCRIPTION_ID"] = sub

    location = (args.location or cfg.get("AZURE_LOCATION") or "").strip()
    if not location:
        location = _prompt_for_location()
    if not location:
        print(f"{RED}FAIL{RESET}  No location. Pass --location or run `azd env set AZURE_LOCATION <region>`.")
        return 2
    cfg["AZURE_LOCATION"] = location

    rg = _settle_resource_group(cfg)
    if rg:
        cfg["AZURE_RESOURCE_GROUP_NAME"] = rg

    voicelive_loc = (args.voicelive_location or cfg.get("FOUNDRY_LOCATION") or location).strip()

    print(f"{BOLD}Preflight — profile '{profile.key}' (channels {profile.channels}){RESET}")
    print(f"{DIM}  location={location}  foundry/voice-live={voicelive_loc}{RESET}\n")

    _print_target(cfg)

    # When reusing an existing Foundry account (BYO), the region checks below do
    # not apply — the account already exists and its region is not ours to pick.
    # Running them anyway would block deployments that work today.
    byo_foundry = bool(cfg.get("FOUNDRY_ACCOUNT_NAME", "").strip())

    checks: list[CheckResult] = [
        check_tool("az", "https://aka.ms/azure-cli"),
        check_tool("azd", "https://aka.ms/azd-install"),
        check_tool("uv", "https://docs.astral.sh/uv/getting-started/installation/"),
        check_login(),
    ]
    for provider in BASE_PROVIDERS + profile.providers:
        checks.append(check_provider_registered(provider))
    if byo_foundry:
        print(f"{DIM}  Reusing Foundry account '{cfg['FOUNDRY_ACCOUNT_NAME']}' — skipping region checks.{RESET}\n")
    else:
        checks += [
            check_aiservices(voicelive_loc),
            check_voicelive(voicelive_loc),
            check_avatar(voicelive_loc),
        ]
    checks += check_required_inputs(profile, cfg)
    for extra in (
        check_dns_label(cfg, location),
        check_vm_password(cfg),
    ):
        if extra is not None:
            checks.append(extra)

    failed: list[CheckResult] = []
    for c in checks:
        if c.ok:
            tag = f"{GREEN}OK  {RESET}"
        elif c.warn_only:
            tag = f"{YELLOW}WARN{RESET}"
        else:
            tag = f"{RED}FAIL{RESET}"
        print(f"{tag}  {c.name}: {c.detail}")
        if not c.ok and not c.warn_only:
            failed.append(c)

    if failed:
        print(f"\n{RED}{len(failed)} check(s) failed.{RESET} Fix these before `azd up`:\n")
        for c in failed:
            print(f"  {BOLD}{c.name}{RESET}")
            if c.fix:
                print(c.fix)
            print()
        return 1

    print(f"\n{GREEN}All preflight checks passed.{RESET}")
    print(render_steps(profile))

    if any(s.who == ADMIN for s in profile.steps):
        print(
            f"{YELLOW}Heads-up:{RESET} this profile has steps only an administrator can perform.\n"
            f"{DIM}  Confirm you can get them done before spending money on resources —\n"
            f"  docs/admin-checklist.md has a request you can forward verbatim.{RESET}\n"
        )
    print(f"{CYAN}Next:{RESET} azd up\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
