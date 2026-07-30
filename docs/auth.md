# Authentication

Voice Live agent sessions (`agent_config = { agent_name, project_name }`) require
Microsoft Entra ID; API-key auth is rejected on the agent path. The backend uses
[`DefaultAzureCredential`](https://learn.microsoft.com/python/api/azure-identity/azure.identity.defaultazurecredential)
(a process singleton — see [`backend/voice/auth.py`](../backend/voice/auth.py)) and
acquires tokens for two scopes:

- `https://ai.azure.com/.default` — Voice Live + Foundry agent
- `https://search.azure.com/.default` — AI Search index (catalogue pre-warm)

Run `az login` locally; in Azure, attach a managed identity.

## Required roles

The signed-in user (local) or the managed identity (Azure) needs:

- **Cognitive Services User** on the AI Services / Foundry resource, plus access to the Foundry project
- **Search Index Data Reader** on AI Search (runtime queries)
- **Search Service Contributor** + **Search Index Data Contributor** on AI Search —
  **only** when running `scripts/setup_aisearch_index.py` (index build)

For the `azd` deploy, the template assigns the managed identity's runtime roles
automatically; BYO cross-RG grants are handled by
[`scripts/grant_byo_rbac.py`](../scripts/grant_byo_rbac.py) — see
[deployment.md](deployment.md#cross-rg-rbac-easy-to-miss).

## Startup credential pre-warm

To avoid paying token-acquisition cost on the first user connect, the FastAPI lifespan
kicks off `_prewarm_startup()` which sequences (1) `credential.get_token(...)` for both
scopes above, then (2) the meeting-catalogue fetch from AI Search. This warms both the
credential chain and the AI Search service before any user arrives. Code:
[`backend/main.py`](../backend/main.py) `_prewarm_startup` and
[`backend/voice/catalog.py`](../backend/voice/catalog.py) `prewarm_catalog`.

## Dev laptop: skipping the IMDS probe

Off-Azure, `DefaultAzureCredential` still tries `ManagedIdentityCredential` (the IMDS
endpoint at `169.254.169.254`) before falling through to `AzureCliCredential`. That
probe takes ~5s to time out per parallel `get_token` call, inflating the startup
pre-warm from ~1.5s to ~7s. To skip it on a dev laptop:

```
AUTH_EXCLUDE_MANAGED_IDENTITY=true
```

`auth.py` then constructs `DefaultAzureCredential(exclude_managed_identity_credential=True)`.
**Leave this unset in any Azure-hosted environment** — Container Apps, App Service, and
AKS workload identity all rely on the IMDS path.

## Token caching wrapper

Even with the IMDS probe skipped, `AzureCliCredential` has no in-memory token cache —
each acquisition shells out to `az account get-access-token` (~1.5s per Windows
subprocess spawn). To avoid paying that on every request, `auth.py` wraps
`DefaultAzureCredential` in a process-wide `_CachingCredentialWrapper` that acquires
**one token per scope and reuses it** until ~5 min before expiry (serving both the
Voice Live `get_token` path and the AI Search `get_token_info` path from the same
cache). Startup pre-warm is sequenced — credential first, then the catalogue fetch — so
the catalogue's `SearchClient` reuses the already-warmed `search.azure.com` token
instead of spawning its own `az` call.

Net effect on a dev laptop: roughly one `az account get-access-token` per distinct
scope at startup, not one per SDK call. In Azure with managed identity those
acquisitions are in-process HTTP calls to IMDS (cached ~1 hour) rather than subprocess
spawns.

## Teams bot identity (separate)

The channel C bot uses its **own** identity — an Entra app registration (client id +
secret) registered as an Azure Bot resource — which is separate from the backend
managed identity above and separate from user SSO (deferred). The bot still reaches
Foundry/Search through the backend's managed identity; only the Bot Framework channel
auth uses the bot's app credentials. Setup steps are in
[`teams/README.md`](../teams/README.md#identity-model-read-this-first).
