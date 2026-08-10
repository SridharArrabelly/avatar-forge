# Authentication

Four identities show up in this repo and they are easy to confuse. Start here:

| Identity | Who it is | Used for | Where it is set up |
| --- | --- | --- | --- |
| **Backend principal** | your signed-in user locally; the **user-assigned managed identity** in Azure | Voice Live, the Foundry agent/model, AI Search queries | `az login` / assigned by the template |
| **Deploying principal** | whoever runs `azd up` | creating resources, stamping RBAC, building the index, registering the agent | `az login` + `azd auth login` |
| **Calling bot** *(channel D only)* | an Entra app registration behind an Azure Bot | the Teams calling/Graph channel | [`../meeting-bot/README.md`](../meeting-bot/README.md) |
| **Web IQ** *(model mode only)* | either a service API key **or** the backend managed identity | the web-search tool | `WEBIQ_API_KEY`, or the identity — which only works once its client id is **bound in the Web IQ portal**, and some profiles cannot bind at all |

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
| `WEBIQ_API_KEY` | the Web IQ web-search tool ([`backend/voice/tools.py`](../backend/voice/tools.py)) | **model mode only**, and optional *if* the keyless route is open to you — agent mode uses Grounding-with-Bing-Custom-Search, a native Foundry tool that rides the Entra path. Unset, the backend authenticates to Web IQ with the managed identity on the `https://api.microsoft.ai/.default` scope, and proves at startup that it can obtain a token before offering the tool. That token still has to be **entitled** — see [below](#the-keyless-web-iq-route-needs-one-thing-azure-cannot-give-you). Set the key to skip both the check and the binding. |
| `AZURE_SEARCH_API_KEY` | the meeting-catalogue `SearchClient` ([`backend/voice/catalog.py`](../backend/voice/catalog.py)) | optional fallback. Unset — the normal case — it uses the credential above. |

`AZURE_VOICELIVE_API_KEY` is deliberately **ignored** on the agent path
([`backend/api/websocket.py`](../backend/api/websocket.py) forces an empty key), so
setting it will not rescue a broken agent session. Both variables in the table above are
documented in [configuration.md](configuration.md).

### The keyless Web IQ route needs one thing Azure cannot give you

Having a managed identity is necessary but **not sufficient**. Web IQ authorises
by *application*, and the application has to be registered with Web IQ itself —
a step that happens in their portal, not in Azure.

You do **not** need to create an app registration or a client secret. A
user-assigned managed identity already *is* an app registration, and it gets its
tokens from the Azure platform rather than from a secret — which is the entire
reason to prefer it.

**Check you can bind at all before you start.** Open the
[Web IQ portal](https://webiq.microsoft.ai/profiles/) and look for **Profile
Management → Application (Client) IDs**. If that tab is not there, the Entra
route is closed to your profile and nothing you do in Azure will open it — skip
to [when you cannot bind](#when-you-cannot-bind). If it is there, two steps:

1. Read the client id of the identity the container app runs as:
   ```powershell
   az containerapp show -g <rg> -n <container-app> --query "identity.userAssignedIdentities" -o json
   ```
   The `clientId` in the output is the GUID you need.
2. In the [Web IQ portal](https://webiq.microsoft.ai/profiles/) open **Profile
   Management → Application (Client) IDs** and **Bind Application (Client) ID**.
   Allow about a minute to sync.

Until that binding exists the identity is just another unknown caller, and it
fails in one of **two** ways. Knowing which one you are looking at saves a lot of
time, because they look nothing alike:

| Symptom | What happened | Where you see it |
| --- | --- | --- |
| `search_web` is never offered; answers come from the internal corpus only | the token request itself failed, so the startup probe returned false | `Web IQ: token unavailable` in the container-app log |
| `search_web` **is** offered, the model calls it, and every call fails | a token *was* issued, but Web IQ does not recognise the calling application | `401` with `"errorCode": "AuthUnauthorizedEntryId"` — *"Unauthorized entry ID"* |

The second is the confusing one: the startup probe passes and the tool is
advertised, so everything looks configured, yet no web question can be answered.
The probe is not lying — **a token proves *authentication*, not *entitlement***.
It asks only whether a token can be obtained, which is the strongest check that
can be made without spending a real query, so a bound-but-unauthorised
application still fails at call time. `web_search_available()` in
[`backend/voice/tools.py`](../backend/voice/tools.py) says so in its own
docstring.

Either way, nothing in Azure reports the binding as missing, so **check it first**
when you expected web grounding and did not get it.

#### When you cannot bind

Web IQ's own documentation notes that Entra authentication is unavailable on some
profiles — typically trial or non-enterprise ones — and that a dedicated app id
has to be requested through your Microsoft contact. If you have no **Application
(Client) IDs** tab, that is where you are.

In that case set `WEBIQ_API_KEY`. It needs no binding: with a key present
`web_search_available()` short-circuits to true and `_auth_headers()` sends
`x-apikey` instead of a bearer token, so the identity route is bypassed
completely. This applies **in Azure as much as locally** — a deployed app whose
identity cannot be bound needs the key exactly like a laptop does.

> **Keyless can never work on your laptop, binding or not.** Web IQ's Entra route
> is *app-only* (OAuth 2.0 client credentials). Locally `DefaultAzureCredential`
> falls through to your `az login`, which is a **user** — `az account show`
> reports `"type": "user"`. A user is not an application and has no client id you
> could bind.

Which route to use, then:

| | keyless (managed identity) | `WEBIQ_API_KEY` |
| --- | --- | --- |
| Azure, identity **bound** | **preferred** — no secret to rotate or leak | works, but unnecessary |
| Azure, binding **unavailable** | not possible | **use this** |
| Local development | not possible — you are a user, not an app | **use this** |

Set the key in `.env` (git-ignored) locally. In Azure set it with
`azd env set WEBIQ_API_KEY <key>`; the template passes it as a **container-app
secret**, never as a plain environment variable, so a bare
`az containerapp update --set-env-vars WEBIQ_API_KEY=<key>` is the wrong shape —
create the secret and reference it, or re-run `azd provision`.

> **A fresh deployment mints a fresh identity.** `azd up` into a new resource
> group creates a new user-assigned managed identity with a new client id, which
> is not the one you bound last time. On the keyless route every new environment
> needs its own binding.

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
