"""Offline check: the Web IQ agent tool's infrastructure gating.

Needs **no Azure resources and no credentials**. Evaluates the compiled
``infra/main.json`` with the same small ARM evaluator as test_webiq_binding.py,
so what is pinned is what ARM will actually deploy.

What matters, and why:

* ``agentWebTool=bing`` (the default) changes nothing: no Web IQ settings, no
  tool route credentials, Bing deployed exactly as before.
* ``webiq`` replaces Bing — Bing is not deployed — and turns on the model-mode
  Web IQ settings **including the allow-list**. The agent's tool runs the same
  ``search_web()``; an enabled tool without ``WEBIQ_ALLOWED_DOMAINS`` would search
  the open web.
* The caller check is emitted in exactly one mode, and a key wins. A half-set
  Entra mode (audience without callers) emits nothing, so the route stays 404
  rather than accepting tokens it cannot authorise.
* The key only ever travels as a secret, and the derived outputs never echo an
  input back into the azd env.

    uv run python tests/test_agent_web_tool_infra.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_webiq_binding import _walk_templates, evaluate, render  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = json.loads((ROOT / "infra" / "main.json").read_text(encoding="utf-8"))
PARAMETERS = json.loads((ROOT / "infra" / "main.parameters.json").read_text(encoding="utf-8"))["parameters"]

FAILURES: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    if actual == expected:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}: expected {expected!r}, got {actual!r}")
        FAILURES.append(label)


def scope_with(variable: str) -> dict:
    return next(body for body in _walk_templates(TEMPLATE) if variable in body.get("variables", {}))


def unwrap(variables: dict) -> dict:
    return {
        key: value[1:-1] if isinstance(value, str) and value.startswith("[") else value
        for key, value in variables.items()
    }


def defaults(scope: dict) -> dict:
    return {key: spec.get("defaultValue", "") for key, spec in scope.get("parameters", {}).items()}


# ── The container app ────────────────────────────────────────────────────────
app_scope = scope_with("agentWebToolEnv")
app_vars = unwrap(app_scope["variables"])
app = next(r for r in app_scope["resources"] if r["type"] == "Microsoft.App/containerApps")
DOMAINS = "mtn.com,jse.co.za"
AUD = "api://22222222-2222-2222-2222-222222222222"
ENTRA = {
    "agentWebToolAudience": AUD,
    "agentWebToolCallerOids": "oid-account,oid-project",
    "agentWebToolTenantId": "tenant-1",
}


def container(**overrides: object) -> tuple[dict, list]:
    params = {**defaults(app_scope), "webIqAllowedDomains": DOMAINS, **overrides}
    env = render(app["properties"]["template"]["containers"][0]["env"], params, app_vars)
    secrets = render(app["properties"]["configuration"]["secrets"], params, app_vars)
    names = [e["name"] for e in env]
    check("no duplicate container setting names", len(set(names)), len(names))
    return {e["name"]: e for e in env}, secrets


def tool_names(env: dict) -> list[str]:
    return sorted(n for n in env if n.startswith("AGENT_WEB_TOOL_"))


def webiq_names(env: dict) -> list[str]:
    return sorted(n for n in env if n.startswith("WEBIQ_"))


print("container: bing (default) is unchanged")
env, secrets = container()
check("no Web IQ settings", webiq_names(env), [])
check("no tool credentials", tool_names(env), [])
check("no secrets", secrets, [])
env, secrets = container(agentWebToolKey="k", webIqApiKey="w", **ENTRA)
check("credentials alone do not switch it on", (webiq_names(env), tool_names(env), secrets), ([], [], []))

print("container: webiq turns on the model-mode Web IQ settings, allow-list included")
BASE = ["WEBIQ_ALLOWED_DOMAINS", "WEBIQ_BASE_URL", "WEBIQ_LANGUAGE", "WEBIQ_REGION"]
env, secrets = container(agentWebIq=True)
check("Web IQ settings present", webiq_names(env), BASE)
check("allow-list is the supplied one", env["WEBIQ_ALLOWED_DOMAINS"]["value"], DOMAINS)
check("AGENT_MODEL still set (agent binding)", "AGENT_MODEL" in env, True)
check("no caller check configured -> no tool credentials (route 404)", tool_names(env), [])
env, secrets = container(agentWebIq=True, webIqApiKey="w")
check("Web IQ key as a secretRef", env["WEBIQ_API_KEY"], {"name": "WEBIQ_API_KEY", "secretRef": "webiq-api-key"})
check("Web IQ key secret", secrets, [{"name": "webiq-api-key", "value": "w"}])

print("container: key mode")
env, secrets = container(agentWebIq=True, agentWebToolKey="tool-key")
check("key as a secretRef, never a value", env["AGENT_WEB_TOOL_KEY"], {"name": "AGENT_WEB_TOOL_KEY", "secretRef": "agent-web-tool-key"})
check("key secret", secrets, [{"name": "agent-web-tool-key", "value": "tool-key"}])
check("only the key is emitted", tool_names(env), ["AGENT_WEB_TOOL_KEY"])
env, secrets = container(agentWebIq=True, agentWebToolKey="tool-key", webIqApiKey="w", **ENTRA)
check("key wins over a complete Entra config", tool_names(env), ["AGENT_WEB_TOOL_KEY"])
check("both secrets, one each", sorted(s["name"] for s in secrets), ["agent-web-tool-key", "webiq-api-key"])

print("container: Entra mode")
env, secrets = container(agentWebIq=True, **ENTRA)
check(
    "audience, callers and tenant emitted",
    tool_names(env),
    ["AGENT_WEB_TOOL_AUDIENCE", "AGENT_WEB_TOOL_CALLER_OIDS", "AGENT_WEB_TOOL_TENANT_ID"],
)
check("audience value", env["AGENT_WEB_TOOL_AUDIENCE"]["value"], AUD)
check("caller list value", env["AGENT_WEB_TOOL_CALLER_OIDS"]["value"], "oid-account,oid-project")
check("no secret in Entra mode", secrets, [])
env, _ = container(agentWebIq=True, agentWebToolAppId="app-id", **ENTRA)
check("client ID emitted when given", env["AGENT_WEB_TOOL_APP_ID"], {"name": "AGENT_WEB_TOOL_APP_ID", "value": "app-id"})
for missing in ENTRA:
    partial = {k: v for k, v in ENTRA.items() if k != missing}
    env, _ = container(agentWebIq=True, **partial)
    check(f"Entra without {missing} emits nothing (route stays 404)", tool_names(env), [])

# ── resources.bicep: what is deployed, and what the app is given ─────────────
res_scope = scope_with("createBing")
res_vars = unwrap(res_scope["variables"])


def res(expr: str, **params: object) -> object:
    base = {"voiceBinding": "agent", "createFoundry": True, "deployBingGrounding": True,
            "agentWebTool": "bing", "agentWebToolKey": "", "agentWebToolAudience": ""}
    return evaluate(expr, {**base, **params}, res_vars)


print("resources: which web tool is deployed")
for label, params, web_iq, bing in (
    ("bing (default)", {}, False, True),
    ("webiq", {"agentWebTool": "webiq"}, True, False),
    ("webiq, case-insensitive", {"agentWebTool": "WebIQ"}, True, False),
    ("webiq in model mode", {"agentWebTool": "webiq", "voiceBinding": "model"}, False, False),
    ("webiq with BYO Foundry", {"agentWebTool": "webiq", "createFoundry": False}, False, False),
):
    check(f"{label}: agentWebIq", res("variables('agentWebIq')", **params), web_iq)
    check(f"{label}: createBing", res("variables('createBing')", **params), bing)

outputs = {k: v["value"][1:-1] for k, v in res_scope["outputs"].items() if k.startswith("agentWebTool")}
print("resources: auth mode handed to the setup script")
for label, params, auth in (
    ("webiq, no credential", {"agentWebTool": "webiq"}, ""),
    ("webiq + key", {"agentWebTool": "webiq", "agentWebToolKey": "k"}, "key"),
    ("webiq + audience", {"agentWebTool": "webiq", "agentWebToolAudience": AUD}, "entra"),
    ("webiq + key + audience", {"agentWebTool": "webiq", "agentWebToolKey": "k", "agentWebToolAudience": AUD}, "key"),
    ("bing + key + audience", {"agentWebToolKey": "k", "agentWebToolAudience": AUD}, ""),
):
    check(f"{label}: AGENT_WEB_TOOL_AUTH", res(outputs["agentWebToolAuth"], **params), auth)

modules = {r["name"]: r for r in res_scope["resources"] if r["type"] == "Microsoft.Resources/deployments"}
connection = modules["agent-web-tool-connection"]
check("connection module deployed only in key mode", connection.get("condition"), "[variables('agentWebToolKeyed')]")
conn_resource = next(r for r in connection["properties"]["template"]["resources"] if r["type"].endswith("/connections"))
props = conn_resource["properties"]
check("connection is CustomKeys", (props["category"], props["authType"]), ("CustomKeys", "CustomKeys"))
check("connection shared like Search and Bing", props["isSharedToAll"], True)
check("key sent in x-tool-key", list(props["credentials"]["keys"]), ["[format('{0}', parameters('header'))]"])
check("header default", connection["properties"]["template"]["parameters"]["header"]["defaultValue"], "x-tool-key")
check("connection key parameter is secure", connection["properties"]["template"]["parameters"]["key"]["type"].lower(), "securestring")

app_module = modules["app"]["properties"]["parameters"]
for name, gate in (
    ("agentWebToolKey", "agentWebToolKeyed"),
    ("agentWebToolAudience", "agentWebToolEntra"),
    ("agentWebToolAppId", "agentWebToolEntra"),
    ("agentWebToolCallerOids", "agentWebToolEntra"),
    ("agentWebToolTenantId", "agentWebToolEntra"),
):
    # Bicep hoists a ternary module parameter into if(cond, {value: a}, {value: b}).
    expr = app_module[name] if isinstance(app_module[name], str) else app_module[name]["value"]
    check(f"app gets {name} only under {gate}", expr.startswith(f"[if(variables('{gate}')"), True)
check("app gets agentWebIq itself", app_module["agentWebIq"], {"value": "[variables('agentWebIq')]"})
callers = app_module["agentWebToolCallerOids"]
check("callers are the Foundry account and project identities",
      ("accountPrincipalId" in callers, "projectPrincipalId" in callers), (True, True))

foundry = modules["foundry"]["properties"]["template"]
check("foundry exposes the account identity", "accountPrincipalId" in foundry["outputs"], True)

# ── main.bicep: inputs and outputs ───────────────────────────────────────────
print("main: inputs and outputs")
main_params = TEMPLATE["parameters"]
check("agentWebTool values", main_params["agentWebTool"].get("allowedValues"), ["bing", "webiq"])
check("agentWebTool defaults to bing", main_params["agentWebTool"]["defaultValue"], "bing")
for body in _walk_templates(TEMPLATE):
    if "agentWebToolKey" in body.get("parameters", {}):
        check("agentWebToolKey is secure at every level", body["parameters"]["agentWebToolKey"]["type"].lower(), "securestring")
for parameter, substitution in {
    "agentWebTool": "${AGENT_WEB_TOOL=bing}",
    "agentWebToolKey": "${AGENT_WEB_TOOL_KEY=}",
    "agentWebToolAudience": "${AGENT_WEB_TOOL_AUDIENCE=}",
    "agentWebToolAppId": "${AGENT_WEB_TOOL_APP_ID=}",
}.items():
    check(f"azd supplies {parameter}", PARAMETERS.get(parameter), {"value": substitution})
main_outputs = set(TEMPLATE["outputs"])
check("derived outputs present", {"AGENT_WEB_TOOL_AUTH", "AGENT_WEB_TOOL_CONNECTION_NAME"} <= main_outputs, True)
check(
    "inputs are never echoed back as outputs",
    sorted(main_outputs & {"AGENT_WEB_TOOL", "AGENT_WEB_TOOL_KEY", "AGENT_WEB_TOOL_AUDIENCE", "AGENT_WEB_TOOL_APP_ID"}),
    [],
)

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED")
    sys.exit(1)
print("All checks passed.")
