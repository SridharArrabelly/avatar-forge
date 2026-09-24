// Subscription-scoped entry point. Creates an RG and deploys all resources into it.
targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the azd environment (used as prefix for resources).')
param environmentName string

@minLength(1)
@description('Azure region for all resources.')
param location string

@description('Region for the Foundry account+project. Leave empty to reuse location. Use a Voice Live supported region (eastus2, swedencentral, southeastasia, centralindia, westus2) if location is not one.')
param foundryLocation string = ''

@minLength(1)
@maxLength(90)
@description('Name of the resource group to create / deploy into.')
param resourceGroupName string

@description('Object ID of the deploying principal (for direct role assignments, optional).')
param principalId string = ''

// ───────── BYO Foundry (set all three to reuse an existing Foundry account+project) ─────────
param foundryAccountName string = ''
param foundryResourceGroup string = ''
param foundryProjectEndpoint string = ''

// ───────── BYO AI Search (set both to reuse an existing Search service) ─────────
// The index name (greenfield or brownfield) always comes from `searchIndexName` below.
param searchServiceName string = ''
param searchResourceGroup string = ''

// ───────── BYO Application Insights ─────────
@description('Name of an existing Application Insights component to reuse. Leave empty to create a new one in this RG.')
param appInsightsName string = ''
@description('Resource group of the existing Application Insights component. Defaults to the deployment RG when empty.')
param appInsightsResourceGroup string = ''

// ───────── Application runtime config ─────────
param agentName string = 'AvatarAgent'
param agentProjectName string = 'avatar-forge'
param searchConnectionName string = 'aisearch-connection'
param searchIndexName string = 'knowledge-index'
param voiceLiveVoice string = 'en-US-AvaMultilingualNeural'

@description('Foundry connection name for the Grounding-with-Bing-Custom-Search resource. Surfaces as BING_CONNECTION_NAME in the container.')
param bingConnectionName string = ''

@description('Bing Custom Search configuration (instance) name — the curated domain allow-list. Surfaces as BING_CUSTOM_CONFIG_NAME in the container.')
param bingCustomConfigName string = ''

@description('Deploy Grounding with Bing Custom Search: the Bing account, the curated site allow-list, and the Foundry connection. Opt-in and additive — when false nothing Bing-related is created and the agent runs on AI Search alone. Also needs TRUSTED_WEB_SITES: with no site list Bing is not deployed.')
param deployBingGrounding string = 'true'

@description('''
Voice Live binding.

"agent" (default) routes every turn through the Foundry agent: speech is
transcribed by a recognizer, the agent reasons and calls its managed AI Search
and Bing grounding tools, and the reply is synthesised back to speech.

"model" binds Voice Live straight to a realtime speech-to-speech model. The
recognizer stage disappears from the answer path, and the tools become
in-process Python functions instead of Foundry-managed ones.

Additive: leaving this unset gives exactly today's behaviour.
''')
@allowed([ 'agent', 'model' ])
param voiceBinding string = 'agent'

@description('Realtime model to bind when voiceBinding is "model". Voice Live deploys and manages this model itself — no model deployment, no quota request. Ignored in agent mode.')
param voiceLiveModel string = ''

@description('''
"true"/"false" string. Developer mode exposes the settings panel, live transcript
and per-event logging, so settings can be changed and tried live while testing.
It changes no pipeline default: the panel is pre-populated with the same values
production uses.

Additive: leaving this unset gives exactly the production experience — panel
hidden, settings locked, session auto-starts.

It does not expose voiceBinding, which is deployment-wide by design.
''')
@allowed([ 'true', 'false' ])
param developerMode string = 'false'

@description('Web IQ endpoint in model mode. Empty resolves to https://api.microsoft.ai/v3. Authentication uses an optional API key or the managed identity.')
param webIqBaseUrl string = ''

@description('Web IQ result language hint in model mode.')
param webIqLanguage string = 'en'

@description('Web IQ result region hint in model mode.')
param webIqRegion string = 'ZA'

@description('Web IQ API key. Stored as a container-app secret, never as a plain env var. Set it with: azd env set WEBIQ_API_KEY <key>')
@secure()
param webIqApiKey string = ''

