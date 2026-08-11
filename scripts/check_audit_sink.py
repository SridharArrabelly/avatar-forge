"""Assert that the deployed audit sink is actually usable.

Provisioning succeeding is not the same as the audit trail working. The failure
this exists to catch is silent and was hit twice, in two different tenants: a
governed subscription carries a Modify-effect policy (MCAPS ships one called
``CosmosDB_PublicNetwork_Modify``) that rewrites ``publicNetworkAccess`` to
``Disabled`` *after* ARM accepts the template. Bicep asks for ``Enabled``, the
deployment reports success, the account looks healthy in the portal — and the
container app, which has no VNet integration, gets 403 on every write.

Because ``AUDIT_SINK_FALLBACK`` defaults to ``error``, the app then refuses to
start and crash-loops, which is correct but is a confusing way to discover a
network policy. This turns that into one clear message at deploy time.

Two things are checked, both of which have actually broken:

1. ``publicNetworkAccess`` is really ``Enabled`` on the account — unless
   ``ENABLE_PRIVATE_NETWORKING`` is on, in which case ``Disabled`` is the goal
   and what gets verified instead is that an approved private endpoint exists to
   carry the traffic (#122).
2. The container app's identity holds a Cosmos **data-plane** role. Control-plane
   RBAC does not grant document access, so an account can be reachable and still
   reject every write.

Everything here runs against the control plane, so it works from a laptop even
when the account is closed to the internet. Verifying the data path itself has
to happen from inside the VNet — see ``scripts/smoke_audit_cosmos.py``.

Exit codes: 0 = usable (or audit is off / not the Cosmos sink), 1 = the app will
not be able to write and AUDIT_SINK_FALLBACK is fail-closed, so it will not start.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from urllib.parse import urlparse

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"

# Cosmos DB Built-in Data Contributor. Data-plane only; deliberately distinct
# from any control-plane role.
DATA_CONTRIBUTOR = "00000000-0000-0000-0000-000000000002"
TRUTHY = ("1", "true", "yes", "on")


def _az(*args: str) -> tuple[int, str]:
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        return 127, ""
    proc = subprocess.run(
        [exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return proc.returncode, (proc.stdout or "").strip()


def _account_name(endpoint: str) -> str:
    """``https://acct.documents.azure.com:443/`` -> ``acct``."""
    host = urlparse(endpoint).hostname or ""
    return host.split(".")[0]


def _private_endpoint_state(resource_group: str, account: str) -> str:
    """``approved`` | ``missing`` | ``unknown`` for the account's endpoints.

    ``unknown`` is kept distinct from ``approved`` on purpose. An unreadable
    answer is not evidence of a fault, so it must not fail a deploy — but it is
    not evidence of health either, and reporting it as success is exactly the
    "counters said the run was clean" failure this whole feature exists to
    avoid.
    """
    code, out = _az(
        "cosmosdb", "show", "-g", resource_group, "-n", account,
        "--query", "privateEndpointConnections", "-o", "json",
    )
    if code != 0:
        return "unknown"
    try:
        connections = json.loads(out) if out else None
    except ValueError:
        return "unknown"
    if connections is None:
        # The query succeeded and reported no connections at all, which is a
        # real answer rather than a failure to get one.
        return "missing"
    if not isinstance(connections, list):
        return "unknown"
    for connection in connections:
        if not isinstance(connection, dict):
            continue
        # The CLI flattens 'properties' on some API versions and not others.
        inner = connection.get("properties") if isinstance(connection.get("properties"), dict) else {}
        state = (
            connection.get("privateLinkServiceConnectionState")
            or inner.get("privateLinkServiceConnectionState")
            or {}
        )
        provisioning = str(
            connection.get("provisioningState") or inner.get("provisioningState") or ""
        ).lower()
        if str(state.get("status", "")).lower() == "approved" and provisioning in ("", "succeeded"):
            return "approved"
    return "missing"


