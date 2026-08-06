"""Idempotently grant runtime RBAC on BYO (brownfield) Foundry and AI Search resources.

Runs from the azd `postprovision` hook AFTER Bicep finishes. Bicep cannot create role
assignments on resources it doesn't own without hitting `RoleAssignmentExists` on every
re-deploy (the deterministic guid() collides with any pre-existing assignment that grants
the same principal+role+scope, regardless of who created it). So we do it here with `az`
and treat duplicate-assignment errors as success.

Grants (only when the relevant BYO vars are set):
    UAMI -> Cognitive Services User on BYO Foundry account
    UAMI -> Foundry User on BYO Foundry account
    Deployer -> Foundry User on BYO Foundry account (so the postprovision scripts
                that follow can reach the CognitiveServices data plane)
    UAMI -> Search Index Data Reader on BYO Search service
    UAMI -> Search Service Contributor on BYO Search service
    Foundry project SMI -> Search Index Data Contributor on BYO Search service (both BYO)
    Foundry project SMI -> Search Service Contributor on BYO Search service (both BYO)

Required env vars (set by Bicep outputs via azd):
    AZURE_SUBSCRIPTION_ID, SERVICE_APP_IDENTITY_PRINCIPAL_ID
    FOUNDRY_ACCOUNT_NAME, FOUNDRY_RESOURCE_GROUP   (for Foundry grants)
    SEARCH_SERVICE_NAME, SEARCH_RESOURCE_GROUP     (for Search grants)
    AGENT_PROJECT_NAME                             (for the both-BYO project SMI lookup)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys


ROLES = {
    "cognitive_services_user": "a97b65f3-24c7-4388-baec-2e87135dc908",
    # Foundry User, NOT 'Azure AI Developer' (64702f94-…). The latter only carries
    # Microsoft.MachineLearningServices/* actions and no dataActions at all, so on a
    # Microsoft.CognitiveServices Foundry account it grants nothing whatsoever.
    "foundry_user": "53ca6127-db72-4b80-b1b0-d745d6d5456d",
    "search_index_data_reader": "1407120a-92aa-4202-b7e9-c0e197c71c8f",
    "search_index_data_contributor": "8ebe5a00-799e-43f5-93ac-243d3dce84a7",
    "search_service_contributor": "7ca78c08-252a-4471-8644-bb5ff32d4ba0",
}


# Resolved at startup. On Windows `az` is `az.cmd`, and CreateProcess won't
# resolve .cmd shims unless you pass the full path - so we do.
_AZ: str | None = None


def _az(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    assert _AZ is not None
    return subprocess.run([_AZ, *args], check=check, capture_output=True, text=True)


def _grant(label: str, principal_id: str, role_id: str, scope: str,
           principal_type: str = "ServicePrincipal") -> None:
    """Create a role assignment and swallow 'already exists' as success."""
    print(f"  -> {label}", flush=True)
    proc = _az([
        "role", "assignment", "create",
        "--assignee-object-id", principal_id,
        "--assignee-principal-type", principal_type,
        "--role", role_id,
        "--scope", scope,
    ])
    if proc.returncode == 0:
        return
    err = (proc.stderr or "") + (proc.stdout or "")
    # Idempotency: treat duplicates as success. Az CLI returns various phrasings here.
    if "RoleAssignmentExists" in err or "already exists" in err.lower():
        print("     (already exists - ok)", flush=True)
        return
    print(f"     FAILED: {err.strip()}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def _deployer_object_id() -> str | None:
    """Object id of the identity running the deploy (azd's value, else the signed-in user)."""
    from_env = os.environ.get("AZURE_PRINCIPAL_ID", "").strip()
    if from_env:
        return from_env
    proc = _az(["ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"])
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return None


def _lookup_foundry_project_principal_id(
    account_name: str, rg: str, project_name: str, sub_id: str
) -> str | None:
    """Read the system-assigned identity of an existing Foundry project."""
    proc = _az([
        "cognitiveservices", "account", "project", "show",
        "--name", account_name,
        "--project-name", project_name,
        "--resource-group", rg,
        "--subscription", sub_id,
        "-o", "json",
    ])
    if proc.returncode != 0:
        print(
            "WARN: could not look up existing Foundry project SMI (project '"
            f"{project_name}' in account '{account_name}' / RG '{rg}').\n"
            f"      {proc.stderr.strip()}\n"
            "      Skipping project->search RBAC grant. The agents azure_ai_search tool may not work\n"
            "      until you grant the project's managed identity Search Index Data Contributor manually.",
            file=sys.stderr,
        )
        return None
    data = json.loads(proc.stdout)
    return (data.get("identity") or {}).get("principalId")


def main() -> int:
    global _AZ
    _AZ = shutil.which("az")
    if _AZ is None:
        print("ERROR: az CLI not found on PATH.", file=sys.stderr)
        return 1

    sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    uami_pid = os.environ.get("SERVICE_APP_IDENTITY_PRINCIPAL_ID", "").strip()
    if not sub_id or not uami_pid:
        print(
            "Skipping BYO RBAC: AZURE_SUBSCRIPTION_ID or SERVICE_APP_IDENTITY_PRINCIPAL_ID not set.\n"
            "(Provision may not have completed - re-run `azd provision` once the underlying issue is fixed.)",
            file=sys.stderr,
        )
        return 0

    foundry_account = os.environ.get("FOUNDRY_ACCOUNT_NAME", "").strip()
    foundry_rg = os.environ.get("FOUNDRY_RESOURCE_GROUP", "").strip()
    search_name = os.environ.get("SEARCH_SERVICE_NAME", "").strip()
    search_rg = os.environ.get("SEARCH_RESOURCE_GROUP", "").strip()
    agent_project = os.environ.get("AGENT_PROJECT_NAME", "").strip()

    byo_foundry = bool(foundry_account and foundry_rg)
    byo_search = bool(search_name and search_rg)

    if not byo_foundry and not byo_search:
        print("No BYO resources configured (FOUNDRY_* / SEARCH_* empty) - nothing to grant.")
        return 0

    if byo_foundry:
        foundry_scope = (
            f"/subscriptions/{sub_id}/resourceGroups/{foundry_rg}"
            f"/providers/Microsoft.CognitiveServices/accounts/{foundry_account}"
        )
        print(f"Granting UAMI ({uami_pid}) runtime roles on BYO Foundry '{foundry_account}':")
        _grant("Cognitive Services User", uami_pid, ROLES["cognitive_services_user"], foundry_scope)
        _grant("Foundry User",            uami_pid, ROLES["foundry_user"],            foundry_scope)

        # The postprovision scripts that follow (setup_aisearch_index.py,
        # setup_foundry_agent.py) run as the HUMAN doing the deploy, and
        # subscription Owner/Contributor grant no CognitiveServices dataActions.
        # Greenfield gets this from Bicep; BYO has to do it here or both scripts
        # 401 on their first data-plane call.
        deployer_pid = _deployer_object_id()
        if deployer_pid:
            print(f"Granting deployer ({deployer_pid}) data-plane access on BYO Foundry:")
            _grant("Foundry User", deployer_pid, ROLES["foundry_user"], foundry_scope,
                   principal_type="User")
        else:
            print(
                "WARN: could not determine the deploying user's object id, so the deployer was\n"
                "      NOT granted Foundry User on the BYO Foundry account. If the steps below\n"
                "      fail with 401 PermissionDenied, grant it manually:\n"
                f"        az role assignment create --assignee <you> --role \"Foundry User\" --scope {foundry_scope}",
                file=sys.stderr,
            )

    if byo_search:
        search_scope = (
            f"/subscriptions/{sub_id}/resourceGroups/{search_rg}"
            f"/providers/Microsoft.Search/searchServices/{search_name}"
        )
        print(f"Granting UAMI ({uami_pid}) runtime roles on BYO Search '{search_name}':")
        _grant("Search Index Data Reader",  uami_pid, ROLES["search_index_data_reader"],  search_scope)
        _grant("Search Service Contributor", uami_pid, ROLES["search_service_contributor"], search_scope)

        # A BYO service may be empty. The postprovision index builder runs as the
        # deploying human, so control-plane Owner/Contributor is not enough.
        deployer_pid = _deployer_object_id()
        if deployer_pid:
            print(f"Granting deployer ({deployer_pid}) index-build access on BYO Search:")
            _grant(
                "Search Index Data Contributor",
                deployer_pid,
                ROLES["search_index_data_contributor"],
                search_scope,
                principal_type="User",
            )
            _grant(
                "Search Service Contributor",
                deployer_pid,
                ROLES["search_service_contributor"],
                search_scope,
                principal_type="User",
            )
        else:
            print(
                "WARN: could not determine the deploying user's object id, so the deployer was\n"
                "      NOT granted index-build access on BYO Search.",
                file=sys.stderr,
            )

    # Both-BYO symmetry: Foundry project SMI also needs access to the BYO Search index
    # so the agents `azure_ai_search` tool can read at runtime. (Greenfield handles this
    # in-Bicep via searchRoleForProject.)
    if byo_foundry and byo_search and agent_project:
        project_pid = _lookup_foundry_project_principal_id(foundry_account, foundry_rg, agent_project, sub_id)
        if project_pid:
            print(f"Granting Foundry project SMI ({project_pid}) Search roles on BYO Search:")
            _grant("Search Index Data Contributor", project_pid, ROLES["search_index_data_contributor"], search_scope)
            _grant("Search Service Contributor",    project_pid, ROLES["search_service_contributor"],    search_scope)

    print("BYO RBAC complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