@description('''
The agent's web tool, agent mode only. Model mode always uses Web IQ in-process.

"bing" (default): Grounding with Bing Custom Search, a managed Foundry tool with a
server-side site allow-list (TRUSTED_WEB_SITES). Not deployed when that list is
empty, because Bing Custom Search has no open-web mode.

"webiq": Web IQ, called through this app's /api/tools/search-web as an OpenAPI
tool. It runs the model-mode search_web() unchanged, so the same trusted sites
(TRUSTED_WEB_SITES, reduced to hosts; empty = open web) and filtering apply.
Replaces Bing: nothing Bing-related is deployed. Greenfield Foundry only.
Measured against Bing on the same agent: faster tool step (0.66 s vs 1.83 s)
and 15/15 good answers; see docs/evaluation-history.md.

Foundry's calls to the app are authenticated with agentWebToolKey when set,
otherwise with a managed-identity token for agentWebToolAudience. The
preprovision hook settles one of the two; see docs/auth.md.
''')
@allowed([ 'bing', 'webiq' ])
param agentWebTool string = 'bing'

@description('Shared key Foundry presents to the app for the Web IQ agent tool. Stored as a container-app secret and in a Foundry project connection. Wins over agentWebToolAudience. Empty in the normal case: preflight creates an app registration instead, and generates this only if it cannot.')
@secure()
param agentWebToolKey string = ''

@description('Application ID URI (api://<client-id>) of the Entra app registration the agent\'s managed-identity token is issued for. Set by preflight.')
param agentWebToolAudience string = ''

@description('Client ID of that app registration. Optional; accepted as a v2-token audience. Set by preflight.')
param agentWebToolAppId string = ''

@description('Bing pricing tier. G2 is the tier this project has run on; G1 is the lower tier.')
@allowed([ 'G1', 'G2' ])
param bingSkuName string = 'G2'

@description('''
The trusted sites every web tool may search: TRUSTED_WEB_SITES. Comma-separated; each
entry is a host or a URL, optionally with a path, e.g.
"+www.mtn.com/investors,www.itweb.co.za,mybroadband.co.za". A leading + marks a
SuperBoost source, which Bing ranks first; the rest are Boosted.

EMPTY MEANS THE OPEN WEB for Web IQ (model mode, and AGENT_WEB_TOOL=webiq). Bing Custom
Search has no open-web mode, so with an empty list Bing is not deployed and an agent on
AGENT_WEB_TOOL=bing gets no web tool. See "Trusted web sources" in docs/configuration.md.
''')
param trustedWebSites string = ''

// One list, two renderings, because the two tools enforce it differently.
//
// Bing Custom Search is a real server-side allow-list: it takes each entry as a URL and
// honours its path and boost level. Web IQ has none; backend/voice/tools.py compiles the
// list into `site:` operators, which match a host and never a path. The container gets
// the list as written and backend/trusted_sites.py reduces it to bare hosts, so a local
// run and a deployment parse it the same way.
//
// Entries are trimmed and empty ones dropped, so "a, b," is "a,b". Bing needs a URL, so a
// missing scheme becomes https://. A leading - has no Bing meaning; such entries are
// skipped rather than failing the deployment, and preflight warns about them.
var trustedSiteEntries = filter(
  map(split(trustedWebSites, ','), e => trim(e)),
  e => !empty(e) && e != '+' && !startsWith(e, '-')
)
var trustedSites = map(trustedSiteEntries, e => {
  site: startsWith(e, '+') ? trim(substring(e, 1)) : e
  superBoost: startsWith(e, '+')
})
var bingAllowedDomains = map(trustedSites, s => {
  domain: contains(s.site, '://') ? s.site : 'https://${s.site}'
  includeSubPages: true
  boostLevel: s.superBoost ? 'SuperBoost' : 'Boosted'
})

