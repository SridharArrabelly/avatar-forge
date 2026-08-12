"""Offline contract for the private audit path (#122).

An org policy sweep sets publicNetworkAccess=Disabled on the audit Cosmos
account. The account is correct to be private; what was missing was a route for
the app to reach it, so the sweep took the container down with it.

Two things matter here, and the second matters more than the first:

  1. When private networking is on, the app reaches Cosmos over a private
     endpoint and the account is genuinely closed to the internet.
  2. When it is off -- which is the default, and what every existing deployment
     runs -- the template must be byte-for-byte inert. A Container Apps
     environment cannot change network type in place, so an accidental
     vnetConfiguration would force every deployment to be torn down and
     recreated, changing the app's FQDN in the process.

    uv run python tests/test_private_networking.py
"""
from __future__ import annotations

import builtins
import ipaddress
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = json.loads((ROOT / "infra" / "main.json").read_text(encoding="utf-8"))
PARAMS = json.loads((ROOT / "infra" / "main.parameters.json").read_text(encoding="utf-8"))

failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        failures.append(name)


def nested(template: dict, deployment_name: str) -> dict:
    for resource in template["resources"]:
        if resource.get("name") == deployment_name:
            return resource["properties"]["template"]
    raise AssertionError(f"no nested deployment named {deployment_name!r}")


RESOURCES = nested(TEMPLATE, "resources")
MODULES = {r.get("name"): r for r in RESOURCES["resources"]}


print("Off by default, and gated on the audit trail existing")
check(
    "enablePrivateNetworking defaults to false",
    TEMPLATE["parameters"]["enablePrivateNetworking"]["defaultValue"] == "false",
)
gate = RESOURCES["variables"]["privateNetworkingEnabled"]
check(
    "the flag alone is not enough -- auditEnabled is required too",
    "auditEnabled" in gate and "enablePrivateNetworking" in gate,
)
check(
    "azd exposes it as ENABLE_PRIVATE_NETWORKING",
    PARAMS["parameters"]["enablePrivateNetworking"]["value"]
    == "${ENABLE_PRIVATE_NETWORKING=false}",
)
for module in ("network", "cosmos-audit-pe"):
    check(
        f"the {module} module only deploys behind the gate",
        MODULES[module].get("condition") == "[variables('privateNetworkingEnabled')]",
    )


print("\nThe off path leaves the environment exactly as it was")
cae = MODULES["cae"]["properties"]["template"]
network_properties = cae["variables"]["networkProperties"]
check(
    "no subnet means no network properties at all",
    network_properties.startswith(
        "[if(empty(parameters('infrastructureSubnetId')), createObject(),"
    ),
)
env_properties = cae["resources"][0]["properties"]
check(
    "the environment's original properties are preserved verbatim",
    env_properties.startswith("[union(createObject('appLogsConfiguration'")
    and "'zoneRedundant', false())" in env_properties
    and env_properties.endswith("variables('networkProperties'))]"),
)
check(
    "vnetConfiguration is reachable only through that empty-subnet guard",
    "vnetConfiguration" in network_properties
    and "vnetConfiguration" not in env_properties,
)
cosmos = MODULES["cosmos-audit"]["properties"]
check(
    "Cosmos stays publicly reachable unless the private path exists",
    cosmos["parameters"]["publicNetworkAccess"]
    == "[if(variables('privateNetworkingEnabled'), createObject('value', 'Disabled'), "
    "createObject('value', 'Enabled'))]",
)
check(
    "the Cosmos module still defaults to Enabled on its own",
    cosmos["template"]["parameters"]["publicNetworkAccess"]["defaultValue"] == "Enabled",
)
app_template = MODULES["app"]["properties"]["template"]
check(
    "no workload profile is named unless one exists to name",
    app_template["variables"]["workloadProfileProperty"].startswith(
        "[if(empty(parameters('workloadProfileName')), createObject(),"
    )
    and "workloadProfileName" not in app_template["resources"][0]["properties"],
)
check(
    "the app is placed on Consumption once the environment has profiles",
    MODULES["app"]["properties"]["parameters"]["workloadProfileName"]
    == "[if(variables('privateNetworkingEnabled'), createObject('value', 'Consumption'), "
    "createObject('value', ''))]",
)


