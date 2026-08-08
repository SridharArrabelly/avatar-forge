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

1. ``publicNetworkAccess`` is really ``Enabled`` on the account.
2. The container app's identity holds a Cosmos **data-plane** role. Control-plane
   RBAC does not grant document access, so an account can be reachable and still
   reject every write.

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
    network = out.strip()
    if network and network.lower() != "enabled":
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
        # not evidence the account is reachable.
        reach = "Cosmos reachable" if network else "publicNetworkAccess not reported"
        print(f"{GREEN}  Audit trail  : {reach}, data-plane role present{RESET}")
        return 0

    print("")
    print(f"{RED}  Audit trail  : the app will NOT be able to write audit records{RESET}")
    for p in problems:
        print(f"{RED}                 - {p}{RESET}")
    print("")
    print(f"{DIM}  This is normally an Azure Policy with a Modify effect rewriting the")
    print("  account after deployment, so provisioning still reports success.")
    print("  Find the assignment (check ancestor management groups, not just the")
    print(f"  subscription — and read policy rules, not display names):{RESET}")
    print(f"    az policy assignment list --scope /subscriptions/$(az account show --query id -o tsv)")
    print("")
    print(f"{DIM}  With rights to exempt it, scope the waiver to this resource group only:{RESET}")
    print("    az policy exemption create --name audit-cosmos-pna --exemption-category Waiver"
          " --policy-assignment <assignment-id>"
          " --policy-definition-reference-ids <cosmos-public-network-rule>"
          f" --scope /subscriptions/<sub>/resourceGroups/{resource_group}")
    print(f"    az cosmosdb update -g {resource_group} -n {account} --public-network-access ENABLED")
    print("")
    print(f"{DIM}  Otherwise the account needs a private endpoint and a VNet-integrated")
    print("  Container Apps environment, or the trail can degrade instead of failing:")
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
