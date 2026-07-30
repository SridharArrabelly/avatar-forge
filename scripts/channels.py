"""Channel/profile definitions — the single source of truth for `DEPLOY_PROFILE`.

Deploying this repo is not one decision, it is a *sequence*: some steps Bicep can
do, some only a human with the right directory role can do, and they interleave.
Every consumer of that sequence (`preflight.py`, `set_profile.py`, the azd hooks,
the docs) reads it from here so the numbered steps a user is told to follow can
never drift apart from what the templates actually deploy.

Profiles map onto the channel ladder documented in `docs/channels/README.md`:

    web         A            the core web app
    teams-tab   A + B        adds a Teams personal tab (no extra Azure resources)
    teams-chat  A + B + C    adds the @mentionable conversational bot
    in-call     A + B + D    adds the live in-meeting avatar (media bot)

`teams-tab` provisions exactly the same Azure resources as `web` — the difference
is entirely in the Teams app package and who has to upload it. That is a feature,
not a redundancy: it means step 2 of the ladder costs nothing to try.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

# The default Windows console codepage (cp1252) cannot encode the em dashes and
# box-drawing characters below, and Python raises UnicodeEncodeError rather than
# degrading. Since these modules exist to *unblock* people, refusing to print is
# the one failure mode we cannot accept.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # not a reconfigurable text stream
        pass

# ── Who performs a step ──────────────────────────────────────────────────────
AZD = "azd"  # automated by `azd up` / hooks
YOU = "you"  # the person deploying, no special privileges needed
ADMIN = "admin"  # needs a Teams administrator or Entra privileged role

WHO_LABEL = {
    AZD: "auto ",
    YOU: "YOU  ",
    ADMIN: "ADMIN",
}

# ── When a step happens relative to `azd up` ─────────────────────────────────
BEFORE = "before"
DURING = "during"
AFTER = "after"

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


@dataclass(frozen=True)
class Step:
    """One instruction in the deployment sequence."""

    title: str
    who: str
    when: str
    detail: str = ""
    command: str = ""


@dataclass(frozen=True)
class RequiredInput:
    """An azd env var the profile cannot deploy without.

    `how` explains where the value comes from — the difference between a check
    that blocks someone and a check that unblocks them.
    """

    name: str
    how: str
    who: str = YOU
    secret: bool = False


@dataclass(frozen=True)
class Profile:
    key: str
    title: str
    channels: str
    summary: str
    # azd env vars this profile sets for you (deterministic; no user input)
    flags: dict[str, str] = field(default_factory=dict)
    requires: list[RequiredInput] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    # extra ARM resource providers that must be registered
    providers: list[str] = field(default_factory=list)
    cost_note: str = ""


# ── Steps shared by every profile ────────────────────────────────────────────
def _core_steps() -> list[Step]:
    return [
        Step(
            "Pick the profile",
            YOU,
            BEFORE,
            "Records which channel you are deploying. Everything else follows from it.",
            "uv run python scripts/set_profile.py",
        ),
        Step(
            "Run preflight",
            YOU,
            BEFORE,
            "Confirms region support, providers, tooling and every input this profile needs. "
            "Fixes here are cheap; the same problems found after a 20-minute deploy are not.",
            "uv run python scripts/preflight.py",
        ),
        Step(
            "Decide on the web/news tool",
            YOU,
            BEFORE,
            "Optional. Without it the avatar answers from your indexed documents alone — a "
            "supported end state, not a broken one. To include it, edit the curated site "
            "allow-list (bingAllowedDomains in infra/main.bicep) to your own sources, then "
            "set the flag below; azd creates the Bing account, the allow-list and the "
            "Foundry connection, and fills in the two BING_* names for you.",
            "azd env set DEPLOY_BING_GROUNDING true",
        ),
        Step(
            "Provision + deploy Azure resources",
            AZD,
            DURING,
            "Container app, Foundry account/project + model, AI Search, ACR, managed identity and roles.",
            "azd up",
        ),
        Step(
            "Build the AI Search index and Foundry agent",
            AZD,
            DURING,
            "Runs automatically in the postprovision hook (greenfield only). "
            "Put your .docx sources in data/ first or the index is skipped.",
        ),
        Step(
            "Open the app and ask a question",
            YOU,
            AFTER,
            "The SERVICE_APP_URI printed at the end. Verify voice and the avatar before adding channels.",
        ),
    ]


def _teams_package_steps() -> list[Step]:
    return [
        Step(
            "Build the Teams app package",
            YOU,
            AFTER,
            "Fills the manifest placeholders from your azd env and zips the package.",
            "uv run python teams/build_package.py",
        ),
        Step(
            "Upload the package to Teams",
            YOU,
            AFTER,
            "Teams > Apps > Manage your apps > Upload an app > Upload a custom app. "
            "If that option is missing, custom app upload is disabled for your tenant and "
            "an administrator must enable it (or publish the package for you).",
        ),
    ]


PROFILES: dict[str, Profile] = {
    "web": Profile(
        key="web",
        title="Web only",
        channels="A",
        summary="The standalone browser app. No Teams, no manifest, no administrator.",
        steps=_core_steps(),
        cost_note=(
            "Container app + Foundry + AI Search. Note this does NOT scale to zero: AI "
            "Search is a `basic` service billed hourly and the container app holds a floor "
            "of 1 replica. `azd down` is the only way to stop paying."
        ),
    ),
    "teams-tab": Profile(
        key="teams-tab",
        title="Web + Teams personal tab",
        channels="A + B",
        summary=(
            "Adds a Teams personal tab that embeds the same web UI. "
            "Provisions ZERO extra Azure resources — the manifest just points at the app URL."
        ),
        steps=_core_steps() + _teams_package_steps(),
        cost_note="Identical to `web`. The tab is free.",
    ),
    "teams-chat": Profile(
        key="teams-chat",
        title="Web + tab + conversational bot",
        channels="A + B + C",
        summary="Adds an @mentionable bot that answers in Teams chat via the same Foundry agent.",
        flags={},
        requires=[
            RequiredInput(
                "BOT_APP_ID",
                "Entra app registration (single tenant) for the chat bot. "
                "Create it in Entra > App registrations, then copy the Application (client) ID.",
            ),
            RequiredInput(
                "BOT_APP_PASSWORD",
                "A client secret on that app registration.",
                secret=True,
            ),
            RequiredInput(
                "BOT_APP_TENANT_ID",
                "The tenant the app registration lives in (`az account show --query tenantId -o tsv`).",
            ),
        ],
        providers=["Microsoft.BotService"],
        steps=(
            _core_steps()[:2]
            + [
                Step(
                    "Create the bot's Entra app registration + secret",
                    YOU,
                    BEFORE,
                    "Single-tenant. Copy the client id, tenant id and a client secret into the azd env. "
                    "Bicep cannot create app registrations — they live in the directory, not the subscription.",
                    'azd env set BOT_APP_ID <id>; azd env set BOT_APP_TENANT_ID <tid>; azd env set BOT_APP_PASSWORD "<secret>"',
                ),
            ]
            + _core_steps()[2:]
            + [
                Step(
                    "Grant admin consent for the bot app",
                    ADMIN,
                    AFTER,
                    "One-time, by someone with Privileged Role Administrator or Global Administrator. "
                    "Without it the bot installs but never receives messages.",
                ),
            ]
            + _teams_package_steps()
            + [
                Step(
                    "@mention the bot in a chat",
                    YOU,
                    AFTER,
                    "It should answer from the same agent as the web app and deep-link to the tab.",
                ),
            ]
        ),
        cost_note="Adds an Azure Bot registration (F0 — free).",
    ),
    "in-call": Profile(
        key="in-call",
        title="Web + tab + in-call meeting avatar",
        channels="A + B + D",
        summary=(
            "The avatar joins a Teams meeting, hears the room and answers aloud with a lip-synced "
            "camera tile. Highest capability and highest administrator burden."
        ),
        flags={"MEETING_BOT_ENABLED": "true", "DEPLOY_MEETING_BOT_HOST": "true"},
        requires=[
            RequiredInput(
                "MEETING_BOT_APP_ID",
                "A SEPARATE Entra app registration for the calling bot. It must not be the same "
                "app as BOT_APP_ID — an Entra app can back only one Azure Bot resource.",
            ),
            RequiredInput(
                "MEETING_BOT_APP_TENANT_ID",
                "Tenant of that app registration.",
            ),
            RequiredInput(
                "MEETING_BOT_DNS_LABEL",
                "Globally-unique DNS label for the Windows host, e.g. avatar-meetingbot-contoso. "
                "Becomes <label>.<region>.cloudapp.azure.com and must resolve for the TLS certificate.",
            ),
            RequiredInput(
                "MEETING_BOT_ADMIN_PASSWORD",
                "Local administrator password for the Windows VM (12+ chars, 3 of 4 character classes).",
                secret=True,
            ),
        ],
        providers=["Microsoft.BotService", "Microsoft.Compute", "Microsoft.Network"],
        steps=(
            _core_steps()[:2]
            + [
                Step(
                    "Create the CALLING bot's Entra app registration + secret",
                    YOU,
                    BEFORE,
                    "Separate from the chat bot. Add the application permissions "
                    "Calls.JoinGroupCall.All, Calls.JoinGroupCallAsGuest.All, Calls.AccessMedia.All "
                    "and OnlineMeetings.Read.All.",
                    "azd env set MEETING_BOT_APP_ID <id>; azd env set MEETING_BOT_APP_TENANT_ID <tid>",
                ),
                Step(
                    "Grant admin consent for those Graph permissions",
                    ADMIN,
                    BEFORE,
                    "One-time. Calls.AccessMedia.All is what lets the bot hear the meeting — "
                    "without consent the bot joins and hears silence.",
                ),
                Step(
                    "Choose a DNS label and VM password",
                    YOU,
                    BEFORE,
                    "The DNS label must be globally unique in the region.",
                    'azd env set MEETING_BOT_DNS_LABEL <label>; azd env set MEETING_BOT_ADMIN_PASSWORD "<pwd>"',
                ),
            ]
            + _core_steps()[2:3]
            + [
                Step(
                    "Provision the Windows media host + calling bot registration",
                    AZD,
                    DURING,
                    "Same `azd up`: Windows VM, public IP with your DNS label, NSG for the signaling "
                    "and media ports, and the Azure Bot registration with the Teams calling webhook.",
                ),
            ]
            + _core_steps()[3:5]
            + [
                Step(
                    "Configure the Windows host",
                    YOU,
                    AFTER,
                    "RDP into the VM, clone this repo there, then run the four stages: "
                    "Prep installs .NET, Cert requests the TLS certificate, Build publishes "
                    "the bot and Run registers the Windows service. Details in meeting-bot/README.md.",
                    r".\meeting-bot\scripts\setup-host.ps1 -Stage Prep|Cert|Build|Run   (on the VM)",
                ),
                Step(
                    "Create a Teams app access policy and assign it",
                    ADMIN,
                    AFTER,
                    "THE HARD BLOCKER. Only a Teams administrator can do this, and without it the "
                    "bot cannot be invited into meetings at all. Confirm this is achievable BEFORE "
                    "you pay for the VM.",
                    "New-CsApplicationAccessPolicy / Grant-CsApplicationAccessPolicy",
                ),
            ]
            + [
                Step(
                    "Build and upload the Teams app package (optional for D)",
                    YOU,
                    AFTER,
                    "The calling bot joins through Graph application permissions, so channel D "
                    "works WITHOUT installing anything in Teams. Build the package only if you "
                    "also want the app's in-meeting presence; --enable-calling sets "
                    "supportsCalling=true in the manifest.",
                    "uv run python teams/build_package.py --bot-id <MEETING_BOT_APP_ID> --enable-calling",
                ),
                Step(
                    "Invite the avatar into a meeting",
                    YOU,
                    AFTER,
                    "POST the meeting join URL to the host's /api/join endpoint, then ask it a question aloud.",
                ),
            ]
        ),
        cost_note=(
            "Adds an always-on Windows VM, Standard_D4s_v5 (~$283/month). Deallocate it "
            "when not testing: `az vm deallocate -n avatar-meetingbot-vm -g <rg>`."
        ),
    ),
}

DEFAULT_PROFILE = "web"
PROFILE_ORDER = ["web", "teams-tab", "teams-chat", "in-call"]


def get_profile(key: str | None) -> Profile:
    """Resolve a profile key, falling back to the env then the default."""
    resolved = (key or os.environ.get("DEPLOY_PROFILE") or DEFAULT_PROFILE).strip().lower()
    if resolved not in PROFILES:
        valid = ", ".join(PROFILE_ORDER)
        raise SystemExit(f"Unknown DEPLOY_PROFILE '{resolved}'. Valid values: {valid}")
    return PROFILES[resolved]


def render_steps(profile: Profile, *, color: bool = True, phases: tuple[str, ...] = (BEFORE, DURING, AFTER)) -> str:
    """Render the ordered step plan, marking who performs each step.

    `phases` narrows the output — the postprovision hook passes `(AFTER,)` so it
    shows only what the person still has to do, rather than re-listing work that
    has just completed.
    """

    def c(code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if color else text

    lines: list[str] = []
    lines.append("")
    partial = phases != (BEFORE, DURING, AFTER)
    heading = "Remaining steps" if partial else "Deployment steps"
    lines.append(c(BOLD, f"{heading} — profile '{profile.key}' (channels {profile.channels})"))
    if not partial:
        lines.append(c(DIM, f"  {profile.summary}"))
    lines.append("")

    phase_titles = {
        BEFORE: "Before you deploy",
        DURING: "`azd up`",
        AFTER: "After the deploy",
    }

    # Number against the full plan so step numbers stay stable between a full
    # listing and a filtered one — "step 10" must mean the same thing in both.
    numbering = {id(s): i for i, s in enumerate(_ordered(profile), start=1)}

    for phase in (BEFORE, DURING, AFTER):
        if phase not in phases:
            continue
        phase_steps = [s for s in profile.steps if s.when == phase]
        if not phase_steps:
            continue
        lines.append(c(CYAN, f"-- {phase_titles[phase]} " + "-" * max(0, 56 - len(phase_titles[phase]))))
        for step in phase_steps:
            marker = WHO_LABEL[step.who]
            if step.who == AZD:
                marker = c(GREEN, marker)
            elif step.who == ADMIN:
                marker = c(RED, marker)
            else:
                marker = c(YELLOW, marker)
            lines.append(f" {numbering[id(step)]:>2}. [{marker}] {step.title}")
            if step.detail:
                for wrapped in _wrap(step.detail, 72):
                    lines.append(f"         {c(DIM, wrapped)}")
            if step.command:
                lines.append(f"         {c(CYAN, '$ ' + step.command)}")
        lines.append("")

    admin_steps = [s for s in profile.steps if s.who == ADMIN and s.when in phases]
    if admin_steps:
        lines.append(c(BOLD, "Needs an administrator:"))
        for step in admin_steps:
            lines.append(f"  - {step.title}")
        lines.append(c(DIM, "  See docs/admin-checklist.md - including what to do when you cannot get one."))
        lines.append("")

    if profile.cost_note and not partial:
        lines.append(c(BOLD, "Cost: ") + profile.cost_note)
        lines.append("")

    return "\n".join(lines)


def _ordered(profile: Profile) -> list[Step]:
    """Steps in execution order: before, then during, then after."""
    return [s for phase in (BEFORE, DURING, AFTER) for s in profile.steps if s.when == phase]


def _wrap(text: str, width: int) -> list[str]:
    words, out, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out
