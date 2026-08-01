// RG-scoped orchestrator: provisions all per-RG resources and wires them up.
targetScope = 'resourceGroup'

param location string
@description('Region for the Foundry account+project (defaults to location).')
param foundryLocation string = location
param environmentName string
param resourceToken string
param tags object
param principalId string

param createFoundry bool
param createSearch bool

param existingFoundryAccountName string
param existingFoundryProjectEndpoint string

param existingSearchServiceName string

@description('Name of an existing Application Insights component to reuse. Leave empty to create a new one.')
param existingAppInsightsName string = ''
@description('Resource group of the existing Application Insights component. Defaults to the deployment RG when empty.')
param existingAppInsightsResourceGroup string = ''

param agentName string
param agentProjectName string
param searchConnectionName string
param searchIndexName string
param voiceLiveVoice string
param bingConnectionName string = ''
param bingCustomConfigName string = ''

@description('Voice Live binding: "agent" (default) or "model". See modules/containerApp.bicep.')
param voiceBinding string = 'agent'
@description('Realtime model used when voiceBinding is "model".')
param voiceLiveModel string = ''

@description('"true"/"false" string. Developer mode exposes the settings panel and live transcript and allows a per-session voiceBinding override. Default "false" is the production experience.')
param developerMode string = 'false'
@description('Web IQ base URL — the web tool in model mode. Empty uses the code default.')
param webIqBaseUrl string = ''
@description('Comma-separated host allow-list for Web IQ results.')
param webIqAllowedDomains string = ''
@description('Web IQ API key, passed to the container app as a secret.')
@secure()
param webIqApiKey string = ''

@description('Deploy Grounding with Bing Custom Search (account + site allow-list + Foundry connection). Opt-in: when false nothing Bing-related is created and the agent uses AI Search alone.')
param deployBingGrounding bool = false
@allowed([ 'G1', 'G2' ])
param bingSkuName string = 'G2'
@description('The curated site allow-list. See modules/bingGrounding.bicep for the entry shape.')
param bingAllowedDomains array = []

// Bing is only created when it is asked for AND there is a Foundry project to
// attach the connection to. Without the project the account would be an orphan
// the agent could never use.
var createBing = deployBingGrounding && createFoundry
// Deployed names are generated when not pinned, so a first-time deploy needs no
// prior knowledge of them — they come back as outputs and land in the azd env.
var bingConnectionNameEffective = empty(bingConnectionName) ? 'bing-grounding-connection' : bingConnectionName
var bingCustomConfigNameEffective = empty(bingCustomConfigName) ? 'avatar-web-search' : bingCustomConfigName
// 'bing-' + '-' + a 13-char resourceToken = 19, leaving 45 of the 64-char account
// name limit for the env segment.
var bingEnvSegment = take(environmentName, 45)
var bingEnvSegmentClean = endsWith(bingEnvSegment, '-') ? take(bingEnvSegment, length(bingEnvSegment) - 1) : bingEnvSegment
var bingAccountName = toLower('bing-${bingEnvSegmentClean}-${resourceToken}')

param modelName string
param modelVersion string
param modelDeploymentName string
param modelSkuName string
param modelCapacity int

// App runtime extras
param agentModel string = ''
param embeddingDeployment string = ''
param avatarName string = ''
param customAvatarName string = ''
param avatarDisplayName string = ''
param avatarTagline string = ''
param photoAvatarName string = ''
param isPhotoAvatar string = ''
param isCustomAvatar string = ''
param avatarBackgroundImageUrl string = ''
param srModel string = 'mai-transcribe-1'
param recognitionLanguage string = 'auto'

// ───────── channel D in-call media (#27) ─────────
@description('Enable channel D ACS Call Automation media participant ("true"/"false"). When not "true" (default), no ACS resource is created and the container behaves as today.')
param enableAcs string = 'false'
@description('ACS data residency geography (NOT an Azure region), e.g. "United States", "Europe", "Africa".')
param acsDataLocation string = 'United States'

