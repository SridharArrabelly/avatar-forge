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

@description('Deploy Grounding with Bing Custom Search: the Bing account, the curated site allow-list, and the Foundry connection. Opt-in and additive — when false nothing Bing-related is created and the agent runs on AI Search alone.')
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

@description('Web IQ base URL. This is the web tool in model mode, where the agent (and therefore its managed Bing grounding tool) is out of the picture. Leave empty to use the code default — the tool is switched on by webIqApiKey, not by this.')
param webIqBaseUrl string = ''

@description('Comma-separated hosts that scope Web IQ searches, e.g. "mtn.com,sashares.co.za". Web IQ has no server-side allow-list, so these are compiled into site: operators on the query. Same intent as bingAllowedDomains — an open-web tool answering to an executive should not be able to cite anywhere at all. LEAVE EMPTY to derive the hosts from bingAllowedDomains, which is what keeps the two bindings searching the same sources; set it only to make model mode diverge deliberately.')
param webIqAllowedDomains string = ''

@description('Web IQ API key. Stored as a container-app secret, never as a plain env var. Set it with: azd env set WEBIQ_API_KEY <key>')
@secure()
param webIqApiKey string = ''

@description('Bing pricing tier. G2 is the tier this project has run on; G1 is the lower tier.')
@allowed([ 'G1', 'G2' ])
param bingSkuName string = 'G2'

@description('''
The curated allow-list the web tool is restricted to — a HARD boundary enforced by
Bing, which is what makes an open-web tool safe for an executive assistant. Replace
these with your own sources.

boostLevel is SuperBoost or Boosted — those are the API values. The portal renders
them as "Super Boost" and "Boost", which are display labels and are NOT accepted here.

SuperBoost is for sources that should win a tie: MTN's own investor, results and
leadership pages, plus the share-price / market-data sources and Reuters Africa.
Boosted is the industry and regulator press that supplies context.

Order below is kept identical to the live configuration this was taken from, so the
two can be diffed line by line:
  az rest --method get --url "https://management.azure.com<configId>?api-version=2025-05-01-preview"
''')
param bingAllowedDomains array = [
  { domain: 'https://www.mtn.com/investors', includeSubPages: true, boostLevel: 'SuperBoost' }
  { domain: 'https://www.mtn.com/media-centre', includeSubPages: true, boostLevel: 'SuperBoost' }
  { domain: 'https://www.mtn.com/leadership', includeSubPages: true, boostLevel: 'SuperBoost' }
  // The trailing '/#' is verbatim from the working configuration. A fragment is
  // client-side only and should not affect scoping; it is kept rather than tidied
  // so this list is a faithful copy. Simplify to '/financial-results' if it ever
  // looks like it is matching nothing.
  { domain: 'https://www.mtn.com/financial-results/#', includeSubPages: true, boostLevel: 'SuperBoost' }
  { domain: 'https://www.jse.co.za/market-data', includeSubPages: true, boostLevel: 'SuperBoost' }
  { domain: 'https://www.ft.com/telecoms', includeSubPages: true, boostLevel: 'Boosted' }
  { domain: 'https://www.itweb.co.za', includeSubPages: true, boostLevel: 'Boosted' }
  { domain: 'https://mybroadband.co.za', includeSubPages: true, boostLevel: 'Boosted' }
  { domain: 'https://www.news24.com/fin24', includeSubPages: true, boostLevel: 'Boosted' }
  { domain: 'https://africanwirelesscomms.com', includeSubPages: true, boostLevel: 'Boosted' }
  { domain: 'https://www.itnewsafrica.com', includeSubPages: true, boostLevel: 'Boosted' }
  { domain: 'https://www.icasa.org.za', includeSubPages: true, boostLevel: 'Boosted' }
  { domain: 'https://www.mtn.com/newsroom', includeSubPages: true, boostLevel: 'Boosted' }
  { domain: 'https://www.reuters.com/world/africa', includeSubPages: true, boostLevel: 'SuperBoost' }
  { domain: 'https://techcentral.co.za', includeSubPages: true, boostLevel: 'Boosted' }
  { domain: 'https://www.moneyweb.co.za/tools-and-data', includeSubPages: true, boostLevel: 'SuperBoost' }
  { domain: 'https://sashares.co.za/mtn-shares', includeSubPages: true, boostLevel: 'SuperBoost' }
]

