"""Offline check: the agent binds to a model deployment that actually exists.

Needs **no Azure resources and no credentials**. Runs in well under a second.

Why it exists: three template parameters all defaulted to the literal ``gpt-5.4``
and two of them are required to agree.

* ``modelName``           -- which model to pull from the catalogue
  (``properties.model.name`` on the deployment resource)
* ``modelDeploymentName`` -- what to *call* that deployment (its resource name)
* ``agentModel``          -- the deployment the Foundry agent binds to at runtime

The first two are distinct by Azure's own design: you can deploy ``gpt-5.4``
under the name ``chat-prod``, and ARM needs both fields. The third is not
independent at all on a greenfield deploy -- the agent has to bind to whatever
``modelDeploymentName`` just created. Leaving it as a third literal meant setting
``MODEL_DEPLOYMENT_NAME=chat-prod`` produced a deployment called ``chat-prod``
and an agent bound to a ``gpt-5.4`` that was never created. Nothing outside the
template reads ``MODEL_DEPLOYMENT_NAME``, so nothing could catch the mismatch.

What it pins, by evaluating the expression in the *generated* ``infra/main.json``
rather than re-implementing it here:

* explicit ``AGENT_MODEL`` always wins, greenfield or BYO
* greenfield with ``AGENT_MODEL`` unset follows ``modelDeploymentName``
* BYO with ``AGENT_MODEL`` unset keeps the historical ``gpt-5.4`` default, since
  the deployment lives in an account this template did not create
* the default-everything case is byte-identical to the old behaviour

Run from the repo root:

    uv run python tests/test_agent_model_binding.py
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
# A very small ARM template-expression evaluator.
#
# Only the handful of functions this template's variables actually use. It is
# deliberately strict: an unknown function raises rather than silently
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
    if name == "if":
        condition = evaluate(args[0], params, variables)
        return evaluate(args[1] if condition else args[2], params, variables)

    raise ValueError(f"unsupported ARM function {name!r} in {expr!r}")


def load_template() -> tuple[dict[str, str], dict[str, str]]:
    if not TEMPLATE.exists():
        print(f"FAIL  {TEMPLATE} not found -- run: az bicep build --file infra/main.bicep")
        raise SystemExit(1)

    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    defaults = {
        key: spec.get("defaultValue", "")
        for key, spec in template.get("parameters", {}).items()
    }
    raw_variables = template.get("variables", {})
    variables = {
        key: value[1:-1] if isinstance(value, str) and value.startswith("[") else value
        for key, value in raw_variables.items()
    }
    return defaults, variables


def resolve(variables: dict[str, str], defaults: dict[str, str], **overrides: str) -> object:
    params = dict(defaults)
    params.update(overrides)
    if "resolvedAgentModel" not in variables:
        print("  FAIL  main.json has no 'resolvedAgentModel' variable")
        FAILURES.append("resolvedAgentModel missing")
        raise SystemExit(1)
    return evaluate(variables["resolvedAgentModel"], params, variables)


# Identifying a BYO Foundry needs all three of these set (createFoundry in main.bicep).
BYO = {
    "foundryAccountName": "existing-foundry",
    "foundryResourceGroup": "rg-shared-ai",
    "foundryProjectEndpoint": "https://existing-foundry.services.ai.azure.com/api/projects/x",
}


def main() -> int:
    defaults, variables = load_template()

    print("Agent model binding (infra/main.json)")
    print("-" * 62)

    # The whole point: the agent must bind to the deployment that was created.
    check(
        "greenfield, custom deployment name -> agent follows it",
        resolve(variables, defaults, modelDeploymentName="chat-prod"),
        "chat-prod",
    )
    check(
        "greenfield, defaults -> unchanged from the old literal",
        resolve(variables, defaults),
        "gpt-5.4",
    )
    check(
        "greenfield, custom model but default deployment name",
        resolve(variables, defaults, modelName="gpt-4.1"),
        "gpt-5.4",
    )

    # An explicit value always wins, so nobody loses control.
    check(
        "explicit AGENT_MODEL overrides the derivation",
        resolve(variables, defaults, agentModel="my-own", modelDeploymentName="chat-prod"),
        "my-own",
    )
    check(
        "explicit AGENT_MODEL wins on BYO too",
        resolve(variables, defaults, agentModel="byo-deployment", **BYO),
        "byo-deployment",
    )

    # BYO cannot derive: this template did not name that deployment.
    check(
        "BYO, AGENT_MODEL unset -> historical default, not the greenfield name",
        resolve(variables, defaults, modelDeploymentName="chat-prod", **BYO),
        "gpt-5.4",
    )

    # Guard the distinction that started all this.
    print()
    print("Model name vs deployment name are separate ARM fields")
    print("-" * 62)
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    blob = json.dumps(template)
    check(
        "deployment resource is named by modelDeploymentName",
        "parameters('modelDeploymentName')" in blob,
        True,
    )
    check(
        "properties.model.name comes from modelName",
        "parameters('modelName')" in blob,
        True,
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