// App runtime extras
@description('Deployment name the Foundry agent binds to. Empty derives it: on a greenfield deploy the agent must bind to the deployment this template just created, so it follows modelDeploymentName. Set explicitly for BYO Foundry, where the deployment already exists and this template did not name it.')
param agentModel string = ''
param embeddingDeployment string = 'text-embedding-3-small'
@description('Avatar type: standard-video, standard-photo, custom-video, or custom-photo.')
@allowed([ 'standard-video', 'standard-photo', 'custom-video', 'custom-photo' ])
param avatarType string = 'standard-photo'
@description('Canonical avatar model id. For custom avatars this is the model provisioned in the Speech resource.')
param avatarModel string = 'Simone'
@description('Assistant persona / display name (e.g. "Nuru") for the bot welcome message. Purely cosmetic; does NOT select the avatar model. Empty falls back to "Avatar".')
param avatarDisplayName string = ''
@description('Identity tagline under the avatar name (e.g. "Your MTN Digital Assistant"). Empty uses the company-agnostic default.')
param avatarTagline string = ''
param avatarBackgroundImageUrl string = ''
@description('Enable the grayscale idle / color speaking avatar treatment ("true"/"false"). Disabled by default to preserve the avatar original appearance.')
param enableAvatarSpeakingStyle string = 'false'
param srModel string = 'mai-transcribe'
param recognitionLanguage string = 'auto'

var resolvedAvatarType = toLower(trim(avatarType))
var resolvedAvatarModel = trim(avatarModel)

// ───────── channels C/D in-call media (#27) ─────────
@description('Deployment profile from `scripts/set_profile.py` — one of "web", "teams-tab", "in-call-browser" (channel C, ACS browser guest) or "in-call" (channel D, Windows media bot). Drives which optional channels deploy. Empty keeps the pre-profile behaviour (explicit flags only).')
param deployProfile string = ''
@description('Enable channel C ACS browser guest media participant ("true"/"false"). When not "true" (default), no ACS resource is created and the deployment behaves exactly as today.')
param enableAcs string = 'false'
@description('ACS data residency geography (NOT an Azure region), e.g. "United States", "Europe", "Africa".')
param acsDataLocation string = 'United States'

@description('Enable the conversation audit trail ("true"/"false"). When not "true" (default), no Cosmos account is created and the deployment behaves exactly as today.')
param enableAudit string = 'false'
@description('Days each audit record is retained, written as a per-item Cosmos TTL.')
param auditRetentionDays string = '365'
@description('"true"/"false". Serve the .NET Teams media-bot bridge without an ACS resource (sets MEETING_BOT_ENABLED). Implied by deployProfile="in-call" exactly; "in-call-browser" is channel C and does not imply it.')
param meetingBotEnabled string = 'false'
@description('PCM sample rate (Hz) the Teams media bot streams (16000).')
param acsAudioSampleRate string = ''
@description('"true"/"false". In-call avatar only answers after a wake phrase.')
param acsRequireWakePhrase string = ''
@description('"true"/"false". In-call avatar sends an outgoing video tile so it is a visible participant.')
param acsAvatarVideoEnabled string = ''
@description('"true"/"false". The browser joiner\'s tile carries the LIVE lip-synced avatar instead of a static placard. Needs acsAvatarVideoEnabled="true" as well — the tile has to exist before it can carry a face.')
param browserJoinVideoEnabled string = ''

// ───────── channel D Windows media host (#27) ─────────
// Deployed only for the in-call channel. Requires its own Entra app — an app can
// back only ONE Azure Bot resource, so this cannot reuse botAppId.
@description('"true"/"false". Provision the Windows media host + calling bot registration. Implied by deployProfile="in-call" exactly; "in-call-browser" is channel C and needs no host.')
param deployMeetingBotHost string = 'false'
@description('Entra app client id of the CALLING bot. Must differ from botAppId.')
param meetingBotAppId string = ''
@description('Tenant id of the calling bot app registration. Defaults to the deployment tenant when empty.')
param meetingBotAppTenantId string = ''
@description('Globally-unique DNS label for the media host public IP (becomes <label>.<region>.cloudapp.azure.com).')
param meetingBotDnsLabel string = ''
@description('Local administrator password for the Windows media host.')
@secure()
param meetingBotAdminPassword string = ''
@description('VM size for the media host. Standard_D4s_v5 (4 vCPU) is the size proven to run the Real-Time Media Platform; a 2-vCPU host had to be resized. Lowering it is a false economy.')
param meetingBotVmSize string = 'Standard_D4s_v5'
@description('Public URL of the bot icon shown for the in-call avatar in Teams.')
param meetingBotIconUrl string = ''

