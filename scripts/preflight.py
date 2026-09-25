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
    uv run python scripts/preflight.py --record-web-tool     # only pin AGENT_WEB_TOOL
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
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
    DEFAULT_WEB_TOOL,
    DIM,
    GREEN,
    RED,
    RESET,
    WEB_TOOL_DEFAULT,
    WEB_TOOL_EXISTING,
    WEB_TOOL_ORDER,
    WEB_TOOL_SET,
    YELLOW,
    get_profile,
    render_steps,
    resolve_web_tool,
    web_tool_reason_note,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import trusted_sites  # noqa: E402

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

# Agent mode's web tool (agentWebTool in infra/main.bicep), chosen in set_profile.py.
AGENT_WEB_TOOLS = tuple(WEB_TOOL_ORDER)
AGENT_WEB_TOOL_APP_PREFIX = "avatar-forge-web-tool-"
AGENT_WEB_TOOL_MIN_KEY_CHARS = 32

# Web IQ rejects a longer query outright (WEBIQ_MAX_QUERY_CHARS in
# backend/voice/tools.py), and the site: clause is spent from the same budget.
WEBIQ_QUERY_CHAR_LIMIT = 1000
# Below this much room the app starts trimming ordinary spoken questions.
WEBIQ_MIN_QUESTION_CHARS = 300
TRUSTED_SITES_DOC = "docs/configuration.md#trusted-web-sources"


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


def _agent_binding(cfg: dict[str, str]) -> bool:
    return (cfg.get("VOICE_BINDING", "").strip().lower() or "agent") == "agent"


def check_agent_web_tool(cfg: dict[str, str]) -> list[CheckResult]:
    """Validate agent mode's web tool choice and how the agent will authenticate.

    ``webiq`` gives the agent an OpenAPI tool that calls the app's own
    /api/tools/search-web, which runs the same allow-listed Web IQ search as
    model mode. That route is on the public internet, so it only answers a
    caller it can verify: a shared key if AGENT_WEB_TOOL_KEY is set, otherwise
    an Entra token from Foundry's managed identity. The token needs an audience
    (an app registration); when none is set, preflight creates one after the
    checks pass — see _settle_agent_web_tool_auth.
    """
    raw = cfg.get("AGENT_WEB_TOOL", "").strip().lower()
    tool, reason = resolve_web_tool(cfg)
    if tool not in AGENT_WEB_TOOLS:
        return [
            CheckResult(
                "Agent web tool",
                False,
                f"{raw!r} is not a valid web tool",
                fix="        Pick one of: " + ", ".join(AGENT_WEB_TOOLS) + "\n"
                        "        uv run python scripts/set_profile.py\n"
                        "        or: azd env set AGENT_WEB_TOOL webiq",
            )
        ]

    if not _agent_binding(cfg):
        if reason != WEB_TOOL_SET or tool == "webiq":
            return []
        return [
            CheckResult(
                "Agent web tool",
                True,
                f"{tool} — ignored in model mode, where Web IQ is the web tool",
                warn_only=True,
            )
        ]

    if tool == "bing":
        note = web_tool_reason_note(reason)
        return [
            CheckResult(
                "Agent web tool",
                True,
                "bing — Grounding with Bing Custom Search" + (f" ({note})" if note else ""),
            )
        ]

    if cfg.get("FOUNDRY_ACCOUNT_NAME", "").strip():
        return [
            CheckResult(
                "Agent web tool",
                False,
                "webiq needs the Foundry account this template creates, not an existing one",
                fix="        The tool's key connection and caller identities come from the\n"
                        "        Foundry account provisioned here. With FOUNDRY_ACCOUNT_NAME set\n"
                        "        the agent would be created with no web tool at all. Either:\n"
                        "        azd env set AGENT_WEB_TOOL bing\n"
                        "        or let this deployment create the Foundry account.",
            )
        ]

    results = [
        CheckResult(
            "Agent web tool",
            True,
            "webiq — trusted-site Web IQ search through the app's /api/tools/search-web"
            + (" (the default)" if reason == WEB_TOOL_DEFAULT else ""),
        )
    ]

    key = cfg.get("AGENT_WEB_TOOL_KEY", "").strip()
    audience = cfg.get("AGENT_WEB_TOOL_AUDIENCE", "").strip()
    if key:
        strong = len(key) >= AGENT_WEB_TOOL_MIN_KEY_CHARS
        results.append(
            CheckResult(
                "Agent web tool: caller auth",
                strong,
                "shared key (AGENT_WEB_TOOL_KEY), held by a Foundry connection"
                if strong
                else f"AGENT_WEB_TOOL_KEY is {len(key)} characters — the route is public, "
                         f"use at least {AGENT_WEB_TOOL_MIN_KEY_CHARS}",
                fix='        python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
                        "        azd env set AGENT_WEB_TOOL_KEY <that value>\n"
                        "        Or clear it to use managed identity instead:\n"
                        '        azd env set AGENT_WEB_TOOL_KEY ""',
            )
        )
    elif audience:
        results.append(
            CheckResult(
                "Agent web tool: caller auth",
                True,
                f"managed identity — Foundry presents an Entra token for {audience}",
            )
        )
    else:
        results.append(
            CheckResult(
                "Agent web tool: caller auth",
                True,
                "managed identity — an app registration for the token audience is "
                "created below (a generated key is used if the directory refuses)",
            )
        )

    results.append(
        CheckResult(
            "Agent web tool: Web IQ access",
            True,
            "key set"
            if cfg.get("WEBIQ_API_KEY", "").strip()
            else "no key — the app will try its Entra identity at startup",
            warn_only=True,
        )
    )
    return results


