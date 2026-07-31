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

@description('Bing pricing tier. G2 is the tier this project has run on; G1 is the lower tier.')
@allowed([ 'G1', 'G2' ])
param bingSkuName string = 'G2'

@description('''
The curated allow-list the web tool is restricted to — a HARD boundary enforced by
Bing, which is what makes an open-web tool safe for an executive assistant. Replace
these with your own sources. boostLevel is SuperBoost or Boosted.
''')
param bingAllowedDomains array = [
  { domain: 'https://www.mtn.com/investors', includeSubPages: true, boostLevel: 'SuperBoost' }
  { domain: 'https://sashares.co.za/mtn-shares', includeSubPages: true, boostLevel: 'SuperBoost' }
  { domain: 'https://www.mtn.com/newsroom', includeSubPages: true, boostLevel: 'Boosted' }
  { domain: 'https://www.mtn.com', includeSubPages: true, boostLevel: 'Boosted' }
  { domain: 'https://www.jse.co.za/jse/instruments', includeSubPages: true, boostLevel: 'Boosted' }
  { domain: 'https://www.itweb.co.za/categories/ojkjlyr7wo7k6amv', includeSubPages: true, boostLevel: 'Boosted' }
  { domain: 'https://www.telecoms.com', includeSubPages: true, boostLevel: 'Boosted' }
]

// App runtime extras
param agentModel string = 'gpt-5.4'
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

// ───────── Teams bot (channel C, issue #53) ─────────
@description('Bot Entra app client id (Microsoft App ID). Leave empty to skip bot provisioning. Surfaces as TEAMS_BOT_ID.')
param botAppId string = ''
@description('Bot app tenant id (single-tenant). Defaults to the deployment tenant when empty.')
param botAppTenantId string = ''
@description('Bot app client secret. Stored as a Container App secret. Required when botAppId is set.')
@secure()
param botAppPassword string = ''
@description('Display name for the Azure Bot resource.')
param botDisplayName string = 'Avatar Forge'
@description('Teams app (manifest) id used for bot deep links to the personal tab. Surfaces as TEAMS_APP_ID.')
param teamsAppId string = ''
@description('Foundry agent id override. Empty resolves the agent by AGENT_NAME.')
param agentId string = ''

// ───────── channel D in-call media (#27) ─────────
@description('Deployment profile from `scripts/set_profile.py` — one of "web", "teams-tab", "teams-chat", "in-call". Drives which optional channels deploy. Empty keeps the pre-profile behaviour (explicit flags only).')
param deployProfile string = ''
@description('Enable channel D ACS Call Automation media participant ("true"/"false"). When not "true" (default), no ACS resource is created and the deployment behaves exactly as today.')
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

// ───────── channel D Windows media host (#27) ─────────
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
    deployBingGrounding: toLower(deployBingGrounding) == 'true'
    bingSkuName: bingSkuName
    bingAllowedDomains: bingAllowedDomains
    modelName: modelName
    modelVersion: modelVersion
    modelDeploymentName: modelDeploymentName
    modelSkuName: modelSkuName
    modelCapacity: modelCapacity
    agentModel: agentModel
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
    botAppId: botAppId
    botAppTenantId: botAppTenantId
    botAppPassword: botAppPassword
    botDisplayName: botDisplayName
    teamsAppId: teamsAppId
    agentId: agentId
    enableAcs: enableAcs
    acsDataLocation: acsDataLocation
    meetingBotEnabled: wantMeetingBotBridge ? 'true' : 'false'
    acsAudioSampleRate: acsAudioSampleRate
    acsRequireWakePhrase: acsRequireWakePhrase
    acsAvatarVideoEnabled: acsAvatarVideoEnabled
  }
}

// ───────── channel D: Windows media host + calling bot registration ─────────
// Conditional and additive. Only instantiated for the in-call channel.
module meetingBotHost 'modules/meetingBotHost.bicep' = if (deployHost) {
  name: 'meeting-bot-host'
  scope: rg
  params: {
    location: location
    tags: tags
    botAppId: meetingBotAppId
    botAppTenantId: empty(meetingBotAppTenantId) ? tenant().tenantId : meetingBotAppTenantId
    avatarDisplayName: empty(avatarDisplayName) ? 'Avatar' : avatarDisplayName
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

// Teams bot (issue #53). Echoed so the operator can configure the manifest and
// the Azure Bot messaging endpoint without re-deriving them.
output BOT_MESSAGING_ENDPOINT string = resources.outputs.botMessagingEndpoint
output TEAMS_BOT_ID string = botAppId
output TEAMS_APP_ID string = teamsAppId

// Channel D in-call media (#27). Empty unless enableAcs=true.
output ACS_ENDPOINT string = resources.outputs.acsEndpoint

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
output AGENT_MODEL string = agentModel
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
output AGENT_ID string = agentId
