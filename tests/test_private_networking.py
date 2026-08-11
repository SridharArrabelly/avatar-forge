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
        {"ENABLE_PRIVATE_NETWORKING": "true", "VNET_ADDRESS_PREFIX": prefix}
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

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    sys.exit(1)
print("All checks passed.")
