"""Tests for the preflight helpers that settle the deploy target.

Needs no Azure and no credentials — `az`, `azd` and `input()` are all replaced
with fakes.

What is worth pinning here is that these helpers run in TWO very different
contexts and must behave differently in each:

  * a human at a terminal running `uv run python scripts/preflight.py`, where
    prompting for a missing subscription / region / resource group is the whole
    point, because `azd up` would otherwise stop for them one at a time;
  * the `preprovision` hook inside `azd up`, where there is no TTY. A prompt
    there would hang the deploy forever with no visible question.

The no-TTY cases are therefore the load-bearing ones. They are easy to break by
"simplifying" a helper, and the breakage does not show up in a normal
interactive run — only in CI or in the hook, where it looks like azd hanging.

    uv run python scripts/test_preflight.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import preflight as pf  # noqa: E402

_failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        _failures.append(name)


class _Patch:
    """Swap module attributes for the duration of a block, then restore them.

    Handles names that are not module attributes at all (``input`` is a
    builtin). Assigning ``preflight.input`` shadows the builtin for code inside
    that module, because a global is resolved before a builtin; on exit the
    name is removed again so the builtin is visible once more.
    """

    _MISSING = object()

    def __init__(self, **attrs: object) -> None:
        self._new = attrs
        self._old: dict[str, object] = {}

    def __enter__(self) -> "_Patch":
        for k, v in self._new.items():
            self._old[k] = getattr(pf, k, self._MISSING)
            setattr(pf, k, v)
        return self

    def __exit__(self, *_exc: object) -> None:
        for k, v in self._old.items():
            if v is self._MISSING:
                delattr(pf, k)
            else:
                setattr(pf, k, v)


class _Stdin:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _tty(is_tty: bool) -> _Patch:
    return _Patch(sys=type("S", (), {"stdin": _Stdin(is_tty)})())


def main() -> int:
    print("preflight helpers")

    saved: list[tuple[str, str]] = []

    def fake_set(var: str, value: str) -> bool:
        saved.append((var, value))
        return True

    # --- already settled: never prompt, never re-save ----------------------
    with _tty(True), _Patch(_azd_env_set=fake_set):
        saved.clear()
        got = pf._settle_subscription({"AZURE_SUBSCRIPTION_ID": "sub-123"})
        check("subscription: existing value returned as-is", got == "sub-123")
        check("subscription: existing value not re-saved", saved == [])

        got = pf._settle_resource_group({"AZURE_RESOURCE_GROUP": "rg-a"})
        check("resource group: reads AZURE_RESOURCE_GROUP", got == "rg-a")
        got = pf._settle_resource_group({"AZURE_RESOURCE_GROUP_NAME": "rg-b"})
        check("resource group: reads AZURE_RESOURCE_GROUP_NAME too", got == "rg-b")

    # --- no TTY (the preprovision hook): return empty, NEVER prompt --------
    def explode(_prompt: str = "") -> str:
        raise AssertionError("prompted without a TTY")

    with _tty(False), _Patch(_azd_env_set=fake_set, input=explode):
        saved.clear()
        check("subscription: no TTY returns empty", pf._settle_subscription({}) == "")
        check("resource group: no TTY returns empty", pf._settle_resource_group({}) == "")
        check("location: no TTY returns empty", pf._prompt_for_location() == "")
        check("no TTY saves nothing", saved == [])

    # --- interactive, accepting the offered default ------------------------
    def accept_default(_prompt: str = "") -> str:
        return ""  # user pressed Enter

    def fake_run_ok(_args: list[str]) -> tuple[int, str, str]:
        return 0, "sub-from-az\tMy Subscription\n", ""

    with _tty(True), _Patch(_azd_env_set=fake_set, input=accept_default, _run=fake_run_ok):
        saved.clear()
        got = pf._settle_subscription({})
        check("subscription: defaults to az's current subscription", got == "sub-from-az")
        check(
            "subscription: persisted to the azd env",
            saved == [("AZURE_SUBSCRIPTION_ID", "sub-from-az")],
        )

        saved.clear()
        got = pf._settle_resource_group({"AZURE_ENV_NAME": "avatar-test"})
        check("resource group: defaults to rg-<env name>", got == "rg-avatar-test")
        check(
            "resource group: persisted under the name azd reads",
            saved == [("AZURE_RESOURCE_GROUP_NAME", "rg-avatar-test")],
        )

        saved.clear()
        got = pf._prompt_for_location()
        check("location: defaults to swedencentral", got == "swedencentral")
        check("location: persisted to the azd env", saved == [("AZURE_LOCATION", "swedencentral")])

    # --- interactive, typing an explicit answer ----------------------------
    with _tty(True), _Patch(_azd_env_set=fake_set, input=lambda _p="": "westeurope"):
        saved.clear()
        check("location: explicit answer wins over the default", pf._prompt_for_location() == "westeurope")

    # --- the region offered must actually be supported ---------------------
    supported = pf.VOICELIVE_REGIONS & pf.AVATAR_REGIONS
    check("offered region supports Voice Live AND the avatar", "swedencentral" in supported)

    # --- degrade quietly rather than guessing ------------------------------
    def fake_run_signed_out(_args: list[str]) -> tuple[int, str, str]:
        return 1, "", "Please run 'az login'"

    with _tty(True), _Patch(_azd_env_set=fake_set, input=explode, _run=fake_run_signed_out):
        check(
            "subscription: signed out returns empty instead of prompting blind",
            pf._settle_subscription({}) == "",
        )

    def cancel(_prompt: str = "") -> str:
        raise KeyboardInterrupt

    with _tty(True), _Patch(_azd_env_set=fake_set, input=cancel, _run=fake_run_ok):
        saved.clear()
        check("subscription: Ctrl-C returns empty", pf._settle_subscription({}) == "")
        check("resource group: Ctrl-C returns empty", pf._settle_resource_group({"AZURE_ENV_NAME": "e"}) == "")
        check("location: Ctrl-C returns empty", pf._prompt_for_location() == "")
        check("cancelling saves nothing", saved == [])

    # --- no env name means no sensible default; do not invent one ----------
    with _tty(True), _Patch(_azd_env_set=fake_set, input=accept_default):
        saved.clear()
        check("resource group: no env name yields empty, not 'rg-'", pf._settle_resource_group({}) == "")
        check("resource group: nothing saved when empty", saved == [])

    # --- an unsaveable value is still usable for this run ------------------
    with _tty(True), _Patch(_azd_env_set=lambda *_a: False, input=accept_default, _run=fake_run_ok):
        check(
            "subscription: returned even when azd could not store it",
            pf._settle_subscription({}) == "sub-from-az",
        )

    # --- meeting-bot input checks -----------------------------------------
    check(
        "dns label: silent when unset",
        pf.check_dns_label({}, "swedencentral") is None,
    )
    bad = pf.check_dns_label({"MEETING_BOT_DNS_LABEL": "Bad_Label"}, "swedencentral")
    check("dns label: invalid label caught", bad is not None and not bad.ok)
    good = pf.check_dns_label({"MEETING_BOT_DNS_LABEL": "avatar-bot-contoso"}, "swedencentral")
    check("dns label: valid label passes", good is not None and good.ok)

    # --- voice binding ----------------------------------------------------
    # The binding is deployment-wide and each mode has a different hard
    # requirement, so a wrong value fails at runtime rather than at deploy time.
    def binding(cfg):
        return {r.name: r for r in pf.check_voice_binding(cfg)}

    r = binding({"VOICE_BINDING": "banana"})
    check(
        "binding: an invalid value fails and is the only result",
        len(r) == 1 and not r["Voice binding"].ok,
    )

    r = binding({"AGENT_NAME": "a"})
    check(
        "binding: unset defaults to agent and says so",
        r["Voice binding"].ok and "default" in r["Voice binding"].detail,
    )

    r = binding({"VOICE_BINDING": "AGENT", "AGENT_NAME": "a"})
    check("binding: value is case-insensitive", r["Voice binding"].ok)

    r = binding({"VOICE_BINDING": "agent"})
    check(
        "binding: agent mode without AGENT_NAME hard-fails",
        not r["Agent mode: AGENT_NAME"].ok
        and not r["Agent mode: AGENT_NAME"].warn_only,
    )

    r = binding({"VOICE_BINDING": "agent", "AGENT_NAME": "a"})
    check("binding: agent mode with AGENT_NAME passes", r["Agent mode: AGENT_NAME"].ok)
    check(
        "binding: agent mode does not check model-mode inputs",
        not any(k.startswith("Model mode") for k in r),
    )

    r = binding({"VOICE_BINDING": "model"})
    check(
        "binding: model mode without Web IQ warns, never blocks",
        not r["Model mode: WEBIQ_API_KEY"].ok
        and r["Model mode: WEBIQ_API_KEY"].warn_only,
    )
    check(
        "binding: model mode does not demand AGENT_NAME",
        not any(k.startswith("Agent mode") for k in r),
    )
    # Bicep gates Bing on the binding, so model mode never provisions it whatever
    # DEPLOY_BING_GROUNDING says. Preflight must therefore REPORT that (so the
    # user is not surprised by a missing resource) without asking them to fix
    # anything. Asserting `ok` is what pins the difference: the previous version
    # reported the same line as a defect with an `azd env set` remedy.
    check(
        "binding: model mode reports Bing as skipped, not as a problem",
        "Model mode: Bing grounding" in r
        and r["Model mode: Bing grounding"].ok
        and r["Model mode: Bing grounding"].warn_only
        and "azd env set DEPLOY_BING_GROUNDING" not in (r["Model mode: Bing grounding"].fix or ""),
    )

    r = binding({"VOICE_BINDING": "model", "DEPLOY_BING_GROUNDING": "false"})
    check(
        "binding: no Bing warning once it is switched off",
        "Model mode: Bing grounding" not in r,
    )

    r = binding({"VOICE_BINDING": "model", "WEBIQ_API_KEY": "k"})
    check(
        "binding: model mode with a Web IQ key passes",
        r["Model mode: WEBIQ_API_KEY"].ok,
    )

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all preflight helper checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
