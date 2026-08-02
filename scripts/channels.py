"""Channel/profile definitions — the single source of truth for `DEPLOY_PROFILE`.

Deploying this repo is not one decision, it is a *sequence*: some steps Bicep can
do, some only a human with the right directory role can do, and they interleave.
Every consumer of that sequence (`preflight.py`, `set_profile.py`, the azd hooks,
the docs) reads it from here so the numbered steps a user is told to follow can
never drift apart from what the templates actually deploy.

Profiles map onto the channel ladder documented in `docs/channels/README.md`:

    web         A            the core web app
    teams-tab   A + B        adds a Teams personal tab (no extra Azure resources)
    in-call     A + B + C    adds the live in-meeting avatar (media bot)

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

# ── When a resource bills ────────────────────────────────────────────────────
HOURLY = "hourly"  # bills whether or not anyone uses it -- the surprising kind
PER_USE = "per-use"  # bills only while someone is using it
FREE = "free"

COST_GROUPS = [
    (HOURLY, "Billed hourly, used or not"),
    (PER_USE, "Billed per use, nothing while idle"),
    (FREE, "Free"),
]

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
    """An azd env var the profile needs from you.

    `how` explains where the value comes from — the difference between a check
    that blocks someone and a check that unblocks them.

    Most are genuinely required. Set `optional=True` when infra supplies a
    working default (e.g. a tenant id that falls back to the deployment tenant):
    preflight then reports it as a WARN instead of failing the deploy, so nobody
    is stopped over a value they never needed to provide.
    """

    name: str
    how: str
    who: str = YOU
    secret: bool = False
    # Advisory rather than blocking: infra supplies a sane default when this is
    # unset, so preflight reports it but must not fail the deploy over it.
    optional: bool = False


@dataclass(frozen=True)
class CostItem:
    """One line on the bill, and — the part people actually get wrong — when it bills.

    Idle cost and per-use cost behave so differently that lumping them together
    misleads in both directions: it makes an idle deployment look free when it is
    not, and a live session look cheap when the per-minute meters are running.
    """

    what: str
    billing: str
    note: str = ""


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
    # Cumulative, not incremental: each profile lists everything it deploys, so
    # nobody has to read the profile below to learn what they are paying for.
    costs: list[CostItem] = field(default_factory=list)
    cost_note: str = ""


# ── What every profile costs ────────────────────────────────────────────────
def _core_costs() -> list[CostItem]:
    """Channel A's bill, which every profile inherits because every profile deploys it."""
    return [
        CostItem("AI Search", HOURLY, "`basic` tier"),
        CostItem("Container app", HOURLY, "floor of 1 replica, so it never idles to nothing"),
        CostItem("Container registry", HOURLY, "`Standard`"),
        CostItem("Log Analytics + App Insights", HOURLY, "ingestion, 30-day retention"),
        CostItem("Voice Live minutes", PER_USE, "higher with avatar video; dominates a live session"),
        CostItem("Model tokens", PER_USE, "`GlobalStandard` chat + embeddings, billed per token"),
        CostItem("Bing searches", PER_USE, "`DEPLOY_BING_GROUNDING=false` to skip the tool"),
    ]


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
            "Also settles the deploy target — subscription, region and resource group — so "
            "`azd up` runs straight through instead of stopping to ask. Fixes here are cheap; "
            "the same problems found after a 20-minute deploy are not.",
            "uv run python scripts/preflight.py",
        ),
        Step(
            "Point the web/news tool at your own sources",
            YOU,
            BEFORE,
            "azd deploys the web tool by default — the Bing account, the curated site "
            "allow-list and the Foundry connection — and fills in the two BING_* names "
            "for you. Edit bingAllowedDomains in infra/main.bicep so it searches your "
            "sources rather than the sample ones. To skip the tool entirely (it is a "
            "billable resource), set the flag below to false; the avatar then answers "
            "from your indexed documents alone, which is a supported end state.",
            "azd env set DEPLOY_BING_GROUNDING false   # only if you do NOT want it",
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
        costs=_core_costs(),
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
        costs=_core_costs() + [CostItem("Teams personal tab", FREE, "adds no Azure resources at all")],
    ),
    "in-call": Profile(
        key="in-call",
        title="Web + tab + in-call meeting avatar",
        channels="A + B + C",
        summary=(
            "The avatar joins a Teams meeting, hears the room and answers aloud with a lip-synced "
            "camera tile. Highest capability and highest administrator burden."
        ),
        flags={"MEETING_BOT_ENABLED": "true", "DEPLOY_MEETING_BOT_HOST": "true"},
        requires=[
            RequiredInput(
                "MEETING_BOT_APP_ID",
                "An Entra app registration for the calling bot. It must be dedicated to this "
                "bot — an Entra app can back only one Azure Bot resource.",
            ),
            RequiredInput(
                "MEETING_BOT_APP_TENANT_ID",
                "Only if that app registration lives in a DIFFERENT tenant than the "
                "subscription. Left unset, infra uses the deployment tenant.",
                optional=True,
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
                    "Dedicated to this bot. Add the application permissions "
                    "Calls.JoinGroupCall.All, Calls.JoinGroupCallAsGuest.All, Calls.AccessMedia.All "
                    "and OnlineMeetings.Read.All.",
                    "azd env set MEETING_BOT_APP_ID <id>",
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
                    "The calling bot joins through Graph application permissions, so channel C "
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
        costs=[
            CostItem(
                "Windows VM (Standard_D4s_v5)",
                HOURLY,
                "~$283/mo — the dominant cost; Windows licensing roughly doubles the Linux rate",
            ),
            CostItem(
                "VM OS disk + static public IP",
                HOURLY,
                "~$20/mo — keeps billing even while the VM is deallocated",
            ),
        ]
        + _core_costs()
        + [
            CostItem("Teams personal tab", FREE, "adds no Azure resources at all"),
            CostItem("Azure Bot registration", FREE, "F0, calling-enabled"),
        ],
        cost_note=(
            "Deallocate the VM whenever you are not testing — "
            "`az vm deallocate -n avatar-meetingbot-vm -g <rg>` — which stops the "
            "compute charge but not the disk or the IP."
        ),
    ),
}

DEFAULT_PROFILE = "web"
PROFILE_ORDER = ["web", "teams-tab", "in-call"]


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

    if profile.costs and not partial:
        lines.append(c(BOLD, f"Cost — everything profile '{profile.key}' deploys"))
        width = max(len(i.what) for i in profile.costs)
        for billing, heading in COST_GROUPS:
            group = [i for i in profile.costs if i.billing == billing]
            if not group:
                continue
            lines.append(c(DIM, f"  {heading}:"))
            for item in group:
                row = f"    {item.what.ljust(width)}"
                if not item.note:
                    lines.append(row)
                    continue
                # Wrap the note under itself so a long one never runs off the line.
                pad = " " * (len(row) + 2)
                for j, wrapped in enumerate(_wrap(item.note, max(24, 74 - len(row)))):
                    lines.append((row + "  " if j == 0 else pad) + c(DIM, wrapped))
        lines.append("")
        tail = (
            "The hourly group does not scale to zero, so `azd down --purge` is the only "
            "way to stop paying for it. During a live session it is the per-use meters "
            "that move, not the hourly infrastructure — price both at "
            "https://azure.microsoft.com/pricing/calculator/ before a long pilot."
        )
        if profile.cost_note:
            tail = profile.cost_note + " " + tail
        for wrapped in _wrap(tail, 72):
            lines.append(f"  {c(DIM, wrapped)}")
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