@description('"true"/"false". Serve the .NET Teams media-bot bridge (/ws/acs/audio) without an ACS resource — sets MEETING_BOT_ENABLED. Independent of enableAcs.')
param meetingBotEnabled string = 'false'
@description('PCM sample rate (Hz) the media bot streams. Teams media bot uses 16000.')
param acsAudioSampleRate string = ''
@description('"true"/"false". In-call avatar only answers after a wake phrase.')
param acsRequireWakePhrase string = ''
@description('"true"/"false". In-call avatar sends an outgoing video tile (visible participant).')
param acsAvatarVideoEnabled string = ''

var acsEnabled = toLower(enableAcs) == 'true'

var abbrs = loadJsonContent('abbreviations.json')

// ───────── Identity ─────────
module uami 'modules/managedIdentity.bicep' = {
  name: 'uami'
  params: {
    name: '${abbrs.managedIdentity}-${environmentName}-${resourceToken}'
    location: location
    tags: tags
  }
}

// ───────── Observability ─────────
module logAnalytics 'modules/logAnalytics.bicep' = {
  name: 'log'
  params: {
    name: '${abbrs.logAnalytics}-${environmentName}-${resourceToken}'
    location: location
    tags: tags
  }
}

module appInsights 'modules/applicationInsights.bicep' = if (empty(existingAppInsightsName)) {
  name: 'appi'
  params: {
    name: '${abbrs.applicationInsights}-${environmentName}-${resourceToken}'
    location: location
    tags: tags
    logAnalyticsWorkspaceId: logAnalytics.outputs.id
  }
}

// Reuse an existing App Insights component when appInsightsName is set (sourced
// from the APPINSIGHTS_NAME env var). Resolved in its own RG (defaults to the
// deployment RG when not specified).
resource existingAppInsights 'Microsoft.Insights/components@2020-02-02' existing = if (!empty(existingAppInsightsName)) {
  name: existingAppInsightsName
  scope: resourceGroup(empty(existingAppInsightsResourceGroup) ? resourceGroup().name : existingAppInsightsResourceGroup)
}

var appInsightsConnectionStringEffective = empty(existingAppInsightsName) ? appInsights.outputs.connectionString : existingAppInsights.properties.ConnectionString

// ───────── Container infrastructure ─────────
module acr 'modules/containerRegistry.bicep' = {
  name: 'acr'
  params: {
    #disable-next-line BCP334
    name: toLower('${abbrs.containerRegistry}${replace(environmentName, '-', '')}${resourceToken}')
    location: location
    tags: tags
    uamiPrincipalId: uami.outputs.principalId
  }
}

module containerAppsEnv 'modules/containerAppsEnvironment.bicep' = {
  name: 'cae'
  params: {
    name: '${abbrs.containerAppsEnvironment}-${environmentName}-${resourceToken}'
    location: location
    tags: tags
    logAnalyticsWorkspaceName: logAnalytics.outputs.name
  }
}

// ───────── Foundry (conditional) ─────────
module foundry 'modules/foundry.bicep' = if (createFoundry) {
  name: 'foundry'
  params: {
    accountName: toLower('${abbrs.cognitiveServices}-${environmentName}-${resourceToken}')
    projectName: 'proj-${environmentName}'
    location: foundryLocation
    tags: tags
    uamiPrincipalId: uami.outputs.principalId
    deployerPrincipalId: principalId
    modelName: modelName
    modelVersion: modelVersion
    modelDeploymentName: modelDeploymentName
    modelSkuName: modelSkuName
    modelCapacity: modelCapacity
    searchServiceName: createSearch ? search!.outputs.name : ''
    searchEndpoint: createSearch ? search!.outputs.endpoint : ''
    searchResourceId: createSearch ? search!.outputs.id : ''
    searchConnectionName: createSearch ? searchConnectionName : ''
    bingAccountId: createBing ? bingGrounding!.outputs.accountId : ''
    bingAccountName: createBing ? bingGrounding!.outputs.accountName : ''
    bingConnectionName: createBing ? bingConnectionNameEffective : ''
  }
}

