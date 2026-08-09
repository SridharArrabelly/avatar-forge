# Authentication

Four identities show up in this repo and they are easy to confuse. Start here:

| Identity | Who it is | Used for | Where it is set up |
| --- | --- | --- | --- |
| **Backend principal** | your signed-in user locally; the **user-assigned managed identity** in Azure | Voice Live, the Foundry agent/model, AI Search queries | `az login` / assigned by the template |
| **Deploying principal** | whoever runs `azd up` | creating resources, stamping RBAC, building the index, registering the agent | `az login` + `azd auth login` |
| **Calling bot** *(channel D only)* | an Entra app registration behind an Azure Bot | the Teams calling/Graph channel | [`../meeting-bot/README.md`](../meeting-bot/README.md) |
| **Web IQ** *(model mode only)* | either a service API key **or** the backend managed identity | the web-search tool | `WEBIQ_API_KEY`, or nothing at all — the app tries the identity by itself |

The first two are what almost everything below is about. They are usually *different*
principals with *different* roles, which is why a deploy can succeed and the running app
still get `401`.

## The Entra path

Voice Live agent sessions (`agent_config = { agent_name, project_name }`) require
Microsoft Entra ID; API-key auth is rejected on the agent path. The backend uses
[`DefaultAzureCredential`](https://learn.microsoft.com/python/api/azure-identity/azure.identity.defaultazurecredential)
(a process singleton — see [`backend/voice/auth.py`](../backend/voice/auth.py)) and
acquires tokens for two scopes:

- `https://ai.azure.com/.default` — Voice Live + Foundry agent
- `https://search.azure.com/.default` — AI Search index (catalogue pre-warm)

Run `az login` locally; in Azure, attach a managed identity. This is unchanged by
[`VOICE_BINDING`](voice-binding.md) — model mode reaches Voice Live over the same
credential; it only adds the Web IQ key below.

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

## Keys, where they still exist

"Entra everywhere" is the intent, and it holds for Voice Live and the Foundry agent. Two
exceptions are real, so it is worth knowing they exist before you go looking for a role
assignment that will not help:

| Variable | Path | When |
| --- | --- | --- |
| `WEBIQ_API_KEY` | the Web IQ web-search tool ([`backend/voice/tools.py`](../backend/voice/tools.py)) | **model mode only**, and **optional** — agent mode uses Grounding-with-Bing-Custom-Search, a native Foundry tool that rides the Entra path. Unset, the backend authenticates to Web IQ with the managed identity on the `https://api.microsoft.ai/.default` scope, and proves at startup that it can before offering the tool. Set the key to skip that check. |
| `AZURE_SEARCH_API_KEY` | the meeting-catalogue `SearchClient` ([`backend/voice/catalog.py`](../backend/voice/catalog.py)) | optional fallback. Unset — the normal case — it uses the credential above. |

`AZURE_VOICELIVE_API_KEY` is deliberately **ignored** on the agent path
([`backend/api/websocket.py`](../backend/api/websocket.py) forces an empty key), so
setting it will not rescue a broken agent session. Both variables in the table above are
documented in [configuration.md](configuration.md).

### The keyless Web IQ route needs one thing Azure cannot give you

Having a managed identity is necessary but **not sufficient**. Web IQ authorises
by *application*, and the application has to be registered with Web IQ itself —
a step that happens in their portal, not in Azure:

1. Take the **client id** of the app's user-assigned managed identity. A managed
   identity is an app registration, so it has one:
   ```powershell
   az identity show -g <rg> -n <identity-name> --query clientId -o tsv
   ```
2. In the [Web IQ portal](https://webiq.microsoft.ai/profiles/) open **Profile
   Management → Application (Client) IDs** and **Bind Application (Client) ID**.
   Allow about a minute to sync.

Until that binding exists, the identity is just another unknown caller: the token
request fails or the call comes back `401`/`403`, `search_web` is not offered, and
the assistant answers from the internal corpus alone. That is the designed
degradation, not a fault — but if you expected web grounding and did not get it,
**this is the first thing to check**, because nothing in Azure will show it as
missing.

> No **Application (Client) IDs** tab? Web IQ's own documentation notes that
> Entra authentication is unavailable in some trial scenarios and you have to
> request a dedicated app id through your Microsoft contact. In that case use
> `WEBIQ_API_KEY`, which needs no binding.

The scope, the header names and the request shape all follow the published
contract and are pinned by
[`tests/test_webiq_contract.py`](../tests/test_webiq_contract.py) so they cannot
drift from it unnoticed.

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

## Calling bot identity (channel D)

The calling bot is the one identity that is **not** the backend principal: an Entra app
registration (client id + secret) registered as an Azure Bot resource. Only the Graph
calling / Bot Framework channel auth uses those app credentials — the bot reaches
Foundry and Search through the *backend's* managed identity over the bridge websocket,
so it never holds AI credentials of its own. User SSO is deferred. Setup steps are in
[`../meeting-bot/README.md`](../meeting-bot/README.md).