// Same sources, two renderings — because the two bindings enforce them differently.
//
// Agent mode gets the list above verbatim: Bing Custom Search is a real server-side
// allow-list, so it can honour a path (/investors) and a boost level. Model mode has
// no agent and no Bing tool; backend/voice/tools.py compiles its allow-list into
// `site:` operators, and `site:` matches a domain and its subdomains but NEVER a path
// or a rank. So Web IQ can only be given the bare hosts those URLs sit on.
//
// Derived rather than hand-maintained. A second hand-typed list drifts, and this
// particular drift is silent and unsafe in one direction: forget to widen the Web IQ
// list and model mode simply cannot see a source; forget to set it at all and an
// enabled Web IQ searches the entire open web while agent mode stays restricted.
// Deriving makes bingAllowedDomains the single source of truth for "where may this
// assistant look", and webIqAllowedDomains an explicit opt-out rather than a duty.
//
// Verified against a real ARM evaluation (bicep does not fold lambdas at compile
// time): 17 URLs -> 13 hosts, www. stripped, first-occurrence order preserved.
var bingHostsRaw = map(
  bingAllowedDomains,
  d => split(replace(replace(d.domain, 'https://', ''), 'http://', ''), '/')[0]
)
// Bare host, not www. — `site:www.jse.co.za` would exclude senspdf.jse.co.za, where
// the JSE's SENS filings live. See the note in docs/configuration.md.
var bingHosts = map(bingHostsRaw, h => startsWith(h, 'www.') ? substring(h, 4) : h)
var webIqEffectiveDomains = empty(webIqAllowedDomains)
  ? join(union(bingHosts, bingHosts), ',')
  : webIqAllowedDomains

// App runtime extras
@description('Deployment name the Foundry agent binds to. Empty derives it: on a greenfield deploy the agent must bind to the deployment this template just created, so it follows modelDeploymentName. Set explicitly for BYO Foundry, where the deployment already exists and this template did not name it.')
param agentModel string = ''
param embeddingDeployment string = 'text-embedding-3-small'
param avatarName string = 'Lisa-casual-sitting'
param customAvatarName string = ''
@description('Assistant persona / display name (e.g. "Nuru") for the bot welcome message. Purely cosmetic; does NOT select the avatar model. Empty falls back to "Avatar".')
param avatarDisplayName string = ''
@description('Identity tagline under the avatar name (e.g. "Your MTN Digital Assistant"). Empty uses the company-agnostic default.')
param avatarTagline string = ''
param photoAvatarName string = 'Simone'
param isPhotoAvatar string = 'true'
param isCustomAvatar string = 'false'
param avatarBackgroundImageUrl string = ''
param srModel string = 'mai-transcribe-1'
param recognitionLanguage string = 'auto'

// ───────── channel C in-call media (#27) ─────────
@description('Deployment profile from `scripts/set_profile.py` — one of "web", "teams-tab", "in-call". Drives which optional channels deploy. Empty keeps the pre-profile behaviour (explicit flags only).')
param deployProfile string = ''
@description('Enable channel C ACS Call Automation media participant ("true"/"false"). When not "true" (default), no ACS resource is created and the deployment behaves exactly as today.')
param enableAcs string = 'false'
@description('ACS data residency geography (NOT an Azure region), e.g. "United States", "Europe", "Africa".')
param acsDataLocation string = 'United States'
@description('"true"/"false". Serve the .NET Teams media-bot bridge without an ACS resource (sets MEETING_BOT_ENABLED). Implied by deployProfile="in-call".')
param meetingBotEnabled string = 'false'
@description('PCM sample rate (Hz) the Teams media bot streams (16000).')
param acsAudioSampleRate string = ''
@description('"true"/"false". In-call avatar only answers after a wake phrase.')
param acsRequireWakePhrase string = ''
@description('"true"/"false". In-call avatar sends an outgoing video tile so it is a visible participant.')
param acsAvatarVideoEnabled string = ''