// BYO Foundry/Search role assignments are NOT done in Bicep (they would fail with
// RoleAssignmentExists on re-runs because the assignment lives on a foreign resource).
// They are granted idempotently by scripts/grant_byo_rbac.py via the postprovision hook.

// ───────── AI Search (conditional) ─────────
module search 'modules/aiSearch.bicep' = if (createSearch) {
  name: 'search'
  params: {
    name: toLower('${abbrs.searchService}-${environmentName}-${resourceToken}')
    location: location
    tags: tags
    uamiPrincipalId: uami.outputs.principalId
    deployerPrincipalId: principalId
  }
}

// BYO Search: role assignment handled by scripts/grant_byo_rbac.py (see note above).

// ───────── Grounding with Bing Custom Search (conditional) ─────────
// On by default, and additive: with deployBingGrounding=false nothing here is created and the
// agent is built with the AI Search tool alone, exactly as before. When enabled,
// all three layers are deployed — the account, the curated site allow-list, and
// the Foundry connection — so no portal step or manual .env edit is required.
module bingGrounding 'modules/bingGrounding.bicep' = if (createBing) {
  name: 'bing-grounding'
  params: {
    // Truncated the same way the container app name is: a long azd env name would
    // otherwise overrun the account-name limit and fail at deploy, which is exactly
    // how the container app broke in a fresh tenant. The resourceToken is kept whole
    // so uniqueness survives the truncation.
    name: bingAccountName
    tags: tags
    skuName: bingSkuName
    configName: bingCustomConfigNameEffective
    allowedDomains: bingAllowedDomains
  }
}

// Grant Foundry project SMI Search RBAC for the agents azure_ai_search tool (greenfield search only).
module searchRoleForProject 'modules/searchRoleForProject.bicep' = if (createSearch && createFoundry) {
  name: 'search-role-for-foundry-project'
  params: {
    searchServiceName: search!.outputs.name
    foundryProjectPrincipalId: foundry!.outputs.projectPrincipalId
  }
}

// Brownfield symmetry: when both Foundry AND Search are BYO, granting the existing
// Foundry project SMI access to the existing Search service is handled by
// scripts/grant_byo_rbac.py (idempotent, swallows duplicate-assignment errors).

// Grant Search service SMI Cognitive Services OpenAI User on Foundry account (vectorizer query-time embeddings).
module foundryRoleForSearch 'modules/foundryRoleForSearch.bicep' = if (createSearch && createFoundry) {
  name: 'foundry-role-for-search'
  params: {
    foundryAccountName: foundry!.outputs.accountName
    searchPrincipalId: search!.outputs.principalId
  }
}

// ───────── channel D in-call media (#27) ─────────
// Only provisioned when channel D is explicitly enabled. Additive + conditional,
// mirroring the botService opt-in: a deploy with enableAcs=false never creates ACS.
module acs 'modules/communicationServices.bicep' = if (acsEnabled) {
  name: 'acs'
  params: {
    name: '${abbrs.communicationServices}-${environmentName}-${resourceToken}'
    tags: tags
    dataLocation: acsDataLocation
  }
}

// Grant the Container App's managed identity access to the ACS resource so it can
// authenticate the Call Automation / Identity clients via Entra (ACS_ENDPOINT path).
module acsRoleForApp 'modules/acsRoleForApp.bicep' = if (acsEnabled) {
  name: 'acs-role-for-app'
  params: {
    acsName: acs!.outputs.name
    appPrincipalId: uami.outputs.principalId
  }
}

