"""Tests for profile selection — the flags it writes, and the command it tells you to run.

Two things here are easy to get wrong and expensive when wrong:

  * **Flags must be authoritative, not cumulative.** Selecting a profile has to reset
    the flags belonging to profiles you did *not* pick. Without that, switching from
    the media bot to the browser guest leaves DEPLOY_MEETING_BOT_HOST=true behind and
    you keep paying ~$283/month for a Windows VM the new profile never wanted.

  * **Greenfield and upgrade need different commands.** On a fresh environment `azd up`
    does everything. On one that is already deployed, these flags arrive as container
    app env vars written by Bicep, so `azd deploy` alone cannot see them — it needs
    `azd provision` first, and then a deploy, because a bare provision reverts the
    container app to the placeholder image while still reporting success.

Needs no Azure and no credentials: `azd` is never invoked.

    uv run python tests/test_set_profile.py
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import channels as ch  # noqa: E402
import set_profile as sp  # noqa: E402

_failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        _failures.append(name)


def run(
    env: dict[str, str],
    profile: str,
    args: list[str] | None = None,
    answers: list[str] | None = None,
) -> tuple[dict[str, str], str]:
    """Run set_profile against a fake azd env; return the flags written and the output.

    ``args`` replaces the default ``--binding agent``; ``answers`` feeds input()
    in order, and any prompt beyond them fails the run instead of hanging.
    """
    written: dict[str, str] = {}

    def fake_set(name: str, value: str) -> bool:
        written[name] = value
        return True

    pending = list(answers or [])

    def fake_input(prompt: str = "") -> str:
        if not pending:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return pending.pop(0)

    old_set, old_values = sp._azd_env_set, sp._azd_env_values
    sp._azd_env_set = fake_set
    sp._azd_env_values = lambda: dict(env)
    old_argv = sys.argv
    sys.argv = ["set_profile.py", "--profile", profile, *(["--binding", "agent"] if args is None else args)]
    try:
        buf = io.StringIO()
        with redirect_stdout(buf), patch("builtins.input", fake_input):
            rc = sp.main()
        assert rc == 0, f"exit code {rc}"
        assert not pending, f"unused answers: {pending}"
        return written, buf.getvalue()
    finally:
        sp._azd_env_set, sp._azd_env_values = old_set, old_values
        sys.argv = old_argv


print("\nProfiles follow the channel order used by the greenfield picker")
check(
    "profile order is A, B, C, D",
    ch.PROFILE_ORDER == ["web", "teams-tab", "in-call-browser", "in-call"],
)
check(
    "profile channel labels match that order",
    [ch.PROFILES[key].channels for key in ch.PROFILE_ORDER]
    == ["A", "A + B", "A + B + C", "A + B + D"],
)
with patch("builtins.input", return_value="3"):
    check("interactive choice 3 selects channel C", sp._choose() == "in-call-browser")
with patch("builtins.input", return_value="4"):
    check("interactive choice 4 selects channel D", sp._choose() == "in-call")

print("\nEvery managed flag is written on every selection")
for key in ch.PROFILE_ORDER:
    written, _ = run({}, key)
    missing = set(ch.PROFILE_MANAGED_FLAGS) - set(written)
    check(f"{key}: writes all {len(ch.PROFILE_MANAGED_FLAGS)} managed flags", not missing)
    wrong = {
        n: written[n]
        for n, off in ch.PROFILE_MANAGED_FLAGS.items()
        if written.get(n) != ch.PROFILES[key].flags.get(n, off)
    }
    check(f"{key}: every flag matches the profile", not wrong)

print("\nChannel C needs nothing from you and switches on all three ACS flags")
written, _ = run({}, "in-call-browser")
check("ENABLE_ACS=true", written.get("ENABLE_ACS") == "true")
check("ACS_AVATAR_VIDEO_ENABLED=true", written.get("ACS_AVATAR_VIDEO_ENABLED") == "true")
check("BROWSER_JOIN_VIDEO_ENABLED=true", written.get("BROWSER_JOIN_VIDEO_ENABLED") == "true")
check("no required inputs", ch.PROFILES["in-call-browser"].requires == [])
check("the Windows host stays off", written.get("DEPLOY_MEETING_BOT_HOST") == "false")

print("\nChannel D enables only the Graph media-bot path")
written, _ = run({}, "in-call")
check("MEETING_BOT_ENABLED=true", written.get("MEETING_BOT_ENABLED") == "true")
check("DEPLOY_MEETING_BOT_HOST=true", written.get("DEPLOY_MEETING_BOT_HOST") == "true")
check("the ACS resource stays off", written.get("ENABLE_ACS") == "false")
check("browser video stays off", written.get("BROWSER_JOIN_VIDEO_ENABLED") == "false")

print("\nSwitching away from the media bot turns the Windows VM off")
was_bot = {
    "DEPLOY_PROFILE": "in-call",
    "MEETING_BOT_ENABLED": "true",
    "DEPLOY_MEETING_BOT_HOST": "true",
    "SERVICE_APP_URI": "https://example.azurecontainerapps.io",
}
written, out = run(was_bot, "in-call-browser")
check("DEPLOY_MEETING_BOT_HOST reset to false", written.get("DEPLOY_MEETING_BOT_HOST") == "false")
check("MEETING_BOT_ENABLED reset to false", written.get("MEETING_BOT_ENABLED") == "false")
check("and it says so", "Reset to off" in out)

print("\nThe command it recommends depends on whether anything exists yet")
_, greenfield = run({}, "in-call-browser")
check("greenfield says azd up", "azd up" in greenfield)
check("greenfield does not say azd provision", "azd provision" not in greenfield)

_, upgrade = run(was_bot, "in-call-browser")
check("upgrade says azd provision", "azd provision" in upgrade)
check("upgrade says azd deploy too", "azd deploy" in upgrade)
check("upgrade warns that provision alone is not enough", "Run both." in upgrade)

unchanged = {
    "DEPLOY_PROFILE": "in-call-browser",
    "ENABLE_ACS": "true",
    "ACS_AVATAR_VIDEO_ENABLED": "true",
    "BROWSER_JOIN_VIDEO_ENABLED": "true",
    "SERVICE_APP_URI": "https://example.azurecontainerapps.io",
}
_, same = run(unchanged, "in-call-browser")
check("re-selecting the same profile asks for nothing", "Nothing to re-provision" in same)

print("\nAn unset flag counts as off, so a fresh env is not reported as changed")
_, fresh_web = run({"DEPLOY_PROFILE": "web", "SERVICE_APP_URI": "https://x"}, "web")
check("web on a deployed env needs no re-provision", "Nothing to re-provision" in fresh_web)

print("\nAgent mode's web tool: one default everywhere")
params = json.loads((REPO / "infra" / "main.parameters.json").read_text(encoding="utf-8"))
check(
    "main.parameters.json defaults AGENT_WEB_TOOL to DEFAULT_WEB_TOOL",
    params["parameters"]["agentWebTool"]["value"] == f"${{AGENT_WEB_TOOL={ch.DEFAULT_WEB_TOOL}}}",
)
check(
    "main.bicep defaults agentWebTool to DEFAULT_WEB_TOOL",
    f"param agentWebTool string = '{ch.DEFAULT_WEB_TOOL}'"
    in (REPO / "infra" / "main.bicep").read_text(encoding="utf-8"),
)
check("the menu offers exactly bing and webiq", ch.WEB_TOOL_ORDER == ["bing", "webiq"])

print("\nThe web tool is asked only in an interactive agent-mode run")
written, out = run({}, "web")
check("CI (--binding agent) leaves AGENT_WEB_TOOL untouched", "AGENT_WEB_TOOL" not in written)
check("and reports the default", "Agent web tool: 'bing'" in out)

written, _ = run({}, "web", ["--binding", "agent", "--web-tool", "webiq"])
check("--web-tool webiq is written without a prompt", written.get("AGENT_WEB_TOOL") == "webiq")

written, out = run({}, "web", [], answers=["1", ""])
check("interactive agent mode asks, and Enter picks bing", written.get("AGENT_WEB_TOOL") == "bing")
check("the menu says what each option measured", "1.83 s" in out and "0.66 s" in out)

written, _ = run({"AGENT_WEB_TOOL": "webiq"}, "web", [], answers=["1", ""])
check("Enter keeps a web tool already chosen", written.get("AGENT_WEB_TOOL") == "webiq")

written, out = run({}, "web", [], answers=["1", "2"])
check("choosing 2 selects webiq", written.get("AGENT_WEB_TOOL") == "webiq")
check("and says Web IQ needs a key", "WEBIQ_API_KEY" in out)

written, _ = run({}, "web", [], answers=["2"])
check("model mode is not asked about the agent's web tool", "AGENT_WEB_TOOL" not in written)

_, out = run({"FOUNDRY_ACCOUNT_NAME": "byo"}, "web", ["--binding", "agent", "--web-tool", "webiq"])
check("webiq with a BYO Foundry account warns before preflight", "FOUNDRY_ACCOUNT_NAME is set" in out)

_, out = run({"AGENT_WEB_TOOL": "google"}, "web")
check("an invalid recorded value is reported, not a crash", "not a valid web tool" in out)

print("\nChanging the web tool or the brain on a deployed env means re-provisioning")
deployed = {"DEPLOY_PROFILE": "web", "SERVICE_APP_URI": "https://x"}
_, out = run(deployed, "web", ["--binding", "agent", "--web-tool", "webiq"])
check("bing -> webiq says azd provision", "azd provision" in out and "AGENT_WEB_TOOL=webiq" in out)
check("and warns the Bing account keeps billing", "keeps billing" in out)

_, out = run({**deployed, "AGENT_WEB_TOOL": "webiq"}, "web", ["--binding", "agent", "--web-tool", "webiq"])
check("re-choosing the same web tool asks for nothing", "Nothing to re-provision" in out)

_, out = run({**deployed, "VOICE_BINDING": "model"}, "web")
check("model -> agent says azd provision", "azd provision" in out and "VOICE_BINDING=agent" in out)
check("switching the brain alone does not mention Bing billing", "keeps billing" not in out)

print()
if _failures:
    print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
    sys.exit(1)
print("All checks passed.")
