"""Channel/profile definitions — the single source of truth for `DEPLOY_PROFILE`.

Deploying this repo is not one decision, it is a *sequence*: some steps Bicep can
do, some only a human with the right directory role can do, and they interleave.
Every consumer of that sequence (`preflight.py`, `set_profile.py`, the azd hooks,
the docs) reads it from here so the numbered steps a user is told to follow can
never drift apart from what the templates actually deploy.

Profiles map onto the channel ladder documented in `docs/channels/README.md`:

    web              A            the core web app
    teams-tab        A + B        adds a Teams personal tab (no extra Azure resources)
    in-call-browser  A + B + C    adds the ACS browser guest
    in-call          A + B + D    adds the Graph media bot

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
class Binding:
    """One of the two brains a deployment can bind Voice Live to.

    Orthogonal to the channel: every channel works with either brain, so this is
    a second question rather than a fifth profile. Recorded as VOICE_BINDING.
    """

    key: str
    title: str
    summary: str
    tradeoff: str


BINDING_ORDER = ["agent", "model"]

BINDINGS: dict[str, Binding] = {
    "agent": Binding(
        key="agent",
        title="Agent mode",
        summary=(
            "Bind to a Foundry agent. Prompt, model and tool routing live in Foundry; "
            "speech is transcribed before the agent sees it."
        ),
        tradeoff=(
            "You choose the web tool: Grounding with Bing or Web IQ through the app. "
            "Tools and prompt are editable in the portal without redeploying. The "
            "answer waits on transcription."
        ),
    ),
    "model": Binding(
        key="model",
        title="Model mode",
        summary=(
            "Bind straight to a realtime model. It works from the audio itself, so "
            "the answer no longer waits on the transcript; prompt and tools travel "
            "in the session."
        ),
        tradeoff=(
            "Lower time-to-first-token, but Grounding with Bing cannot follow — web "
            "search runs through Web IQ instead, and the prompt ships in the image."
        ),
    ),
}


@dataclass(frozen=True)
class WebTool:
    """Agent mode's web search tool. Recorded as AGENT_WEB_TOOL.

    Only asked for agent mode: model mode always searches through Web IQ, because
    Grounding with Bing has no agent to attach to there.
    """

    key: str
    title: str
    summary: str
    tradeoff: str


WEB_TOOL_ORDER = ["webiq", "bing"]
# Must match agentWebTool's default in infra/main.bicep and main.parameters.json.
DEFAULT_WEB_TOOL = "webiq"

# Why resolve_web_tool() picked a tool.
WEB_TOOL_SET = "set"
WEB_TOOL_DEFAULT = "default"
WEB_TOOL_EXISTING = "existing"
WEB_TOOL_BYO_FOUNDRY = "byo-foundry"


def resolve_web_tool(env) -> tuple[str, str]:
    """The agent web tool an environment deploys, and why: ``(tool, reason)``.

    AGENT_WEB_TOOL wins when set (lower-cased, not validated). Unset, Web IQ is
    the default for NEW environments only; two kinds keep Bing, because Web IQ
    would change or break them:

    * ``existing``: agent mode and already deployed (SERVICE_APP_URI is a
      provision output). The environment predates the choice, when Bing was the
      only web tool, so defaulting it to Web IQ would silently swap a live
      agent's tool. Preflight records the tool in agent mode, so a deployed
      agent-mode environment with nothing recorded is always one of these.
    * ``byo-foundry``: FOUNDRY_ACCOUNT_NAME is set, and Web IQ needs the Foundry
      account this template creates.
    """
    raw = (env.get("AGENT_WEB_TOOL") or "").strip().lower()
    if raw:
        return raw, WEB_TOOL_SET
    if (env.get("FOUNDRY_ACCOUNT_NAME") or "").strip():
        return "bing", WEB_TOOL_BYO_FOUNDRY
    agent = ((env.get("VOICE_BINDING") or "").strip().lower() or "agent") == "agent"
    if agent and (env.get("SERVICE_APP_URI") or "").strip():
        return "bing", WEB_TOOL_EXISTING
    return DEFAULT_WEB_TOOL, WEB_TOOL_DEFAULT


def web_tool_reason_note(reason: str) -> str:
    """One line on why an unset AGENT_WEB_TOOL resolved to Bing, or ""."""
    if reason == WEB_TOOL_EXISTING:
        return ("kept: this environment was deployed before Web IQ became the default, "
                "so its agent stays on Bing until you choose otherwise")
    if reason == WEB_TOOL_BYO_FOUNDRY:
        return "Web IQ needs the Foundry account this template creates, and FOUNDRY_ACCOUNT_NAME is set"
    return ""


WEB_TOOLS: dict[str, WebTool] = {
    "bing": WebTool(
        key="bing",
        title="Grounding with Bing Custom Search",
        summary=(
            "A native Foundry tool. azd deploys the Bing account, its trusted-site "
            "list and the connection."
        ),
        tradeoff=(
            "Slower in our tests: 1.83 s per search, 13/15 good answers. The one to "
            "use with an existing Foundry account (FOUNDRY_ACCOUNT_NAME)."
        ),
    ),
    "webiq": WebTool(
        key="webiq",
        title="Web IQ through the app",
        summary=(
            "An OpenAPI tool that calls this app's /api/tools/search-web, which runs "
            "Web IQ over the same trusted sites as model mode (TRUSTED_WEB_SITES; unset "
            "means the open web). No Bing is deployed."
        ),
        tradeoff=(
            "The default. Faster in our tests: 0.66 s per search, first token 1.2 s "
            "sooner, 15/15 good answers. Needs WEBIQ_API_KEY (unless the app's identity is bound in "
            "the Web IQ portal) and the Foundry account this template creates."
        ),
    ),
}


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
        CostItem("Web searches", PER_USE, "Bing or Web IQ in agent mode (AGENT_WEB_TOOL), Web IQ in model mode; either can be left off"),
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
            "Point the web tool at your own sources",
            YOU,
            BEFORE,
            "Every web tool searches the same trusted-site list, TRUSTED_WEB_SITES: "
            "comma-separated hosts or URLs, a leading + to rank a source first on Bing "
            "(docs/configuration.md#trusted-web-sources). Agent mode uses the tool "
            "set_profile.py recorded (AGENT_WEB_TOOL): with bing, azd deploys Grounding "
            "with Bing Custom Search over the list, and with no list deploys no Bing, "
            "because Bing has no open-web mode; with webiq, the agent calls the app's "
            "Web IQ route and no Bing is deployed. Model mode always uses Web IQ (Bing "
            "has no agent to attach to). Web IQ needs WEBIQ_API_KEY unless the app's "
            "identity is bound in the Web IQ portal, reduces the list to bare hosts, "
            "since site: cannot match a path, and with no list searches the open web. "
            "Skipping web search is supported too — the avatar then answers from your "
            "documents alone.",
            'azd env set TRUSTED_WEB_SITES "+www.example.com/investors,news.example.com"',
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
        channels="A + B + D",
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
    "in-call-browser": Profile(
        key="in-call-browser",
        title="Web + tab + in-call avatar (browser guest)",
        channels="A + B + C",
        summary=(
            "The avatar joins a Teams meeting as an anonymous guest from a browser tab, "
            "hears the room and answers aloud with a lip-synced camera tile. No Windows "
            "host and no administrator of any kind — but the tab has to stay open."
        ),
        # Everything channel C needs. ENABLE_ACS provisions the resource; the other two
        # give her a face, and without them the joiner still works but publishes no video.
        flags={
            "ENABLE_ACS": "true",
            "ACS_AVATAR_VIDEO_ENABLED": "true",
            "BROWSER_JOIN_VIDEO_ENABLED": "true",
        },
        providers=["Microsoft.Communication"],
        steps=(
            _core_steps()[:4]
            + [
                Step(
                    "Provision Communication Services",
                    AZD,
                    DURING,
                    "Part of the same `azd up`, because selecting this profile set ENABLE_ACS "
                    "for you: the ACS resource, plus a role assignment that lets the container "
                    "app mint guest tokens with its own managed identity. No VM, no bot "
                    "registration, nothing for an administrator to approve.",
                ),
            ]
            + _core_steps()[4:]
            + _teams_package_steps()
            + [
                Step(
                    "Open the joiner and paste a meeting link",
                    YOU,
                    AFTER,
                    "Browse to /acs-join.html on the deployed app, paste the Teams meeting URL "
                    "and join. Keep that tab visible — a backgrounded tab throttles her video.",
                ),
                Step(
                    "Admit her from the meeting lobby",
                    YOU,
                    AFTER,
                    "Anonymous guests wait in the lobby unless the organiser has turned it off, "
                    "so someone already in the meeting has to click Admit.",
                ),
            ]
        ),
        costs=_core_costs()
        + [
            CostItem(
                "Communication Services",
                PER_USE,
                "per participant-minute while she is in a call; nothing at all when idle",
            ),
            CostItem("Teams personal tab", FREE, "adds no Azure resources at all"),
        ],
        cost_note=(
            "The cheapest way into a meeting by a wide margin: it adds no always-on "
            "resource whatsoever, where the media bot adds a Windows VM at roughly "
            "$283/month whether or not anyone calls."
        ),
    ),
}

DEFAULT_PROFILE = "web"
PROFILE_ORDER = ["web", "teams-tab", "in-call-browser", "in-call"]

# Every flag any profile sets, mapped to the value that means "off".
#
# Selecting a profile writes its own flags and resets all the others, so the profile is
# genuinely the source of truth rather than a set of switches that only ever accumulate.
# Without the reset, moving from the media bot to the browser guest would leave
# DEPLOY_MEETING_BOT_HOST=true behind and quietly keep paying for a Windows VM that the
# newly-chosen profile never wanted.
PROFILE_MANAGED_FLAGS: dict[str, str] = {
    "MEETING_BOT_ENABLED": "false",
    "DEPLOY_MEETING_BOT_HOST": "false",
    "ENABLE_ACS": "false",
    "ACS_AVATAR_VIDEO_ENABLED": "false",
    "BROWSER_JOIN_VIDEO_ENABLED": "false",
}

# A flag a profile sets but that is missing from the reset map would never be cleared
# when switching away, which is the exact leak the map exists to prevent.
_unmanaged = {n for p in PROFILES.values() for n in p.flags} - set(PROFILE_MANAGED_FLAGS)
assert not _unmanaged, f"profile flags missing from PROFILE_MANAGED_FLAGS: {sorted(_unmanaged)}"


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
