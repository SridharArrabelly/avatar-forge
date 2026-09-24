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

    uv run python tests/test_preflight.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

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
        "binding: agent mode uses the built-in name when AGENT_NAME is unset",
        r["Agent mode: AGENT_NAME"].ok
        and pf.DEFAULT_AGENT_NAME in r["Agent mode: AGENT_NAME"].detail
        and "default" in r["Agent mode: AGENT_NAME"].detail,
    )

    r = binding({"VOICE_BINDING": "agent", "AGENT_NAME": "a"})
    check("binding: agent mode with AGENT_NAME passes", r["Agent mode: AGENT_NAME"].ok)
    check(
        "binding: an explicit AGENT_NAME overrides the default",
        r["Agent mode: AGENT_NAME"].detail == "a",
    )
    check(
        "binding: agent mode does not check model-mode inputs",
        not any(k.startswith("Model mode") for k in r),
    )
    template = json.loads((ROOT / "infra" / "main.json").read_text(encoding="utf-8"))
    check(
        "binding: preflight and infrastructure use the same default agent name",
        pf.DEFAULT_AGENT_NAME == template["parameters"]["agentName"]["defaultValue"],
    )

    r = binding({"VOICE_BINDING": "model"})
    check(
        "binding: model mode without a Web IQ key still passes, never blocks",
        r["Model mode: Web IQ"].ok and r["Model mode: Web IQ"].warn_only,
    )
    check(
        "binding: keyless model mode is reported as Entra, not as a defect",
        "Entra" in r["Model mode: Web IQ"].detail,
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
        r["Model mode: Web IQ"].ok and "key set" in r["Model mode: Web IQ"].detail,
    )

    # --- agent web tool ---------------------------------------------------
    def web_tool(cfg):
        return {r.name: r for r in pf.check_agent_web_tool(cfg)}

    r = web_tool({})
    check("web tool: defaults to bing and says so",
          r["Agent web tool"].ok and "bing" in r["Agent web tool"].detail and "default" in r["Agent web tool"].detail)
    check("web tool: bing needs no auth lines", list(r) == ["Agent web tool"])
    r = web_tool({"AGENT_WEB_TOOL": "google"})
    check("web tool: an invalid value fails and is the only result",
          len(r) == 1 and not r["Agent web tool"].ok and not r["Agent web tool"].warn_only)
    check("web tool: silent in model mode when left at bing", web_tool({"VOICE_BINDING": "model"}) == {})
    r = web_tool({"VOICE_BINDING": "model", "AGENT_WEB_TOOL": "webiq"})
    check("web tool: webiq in model mode is reported as ignored, never blocks",
          r["Agent web tool"].ok and "ignored" in r["Agent web tool"].detail)
    # With an existing Foundry account bicep deploys neither the connection nor
    # the caller identities, so the agent would silently get no web tool.
    r = web_tool({"AGENT_WEB_TOOL": "webiq", "FOUNDRY_ACCOUNT_NAME": "byo"})
    check("web tool: webiq with BYO Foundry blocks", not r["Agent web tool"].ok and not r["Agent web tool"].warn_only)
    r = web_tool({"AGENT_WEB_TOOL": "WebIQ"})
    check("web tool: webiq is case-insensitive", r["Agent web tool"].ok and "webiq" in r["Agent web tool"].detail)
    check("web tool: no credential -> managed identity with fallback explained",
          r["Agent web tool: caller auth"].ok and "managed identity" in r["Agent web tool: caller auth"].detail
          and "key" in r["Agent web tool: caller auth"].detail)
    check("web tool: keyless Web IQ access is a note, not a defect", r["Agent web tool: Web IQ access"].ok)
    r = web_tool({"AGENT_WEB_TOOL": "webiq", "AGENT_WEB_TOOL_AUDIENCE": "api://x"})
    check("web tool: audience -> managed identity", "api://x" in r["Agent web tool: caller auth"].detail)
    strong = "k" * pf.AGENT_WEB_TOOL_MIN_KEY_CHARS
    r = web_tool({"AGENT_WEB_TOOL": "webiq", "AGENT_WEB_TOOL_KEY": strong, "AGENT_WEB_TOOL_AUDIENCE": "api://x"})
    check("web tool: a key wins over an audience", "shared key" in r["Agent web tool: caller auth"].detail)
    r = web_tool({"AGENT_WEB_TOOL": "webiq", "AGENT_WEB_TOOL_KEY": "short"})
    check("web tool: a short key on a public route blocks",
          not r["Agent web tool: caller auth"].ok and not r["Agent web tool: caller auth"].warn_only)
    check("web tool: the short key itself is never printed", "short" not in r["Agent web tool: caller auth"].detail)

    # --- agent web tool: settling auth -----------------------------------
    class FakeDirectory:
        """Just enough of `az ad` to drive _ensure_web_tool_app."""

        def __init__(self, apps=None, fail=()):
            self.apps = apps or {}  # appId -> {"name", "uris", "sp"}
            self.fail = set(fail)
            self.calls: list[list[str]] = []

        def run(self, args):
            self.calls.append(args)
            verb = " ".join(args[:3])
            if verb in self.fail:
                return 1, "", "ERROR: Insufficient privileges to complete the operation.\nmore"
            opt = dict(zip(args[3::2], args[4::2]))
            if verb == "ad app show":
                app = self.apps.get(opt["--id"])
                return (0, json.dumps(app["uris"]), "") if app else (3, "", "ERROR: not found")
            if verb == "ad app list":
                return 0, json.dumps([
                    {"appId": k, "name": v["name"], "uris": v["uris"]} for k, v in self.apps.items()
                    if v["name"].startswith(opt["--display-name"])
                ]), ""
            if verb == "ad app create":
                app_id = f"app-{len(self.apps) + 1}"
                self.apps[app_id] = {"name": opt["--display-name"], "uris": [], "sp": False}
                return 0, app_id + "\n", ""
            if verb == "ad app update":
                self.apps[opt["--id"]]["uris"] = [opt["--identifier-uris"]]
                return 0, "", ""
            if verb == "ad sp show":
                return (0, "sp", "") if self.apps[opt["--id"]]["sp"] else (3, "", "ERROR: not found")
            if verb == "ad sp create":
                self.apps[opt["--id"]]["sp"] = True
                return 0, "sp", ""
            raise AssertionError(f"unexpected az call: {args}")

    def settle(cfg, directory, env_ok=True):
        saved.clear()
        with _Patch(_run=directory.run, _azd_env_set=(fake_set if env_ok else lambda *_a: False)):
            pf._settle_agent_web_tool_auth(cfg)
        return dict(saved)

    base = {"AGENT_WEB_TOOL": "webiq", "AZURE_ENV_NAME": "ava"}
    name = pf.AGENT_WEB_TOOL_APP_PREFIX + "ava"

    for label, cfg in (
        ("bing", {"AZURE_ENV_NAME": "ava"}),
        ("model mode", {**base, "VOICE_BINDING": "model"}),
        ("BYO Foundry", {**base, "FOUNDRY_ACCOUNT_NAME": "byo"}),
        ("key already set", {**base, "AGENT_WEB_TOOL_KEY": strong}),
        ("audience already set", {**base, "AGENT_WEB_TOOL_AUDIENCE": "api://x"}),
    ):
        d = FakeDirectory()
        check(f"settle: {label} -> touches nothing", settle(dict(cfg), d) == {} and d.calls == [])

    d = FakeDirectory()
    cfg = dict(base)
    got = settle(cfg, d)
    app_id = next(iter(d.apps))
    check("settle: creates one single-tenant app named for the env",
          list(d.apps.values())[0]["name"] == name
          and any(c[:3] == ["ad", "app", "create"] and "AzureADMyOrg" in c for c in d.calls))
    check("settle: identifier URI is api://<appId>", d.apps[app_id]["uris"] == [f"api://{app_id}"])
    check("settle: service principal created (Entra needs it to issue tokens)", d.apps[app_id]["sp"])
    check("settle: audience and app ID saved for bicep",
          got == {"AGENT_WEB_TOOL_AUDIENCE": f"api://{app_id}", "AGENT_WEB_TOOL_APP_ID": app_id})
    check("settle: in-memory config updated too", cfg.get("AGENT_WEB_TOOL_AUDIENCE") == f"api://{app_id}")
    check("settle: no key generated when managed identity works", "AGENT_WEB_TOOL_KEY" not in got)

    d = FakeDirectory({"app-9": {"name": name, "uris": ["api://app-9"], "sp": True},
                       "app-8": {"name": name + "-other", "uris": [], "sp": False}})
    got = settle(dict(base), d)
    check("settle: reuses the exact-name app on a re-run, creates nothing",
          got.get("AGENT_WEB_TOOL_APP_ID") == "app-9" and len(d.apps) == 2
          and not any(c[:3] in (["ad", "app", "create"], ["ad", "app", "update"], ["ad", "sp", "create"]) for c in d.calls))

    d = FakeDirectory({"app-7": {"name": "renamed", "uris": [], "sp": False}})
    got = settle({**base, "AGENT_WEB_TOOL_APP_ID": "app-7"}, d)
    check("settle: a stored app ID is reused and completed",
          got.get("AGENT_WEB_TOOL_AUDIENCE") == "api://app-7" and d.apps["app-7"]["sp"] and len(d.apps) == 1)

    d = FakeDirectory()
    settle({**base, "AGENT_WEB_TOOL_SERVICE_MANAGEMENT_REFERENCE": "ref-1"}, d)
    create = next(c for c in d.calls if c[:3] == ["ad", "app", "create"])
    check("settle: service management reference passed when a tenant requires one",
          create[create.index("--service-management-reference") + 1] == "ref-1")

    for label, fail in (
        ("create refused", ["ad app create"]),
        ("directory unreadable", ["ad app list"]),
        ("identifier URI refused", ["ad app update"]),
        ("service principal refused", ["ad sp create"]),
    ):
        d = FakeDirectory(fail=fail)
        got = settle(dict(base), d)
        key = got.get("AGENT_WEB_TOOL_KEY", "")
        check(f"settle: {label} -> falls back to a generated key",
              len(key) >= pf.AGENT_WEB_TOOL_MIN_KEY_CHARS and "AGENT_WEB_TOOL_AUDIENCE" not in got)
    check("settle: generated keys are not reused between runs",
          settle(dict(base), FakeDirectory(fail=["ad app create"]))["AGENT_WEB_TOOL_KEY"] != key)

    d = FakeDirectory()
    check("settle: never raises when azd cannot store anything", settle(dict(base), d, env_ok=False) == {})

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all preflight helper checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