// ───────── Container App ─────────
var foundryEndpointEffective = createFoundry ? foundry!.outputs.accountEndpoint : 'https://${existingFoundryAccountName}.services.ai.azure.com/'
var foundryProjectEndpointEffective = createFoundry ? foundry!.outputs.projectEndpoint : existingFoundryProjectEndpoint
var searchEndpointEffective = createSearch ? search!.outputs.endpoint : 'https://${existingSearchServiceName}.search.windows.net/'

// Container App names are capped at 32 characters, must not contain '--' and must end
// in an alphanumeric. The 'ca-' prefix plus the 13-char resourceToken consume 17, so the
// environment segment is truncated to the remaining budget (keeping the token, and thus
// uniqueness, intact) and any trailing '-' left by that cut is removed.
var caEnvBudget = 32 - length('${abbrs.containerApp}-') - length('-${resourceToken}')
var caEnvSegment = take(environmentName, caEnvBudget)
var caEnvSegmentClean = endsWith(caEnvSegment, '-') ? take(caEnvSegment, length(caEnvSegment) - 1) : caEnvSegment

module app 'modules/containerApp.bicep' = {
  name: 'app'
  params: {
    name: toLower('${abbrs.containerApp}-${caEnvSegmentClean}-${resourceToken}')
    location: location
    tags: union(tags, { 'azd-service-name': 'web' })
    containerAppsEnvironmentId: containerAppsEnv.outputs.id
    acrLoginServer: acr.outputs.loginServer
    uamiId: uami.outputs.id
    uamiClientId: uami.outputs.clientId
    voiceliveEndpoint: foundryEndpointEffective
    projectEndpoint: foundryProjectEndpointEffective
    agentName: agentName
    agentProjectName: createFoundry ? 'proj-${environmentName}' : agentProjectName
    searchConnectionName: searchConnectionName
    searchIndexName: searchIndexName
    searchEndpoint: searchEndpointEffective
    voiceLiveVoice: voiceLiveVoice
    bingConnectionName: createBing ? bingConnectionNameEffective : bingConnectionName
    bingCustomConfigName: createBing ? bingCustomConfigNameEffective : bingCustomConfigName
    voiceBinding: voiceBinding
    voiceLiveModel: voiceLiveModel
    developerMode: developerMode
    webIqBaseUrl: webIqBaseUrl
    webIqAllowedDomains: webIqAllowedDomains
    webIqApiKey: webIqApiKey
    appInsightsConnectionString: appInsightsConnectionStringEffective
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
    acsEndpoint: acsEnabled ? acs!.outputs.endpoint : ''
    meetingBotEnabled: meetingBotEnabled
    acsAudioSampleRate: acsAudioSampleRate
    acsRequireWakePhrase: acsRequireWakePhrase
    acsAvatarVideoEnabled: acsAvatarVideoEnabled
  }
}

// ───────── Outputs ─────────
output acrName string = acr.outputs.name
output acrLoginServer string = acr.outputs.loginServer
output containerAppsEnvironmentName string = containerAppsEnv.outputs.name
output containerAppName string = app.outputs.name
output containerAppUri string = app.outputs.uri
output uamiPrincipalId string = uami.outputs.principalId
output foundryEndpoint string = foundryEndpointEffective
output foundryProjectEndpoint string = foundryProjectEndpointEffective
output searchEndpoint string = searchEndpointEffective
output appInsightsConnectionString string = appInsightsConnectionStringEffective
output effectiveAgentProjectName string = createFoundry ? 'proj-${environmentName}' : agentProjectName
output acsEndpoint string = acsEnabled ? acs!.outputs.endpoint : ''

// The two values the agent setup script needs to wire the web tool. When Bing is
// deployed these are the names that were actually created, so they flow into the
// azd env and no one has to copy them out of the portal by hand. When it is not,
// they pass through whatever was supplied (possibly empty = web tool disabled).
output bingConnectionName string = createBing ? bingConnectionNameEffective : bingConnectionName
output bingCustomConfigName string = createBing ? bingCustomConfigNameEffective : bingCustomConfigName