print("\nThe on path is actually wired")
subnets = MODULES["network"]["properties"]["template"]["resources"][0]["properties"][
    "subnets"
]
apps_subnet, pep_subnet = subnets[0], subnets[1]
check(
    "the apps subnet is delegated to Microsoft.App/environments",
    apps_subnet["properties"]["delegations"][0]["properties"]["serviceName"]
    == "Microsoft.App/environments",
)
check(
    "the private endpoint subnet disables network policies",
    pep_subnet["properties"]["privateEndpointNetworkPolicies"] == "Disabled",
)
check(
    "the environment is external, so ingress still works",
    "'internal', false()" in network_properties,
)
check(
    "workload profiles, not the legacy consumption-only environment",
    "'workloadProfiles'" in network_properties,
)

pe_template = MODULES["cosmos-audit-pe"]["properties"]["template"]
pe = pe_template["resources"]
by_type = {r["type"]: r for r in pe}
check(
    "the endpoint targets the Cosmos SQL sub-resource",
    by_type["Microsoft.Network/privateEndpoints"]["properties"][
        "privateLinkServiceConnections"
    ][0]["properties"]["groupIds"]
    == ["Sql"],
)
check(
    "the Cosmos private DNS zone is created and linked",
    pe_template["variables"]["privateDnsZoneName"] == "privatelink.documents.azure.com"
    and "Microsoft.Network/privateDnsZones" in by_type
    and "Microsoft.Network/privateDnsZones/virtualNetworkLinks" in by_type,
)
check(
    "a zone group binds the endpoint to the zone, which is what writes the records",
    "Microsoft.Network/privateEndpoints/privateDnsZoneGroups" in by_type,
)
check(
    "the app waits for the endpoint, because warm() runs at startup",
    "[resourceId('Microsoft.Resources/deployments', 'cosmos-audit-pe')]"
    in MODULES["app"]["dependsOn"],
)


print("\nDefault address space is usable")
vnet = ipaddress.ip_network(TEMPLATE["parameters"]["vnetAddressPrefix"]["defaultValue"])
apps = ipaddress.ip_network("10.100.0.0/23")
pep = ipaddress.ip_network("10.100.2.0/24")
check("both subnets sit inside the address space", apps.subnet_of(vnet) and pep.subnet_of(vnet))
check("the subnets do not overlap", not apps.overlaps(pep))
check(
    "the apps subnet clears the /27 minimum for workload profiles",
    apps.prefixlen <= 27,
)
# Container Apps rejects these outright: they collide with ranges AKS reserves
# underneath the environment, and a workload-profile environment reserves the
# 100.100.x blocks on top of that.
reserved = [
    ipaddress.ip_network(r)
    for r in (
        "169.254.0.0/16", "172.30.0.0/16", "172.31.0.0/16", "192.0.2.0/24",
        "100.100.0.0/17", "100.100.128.0/19", "100.100.160.0/19", "100.100.192.0/19",
    )
]
check(
    "the address space avoids the ranges Container Apps reserves",
    not any(vnet.overlaps(r) for r in reserved),
)
check(
    "the address space is a /22 or larger, which the cidrSubnet() calls require",
    vnet.prefixlen <= 22,
)


print("\npreflight rejects an address space the template cannot carve up")
sys.path.insert(0, str(ROOT / "scripts"))
import preflight  # noqa: E402

def _pn(prefix: str) -> list:
    return preflight.check_private_networking(
        {
            "ENABLE_PRIVATE_NETWORKING": "true",
            "ENABLE_AUDIT": "true",
            "VNET_ADDRESS_PREFIX": prefix,
        }
    )

check("the flag off means nothing to validate", preflight.check_private_networking({}) == [])
check("the default address space passes", all(r.ok for r in _pn("10.100.0.0/16")))
check("a /22 is accepted as the documented minimum", all(r.ok for r in _pn("10.20.0.0/22")))
check("a /23 is rejected before ARM sees it", not all(r.ok for r in _pn("10.20.0.0/23")))
check("a /24 is rejected", not all(r.ok for r in _pn("10.20.0.0/24")))
check("nonsense is rejected", not all(r.ok for r in _pn("not-a-cidr")))
check("a host address that is not a network is rejected", not all(r.ok for r in _pn("10.20.0.5/22")))
check(
    "a reserved workload-profile range is rejected",
    not all(r.ok for r in _pn("100.100.0.0/16")),
)
check(
    "an operator is warned that the environment cannot be converted in place",
    any("in place" in r.detail for r in _pn("10.100.0.0/16")),
)