// ───────── channel C Windows media host (#27) ─────────
// Deployed only for the in-call channel. Requires its own Entra app — an app can
// back only ONE Azure Bot resource, so this cannot reuse botAppId.
@description('"true"/"false". Provision the Windows media host + calling bot registration. Implied by deployProfile="in-call".')
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
param modelName string = 'gpt-5.4'
param modelVersion string = '2026-03-05'
param modelDeploymentName string = 'gpt-5.4'
@allowed([ 'GlobalStandard', 'Standard', 'DataZoneStandard' ])
param modelSkuName string = 'GlobalStandard'
param modelCapacity int = 50

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
// could never find, so greenfield derives it. BYO keeps the old default because the
// deployment lives in an account this template did not create and cannot inspect.
var resolvedAgentModel = !empty(agentModel) ? agentModel : (createFoundry ? modelDeploymentName : 'gpt-5.4')

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
    webIqAllowedDomains: webIqEffectiveDomains
    webIqApiKey: webIqApiKey
    deployBingGrounding: toLower(deployBingGrounding) == 'true'
    bingSkuName: bingSkuName
    bingAllowedDomains: bingAllowedDomains
    modelName: modelName
    modelVersion: modelVersion
    modelDeploymentName: modelDeploymentName
    modelSkuName: modelSkuName
    modelCapacity: modelCapacity
    agentModel: resolvedAgentModel
    embeddingDeployment: embeddingDeployment
    avatarName: avatarName
    customAvatarName: customAvatarName
    avatarDisplayName: avatarDisplayName
    avatarTagline: avatarTagline
    photoAvatarName: photoAvatarName
    isPhotoAvatar: isPhotoAvatar
    isCustomAvatar: isCustomAvatar
    avatarBackgroundImageUrl: avatarBackgroundImageUrl
    srModel: srModel
    recognitionLanguage: recognitionLanguage
    enableAcs: enableAcs
    acsDataLocation: acsDataLocation
    meetingBotEnabled: wantMeetingBotBridge ? 'true' : 'false'
    acsAudioSampleRate: acsAudioSampleRate
    acsRequireWakePhrase: acsRequireWakePhrase
    acsAvatarVideoEnabled: acsAvatarVideoEnabled
  }
}

// ───────── channel C: Windows media host + calling bot registration ─────────
// Conditional and additive. Only instantiated for the in-call channel.

// The name in the Teams meeting roster must match the name on the web stage and
// the name the agent calls itself. The Windows host has no avatar-model variables
// to derive from, so resolve it here with the same rule backend/avatar_identity.py
// applies: the explicit knob, else the ACTIVE avatar model's leading segment,
// else 'Avatar'. The IS_* gating is what makes reading the model safe —
// customAvatarName is a Speech model id that is stale unless its gate is on.
var truthyValues = ['1', 'true', 'yes', 'on']
var activeAvatarModel = contains(truthyValues, toLower(trim(isCustomAvatar)))
  ? customAvatarName
  : (contains(truthyValues, toLower(trim(isPhotoAvatar))) ? photoAvatarName : avatarName)
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
output APPLICATIONINSIGHTS_CONNECTION_STRING string = resources.outputs.appInsightsConnectionString

// Channel C in-call media (#27). Empty unless enableAcs=true.
output ACS_ENDPOINT string = resources.outputs.acsEndpoint

// Channel C Windows media host. Empty strings unless the in-call channel deployed.
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
output VOICELIVE_VOICE string = voiceLiveVoice
output SR_MODEL string = srModel
output RECOGNITION_LANGUAGE string = recognitionLanguage
output AVATAR_NAME string = avatarName
output CUSTOM_AVATAR_NAME string = customAvatarName
output PHOTO_AVATAR_NAME string = photoAvatarName
output AVATAR_DISPLAY_NAME string = avatarDisplayName
output AVATAR_TAGLINE string = avatarTagline
output AVATAR_BACKGROUND_IMAGE_URL string = avatarBackgroundImageUrl
output IS_PHOTO_AVATAR string = isPhotoAvatar
output IS_CUSTOM_AVATAR string = isCustomAvatar
