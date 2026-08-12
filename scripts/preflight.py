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
import base64
import ipaddress
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
    BINDING_ORDER,
    BINDINGS,
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
DEFAULT_AGENT_NAME = "AvatarAgent"

# ENABLE_AUDIT is read loosely because backend/config.py reads it loosely. Note
# this is NOT the set used for ENABLE_PRIVATE_NETWORKING -- see _is_true.
_AUDIT_TRUTHY = ("1", "true", "yes", "on")


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
    tenant = acct.get("tenantId", "?")
    # The tenant is shown because switching tenants is the one change that leaves
    # `az` correct and every other credential store stale, and it is invisible
    # from the account name alone.
    return CheckResult("az login", True, f"{user} / sub {acct.get('name')} / tenant {tenant}")


def check_azd_login() -> CheckResult:
    """Verify `azd` itself can get a token for the environment's subscription.

    `az` and `azd` keep entirely separate credential stores. Signing into a new
    tenant with `az login` leaves azd authenticated to the old one, and azd also
    caches AZURE_SUBSCRIPTION_ID in the environment, so it keeps targeting a
    subscription the new identity may not be able to see. Checking only `az`
    reports green and the failure surfaces minutes later, inside `azd up`, as an
    error naming an account the user thought they had stopped using.

    `azd auth token` is the same call `azd up` makes, so it fails here for the
    same reason it would fail there — in about a second rather than after
    provisioning has started.
    """
    exe = shutil.which("azd") or shutil.which("azd.exe")
    if not exe:
        return CheckResult("azd login", True, "azd not on PATH — skipped", warn_only=True)

    res = subprocess.run(
        [exe, "auth", "token", "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if res.returncode != 0:
        raw = (res.stderr or res.stdout or "").strip()
        # azd emits JSON-wrapped console messages; its own text is the best
        # guidance available, so surface it rather than paraphrasing.
        message = raw
        for line in raw.splitlines():
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            found = payload.get("data", {}).get("message", "")
            if found:
                message = found.strip()
                break
        message = " ".join(message.split())
        if len(message) > 240:
            message = message[:237] + "..."
        return CheckResult(
            "azd login",
            False,
            message or f"`azd auth token` exited {res.returncode}",
            fix=(
                "        azd auth login\n"
                "        If you switched tenants, name it explicitly:\n"
                "        azd auth login --tenant-id <tenant-id>\n"
                "        If the environment still points at the old subscription:\n"
                "        azd env set AZURE_SUBSCRIPTION_ID <subscription-id>"
            ),
        )

    # The token proves azd can authenticate. Compare the tenant it actually got
    # against `az`, because the two can succeed independently against different
    # tenants and still produce a deployment in the wrong place.
    azd_tenant = ""
    try:
        token = json.loads(res.stdout).get("token", "")
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        azd_tenant = json.loads(base64.urlsafe_b64decode(body)).get("tid", "")
    except Exception:
        # Never fail the check on token shape: the token is proof enough, and the
        # claim is only used to make a mismatch legible.
        azd_tenant = ""

    code, out, _ = _run(["account", "show", "--query", "tenantId", "-o", "tsv"])
    az_tenant = out.strip() if code == 0 else ""

    if azd_tenant and az_tenant and azd_tenant != az_tenant:
        return CheckResult(
            "azd login",
            False,
            f"azd is signed into tenant {azd_tenant}, `az` into {az_tenant}",
            fix=(
                "        The two will deploy to different places. Point azd at the\n"
                "        tenant you want:\n"
                f"        azd auth login --tenant-id {az_tenant}"
            ),
        )
    detail = "authenticated"
    if azd_tenant:
        detail += f" / tenant {azd_tenant}"
        if az_tenant:
            detail += " (matches `az`)"
    return CheckResult("azd login", True, detail)


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


def check_voice_binding(cfg: dict[str, str]) -> list[CheckResult]:
    """Validate the brain choice and the config that choice implies.

    The binding is set by scripts/set_profile.py alongside the channel. It is
    checked here because each mode has different runtime configuration. Agent
    mode uses the same default name as the infrastructure template when the user
    has not chosen one; model mode silently drops web search when Web IQ is
    unconfigured, because Grounding with Bing cannot follow into model mode.
    """
    raw = cfg.get("VOICE_BINDING", "").strip().lower()
    binding = raw or "agent"

    if binding not in BINDINGS:
        return [
            CheckResult(
                "Voice binding",
                False,
                f"{raw!r} is not a valid binding",
                fix="        Pick one of: " + ", ".join(BINDING_ORDER) + "\n"
                    "        uv run python scripts/set_profile.py",
            )
        ]

    results = [
        CheckResult(
            "Voice binding",
            True,
            f"{binding} — {BINDINGS[binding].title}"
            + ("" if raw else " (default; not set explicitly)"),
        )
    ]

    if binding == "agent":
        configured_agent = cfg.get("AGENT_NAME", "").strip()
        agent = configured_agent or DEFAULT_AGENT_NAME
        results.append(
            CheckResult(
                "Agent mode: AGENT_NAME",
                True,
                agent + ("" if configured_agent else " (built-in default)"),
            )
        )
    else:
        model = cfg.get("VOICELIVE_MODEL", "").strip()
        results.append(
            CheckResult(
                "Model mode: VOICELIVE_MODEL",
                True,
                model or "not set — using the built-in default",
                warn_only=True,
            )
        )
        web_iq = cfg.get("WEBIQ_API_KEY", "").strip()
        results.append(
            CheckResult(
                "Model mode: Web IQ",
                True,
                "key set"
                if web_iq
                else "no key — the app will try its Entra identity at startup",
                fix="        Grounding with Bing cannot follow into model mode, so web\n"
                    "        search runs through Web IQ. With no key the app asks for a\n"
                    "        Web IQ token at startup and only registers the tool if one\n"
                    "        comes back; otherwise the avatar answers from AI Search\n"
                    "        alone. Set a key to skip that check:\n"
                    "        azd env set WEBIQ_API_KEY <key>",
                warn_only=True,
            )
        )
        # Bing is now gated on the binding in resources.bicep, so model mode no
        # longer provisions it whatever DEPLOY_BING_GROUNDING says. Report that
        # as a fact rather than a warning — there is nothing for the user to fix.
        if cfg.get("DEPLOY_BING_GROUNDING", "true").strip().lower() != "false":
            results.append(
                CheckResult(
                    "Model mode: Bing grounding",
                    True,
                    "not deployed — model mode cannot attach it",
                    fix="        Voice Live accepts only FUNCTION and MCP tools in model mode,\n"
                        "        so the managed Grounding-with-Bing tool has nothing to attach\n"
                        "        to. Bicep skips it under this binding even though\n"
                        "        DEPLOY_BING_GROUNDING is set. Web IQ is the web tool here.",
                    warn_only=True,
                )
            )

    return results


def check_audit(cfg: dict[str, str]) -> list[CheckResult]:
    """Validate the conversation audit trail (docs/audit.md).

    Audit is opt-in and off by default, and until now nothing in preflight
    mentioned it. That was a real gap: turning it on adds a Cosmos DB account
    that no other check knows about, so a subscription without
    ``Microsoft.DocumentDB`` registered only failed once provisioning was
    already underway.

    Defaults mirror backend/config.py deliberately — AUDIT_SINK defaults to
    ``cosmos`` and AUDIT_SINK_FALLBACK to ``error``, so an operator who sets
    only ENABLE_AUDIT=true gets the Cosmos path and an app that refuses to
    start if that path is broken. Both are worth stating out loud before a
    deploy rather than after one.
    """
    raw = cfg.get("ENABLE_AUDIT", "").strip()
    if raw.lower() not in _AUDIT_TRUTHY:
        return [
            CheckResult(
                "Audit trail",
                True,
                "off — no Cosmos account will be created"
                + (f" (ENABLE_AUDIT={raw})" if raw else " (ENABLE_AUDIT not set)"),
            )
        ]

    sink = cfg.get("AUDIT_SINK", "").strip().lower() or "cosmos"
    fallback = cfg.get("AUDIT_SINK_FALLBACK", "").strip().lower() or "error"
    results = [CheckResult("Audit trail", True, f"on — sink={sink}, fallback={fallback}")]

    # Ordered deliberately. The public-access probe can turn private networking ON
    # for this run, and the address-space validation in check_private_networking
    # must see the value we end up with, not the one this run started with.
    results.extend(check_cosmos_public_access(cfg))

    # Checked before the sink short-circuit below on purpose: the template gates
    # the VNet on enableAudit alone, not on the sink, so a non-Cosmos sink still
    # gets the environment replaced. Skipping this here would let that happen
    # with no warning and no address-space validation.
    results.extend(check_private_networking(cfg))

    if sink != "cosmos":
        return results

    # Only the Cosmos sink provisions infrastructure, so this is the only sink
    # whose provider can be missing.
    results.append(check_provider_registered("Microsoft.DocumentDB"))

    # The failure this is really guarding against is not a missing provider but
    # a governed tenant: a Modify policy rewrites publicNetworkAccess to
    # Disabled after ARM accepts the template, so provisioning reports success
    # and the container app then cannot reach the account it was given. It
    # cannot be detected reliably before the account exists, so preflight only
    # names it and postprovision asserts the deployed truth.
    #
    # Stated in `detail` rather than `fix` because only failing checks print
    # their fix, and this is a correct default rather than a problem.
    #
    # Mirrors backend/audit/__init__.py:_fallback_or_raise: anything that is not
    # an explicit 'file' or 'none' is fail-closed, so a typo is reported as the
    # fail-closed posture it actually produces.
    if fallback not in ("file", "none"):
        results.append(
            CheckResult(
                "Audit trail: fail-closed",
                True,
                f"AUDIT_SINK_FALLBACK={fallback} — the app refuses to start if Cosmos is "
                "unreachable (set it to `file` to degrade instead)",
            )
        )

    return results


# Deliberately narrower than the truthy set used elsewhere in this file: the
# template gates on toLower(x) == 'true', and a script that disagreed with the
# template about whether the flag is on would report a private deployment while
# ARM built a public one — and then postprovision would fail a deploy that is
# perfectly healthy. The two must mean the same thing.
def _is_true(value: str) -> bool:
    return value.strip().lower() == "true"


def check_private_networking(cfg: dict[str, str]) -> list[CheckResult]:
    """Validate ENABLE_PRIVATE_NETWORKING before ARM sees it (#122).

    Two things are worth catching here rather than mid-deploy. The address space
    is one: the template carves the apps subnet out as the first ``/23`` and the
    private endpoint subnet as the third ``/24``, so anything narrower than a
    ``/22`` produces a subnet that does not fit, and ``cidrSubnet`` fails partway
    through provisioning with an error that does not name the setting that
    caused it.

    The other is that this flag replaces the Container Apps environment, which
    Azure will not do in place. An operator turning it on against a live
    deployment should hear that before the deploy fails, not after.
    """
    raw = cfg.get("ENABLE_PRIVATE_NETWORKING", "").strip()
    if not raw or raw.lower() == "false":
        return []

    if not _is_true(raw):
        # 'yes', '1' and 'on' read as on to a human and as off to the template.
        # Saying so here is the difference between a one-line fix and a deploy
        # that provisions the wrong shape and fails a check downstream.
        return [
            CheckResult(
                "Private networking",
                False,
                f"ENABLE_PRIVATE_NETWORKING={raw!r} is neither 'true' nor 'false', so the "
                "template will treat it as off",
                fix="        azd env set ENABLE_PRIVATE_NETWORKING true",
            )
        ]

    results: list[CheckResult] = []

    if not _is_true(cfg.get("ENABLE_AUDIT", "")):
        results.append(
            CheckResult(
                "Private networking",
                False,
                "ENABLE_PRIVATE_NETWORKING is on but ENABLE_AUDIT is not 'true'. There is no "
                "Cosmos account to reach privately, so the VNet would be created for nothing",
                fix="        azd env set ENABLE_AUDIT true\n"
                    "        (or turn private networking back off)",
            )
        )
        return results

    prefix = cfg.get("VNET_ADDRESS_PREFIX", "").strip() or "10.100.0.0/16"

    try:
        network = ipaddress.ip_network(prefix, strict=True)
    except ValueError as exc:
        return [
            CheckResult(
                "Private networking: address space",
                False,
                f"VNET_ADDRESS_PREFIX={prefix!r} is not a valid CIDR block ({exc})",
                fix="        azd env set VNET_ADDRESS_PREFIX 10.100.0.0/16",
            )
        ]

    if network.version != 4:
        return [
            CheckResult(
                "Private networking: address space",
                False,
                f"VNET_ADDRESS_PREFIX={prefix} is IPv6; Container Apps needs an IPv4 range",
                fix="        azd env set VNET_ADDRESS_PREFIX 10.100.0.0/16",
            )
        ]

    if network.prefixlen > 22:
        return [
            CheckResult(
                "Private networking: address space",
                False,
                f"VNET_ADDRESS_PREFIX={prefix} is too small — the template needs a /23 for "
                "the apps subnet and a /24 for private endpoints, so /22 is the minimum",
                fix="        azd env set VNET_ADDRESS_PREFIX 10.100.0.0/16",
            )
        ]

    # Container Apps rejects these outright: they collide with ranges reserved by
    # the AKS layer underneath the environment, and the workload-profile
    # environment reserves the 100.100.x blocks on top of that.
    reserved = [
        "169.254.0.0/16", "172.30.0.0/16", "172.31.0.0/16", "192.0.2.0/24",
        "100.100.0.0/17", "100.100.128.0/19", "100.100.160.0/19", "100.100.192.0/19",
    ]
    clashes = [r for r in reserved if network.overlaps(ipaddress.ip_network(r))]
    if clashes:
        return [
            CheckResult(
                "Private networking: address space",
                False,
                f"VNET_ADDRESS_PREFIX={prefix} overlaps ranges Container Apps reserves "
                f"({', '.join(clashes)})",
                fix="        azd env set VNET_ADDRESS_PREFIX 10.100.0.0/16",
            )
        ]

    results.append(
        CheckResult(
            "Private networking",
            True,
            f"on — Cosmos reached over a private endpoint, VNet {prefix}",
        )
    )
    results.append(
        CheckResult(
            "Private networking: environment",
            True,
            "the Container Apps environment is VNet-injected, which Azure cannot do in "
            "place — an existing environment must be deleted first and the app's FQDN "
            "will change (docs/audit.md#private-networking)",
        )
    )
    return results


def _cosmos_accounts(subscription: str) -> list[dict] | None:
    """Every Cosmos account in the subscription, or None if they could not be read.

    None means "unknown" and never means "none exist" — the caller must not read
    an unreadable subscription as a clean one.
    """
    args = [
        "cosmosdb", "list",
        "--query", "[].{name:name,rg:resourceGroup,pna:publicNetworkAccess}",
        "-o", "json",
    ]
    if subscription:
        args += ["--subscription", subscription]
    code, out, _ = _run(args)
    if code != 0 or not out.strip():
        return None
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


_PRIVATE_FIX = (
    "        azd env set ENABLE_PRIVATE_NETWORKING true\n"
    "        Deploys Cosmos behind a private endpoint the app can reach.\n"
    "        Replaces the Container Apps environment — docs/audit.md#private-networking"
)


def _require_private_networking(
    cfg: dict[str, str], reason: str, *, blocking: bool
) -> list[CheckResult]:
    """Report a subscription that closes Cosmos to public traffic, and offer the fix.

    Interactive runs can turn the flag on here and persist it. As the preprovision
    hook there is no TTY, so there this reports and moves on.

    ``blocking`` separates proof from inference. This environment's own account
    being Disabled is proof the app cannot start, and stopping the deploy is the
    only useful thing to do with that. Some *other* account being Disabled is a
    strong hint about the subscription, not proof about this deploy — a shared
    subscription can hold a private Cosmos for reasons that have nothing to do
    with policy — so that case warns loudly and lets the deploy through.
    """
    if sys.stdin.isatty():
        print(f"{YELLOW}Cosmos public access:{RESET} {reason}.")
        print(f"{DIM}  Private networking puts the account behind a private endpoint the app can reach,{RESET}")
        print(f"{DIM}  so `Disabled` becomes the steady state instead of an outage. About $33/month.{RESET}")
        print(f"{DIM}  It also replaces the Container Apps environment, which changes the app's FQDN.{RESET}")
        try:
            answer = input("Deploy Cosmos with private networking? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = "n"
        if answer in ("", "y", "yes"):
            if _azd_env_set("ENABLE_PRIVATE_NETWORKING", "true"):
                # Mutating cfg so check_private_networking, which runs next,
                # validates the address space for the deploy we just chose.
                cfg["ENABLE_PRIVATE_NETWORKING"] = "true"
                print(f"{GREEN}  Saved: azd env set ENABLE_PRIVATE_NETWORKING true{RESET}\n")
                return [
                    CheckResult(
                        "Cosmos public access",
                        True,
                        f"{reason} — private networking turned on, so Cosmos will be deployed "
                        "behind a private endpoint",
                    )
                ]
            print(f"{YELLOW}  Could not save it; set it by hand before deploying.{RESET}\n")

    # A warning's fix block is never printed by main(), so the remedy has to
    # travel in the detail or nobody sees it.
    detail = reason if blocking else f"{reason}. Fix: azd env set ENABLE_PRIVATE_NETWORKING true"
    return [
        CheckResult("Cosmos public access", False, detail, fix=_PRIVATE_FIX, warn_only=not blocking)
    ]


# Reads deployed Cosmos accounts rather than policy assignments, on evidence: on
# the subscription this was built against, `az policy assignment list
# --disable-scope-strict-match` returns only the three ASC defaults and says
# nothing about Cosmos, because the governing assignment lives at a management
# group the deploying identity cannot read. Scanning assignments would therefore
# report "no policy" on precisely the subscription that has one. The accounts
# themselves are readable, and their publicNetworkAccess is the effect of the
# policy rather than a guess at it.
def check_cosmos_public_access(cfg: dict[str, str]) -> list[CheckResult]:
    """Warn when the subscription is closing Cosmos accounts to public traffic (#122).

    This is the check that would have prevented #122. A management-group policy
    sweep set publicNetworkAccess=Disabled on the audit account overnight; the
    audit sink is fail-closed, so warm() failed, the revision never became
    healthy, and the app went down — while Container Apps still reported the
    deploy as successful. Nothing in the deploy path mentioned any of it.

    The signal a deployer can actually see is the state of Cosmos accounts that
    already exist. If this environment's own account is already Disabled the app
    is broken right now, which is not a prediction. If some other account in the
    subscription is Disabled, the platform closes Cosmos accounts and this one
    will be swept too.
    """
    if cfg.get("ENABLE_AUDIT", "").strip().lower() not in _AUDIT_TRUTHY:
        return []
    if (cfg.get("AUDIT_SINK", "").strip().lower() or "cosmos") != "cosmos":
        return []

    private_on = _is_true(cfg.get("ENABLE_PRIVATE_NETWORKING", ""))
    accounts = _cosmos_accounts(cfg.get("AZURE_SUBSCRIPTION_ID", "").strip())

    if accounts is None:
        return [
            CheckResult(
                "Cosmos public access",
                True,
                "could not read the subscription's Cosmos accounts, so the policy posture is "
                "unknown — see docs/audit.md#private-networking",
                warn_only=True,
            )
        ]

    # The account name is cosmos-${environmentName}-${resourceToken}, and
    # resourceToken is uniqueString(subscription, env, location) — not something
    # this script can recompute, so match on the part that is knowable.
    env_name = cfg.get("AZURE_ENV_NAME", "").strip().lower()
    prefix = f"cosmos-{env_name}-" if env_name else ""
    mine = {
        (a.get("name") or "")
        for a in accounts
        if prefix and (a.get("name") or "").lower().startswith(prefix)
    }
    disabled = [a for a in accounts if (a.get("pna") or "") == "Disabled"]
    disabled_mine = [a for a in disabled if (a.get("name") or "") in mine]
    disabled_other = [a for a in disabled if (a.get("name") or "") not in mine]

    if disabled_mine:
        name = disabled_mine[0].get("name") or "the audit account"
        if private_on:
            return [
                CheckResult(
                    "Cosmos public access",
                    True,
                    f"{name} already has public access Disabled — private networking is on, so "
                    "this deploy gives it the private endpoint it has been missing",
                )
            ]
        return _require_private_networking(
            cfg,
            f"{name} has public network access Disabled. The audit sink is fail-closed, so the "
            "app cannot start until it can reach that account privately",
            blocking=True,
        )

    if disabled_other:
        sample = ", ".join(sorted((a.get("name") or "") for a in disabled_other)[:3])
        more = "" if len(disabled_other) <= 3 else f", +{len(disabled_other) - 3} more"
        observed = (
            f"{len(disabled_other)} of {len(accounts)} Cosmos accounts in this subscription have "
            f"public network access Disabled ({sample}{more}), so a platform policy is closing them"
        )
        if private_on:
            return [
                CheckResult(
                    "Cosmos public access",
                    True,
                    f"{observed}; private networking is on, so this deploy already matches",
                )
            ]
        return _require_private_networking(
            cfg,
            f"{observed} — this deploy's account will very likely be closed the same way",
            blocking=False,
        )

    if private_on:
        return [
            CheckResult(
                "Cosmos public access",
                True,
                "will be Disabled on the audit account, which the app reaches over a private "
                "endpoint instead",
            )
        ]

    if accounts:
        return [
            CheckResult(
                "Cosmos public access",
                True,
                f"public on all {len(accounts)} existing Cosmos account(s) here — no sweep observed",
            )
        ]

    return [
        CheckResult(
            "Cosmos public access",
            True,
            "no existing Cosmos accounts to compare against, so a private-link policy cannot be "
            "ruled out — docs/audit.md#private-networking",
            warn_only=True,
        )
    ]


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
        check_azd_login(),
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
    checks += check_voice_binding(cfg)
    checks += check_audit(cfg)
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