// ───────── Model deployment (used only when creating Foundry) ─────────
param modelName string = 'gpt-5.6-terra'
param modelVersion string = '2026-07-09'
param modelDeploymentName string = 'gpt-5.6-terra'
// GlobalStandard, not DataZoneStandard: on 23 Sep 2026 the EU data-zone pool for
// gpt-5.6-terra stalled ~60-87 s or refused ("high demand") on 15/16 calls while
// GlobalStandard in the same account served 16/16 (docs/evaluation-history.md).
// GlobalStandard may process prompts outside the data zone; pin DataZoneStandard
// via MODEL_SKU_NAME where data residency outranks availability.
@allowed([ 'GlobalStandard', 'Standard', 'DataZoneStandard' ])
param modelSkuName string = 'GlobalStandard'
// TPM capacity in thousands, so '250' is 250K tokens/minute. Typed as a string,
// not an int, because azd substitutes MODEL_CAPACITY textually and ARM refuses a
// JSON string for an int parameter -- the same reason enableAcs is a string.
// On GlobalStandard this is a rate ceiling, not a reservation: billing is per token
// consumed, so raising it costs nothing and only buys headroom against 429s. Every
// turn resends the full agent prompt plus retrieved chunks, so 50 was easy to trip.
// Bounded by regional quota: az cognitiveservices usage list -l <region>
param modelCapacity string = '250'

var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = {
  'azd-env-name': environmentName
  workload: 'avatar-forge'
}

var createFoundry = empty(foundryAccountName) || empty(foundryResourceGroup) || empty(foundryProjectEndpoint)
var createSearch  = empty(searchServiceName) || empty(searchResourceGroup)

// AGENT_MODEL is a *deployment* name, not a catalogue model name — the agent binds to
// whatever `modelDeploymentName` called the deployment. Keeping them as two independent
// literals made customising MODEL_DEPLOYMENT_NAME silently create a deployment the agent
// could never find, so greenfield derives it. BYO uses the current default;
// override it when an existing account uses a differently named deployment.
var resolvedAgentModel = !empty(agentModel) ? agentModel : (createFoundry ? modelDeploymentName : 'gpt-5.6-terra')

// Guard the empty case: `azd env set MODEL_CAPACITY ""` reaches the template as an
// empty string, and int('') fails at deploy time rather than falling back.
var resolvedModelCapacity = int(!empty(modelCapacity) ? modelCapacity : '250')

// ───────── Profile derivation ─────────
// The profile RAISES capability; it never lowers it. Explicit flags still work
// on their own, so environments created before profiles existed deploy exactly
// as they did before (deployProfile is empty -> every term below is false).
var profileInCall = toLower(deployProfile) == 'in-call'
var wantMeetingBotBridge = profileInCall || toLower(meetingBotEnabled) == 'true'
var wantMeetingBotHost = profileInCall || toLower(deployMeetingBotHost) == 'true'