print("\npreflight agrees with the template about what 'on' means")
# The template gates on toLower(x) == 'true'. A script that read '1'/'yes'/'on'
# as enabled would announce a private deployment while ARM built a public one,
# and postprovision would then fail a deploy that is perfectly healthy.
GATE = nested(TEMPLATE, "resources")["variables"]["privateNetworkingEnabled"]
gate_expr = json.dumps(GATE)
check(
    "the template's gate is an exact 'true' comparison",
    "toLower(parameters('enablePrivateNetworking')), 'true'" in gate_expr,
)
check(
    "the flag is constrained to true/false so a bad spelling fails loudly",
    TEMPLATE["parameters"]["enablePrivateNetworking"]["allowedValues"] == ["true", "false"],
)
for spelling in ("1", "yes", "on", "TRUE "):
    results = preflight.check_private_networking(
        {"ENABLE_PRIVATE_NETWORKING": spelling, "ENABLE_AUDIT": "true"}
    )
    treated_on = bool(results) and all(r.ok for r in results)
    template_on = spelling.strip().lower() == "true"
    check(
        f"preflight and the template agree on ENABLE_PRIVATE_NETWORKING={spelling!r}",
        treated_on == template_on,
    )
check(
    "an ambiguous spelling is reported rather than silently ignored",
    not preflight.check_private_networking(
        {"ENABLE_PRIVATE_NETWORKING": "1", "ENABLE_AUDIT": "true"}
    )[0].ok,
)
check(
    "private networking without the audit trail is caught before ARM",
    not preflight.check_private_networking(
        {"ENABLE_PRIVATE_NETWORKING": "true", "ENABLE_AUDIT": "false"}
    )[0].ok,
)
check(
    "a non-Cosmos sink still gets the VNet warning, because the template ignores the sink",
    any(
        "in place" in r.detail
        for r in preflight.check_audit(
            {"ENABLE_AUDIT": "true", "AUDIT_SINK": "file", "ENABLE_PRIVATE_NETWORKING": "true"}
        )
    ),
)

print("\npreflight warns when the subscription closes Cosmos to public traffic")
# The failure this guards against is #122 itself: a management-group policy set
# publicNetworkAccess=Disabled on the audit account overnight, the fail-closed
# sink could not warm(), and the app went down while Container Apps still called
# the deploy successful.


class _FakeStdin:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


_real_stdin, _real_run, _real_set = sys.stdin, preflight._run, preflight._azd_env_set
_real_input = builtins.input

AUDIT_ON = {"ENABLE_AUDIT": "true", "AZURE_ENV_NAME": "avatar-gf-test"}
MINE_SWEPT = [{"name": "cosmos-avatar-gf-test-r7qyqm7kattdc", "rg": "rg-a", "pna": "Disabled"}]
MINE_OPEN = [{"name": "cosmos-avatar-gf-test-r7qyqm7kattdc", "rg": "rg-a", "pna": "Enabled"}]
OTHER_SWEPT = [{"name": "cosmos-someone-else-9x8y7z", "rg": "rg-b", "pna": "Disabled"}]


def _cpa(cfg: dict, accounts, *, tty: bool = False, answer: str = "y", saves: bool = True):
    """Run check_cosmos_public_access with `az` and `azd` stubbed out.

    accounts=None stands for an `az cosmosdb list` that failed, which must never
    be read as "the subscription is clean".
    """
    def fake_run(args):
        if args[:2] == ["cosmosdb", "list"]:
            return (1, "", "denied") if accounts is None else (0, json.dumps(accounts), "")
        raise AssertionError(f"unexpected az call: {args}")

    preflight._run = fake_run
    preflight._azd_env_set = lambda *_: saves
    sys.stdin = _FakeStdin(tty)
    builtins.input = lambda *_: answer
    try:
        return preflight.check_cosmos_public_access(cfg)
    finally:
        preflight._run, preflight._azd_env_set = _real_run, _real_set
        sys.stdin = _real_stdin
        builtins.input = _real_input


def _blocks(results) -> bool:
    """Mirrors main()'s rule: only a failing check that is not warn_only stops a deploy."""
    return any(not r.ok and not r.warn_only for r in results)


