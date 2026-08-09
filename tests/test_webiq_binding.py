"""Offline check: the Web IQ allow-list is emitted unconditionally.

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
        return evaluate(variables[key], params, variables)
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

    print("Web IQ env gating (infra/main.json)")
    print("-" * 62)

    check(
        "no key -> allow-list is STILL emitted (the regression this pins)",
        env_names(defaults, variables, **DOMAINS),
        ["WEBIQ_ALLOWED_DOMAINS"],
    )
    check(
        "API key -> key as secretRef, allow-list alongside",
        env_names(defaults, variables, webIqApiKey="k", **DOMAINS),
        ["WEBIQ_API_KEY", "WEBIQ_ALLOWED_DOMAINS"],
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
        ["WEBIQ_BASE_URL", "WEBIQ_ALLOWED_DOMAINS"],
    )
    check(
        "API key + base URL",
        env_names(
            defaults, variables,
            webIqApiKey="k", webIqBaseUrl="https://example.invalid/v3", **DOMAINS,
        ),
        ["WEBIQ_API_KEY", "WEBIQ_BASE_URL", "WEBIQ_ALLOWED_DOMAINS"],
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