def check_trusted_web_sites(cfg: dict[str, str]) -> list[CheckResult]:
    """Where the web tools may search: TRUSTED_WEB_SITES.

    Unset never blocks. It is a supported choice, and Web IQ then searches the
    open web. It does warn when Bing is the agent's web tool, because Bing Custom
    Search has no open-web mode, so Bing is not deployed and the agent gets no
    web tool; and when a deployed environment never set it, because until the
    variable existed infra/main.bicep supplied a list, so the next deploy changes
    what it searches. `azd env set TRUSTED_WEB_SITES ""` records the open web as
    a choice, which silences that second warning: azd keeps the empty key.
    """
    raw = cfg.get("TRUSTED_WEB_SITES", "").strip()
    chosen = "TRUSTED_WEB_SITES" in cfg
    legacy = cfg.get("WEBIQ_ALLOWED_DOMAINS", "").strip()
    agent = _agent_binding(cfg)
    tool = resolve_web_tool(cfg)[0]
    uses_web_iq = not agent or tool == "webiq"
    uses_bing = (
        agent and tool == "bing"
        and cfg.get("DEPLOY_BING_GROUNDING", "true").strip().lower() != "false"
    )
    deployed = bool(cfg.get("SERVICE_APP_URI", "").strip())
    name = "Trusted web sites"
    example = '        azd env set TRUSTED_WEB_SITES "+www.example.com/investors,news.example.com"\n'
    results: list[CheckResult] = []

    if legacy:
        move = (
            "        Clear the old one; TRUSTED_WEB_SITES is already set:\n"
            if raw
            else "        Move the value across, then clear the old one:\n"
                 f'        azd env set TRUSTED_WEB_SITES "{legacy}"\n'
        )
        results.append(
            CheckResult(
                f"{name}: WEBIQ_ALLOWED_DOMAINS",
                False,
                "set, but deployments no longer read it; TRUSTED_WEB_SITES replaced it",
                fix="        One list now scopes every web tool.\n" + move
                    + '        azd env set WEBIQ_ALLOWED_DOMAINS ""',
                warn_only=True,
            )
        )

    sites = trusted_sites.entries(raw)
    items = [item.strip() for item in raw.split(",") if item.strip()]
    skipped = [item for item in items if item.startswith("-")]
    malformed = [
        site for site, _ in sites
        if not trusted_sites.host(site) or any(ch.isspace() for ch in site)
    ]
    if skipped or malformed:
        parts = []
        if skipped:
            parts.append("skipped " + ", ".join(repr(s) for s in skipped))
        if malformed:
            parts.append("malformed " + ", ".join(repr(s) for s in malformed))
        results.append(
            CheckResult(
                f"{name}: entries",
                False,
                "; ".join(parts),
                fix="        Each entry is a host or URL with an optional path, separated by\n"
                    "        commas; a leading + marks a SuperBoost source. Exclusions (-) are\n"
                    "        not supported, so they are left out. A malformed entry is still\n"
                    f"        sent, and Bing may reject it. See {TRUSTED_SITES_DOC}.",
                warn_only=True,
            )
        )

    if not sites:
        state = "has no usable entries" if raw else ("set empty" if chosen else "not set")
        if uses_bing:
            was_scoped = deployed and not chosen and cfg.get("BING_CUSTOM_CONFIG_NAME", "").strip()
            results.append(
                CheckResult(
                    name,
                    False,
                    f"{state}: Bing needs a site list, so the agent gets NO web tool",
                    fix="        Bing Custom Search has no open-web mode, so without a list it is\n"
                        "        not deployed and the agent answers from your documents alone.\n"
                        + (
                            "        This environment's agent searches the list that used to be built\n"
                            "        into infra/main.bicep. The next deploy removes its web tool; the\n"
                            "        Bing account stays, and keeps billing.\n"
                            if was_scoped else ""
                        )
                        + f"        Set your sites ({TRUSTED_SITES_DOC} has MTN's list):\n"
                        + example
                        + "        Or use Web IQ, which can search the open web:\n"
                          "        uv run python scripts/set_profile.py",
                    warn_only=True,
                )
            )
        elif uses_web_iq:
            results.append(
                CheckResult(
                    name,
                    chosen or not deployed,
                    f"{state}: Web IQ searches the open web",
                    fix="        This environment was deployed before TRUSTED_WEB_SITES existed,\n"
                        "        when infra/main.bicep supplied a list, so this deploy opens its web\n"
                        f"        search to the whole web. To keep a list ({TRUSTED_SITES_DOC}\n"
                        "        has MTN's):\n"
                        + example
                        + "        Or keep the open web and silence this warning:\n"
                          '        azd env set TRUSTED_WEB_SITES ""',
                    warn_only=True,
                )
            )
        return results

    hosts = trusted_sites.hosts(raw)
    boosted = sum(1 for _, boost in sites if boost)
    cost = trusted_sites.scope_chars(hosts)
    detail = f"{len(sites)} site(s)"
    if uses_bing:
        detail += f", {boosted} SuperBoost"
    if uses_web_iq:
        detail += f"; Web IQ scoped to {len(hosts)} host(s), {cost} of {WEBIQ_QUERY_CHAR_LIMIT} query characters"
    results.append(CheckResult(name, True, detail))

    if uses_web_iq and cost >= WEBIQ_QUERY_CHAR_LIMIT:
        results.append(
            CheckResult(
                f"{name}: Web IQ query budget",
                False,
                f"the site list alone needs {cost} characters of Web IQ's "
                f"{WEBIQ_QUERY_CHAR_LIMIT}, so no search would carry the question",
                fix="        Web IQ scopes a search with site: operators in the query text, and\n"
                    "        the question has to fit in what is left. Remove hosts; paths on\n"
                    "        the same host cost nothing extra, because Web IQ uses the host.",
            )
        )
    elif uses_web_iq and WEBIQ_QUERY_CHAR_LIMIT - cost < WEBIQ_MIN_QUESTION_CHARS:
        results.append(
            CheckResult(
                f"{name}: Web IQ query budget",
                False,
                f"leaves {WEBIQ_QUERY_CHAR_LIMIT - cost} characters for the question; "
                "longer ones are trimmed to fit",
                fix="        Remove hosts that no longer earn their place.",
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
    if raw.lower() not in ("1", "true", "yes", "on"):
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


def _az_error(err: str, fallback: str) -> str:
    for line in (err or "").splitlines():
        line = line.strip()
        if line:
            return line.removeprefix("ERROR:").strip()
    return fallback


def _ensure_web_tool_app(cfg: dict[str, str], display_name: str) -> tuple[str, str]:
    """Find or create the app registration that names the web tool's token audience.

    Returns ``(app_id, error)``; success is an app ID with no error. The app has
    no secrets, roles or permissions — it exists only so Foundry's managed
    identity can ask Entra for a token *for this API*, which the app then checks.
    ``api://<appId>`` is used as the identifier URI because tenant policy always
    allows that form.

    Idempotent: a re-run finds the app by AGENT_WEB_TOOL_APP_ID, else by display
    name, and only fills in what is missing (identifier URI, service principal).
    """
    app_id = cfg.get("AGENT_WEB_TOOL_APP_ID", "").strip()
    uris: list[str] = []
    if app_id:
        code, out, _ = _run(["ad", "app", "show", "--id", app_id, "--query", "identifierUris", "-o", "json"])
        if code == 0:
            uris = json.loads(out or "[]") or []
        else:
            app_id = ""
    if not app_id:
        code, out, err = _run(
            ["ad", "app", "list", "--display-name", display_name,
             "--query", "[].{appId:appId,name:displayName,uris:identifierUris}", "-o", "json"]
        )
        if code != 0:
            return "", _az_error(err, "could not search the directory for app registrations")
        # --display-name is a prefix match; only an exact name is ours.
        found = [a for a in json.loads(out or "[]") if a.get("name") == display_name]
        if found:
            app_id, uris = found[0]["appId"], found[0].get("uris") or []
    if app_id:
        print(f"{DIM}  Reusing app registration {display_name} ({app_id}){RESET}")
    else:
        args = ["ad", "app", "create", "--display-name", display_name,
                "--sign-in-audience", "AzureADMyOrg", "--query", "appId", "-o", "tsv"]
        reference = cfg.get("AGENT_WEB_TOOL_SERVICE_MANAGEMENT_REFERENCE", "").strip()
        if reference:
            args += ["--service-management-reference", reference]
        code, out, err = _run(args)
        if code != 0 or not out.strip():
            return "", _az_error(err, "az ad app create failed")
        app_id = out.strip()
        print(f"{DIM}  Created app registration {display_name} ({app_id}){RESET}")

    audience = f"api://{app_id}"
    if audience not in uris:
        code, _, err = _run(["ad", "app", "update", "--id", app_id, "--identifier-uris", audience])
        if code != 0:
            return app_id, _az_error(err, "could not set the app's identifier URI")
    # Entra only issues tokens for an API that has a service principal in the tenant.
    code, _, _ = _run(["ad", "sp", "show", "--id", app_id, "--query", "id", "-o", "tsv"])
    if code != 0:
        code, _, err = _run(["ad", "sp", "create", "--id", app_id, "--query", "id", "-o", "tsv"])
        if code != 0:
            return app_id, _az_error(err, "could not create the app's service principal")
    return app_id, ""


def _settle_agent_web_tool(cfg: dict[str, str]) -> CheckResult | None:
    """Record agent mode's web tool in the azd env when nothing chose one.

    Unset, AGENT_WEB_TOOL resolves by environment (see channels.resolve_web_tool):
    Web IQ for a new environment, Bing for one deployed before the choice existed
    or bringing its own Foundry account. Bicep only knows its own default, so the
    resolved value is written here, in the preprovision hook, before bicep reads
    the env. Recording it also pins the choice: a deployed environment keeps its
    tool whatever a later default says.

    Returns a failing check only when the value could not be stored AND it
    differs from the Bicep default, because then this deploy would swap the
    agent's web tool.
    """
    if not _agent_binding(cfg):
        return None
    tool, reason = resolve_web_tool(cfg)
    if reason == WEB_TOOL_SET:
        return None
    note = web_tool_reason_note(reason)
    if _azd_env_set("AGENT_WEB_TOOL", tool):
        cfg["AGENT_WEB_TOOL"] = tool
        print(f"{BOLD}Agent web tool:{RESET} recorded AGENT_WEB_TOOL={tool}"
              + (f" — {note}." if note else ", the default for new environments."))
        if reason == WEB_TOOL_EXISTING:
            print(f"{DIM}  To move to Web IQ: uv run python scripts/set_profile.py --profile "
                  f"{cfg.get('DEPLOY_PROFILE') or 'web'} --binding agent --web-tool webiq{RESET}")
        print()
        return None
    cfg["AGENT_WEB_TOOL"] = tool
    if tool == DEFAULT_WEB_TOOL:
        return None
    return CheckResult(
        "Agent web tool",
        False,
        f"could not record AGENT_WEB_TOOL={tool}; without it this deploy would switch "
        f"the agent to {DEFAULT_WEB_TOOL}",
        fix=f"        {note[0].upper()}{note[1:]}.\n"
            f"        azd env set AGENT_WEB_TOOL {tool}",
    )


def _settle_agent_web_tool_auth(cfg: dict[str, str]) -> None:
    """Give the Web IQ agent tool a way to authenticate, preferring managed identity.

    Runs only for agent mode with AGENT_WEB_TOOL=webiq when neither
    AGENT_WEB_TOOL_KEY nor AGENT_WEB_TOOL_AUDIENCE is set. Precedence:

    1. a key, if the operator set one (nothing to do here);
    2. managed identity: create the app registration, then store its audience
       for bicep (the preprovision hook runs before bicep reads the env);
    3. if the directory refuses — many tenants restrict app registrations — a
       generated key, stored the same way, and said out loud.

    Never fails the deploy: the worst case is a key instead of a token.
    """
    if not _agent_binding(cfg):
        return
    if resolve_web_tool(cfg)[0] != "webiq":
        return
    if cfg.get("FOUNDRY_ACCOUNT_NAME", "").strip():
        return
    if cfg.get("AGENT_WEB_TOOL_KEY", "").strip() or cfg.get("AGENT_WEB_TOOL_AUDIENCE", "").strip():
        return

    env_name = cfg.get("AZURE_ENV_NAME", "").strip() or "default"
    display_name = f"{AGENT_WEB_TOOL_APP_PREFIX}{env_name}"
    print(f"{BOLD}Agent web tool: setting up managed-identity auth{RESET}")
    app_id, error = _ensure_web_tool_app(cfg, display_name)

    if app_id and not error:
        audience = f"api://{app_id}"
        if _azd_env_set("AGENT_WEB_TOOL_AUDIENCE", audience):
            cfg["AGENT_WEB_TOOL_AUDIENCE"] = audience
            if _azd_env_set("AGENT_WEB_TOOL_APP_ID", app_id):
                cfg["AGENT_WEB_TOOL_APP_ID"] = app_id
            print(f"{GREEN}  Saved: AGENT_WEB_TOOL_AUDIENCE={audience}{RESET}")
            print(f"{DIM}  Foundry's managed identity will call the tool with an Entra token; no key is stored.")
            print(f"  azd down does not remove app registrations: az ad app delete --id {app_id}{RESET}\n")
            return
        error = "could not save the audience to the azd environment"

    key = secrets.token_urlsafe(32)
    print(f"{YELLOW}  Managed identity is not available: {error}{RESET}")
    if app_id:
        print(f"{DIM}  (partly set up app registration left in place: {display_name}, {app_id}){RESET}")
    if _azd_env_set("AGENT_WEB_TOOL_KEY", key):
        cfg["AGENT_WEB_TOOL_KEY"] = key
        print(f"{YELLOW}  Falling back to a generated shared key.{RESET}")
        print(f"{DIM}  Stored in the azd env as AGENT_WEB_TOOL_KEY; Foundry holds it in a project")
        print("  connection and sends it as x-tool-key. To move to managed identity later, have")
        print("  an admin create the app registration, then:")
        print("    azd env set AGENT_WEB_TOOL_AUDIENCE api://<appId>")
        print('    azd env set AGENT_WEB_TOOL_KEY ""')
        print(f"    azd provision{RESET}\n")
    else:
        print(f"{YELLOW}  Could not store a generated key either. The agent will be created without")
        print("  its web tool; set AGENT_WEB_TOOL_KEY or AGENT_WEB_TOOL_AUDIENCE and re-provision.")
        print(f"{RESET}")


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
    ap.add_argument(
        "--record-web-tool",
        action="store_true",
        help="Only record agent mode's resolved AGENT_WEB_TOOL, then exit. Never fails "
             "(used by the preprovision hook when PREFLIGHT_SKIP=true).",
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
        return 0 if args.record_web_tool else 2

    if args.record_web_tool:
        # PREFLIGHT_SKIP skips every check, but not this: without a recorded value
        # an environment deployed before Web IQ became the default would get
        # Bicep's default and silently swap its agent's web tool.
        failed = _settle_agent_web_tool(cfg)
        if failed is not None:
            print(f"{YELLOW}WARN{RESET}  {failed.name}: {failed.detail}")
            print(f"{DIM}{failed.fix}{RESET}")
        return 0

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
    recorded = _settle_agent_web_tool(cfg)
    if recorded is not None:
        checks.append(recorded)
    checks += check_agent_web_tool(cfg)
    checks += check_trusted_web_sites(cfg)
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
        if not c.ok and c.warn_only and c.fix:
            print(c.fix)
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
    # After the checks, not among them: this one writes to the directory and the
    # azd env, which should not happen for a deploy that is about to be blocked.
    _settle_agent_web_tool_auth(cfg)
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
