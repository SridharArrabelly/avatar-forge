"""Offline contract for a new Foundry project paired with BYO AI Search.

This hybrid is the quota-recovery path: Search already exists, but its index and
the new Foundry project's connection do not. The deployment must wire and build
those pieces before creating the agent.

    uv run python tests/test_hybrid_search.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = json.loads((ROOT / "infra" / "main.json").read_text(encoding="utf-8"))
AZURE_YAML = (ROOT / "azure.yaml").read_text(encoding="utf-8")
RBAC = (ROOT / "scripts" / "grant_byo_rbac.py").read_text(encoding="utf-8")
INDEX_SETUP = (ROOT / "scripts" / "setup_aisearch_index.py").read_text(encoding="utf-8")

failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        failures.append(name)


blob = json.dumps(TEMPLATE)

print("New Foundry + BYO Search infrastructure")
check(
    "Foundry receives the existing Search service name",
    "parameters('existingSearchServiceName')" in blob,
)
check(
    "Foundry receives the existing Search resource ID",
    "resourceId(parameters('existingSearchResourceGroup'), "
    "'Microsoft.Search/searchServices', parameters('existingSearchServiceName'))" in blob,
)
check(
    "the generated template creates a CognitiveSearch project connection",
    '"category": "CognitiveSearch"' in blob,
)
check(
    "the new Foundry project gets roles in the BYO Search resource group",
    '"name": "byo-search-role-for-foundry-project"' in blob
    and '"resourceGroup": "[parameters(\'existingSearchResourceGroup\')]"' in blob,
)

print("\nPostprovision ordering")
index_pos = AZURE_YAML.find("Creating/updating AI Search index")
agent_pos = AZURE_YAML.find("Creating Foundry agent")
check("index setup runs before agent creation", 0 <= index_pos < agent_pos)
check(
    "PowerShell builds the index when Foundry is new",
    "if ($greenfieldSearch -or $greenfieldFoundry)" in AZURE_YAML,
)
check(
    "POSIX builds the index when Foundry is new",
    'if [ -z "$SEARCH_SERVICE_NAME" ] || [ -z "$FOUNDRY_ACCOUNT_NAME" ]; then'
    in AZURE_YAML,
)
check(
    "the BYO Search deployer receives index-build data roles",
    'Granting deployer ({deployer_pid}) index-build access on BYO Search' in RBAC
    and '"Search Index Data Contributor"' in RBAC,
)
check(
    "BYO Search gets an identity and Foundry vectorization access",
    '"--identity-type", "SystemAssigned"' in RBAC
    and '"Cognitive Services OpenAI User"' in RBAC
    and "AZURE_VOICELIVE_ENDPOINT" in RBAC,
)
check(
    "greenfield Foundry role scope uses azd's resource-group output",
    'os.environ.get("AZURE_RESOURCE_GROUP", "")' in RBAC,
)
check(
    "index creation waits for new Search data-plane roles to propagate",
    "creating/updating Search index" in INDEX_SETUP
    and "lambda: ensure_index(s)" in INDEX_SETUP,
)
check(
    "document access waits before embedding and upload",
    "accessing documents in Search index" in INDEX_SETUP
    and INDEX_SETUP.find("search.get_document_count")
    < INDEX_SETUP.find("upload(s, iter_documents"),
)
check(
    "agent creation is blocked when index setup fails",
    "$indexState -eq 'failed'" in AZURE_YAML
    and '[ "$index_state" = "failed" ]' in AZURE_YAML,
)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    sys.exit(1)
print("All checks passed.")
