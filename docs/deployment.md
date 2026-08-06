# Deployment (Azure)

Provision and deploy Avatar Forge to Azure with the [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/overview)
(`azd`). One command provisions the infra, builds the image, and deploys the app.
For local development see [development.md](development.md); for env vars see
[configuration.md](configuration.md); for the Teams tab + bot see
[`teams/README.md`](../teams/README.md).

## Target topology

- **Azure Container Apps** (WebSockets-enabled, ingress port 3000, 1–3 replicas) — runs the app
- **Azure Container Registry** (Standard, admin disabled) — image registry
- **User-Assigned Managed Identity** — ACR pull + Foundry + Search access (no secrets in env)
- **Log Analytics + Application Insights** — observability
- **Azure AI Foundry** (account + project + model deployment) — created or BYO
- **Azure AI Search** (Basic, AAD auth) — created or BYO
- **Windows VM + NSG + public FQDN, and a second Azure Bot with the Teams calling
  channel** *(channel D only)* — the media host; created only when the in-call profile
  is selected and its inputs are set. This is the one costly addition (~$283/month).

Everything after the first six lines is additive and conditional: a `web` profile
deploys exactly the first six and nothing else.

## Prerequisites

- [Azure Developer CLI](https://aka.ms/azd-install) (`azd`)
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (for `az login`)
- An Azure subscription with **Owner** (or Contributor + User Access Administrator) —
  the template grants RBAC roles
- Docker Desktop **running** (the `Dockerfile` is built during `azd up`/`azd deploy`;
  you don't call `docker build`/`run` yourself — remote ACR build is also supported)

## Regions

Voice Live and the avatar are each limited to a handful of regions, and you need the
**intersection** — four regions support both:

`eastus2` · `southeastasia` · `swedencentral` · `westus2`

| Feature | Supported regions |
| --- | --- |
| Voice Live | `eastus2` `swedencentral` `southeastasia` `centralindia` `westus2` |
| TTS Avatar | `eastus2` `westus2` `northeurope` `westeurope` `swedencentral` `southeastasia` |

`scripts/preflight.py` holds both lists — it is the authoritative copy, and it checks
your region before anything deploys.

> **Why this is worth checking rather than discovering.** Deploying the Foundry
> account outside the Voice Live set fails *silently*: the WebSocket connects and the
> managed-identity token succeeds, then the server closes the socket within ~2s with
> no error event. It surfaces only as `SESSION_UPDATED event not received`.

If your primary `AZURE_LOCATION` must be somewhere else, split the deployment — the
Foundry account, project and avatar voice path are created in a supported region
while the rest of the stack stays put:

```powershell
azd env set AZURE_LOCATION   southafricanorth
azd env set FOUNDRY_LOCATION eastus2
```

## Deploy (greenfield)

> **Load your documents first.** The `postprovision` hook indexes every `data/*.docx`
> into the freshly-created AI Search service. Drop your documents into
> [`data/`](../data/) **before** `azd up`; otherwise the index is empty and you must
> rerun `scripts/setup_aisearch_index.py` manually. BYO Search skips this.

Steps 4 and 5 are what make the rest predictable — they are not optional extras. Some
steps Bicep performs and some only an administrator can, and they interleave, so the
plan is printed before anything is created.

```powershell
# 1. Authenticate
az login
azd auth login

# 2. Create an azd environment
azd env new <environment-name>

# 3. Pick a region that supports both Voice Live and the avatar (see above).
#    Skip it and preflight asks; either way it is validated before azd runs.
azd env set AZURE_LOCATION swedencentral

# 4. Choose the avatar for a new deployment. These are the canonical settings.
#    Types: standard-video, standard-photo, custom-video, custom-photo.
#    AVATAR_MODEL is a catalogue name, or your custom Speech model id.
azd env set AVATAR_TYPE standard-photo
azd env set AVATAR_MODEL Simone
# Optional branding; this does not select the face.
# azd env set AVATAR_DISPLAY_NAME Nuru
# Agent mode creates a Foundry agent named AvatarAgent by default.
# Optional: choose a different name before the first deployment.
# azd env set AGENT_NAME ContosoAvatarAgent

# 5. Choose the channel AND the brain. Records DEPLOY_PROFILE (web · teams-tab ·
#    in-call-browser · in-call) and VOICE_BINDING (agent · model), sets every flag
#    those imply and resets the ones they do not, and
#    prints the full numbered plan marking who performs each step.
#    channels/README.md to choose the channel; voice-binding.md to choose the brain.
uv run python scripts/set_profile.py

# 6. Verify you can actually finish that plan — regions, providers, tooling and every
#    input your profile needs. Also settles the deploy target (subscription, region,
#    resource group) so step 7 never stops to ask.
uv run python scripts/preflight.py

# 7. Provision infra + build + deploy app
azd up
```

`azd` reads these values from its environment, not from the local `.env` file.
These two variables are the only avatar-selection settings. See
[configuration.md](configuration.md#selecting-an-avatar) for the four modes.

Preflight also runs automatically as the `preprovision` hook, so a doomed `azd up`
stops in seconds instead of failing twenty minutes in — skipping step 5 only means you
find out at `azd up` instead of before it. Bypass with
`azd env set PREFLIGHT_SKIP true` if you ever need to.

Useful variants:

```powershell
uv run python scripts/preflight.py --steps-only    # just print the plan
uv run python scripts/preflight.py --remaining     # only the steps left after deploying

# override the regions it checks (otherwise taken from the azd env)
uv run python scripts/preflight.py --location southafricanorth --voicelive-location eastus2
```

> [!IMPORTANT]
> `azd` only asks for an environment name when none exists yet. After that it
> silently reuses the default recorded in `.azure/config.json`, so a later `azd up`
> — or `azd down` — targets whatever you used last **without asking**. Preflight
> prints the environment, subscription and resource group it is about to deploy
> into; read that banner before confirming. To switch: `azd env select <name>`.
> To start a clean one: `azd env new <name>`.

After `azd up` the URL of the running container app is printed (and stored as
`SERVICE_APP_URI` in the azd env).

## Bring-your-own Foundry / Search (brownfield)

The two big-ticket resources — **Azure AI Foundry** and **Azure AI Search** — can be
created fresh (default) or reused. Each has its own independent switch:

```bicep
// infra/main.bicep
var createFoundry = empty(foundryAccountName) || empty(foundryResourceGroup) || empty(foundryProjectEndpoint)
var createSearch  = empty(searchServiceName)  || empty(searchResourceGroup)
```

A resource is treated as BYO **only when its identifying env vars are set** (all three
`FOUNDRY_*` for Foundry, both `SEARCH_*` for Search) — otherwise the template
provisions a new one. The switches are independent: BYO Foundry while creating Search,
or vice versa.

### Full BYO walkthrough

```powershell
# 1. Authenticate
az login
azd auth login

# 2. Initialise the azd environment
azd env new <environment-name>

# 3. Subscription / region / RG
azd env set AZURE_SUBSCRIPTION_ID     <sub-guid>
azd env set AZURE_LOCATION            eastus2
azd env set AZURE_RESOURCE_GROUP_NAME rg-demo-dev

# 4. Point at the EXISTING Foundry account + project
azd env set FOUNDRY_ACCOUNT_NAME     your-foundry-prod
azd env set FOUNDRY_RESOURCE_GROUP   rg-shared-ai
azd env set FOUNDRY_PROJECT_ENDPOINT https://your-foundry-prod.services.ai.azure.com/api/projects/avatar-forge

# 5. Point at the EXISTING AI Search service + index
azd env set SEARCH_SERVICE_NAME   your-search-prod
azd env set SEARCH_RESOURCE_GROUP rg-shared-ai
azd env set SEARCH_INDEX_NAME     your-existing-index-name

# 5b. (optional) BYO Application Insights
azd env set APPINSIGHTS_NAME           your-appi-prod
azd env set APPINSIGHTS_RESOURCE_GROUP rg-shared-observability

# (optional) Pin the agent / search names the container reads at runtime
azd env set AGENT_NAME              MtnAvatarAgent
azd env set SEARCH_CONNECTION_NAME  aisearch-connection

# The web tool is ON by default: azd deploys the Bing account, the curated site
# allow-list and the Foundry connection, and feeds the two names back automatically.
# Edit the allow-list in infra/main.bicep (bingAllowedDomains) so it points at YOUR
# sources. To skip it (it is billable), or to reuse a connection you already have,
# see "The web tool is optional" below.
# azd env set DEPLOY_BING_GROUNDING false

# 6. Provision + deploy
azd up
```

### What gets created vs. skipped

| Resource | Created? | Notes |
|---|---|---|
| Resource Group | ✅ | From `AZURE_RESOURCE_GROUP_NAME` |
| User-Assigned Managed Identity | ✅ | App-scoped identity |
| Log Analytics + App Insights | ✅ | Per-app observability (App Insights conditional if `APPINSIGHTS_NAME` set) |
| Azure Container Registry | ✅ | App's own ACR |
| Container Apps Environment + Container App | ✅ | The web app |
| **Foundry account + project + model deployment** | ❌ SKIPPED | Reuses BYO Foundry |
| **AI Search service + index** | ❌ SKIPPED | Reuses BYO Search |

You still get a self-contained RG (app, logs, ACR, identity) but **no duplicate
Foundry/Search**.

### Cross-RG RBAC (easy to miss)

Because BYO Foundry/Search live in a *different* resource group, the new managed
identity needs role assignments on those foreign resources. Bicep can't do this safely
(a deterministic role-assignment name collides with any pre-existing assignment for the
same principal+role+scope — Azure rejects it with `RoleAssignmentExists`). So the
grants are made idempotently by [`scripts/grant_byo_rbac.py`](../scripts/grant_byo_rbac.py),
invoked from the `postprovision` hook in [`azure.yaml`](../azure.yaml). It calls
`az role assignment create` and swallows duplicate errors, so re-running `azd up` is
always safe.

It grants the UAMI:

- **Cognitive Services User** + **Foundry User** on the BYO Foundry account
- **Search Index Data Reader** + **Search Service Contributor** on the BYO Search service

It also grants **you** (the deploying identity) **Foundry User** on the BYO Foundry
account, because the two data-plane setup steps that run straight afterwards — building
the search index and creating the agent — call the account as *you*, not as the UAMI.

When **both** are BYO, it also grants the existing Foundry project's system-assigned
identity **Search Index Data Contributor** + **Search Service Contributor** on the BYO
Search service so the agent's `azure_ai_search` tool can read the index at runtime.

> **Permissions:** the principal running `azd up` needs **User Access Administrator**
> (or **Owner**) on the foreign resource group(s) to stamp these assignments. This is
> the only extra permission vs. the all-new path.

### How the app finds BYO resources at runtime

[`infra/resources.bicep`](../infra/resources.bicep) picks the effective endpoints:

```bicep
var foundryEndpointEffective        = createFoundry ? foundry!.outputs.accountEndpoint : 'https://${existingFoundryAccountName}.services.ai.azure.com/'
var foundryProjectEndpointEffective = createFoundry ? foundry!.outputs.projectEndpoint : existingFoundryProjectEndpoint
var searchEndpointEffective         = createSearch  ? search!.outputs.endpoint         : 'https://${existingSearchServiceName}.search.windows.net/'
```

These flow into the container app as `AZURE_VOICELIVE_ENDPOINT`, `PROJECT_ENDPOINT`,
and `AZURE_SEARCH_ENDPOINT` — the same env vars your local `.env` uses, so the backend
doesn't notice any difference between BYO and freshly-created resources.

### Mixed mode (BYO one, create the other)

Same flow, set only one BYO triplet. Example — BYO Foundry, fresh Search:

```powershell
azd env set FOUNDRY_ACCOUNT_NAME     your-foundry-prod
azd env set FOUNDRY_RESOURCE_GROUP   rg-shared-ai
azd env set FOUNDRY_PROJECT_ENDPOINT https://your-foundry-prod.services.ai.azure.com/api/projects/avatar-forge
# (no SEARCH_* — template creates a fresh Search service)
azd up
# Then populate the new index:
uv run python scripts/setup_aisearch_index.py
```

## Runtime config / model deployment overrides

> ⚠️ **Never run `azd provision` on its own against a live deployment — always follow it
> with `azd deploy`, or use `azd up`.**
>
> The container app is declared in Bicep with a placeholder image
> (`mcr.microsoft.com/k8se/quickstart:latest`), because on a greenfield deploy the real
> image does not exist until `azd deploy` builds it. `azd provision` therefore *resets*
> the app template back to that placeholder and creates a new revision from it. The app
> runs in **`Single` revision mode**, so the newest revision takes 100% of traffic as
> soon as it reports ready — and your site starts serving the Azure quickstart welcome
> page instead of the avatar.
>
> The failure is quiet and delayed. `azd provision` prints `SUCCESS`, and for the first
> minute or two the old revision is still the `latestReadyRevisionName`, so the site
> looks fine. Verify with:
>
> ```powershell
> az containerapp show -g <rg> -n <app> `
>   --query "properties.template.containers[0].image" -o tsv
> ```
>
> If that prints `mcr.microsoft.com/k8se/quickstart:latest`, run `azd deploy` now. It
> rebuilds, pushes and cuts traffic to a revision carrying both the real image and any
> env vars the provision added — which is also why flipping an infra flag needs *both*
> steps before the new setting is live in the running container.

The Bicep template accepts overrides via azd env vars — set them before
`azd provision`. [configuration.md](configuration.md) is the single source of truth
for every variable; the sections that apply at provisioning time are:

| Section | Covers |
| --- | --- |
| [Foundry agent provisioning](configuration.md#foundry-agent-provisioning-provisioning-only) | `AGENT_NAME`, `AGENT_PROJECT_NAME`, the Bing grounding flags |
| [AI Search & index build](configuration.md#ai-search--index-build-provisioning-only) | `SEARCH_CONNECTION_NAME`, `SEARCH_INDEX_NAME`, chunking |
| [Greenfield model deployment](configuration.md#greenfield-model-deployment-azd-provision-only) | `MODEL_NAME`, `MODEL_VERSION`, `MODEL_DEPLOYMENT_NAME`, `MODEL_SKU_NAME`, `MODEL_CAPACITY` |
| [Voice](configuration.md#voice) · [Avatar](configuration.md#avatar--model--identity) | `VOICELIVE_VOICE`, avatar model and identity |

## Post-deploy steps

For **greenfield** (template provisions Foundry + Search) the `postprovision` hook in
[`azure.yaml`](../azure.yaml) runs both setup scripts automatically:

- `scripts/setup_aisearch_index.py` — chunks + embeds every `data/*.docx` and builds
  the AI Search index. **Drop documents into `data/` BEFORE `azd up`** — otherwise the
  hook prints a warning and you must run it manually after adding files.
- `scripts/setup_foundry_agent.py` — registers the Foundry agent (`AGENT_NAME`) with the
  AI Search tool, plus the Grounding-with-Bing-Custom-Search tool **if** it is configured.

### The web tool is optional, and the deploy tells you which state you got

The agent needs two things, and they fail differently on purpose:

| | Missing means | Result |
|---|---|---|
| **AI Search connection** | the agent has no corpus | **Fatal.** Nothing usable is created. |
| **Bing connection** | no site-scoped web grounding | **Degraded.** The agent is created and answers from your indexed documents. |

The Bing tool is skipped — with a warning, not an error — both when
`BING_CONNECTION_NAME` / `BING_CUSTOM_CONFIG_NAME` are unset *and* when they name a
connection that doesn't exist in your project. That second case is the common one: it
happens whenever a `.env` is copied from another environment. So you can deploy today
without Bing and add it later.

Every deploy ends with a **`--- Data plane ---`** block stating what actually exists:

```text
--- Data plane ---
  Search index : built
  Agent        : ready, WEB TOOL OFF - answers from indexed documents only.
```

If the agent could not be created at all, the hook **exits non-zero** so a broken deploy
can never look successful. Your Azure resources are still fine — only the data-plane
step needs re-running; nothing needs re-provisioning.

To add the web tool later — for example after a deploy where you set
`DEPLOY_BING_GROUNDING=false` — pick whichever fits:

- **Let azd deploy it** — `azd env set DEPLOY_BING_GROUNDING true` then `azd up`. This
  creates the Bing account, the curated allow-list and the Foundry connection, and sets
  `BING_CONNECTION_NAME` / `BING_CUSTOM_CONFIG_NAME` for you.
- **Point at one you already have** — `azd env set BING_CONNECTION_NAME` +
  `BING_CUSTOM_CONFIG_NAME`, then re-run the agent script below.

Either way the "add it later" path runs the *same* code as a first deploy, so there is no
separate catch-up procedure to get wrong.

For **brownfield** (BYO) the hook skips both — your existing agent and index are reused.
Make sure your `AGENT_NAME` / `AGENT_PROJECT_NAME` / `SEARCH_CONNECTION_NAME` /
`SEARCH_INDEX_NAME` match what's actually in the BYO resources (override with
`azd env set` before `azd provision`).

You can always rerun them manually (point your local `.env` at the deployed endpoints
via `azd env get-values`). All three are idempotent:

```powershell
azd hooks run postprovision                      # both steps, in order
uv run python scripts/setup_aisearch_index.py     # rebuild the index
uv run python scripts/setup_foundry_agent.py      # re-register the agent + tools
```

## Teams (tab + in-call)

The deployed Container App HTTPS URL is the Teams tab `contentUrl` and the bridge
endpoint channel D's media bot connects back to. Building the package and sideloading
are in [`teams/README.md`](../teams/README.md); the calling bot's registration and host
are in [`../meeting-bot/README.md`](../meeting-bot/README.md). Both are **opt-in**: with
no Teams package built and no in-call flags set, the deploy is the channel A web app.