check("audit off means there is nothing to probe", _cpa({}, MINE_SWEPT) == [])
check(
    "a non-Cosmos sink is not probed",
    _cpa({**AUDIT_ON, "AUDIT_SINK": "file"}, MINE_SWEPT) == [],
)
check(
    "this environment's own swept account blocks the deploy",
    _blocks(_cpa(dict(AUDIT_ON), MINE_SWEPT)),
)
check(
    "the block names the account rather than describing the policy in the abstract",
    any("cosmos-avatar-gf-test-r7qyqm7kattdc" in r.detail for r in _cpa(dict(AUDIT_ON), MINE_SWEPT)),
)
check(
    "the fix line uses the exact spelling the template accepts",
    any(
        "azd env set ENABLE_PRIVATE_NETWORKING true" in r.fix
        for r in _cpa(dict(AUDIT_ON), MINE_SWEPT)
    ),
)
check(
    "a swept account with private networking already on is fine",
    not _blocks(_cpa({**AUDIT_ON, "ENABLE_PRIVATE_NETWORKING": "true"}, MINE_SWEPT)),
)
check(
    "someone else's swept account warns without blocking, because it is inference not proof",
    not _blocks(_cpa(dict(AUDIT_ON), OTHER_SWEPT))
    and any(r.warn_only for r in _cpa(dict(AUDIT_ON), OTHER_SWEPT)),
)
check(
    "and it says why: the platform is closing accounts, so this one is next",
    any("policy is closing them" in r.detail for r in _cpa(dict(AUDIT_ON), OTHER_SWEPT)),
)
check(
    "the warning carries its own remedy, because main() never prints a warning's fix block",
    any(
        "azd env set ENABLE_PRIVATE_NETWORKING true" in r.detail
        for r in _cpa(dict(AUDIT_ON), OTHER_SWEPT)
    ),
)
check(
    "someone else's swept account is fine once private networking is on",
    not _blocks(_cpa({**AUDIT_ON, "ENABLE_PRIVATE_NETWORKING": "true"}, OTHER_SWEPT)),
)
check(
    "an open subscription passes without a warning",
    _cpa(dict(AUDIT_ON), MINE_OPEN)[0].ok
    and not _cpa(dict(AUDIT_ON), MINE_OPEN)[0].warn_only,
)
check(
    "private networking on states that public access will be Disabled",
    "Disabled" in _cpa({**AUDIT_ON, "ENABLE_PRIVATE_NETWORKING": "true"}, [])[0].detail,
)
check(
    "an empty subscription warns rather than claiming the posture is known",
    _cpa(dict(AUDIT_ON), [])[0].warn_only,
)
check(
    "an unreadable subscription never blocks a deploy",
    not _blocks(_cpa(dict(AUDIT_ON), None)),
)
check(
    "an unreadable subscription is reported as unknown, not as clean",
    _cpa(dict(AUDIT_ON), None)[0].warn_only,
)

print("\nthe operator can turn private networking on from the prompt")
_flipped = dict(AUDIT_ON)
_flip_results = _cpa(_flipped, MINE_SWEPT, tty=True, answer="y")
check("accepting the prompt clears the check", not _blocks(_flip_results))
check("accepting the prompt persists the flag into the config", _flipped.get("ENABLE_PRIVATE_NETWORKING") == "true")
_declined = dict(AUDIT_ON)
check(
    "declining the prompt still blocks the deploy",
    _blocks(_cpa(_declined, MINE_SWEPT, tty=True, answer="n")),
)
check("declining the prompt leaves the flag alone", "ENABLE_PRIVATE_NETWORKING" not in _declined)
_unsaved = dict(AUDIT_ON)
check(
    "an azd that cannot store the flag blocks rather than pretending it worked",
    _blocks(_cpa(_unsaved, MINE_SWEPT, tty=True, answer="y", saves=False)),
)

print("\nturning the flag on mid-run still validates the address space")
# Ordering regression: the probe runs before check_private_networking inside
# check_audit. If it ran after, a flag flipped at the prompt would skip the /22
# and reserved-range validation entirely and fail during provisioning instead.


def _audit(cfg: dict, accounts, **kw):
    def fake_run(args):
        if args[:2] == ["cosmosdb", "list"]:
            return 0, json.dumps(accounts), ""
        if args[:2] == ["provider", "show"]:
            return 0, "Registered\n", ""
        raise AssertionError(f"unexpected az call: {args}")

    preflight._run = fake_run
    preflight._azd_env_set = lambda *_: True
    sys.stdin = _FakeStdin(True)
    builtins.input = lambda *_: "y"
    try:
        return preflight.check_audit(cfg)
    finally:
        preflight._run, preflight._azd_env_set = _real_run, _real_set
        sys.stdin = _real_stdin
        builtins.input = _real_input


_late = {"ENABLE_AUDIT": "true", "AZURE_ENV_NAME": "avatar-gf-test"}
_late_results = _audit(_late, MINE_SWEPT)
check(
    "a flag turned on at the prompt still reaches the environment-replacement warning",
    any("in place" in r.detail for r in _late_results),
)
_bad_space = {
    "ENABLE_AUDIT": "true",
    "AZURE_ENV_NAME": "avatar-gf-test",
    "VNET_ADDRESS_PREFIX": "10.20.0.0/24",
}
check(
    "a flag turned on at the prompt is still checked against a too-small address space",
    any(not r.ok and not r.warn_only for r in _audit(_bad_space, MINE_SWEPT)),
)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    sys.exit(1)
print("All checks passed.")