// The host needs inputs Bicep cannot invent: its own Entra app, a globally
// unique DNS label and a VM password. `scripts/preflight.py` blocks the deploy
// when they are missing; this guard means a bypassed preflight degrades to
// "host not deployed" rather than a mid-deployment failure.
var meetingBotInputsReady = !empty(meetingBotAppId) && !empty(meetingBotDnsLabel) && !empty(meetingBotAdminPassword)
var deployHost = wantMeetingBotHost && meetingBotInputsReady

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    location: location
    foundryLocation: empty(foundryLocation) ? location : foundryLocation
    environmentName: environmentName
    resourceToken: resourceToken
    tags: tags
    principalId: principalId
    createFoundry: createFoundry
    createSearch: createSearch
    existingFoundryAccountName: foundryAccountName
    existingFoundryProjectEndpoint: foundryProjectEndpoint
    existingSearchServiceName: searchServiceName
    existingSearchResourceGroup: searchResourceGroup
    existingAppInsightsName: appInsightsName
    existingAppInsightsResourceGroup: appInsightsResourceGroup
    agentName: agentName
    agentProjectName: agentProjectName
    searchConnectionName: searchConnectionName
    searchIndexName: searchIndexName
    voiceLiveVoice: voiceLiveVoice
    bingConnectionName: bingConnectionName
    bingCustomConfigName: bingCustomConfigName
    voiceBinding: voiceBinding
    voiceLiveModel: voiceLiveModel
    developerMode: developerMode
    webIqBaseUrl: webIqBaseUrl
    webIqLanguage: webIqLanguage
    webIqRegion: webIqRegion
    trustedWebSites: trustedWebSites
    webIqApiKey: webIqApiKey
    agentWebTool: agentWebTool
    agentWebToolKey: agentWebToolKey
    agentWebToolAudience: agentWebToolAudience
    agentWebToolAppId: agentWebToolAppId
    deployBingGrounding: toLower(deployBingGrounding) == 'true'
    bingSkuName: bingSkuName
    bingAllowedDomains: bingAllowedDomains
    modelName: modelName
    modelVersion: modelVersion
    modelDeploymentName: modelDeploymentName
    modelSkuName: modelSkuName
    modelCapacity: resolvedModelCapacity
    agentModel: resolvedAgentModel
    embeddingDeployment: embeddingDeployment
    avatarType: resolvedAvatarType
    avatarModel: resolvedAvatarModel
    avatarDisplayName: avatarDisplayName
    avatarTagline: avatarTagline
    avatarBackgroundImageUrl: avatarBackgroundImageUrl
    enableAvatarSpeakingStyle: enableAvatarSpeakingStyle
    srModel: srModel
    recognitionLanguage: recognitionLanguage
    enableAcs: enableAcs
    acsDataLocation: acsDataLocation
    meetingBotEnabled: wantMeetingBotBridge ? 'true' : 'false'
    acsAudioSampleRate: acsAudioSampleRate
    acsRequireWakePhrase: acsRequireWakePhrase
    acsAvatarVideoEnabled: acsAvatarVideoEnabled
    browserJoinVideoEnabled: browserJoinVideoEnabled
    enableAudit: enableAudit
    auditRetentionDays: auditRetentionDays
  }
}

// ───────── channel D: Windows media host + calling bot registration ─────────
// Conditional and additive. Only instantiated for the in-call channel.

// The name in the Teams meeting roster must match the name on the web stage and
// the name the agent calls itself. The Windows host has no avatar-model variables
// to derive from, so resolve it here with the same rule backend/avatar_identity.py
// applies: the explicit knob, else the ACTIVE avatar model's leading segment,
// else 'Avatar'. The IS_* gating is what makes reading the model safe —
var activeAvatarModel = resolvedAvatarModel
var derivedAvatarName = split(activeAvatarModel, '-')[0]
var resolvedAvatarDisplayName = !empty(avatarDisplayName)
  ? avatarDisplayName
  : (empty(derivedAvatarName) ? 'Avatar' : derivedAvatarName)

module meetingBotHost 'modules/meetingBotHost.bicep' = if (deployHost) {
  name: 'meeting-bot-host'
  scope: rg
  params: {
    location: location
    tags: tags
    botAppId: meetingBotAppId
    botAppTenantId: empty(meetingBotAppTenantId) ? tenant().tenantId : meetingBotAppTenantId
    avatarDisplayName: resolvedAvatarDisplayName
    botIconUrl: meetingBotIconUrl
    adminPassword: meetingBotAdminPassword
    vmSize: meetingBotVmSize
    dnsLabel: meetingBotDnsLabel
  }
}

// Outputs consumed by azd
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = rg.name

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.acrLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.acrName
output AZURE_CONTAINER_APPS_ENVIRONMENT_NAME string = resources.outputs.containerAppsEnvironmentName

output SERVICE_APP_NAME string = resources.outputs.containerAppName
output SERVICE_APP_URI string = resources.outputs.containerAppUri
output SERVICE_APP_IDENTITY_PRINCIPAL_ID string = resources.outputs.uamiPrincipalId

