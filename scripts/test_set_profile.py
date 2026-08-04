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

    uv run python scripts/test_set_profile.py
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import channels as ch  # noqa: E402
import set_profile as sp  # noqa: E402

_failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        _failures.append(name)


def run(env: dict[str, str], profile: str) -> tuple[dict[str, str], str]:
    """Run set_profile against a fake azd env; return the flags written and the output."""
    written: dict[str, str] = {}

    def fake_set(name: str, value: str) -> bool:
        written[name] = value
        return True

    old_set, old_values = sp._azd_env_set, sp._azd_env_values
    sp._azd_env_set = fake_set
    sp._azd_env_values = lambda: dict(env)
    old_argv = sys.argv
    sys.argv = ["set_profile.py", "--profile", profile, "--binding", "agent"]
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = sp.main()
        assert rc == 0, f"exit code {rc}"
        return written, buf.getvalue()
    finally:
        sp._azd_env_set, sp._azd_env_values = old_set, old_values
        sys.argv = old_argv


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

print("\nChannel D needs nothing from you and switches on all three ACS flags")
written, _ = run({}, "in-call-browser")
check("ENABLE_ACS=true", written.get("ENABLE_ACS") == "true")
check("ACS_AVATAR_VIDEO_ENABLED=true", written.get("ACS_AVATAR_VIDEO_ENABLED") == "true")
check("BROWSER_JOIN_VIDEO_ENABLED=true", written.get("BROWSER_JOIN_VIDEO_ENABLED") == "true")
check("no required inputs", ch.PROFILES["in-call-browser"].requires == [])
check("the Windows host stays off", written.get("DEPLOY_MEETING_BOT_HOST") == "false")

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

print()
if _failures:
    print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
    sys.exit(1)
print("All checks passed.")
