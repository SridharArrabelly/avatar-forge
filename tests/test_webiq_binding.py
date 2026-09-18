"""Offline check: model-mode settings and credential-independent Web IQ scoping.

Needs **no Azure resources and no credentials**. Runs in well under a second.

Why it exists: Web IQ is the web tool in model mode, and everything about it
used to be gated on the API key::

    var webIqConfigured = !empty(webIqApiKey)

That was safe only while the key was the sole way to switch the tool on. It no
longer is. ``web_search_available()`` in ``backend/voice/tools.py`` decides at
startup by asking for a Web IQ token, so a deployment with **no key at all** can
legitimately enable ``search_web`` -- and under the old gate that deployment got
no ``WEBIQ_ALLOWED_DOMAINS`` either.

That combination is the one genuinely dangerous state. ``_allowed_domains()``
returns empty, ``build_query()`` adds no ``site:`` operators, and an open-web
tool answering to an executive assistant can cite anywhere at all, while agent
mode stays scoped to ``bingAllowedDomains``.

So the allow-list must not depend on the credential. What this pins, by
evaluating the expression in the *generated* ``infra/main.json`` rather than
re-implementing it here:

* no key                 -> allow-list still present, no empty secret
* API key                -> key as a secretRef, allow-list present
* base URL               -> passes through under either
* **the invariant**: the allow-list is emitted for every credential combination
* model mode emits realtime/Web IQ defaults, never AGENT_MODEL
* agent mode emits AGENT_MODEL, never realtime/Web IQ settings or secrets
* azd inputs pass through both module boundaries into the container

Run from the repo root:

    uv run python tests/test_webiq_binding.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "infra" / "main.json"

FAILURES: list[str] = []


def check(label: str, actual: object, expected: object) -> None:
    if actual == expected:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}: expected {expected!r}, got {actual!r}")
        FAILURES.append(label)


# --------------------------------------------------------------------------
# A very small ARM template-expression evaluator, same approach as
# test_agent_model_binding.py but covering the array/object functions this
# variable needs. Deliberately strict: an unknown function raises rather than
# returning something plausible, so the test fails loudly if the template
# starts using an expression this cannot reason about.
# --------------------------------------------------------------------------

def _split_args(body: str) -> list[str]:
    """Split a function argument list on top-level commas."""
    args, depth, quoted, current = [], 0, False, ""
    for char in body:
        if char == "'":
            quoted = not quoted
        if not quoted:
            if char in "([":
                depth += 1
            elif char in ")]":
                depth -= 1
            elif char == "," and depth == 0:
                args.append(current.strip())
                current = ""
                continue
        current += char
    if current.strip():
        args.append(current.strip())
    return args


def evaluate(expr: str, params: dict[str, str], variables: dict[str, str]) -> object:
    expr = expr.strip()

    if expr.startswith("'") and expr.endswith("'"):
        return expr[1:-1]

    match = re.match(r"^([a-zA-Z]+)\((.*)\)$", expr, re.DOTALL)
    if not match:
        raise ValueError(f"cannot parse ARM expression: {expr!r}")

    name, body = match.group(1), match.group(2)
    args = _split_args(body)

    if name == "parameters":
        key = evaluate(args[0], params, variables)
        if key not in params:
            raise ValueError(f"template reads unknown parameter {key!r}")
        return params[key]
    if name == "variables":
        key = evaluate(args[0], params, variables)
        if key not in variables:
            raise ValueError(f"template reads unknown variable {key!r}")
        value = variables[key]
        return evaluate(value, params, variables) if isinstance(value, str) else render(value, params, variables)
    if name == "empty":
        return evaluate(args[0], params, variables) == ""
    if name == "not":
        return not evaluate(args[0], params, variables)
    if name == "or":
        return any(evaluate(a, params, variables) for a in args)
    if name == "and":
        return all(evaluate(a, params, variables) for a in args)
    if name == "equals":
        return evaluate(args[0], params, variables) == evaluate(args[1], params, variables)
    if name == "toLower":
        return str(evaluate(args[0], params, variables)).lower()
    if name == "if":
        condition = evaluate(args[0], params, variables)
        return evaluate(args[1] if condition else args[2], params, variables)
    if name == "createArray":
        return [evaluate(a, params, variables) for a in args]
    if name == "createObject":
        values = [evaluate(a, params, variables) for a in args]
        return dict(zip(values[::2], values[1::2]))
    if name == "concat":
        out: list[object] = []
        for a in args:
            out.extend(evaluate(a, params, variables))
        return out

    raise ValueError(f"unsupported ARM function {name!r} in {expr!r}")


def render(value: object, params: dict, variables: dict) -> object:
    """Evaluate expressions inside a compiled ARM object, preserving literals."""
    if isinstance(value, dict):
        return {key: render(item, params, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, params, variables) for item in value]
    if isinstance(value, str) and value.startswith("[") and value.endswith("]"):
        return evaluate(value[1:-1], params, variables)
    return value


def _walk_templates(node: object):
    """Yield every template body in main.json, including nested module ones.

    ``webIqEnv`` belongs to the containerApp module, so it lands in a nested
    template rather than at the top level.
    """
    if isinstance(node, dict):
        if "variables" in node and isinstance(node["variables"], dict):
            yield node
        for value in node.values():
            yield from _walk_templates(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_templates(item)


def load_webiq_scope() -> tuple[dict[str, str], dict[str, str]]:
    if not TEMPLATE.exists():
        print(f"FAIL  {TEMPLATE} not found -- run: az bicep build --file infra/main.bicep")
        raise SystemExit(1)

    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    for body in _walk_templates(template):
        if "webIqEnv" not in body.get("variables", {}):
            continue
        defaults = {
            key: spec.get("defaultValue", "")
            for key, spec in body.get("parameters", {}).items()
        }
        variables = {
            key: value[1:-1] if isinstance(value, str) and value.startswith("[") else value
            for key, value in body["variables"].items()
        }
        return defaults, variables

    print("FAIL  no template in main.json defines a 'webIqEnv' variable")
    raise SystemExit(1)


def env_names(defaults: dict[str, str], variables: dict[str, str], **overrides: str) -> list[str]:
    """Resolve webIqEnv and return just the env var names it emits."""
    params = dict(defaults)
    params.update(overrides)
    emitted = evaluate(variables["webIqEnv"], params, variables)
    return [entry["name"] for entry in emitted]


def secret_names(defaults: dict[str, str], variables: dict[str, str], **overrides: str) -> list[str]:
    params = dict(defaults)
    params.update(overrides)
    emitted = evaluate(variables["webIqSecrets"], params, variables)
    return [entry["name"] for entry in emitted]


# A realistic allow-list: main.bicep derives these from bingAllowedDomains.
DOMAINS = {"webIqAllowedDomains": "mtn.com,sashares.co.za"}


def main() -> int:
    defaults, variables = load_webiq_scope()
    defaults["voiceBinding"] = "model"
    base_names = ["WEBIQ_BASE_URL", "WEBIQ_LANGUAGE", "WEBIQ_REGION", "WEBIQ_ALLOWED_DOMAINS"]

    print("Web IQ env gating (infra/main.json)")
    print("-" * 62)

    check(
        "no key -> allow-list is STILL emitted (the regression this pins)",
        env_names(defaults, variables, **DOMAINS),
        base_names,
    )
    check(
        "API key -> key as secretRef, allow-list alongside",
        env_names(defaults, variables, webIqApiKey="k", **DOMAINS),
        ["WEBIQ_API_KEY", *base_names],
    )

    print()
    print("No empty secret is declared for a keyless deployment")
    print("-" * 62)
    check(
        "no key -> no webiq-api-key secret",
        secret_names(defaults, variables, **DOMAINS),
        [],
    )
    check(
        "API key -> secret declared",
        secret_names(defaults, variables, webIqApiKey="k", **DOMAINS),
        ["webiq-api-key"],
    )

    print()
    print("The invariant: the allow-list never depends on the credential")
    print("-" * 62)
    # web_search_available() can enable search_web with no key at all, so there
    # is no combination in which the app may be searching without a host scope.
    for label, overrides in (
        ("no key", {}),
        ("API key", {"webIqApiKey": "k"}),
    ):
        names = env_names(defaults, variables, **overrides, **DOMAINS)
        check(
            f"{label}: WEBIQ_ALLOWED_DOMAINS present",
            "WEBIQ_ALLOWED_DOMAINS" in names,
            True,
        )

    # The base URL is optional -- the code defaults it -- but when supplied it
    # must reach the app whether or not a key is set.
    print()
    print("Optional base URL reaches the app with or without a key")
    print("-" * 62)
    check(
        "no key + base URL",
        env_names(
            defaults, variables,
            webIqBaseUrl="https://example.invalid/v3", **DOMAINS,
        ),
        base_names,
    )
    check(
        "API key + base URL",
        env_names(
            defaults, variables,
            webIqApiKey="k", webIqBaseUrl="https://example.invalid/v3", **DOMAINS,
        ),
        ["WEBIQ_API_KEY", *base_names],
    )

    print()
    print("Effective container settings and mode isolation")
    print("-" * 62)
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    scope = next(body for body in _walk_templates(template) if "webIqEnv" in body["variables"])
    app = next(resource for resource in scope["resources"] if resource["type"] == "Microsoft.App/containerApps")
    container_env = app["properties"]["template"]["containers"][0]["env"]

    def settings(**overrides: str) -> dict:
        params = {**defaults, **DOMAINS, **overrides}
        entries = render(container_env, params, variables)
        check("no duplicate container setting names", len({entry["name"] for entry in entries}), len(entries))
        return {entry["name"]: entry for entry in entries}

    model = settings(agentModel="chat-deployment")
    expected_defaults = {
        "VOICELIVE_MODEL": "gpt-realtime-2",
        "WEBIQ_BASE_URL": "https://api.microsoft.ai/v3",
        "WEBIQ_LANGUAGE": "en",
        "WEBIQ_REGION": "ZA",
        "WEBIQ_ALLOWED_DOMAINS": DOMAINS["webIqAllowedDomains"],
    }
    for name, value in expected_defaults.items():
        check(f"model default {name}", model.get(name), {"name": name, "value": value})
    check("model omits AGENT_MODEL", "AGENT_MODEL" in model, False)
    check("keyless model omits WEBIQ_API_KEY", "WEBIQ_API_KEY" in model, False)

    empty = settings(voiceLiveModel="", webIqBaseUrl="", webIqLanguage="", webIqRegion="")
    for name, value in expected_defaults.items():
        check(f"empty input preserves {name} default", empty.get(name), {"name": name, "value": value})

    custom = settings(
        voiceLiveModel="gpt-realtime", webIqBaseUrl="https://example.invalid/v3",
        webIqLanguage="fr", webIqRegion="FR", webIqApiKey="test-key",
        webIqAllowedDomains="example.org",
    )
    for name, value in {
        "VOICELIVE_MODEL": "gpt-realtime",
        "WEBIQ_BASE_URL": "https://example.invalid/v3",
        "WEBIQ_LANGUAGE": "fr",
        "WEBIQ_REGION": "FR",
        "WEBIQ_ALLOWED_DOMAINS": "example.org",
    }.items():
        check(f"override {name}", custom.get(name), {"name": name, "value": value})
    check(
        "API key is a secret reference, never a plain container value",
        custom.get("WEBIQ_API_KEY"),
        {"name": "WEBIQ_API_KEY", "secretRef": "webiq-api-key"},
    )
    check(
        "API key secret contains the supplied value",
        render(app["properties"]["configuration"]["secrets"], {**defaults, "webIqApiKey": "test-key"}, variables),
        [{"name": "webiq-api-key", "value": "test-key"}],
    )
    for binding in ("agent", "AGENT"):
        agent = settings(
            voiceBinding=binding, agentModel="chat-deployment", voiceLiveModel="gpt-realtime",
            webIqApiKey="test-key", webIqBaseUrl="https://example.invalid/v3",
        )
        check("agent keeps AGENT_MODEL", agent.get("AGENT_MODEL"), {"name": "AGENT_MODEL", "value": "chat-deployment"})
        check("agent omits VOICELIVE_MODEL", "VOICELIVE_MODEL" in agent, False)
        check("agent omits all Web IQ settings", [name for name in agent if name.startswith("WEBIQ_")], [])
        check("agent omits Web IQ secret even with a key", secret_names(defaults, variables, voiceBinding=binding, webIqApiKey="test-key"), [])
    upper_model = settings(voiceBinding="MODEL")
    check("model binding is case insensitive", upper_model, {**model, "VOICE_BINDING": {"name": "VOICE_BINDING", "value": "MODEL"}})

    print()
    print("azd parameter and nested module wiring")
    print("-" * 62)
    parameter_file = json.loads((ROOT / "infra" / "main.parameters.json").read_text(encoding="utf-8"))
    for parameter, substitution in {
        "voiceLiveModel": "${VOICELIVE_MODEL=}",
        "webIqBaseUrl": "${WEBIQ_BASE_URL=}",
        "webIqAllowedDomains": "${WEBIQ_ALLOWED_DOMAINS=}",
        "webIqApiKey": "${WEBIQ_API_KEY=}",
        "webIqLanguage": "${WEBIQ_LANGUAGE=en}",
        "webIqRegion": "${WEBIQ_REGION=ZA}",
    }.items():
        check(f"azd supplies {parameter}", parameter_file["parameters"].get(parameter), {"value": substitution})
        forwarding = []
        for body in _walk_templates(template):
            for resource in body.get("resources", []):
                if resource["type"] != "Microsoft.Resources/deployments":
                    continue
                value = resource["properties"].get("parameters", {}).get(parameter)
                if value is not None:
                    forwarding.append(value["value"])
        expression = f"[parameters('{parameter}')]"
        expected = [expression, expression]
        if parameter == "webIqAllowedDomains":
            expected[0] = "[variables('webIqEffectiveDomains')]"
        check(f"{parameter} crosses both module boundaries", forwarding, expected)
    for body in (template, scope):
        check("API key parameter stays secure", body["parameters"]["webIqApiKey"]["type"].lower(), "securestring")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
