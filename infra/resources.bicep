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

// ───────── Teams bot (issue #53) ─────────
param botAppId string = ''
param botAppTenantId string = ''
@secure()
param botAppPassword string = ''
param botDisplayName string = 'Avatar Forge'
param teamsAppId string = ''
param agentId string = ''

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
    bingConnectionName: bingConnectionName
    bingCustomConfigName: bingCustomConfigName
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
    botAppId: botAppId
    botAppTenantId: empty(botAppTenantId) ? tenant().tenantId : botAppTenantId
    botAppPassword: botAppPassword
    teamsAppId: teamsAppId
    agentId: agentId
    acsEndpoint: acsEnabled ? acs!.outputs.endpoint : ''
    meetingBotEnabled: meetingBotEnabled
    acsAudioSampleRate: acsAudioSampleRate
    acsRequireWakePhrase: acsRequireWakePhrase
    acsAvatarVideoEnabled: acsAvatarVideoEnabled
  }
}

// ───────── Teams bot (channel C, issue #53) ─────────
// Only provisioned when a bot app id is supplied. The messaging endpoint is the
// Container App HTTPS URL + /api/messages.
module botService 'modules/botService.bicep' = if (!empty(botAppId)) {
  name: 'bot'
  params: {
    name: '${abbrs.botService}-${environmentName}-${resourceToken}'
    botDisplayName: botDisplayName
    tags: tags
    msaAppId: botAppId
    msaAppTenantId: empty(botAppTenantId) ? tenant().tenantId : botAppTenantId
    endpoint: '${app.outputs.uri}/api/messages'
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
output botMessagingEndpoint string = !empty(botAppId) ? '${app.outputs.uri}/api/messages' : ''
output acsEndpoint string = acsEnabled ? acs!.outputs.endpoint : ''