output AZURE_VOICELIVE_ENDPOINT string = resources.outputs.foundryEndpoint
output PROJECT_ENDPOINT string = resources.outputs.foundryProjectEndpoint
output AZURE_AI_PROJECT_ENDPOINT string = resources.outputs.foundryProjectEndpoint
output AZURE_SEARCH_ENDPOINT string = resources.outputs.searchEndpoint
output AGENT_NAME string = agentName
output AGENT_PROJECT_NAME string = resources.outputs.effectiveAgentProjectName
output SEARCH_CONNECTION_NAME string = searchConnectionName
output SEARCH_INDEX_NAME string = searchIndexName
output BING_CONNECTION_NAME string = resources.outputs.bingConnectionName
output BING_CUSTOM_CONFIG_NAME string = resources.outputs.bingCustomConfigName
// Read by setup_foundry_agent.py. Derived, never inputs, so writing them back to
// the azd env cannot overwrite a choice the user made.
output AGENT_WEB_TOOL_AUTH string = resources.outputs.agentWebToolAuth
output AGENT_WEB_TOOL_CONNECTION_NAME string = resources.outputs.agentWebToolConnectionName
output APPLICATIONINSIGHTS_CONNECTION_STRING string = resources.outputs.appInsightsConnectionString

// Channel C in-call media (#27). Empty unless enableAcs=true.
output ACS_ENDPOINT string = resources.outputs.acsEndpoint

// Conversation audit trail (#30). Empty unless enableAudit=true. Exported so it
// lands in the azd env: the postprovision sink check and scripts/smoke_audit_cosmos.py
// both read it back with `azd env get-values`, and operators need it to point a
// local run at the deployed account.
output AUDIT_COSMOS_ENDPOINT string = resources.outputs.auditCosmosEndpoint

// Channel D Windows media host. Empty strings unless the in-call channel deployed.
output DEPLOY_PROFILE string = deployProfile
output MEETING_BOT_HOST_DEPLOYED string = deployHost ? 'true' : 'false'
output MEETING_BOT_FQDN string = deployHost ? meetingBotHost.outputs.publicFqdn : ''
output MEETING_BOT_OPERATOR_API string = deployHost ? meetingBotHost.outputs.operatorApi : ''
output MEETING_BOT_SIGNALING_ENDPOINT string = deployHost ? meetingBotHost.outputs.signalingEndpoint : ''

// Echo BYO inputs back as outputs so they end up in the azd env and the postprovision
// RBAC script can read them without needing the original GitHub vars / .env values.
output FOUNDRY_ACCOUNT_NAME string = foundryAccountName
output FOUNDRY_RESOURCE_GROUP string = foundryResourceGroup
output FOUNDRY_PROJECT_ENDPOINT string = foundryProjectEndpoint
output SEARCH_SERVICE_NAME string = searchServiceName
output SEARCH_RESOURCE_GROUP string = searchResourceGroup
output APPINSIGHTS_NAME string = appInsightsName
output APPINSIGHTS_RESOURCE_GROUP string = appInsightsResourceGroup

// Echo Bicep param defaults / values back so host-side scripts (postprovision
// hook) and local `uv run` invocations see the same values that were baked
// into the container. Otherwise a true greenfield clone (no .env) gets
// AGENT_MODEL=""/EMBEDDING_DEPLOYMENT="" in the azd env, and the postprovision
// scripts fail even though the matching Foundry deployments were created.
output AGENT_MODEL string = resolvedAgentModel
output EMBEDDING_DEPLOYMENT string = embeddingDeployment
output AVATAR_TYPE string = resolvedAvatarType
output AVATAR_MODEL string = resolvedAvatarModel
output VOICELIVE_VOICE string = voiceLiveVoice
output SR_MODEL string = srModel
output RECOGNITION_LANGUAGE string = recognitionLanguage
output AVATAR_DISPLAY_NAME string = avatarDisplayName
output AVATAR_TAGLINE string = avatarTagline
output AVATAR_BACKGROUND_IMAGE_URL string = avatarBackgroundImageUrl