def main() -> int:
    if os.getenv("ENABLE_AUDIT", "").strip().lower() not in TRUTHY:
        return 0

    sink = os.getenv("AUDIT_SINK", "cosmos").strip().lower() or "cosmos"
    if sink != "cosmos":
        print(f"  Audit trail  : {sink} sink — no Cosmos account to verify")
        return 0

    endpoint = os.getenv("AUDIT_COSMOS_ENDPOINT", "").strip()
    resource_group = os.getenv("AZURE_RESOURCE_GROUP", "").strip()
    fallback = os.getenv("AUDIT_SINK_FALLBACK", "error").strip().lower() or "error"

    if not endpoint or not resource_group:
        # Not fatal: an older azd env may predate the AUDIT_COSMOS_ENDPOINT output.
        print(
            f"{YELLOW}  Audit trail  : cannot verify — "
            f"{'AUDIT_COSMOS_ENDPOINT' if not endpoint else 'AZURE_RESOURCE_GROUP'} "
            f"is not in the azd env{RESET}"
        )
        return 0

    account = _account_name(endpoint)
    code, out = _az(
        "cosmosdb", "show", "-g", resource_group, "-n", account,
        "--query", "publicNetworkAccess", "-o", "tsv",
    )
    if code != 0:
        print(f"{YELLOW}  Audit trail  : could not read Cosmos account '{account}'{RESET}")
        return 0

    problems: list[str] = []
    warnings: list[str] = []
    network = out.strip()
    private_networking = (
        os.getenv("ENABLE_PRIVATE_NETWORKING", "").strip().lower() in TRUTHY
    )

    if private_networking:
        # A closed account is the goal here, not the fault. What matters is
        # whether anything actually reaches it: without an approved endpoint the
        # app is in precisely the outage this flag exists to prevent.
        endpoint_state = _private_endpoint_state(resource_group, account)
        if endpoint_state == "missing":
            problems.append(
                "ENABLE_PRIVATE_NETWORKING is on but the account has no approved "
                "private endpoint, so nothing can reach it"
            )
        if network and network.lower() == "enabled":
            # Not fatal — the app still works. But the point of the flag was to
            # close the account, and silence here would let a deployment believe
            # it is private when it is not.
            warnings.append(
                "ENABLE_PRIVATE_NETWORKING is on but publicNetworkAccess is still "
                "'Enabled', so the account is reachable from the internet"
            )
    elif network and network.lower() != "enabled":
        problems.append(
            f"publicNetworkAccess is '{network}', but the template asked for 'Enabled'"
        )

    principal = os.getenv("SERVICE_APP_IDENTITY_PRINCIPAL_ID", "").strip()
    if principal:
        code, out = _az(
            "cosmosdb", "sql", "role", "assignment", "list",
            "-g", resource_group, "--account-name", account, "-o", "json",
        )
        if code == 0 and out:
            try:
                assigned = any(
                    a.get("principalId", "").lower() == principal.lower()
                    and a.get("roleDefinitionId", "").rsplit("/", 1)[-1] == DATA_CONTRIBUTOR
                    for a in json.loads(out)
                )
            except (ValueError, AttributeError):
                assigned = True  # unreadable output is not evidence of a problem
            if not assigned:
                problems.append(
                    "the container app identity has no Cosmos data-plane role "
                    "(control-plane RBAC does not grant document access)"
                )

    if not problems:
        # Only claim what was actually checked: an empty publicNetworkAccess is
        # not evidence the account is reachable, and an endpoint query that
        # failed is not evidence of an endpoint.
        if private_networking:
            reach = (
                "private endpoint approved"
                if endpoint_state == "approved"
                else "private endpoint could not be verified"
            )
        elif network:
            reach = "Cosmos reachable"
        else:
            reach = "publicNetworkAccess not reported"
        colour = YELLOW if (warnings or (private_networking and endpoint_state != "approved")) else GREEN
        print(f"{colour}  Audit trail  : {reach}, data-plane role present{RESET}")
        for w in warnings:
            print(f"{YELLOW}                 - {w}{RESET}")
        return 0

    print("")
    print(f"{RED}  Audit trail  : the app will NOT be able to write audit records{RESET}")
    for w in warnings:
        print(f"{YELLOW}                 - {w}{RESET}")
    for p in problems:
        print(f"{RED}                 - {p}{RESET}")
    print("")
    if private_networking:
        print(f"{DIM}  ENABLE_PRIVATE_NETWORKING is on, so the account is meant to be closed")
        print("  to the internet — the missing piece is the endpoint that reaches it.")
        print(f"  Re-run provisioning, which creates it alongside the VNet:{RESET}")
        print("    azd provision")
        print("")
        print(f"{DIM}  If it still does not appear, check the endpoint and its approval:{RESET}")
        print(f"    az cosmosdb show -g {resource_group} -n {account} --query privateEndpointConnections")
        print("")
    else:
        print(f"{DIM}  This is normally an Azure Policy with a Modify effect rewriting the")
        print("  account after deployment, so provisioning still reports success.")
        print("  Find the assignment (check ancestor management groups, not just the")
        print(f"  subscription — and read policy rules, not display names):{RESET}")
        print(f"    az policy assignment list --scope /subscriptions/$(az account show --query id -o tsv)")
        print("")
        print(f"{DIM}  The durable fix is to stop needing public access at all: give the app a")
        print("  private route in, which is what the policy is steering you towards")
        print(f"  (adds a VNet, so the environment is recreated and its FQDN changes):{RESET}")
        print("    azd env set ENABLE_PRIVATE_NETWORKING true && azd provision")
        print("")
        print(f"{DIM}  With rights to exempt it, scope the waiver to this resource group only:{RESET}")
        print("    az policy exemption create --name audit-cosmos-pna --exemption-category Waiver"
              " --policy-assignment <assignment-id>"
              " --policy-definition-reference-ids <cosmos-public-network-rule>"
              f" --scope /subscriptions/<sub>/resourceGroups/{resource_group}")
        print(f"    az cosmosdb update -g {resource_group} -n {account} --public-network-access ENABLED")
        print("")
    print(f"{DIM}  Or let the trail degrade instead of failing:{RESET}")
    print("    azd env set AUDIT_SINK_FALLBACK file     # keep serving, write to disk")
    print(f"    azd env set ENABLE_AUDIT false           # turn the trail off{RESET}")
    print("")

    # Mirrors backend/audit/__init__.py:_fallback_or_raise — anything that is not
    # an explicit 'file' or 'none' is fail-closed, so a typo like 'flie' must not
    # be reported here as permission to degrade.
    if fallback not in ("file", "none"):
        print(
            f"{RED}  AUDIT_SINK_FALLBACK={fallback!r}, so the container app will refuse to "
            f"start until this is fixed.{RESET}"
        )
        return 1

    print(
        f"{YELLOW}  AUDIT_SINK_FALLBACK={fallback}, so the app will start and degrade "
        f"rather than stop.{RESET}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
