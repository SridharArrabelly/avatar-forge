@description('Cognitive Services account (kind=AIServices) name.')
param accountName string
@description('Foundry project name (child of the account).')
param projectName string
param location string
param tags object
param uamiPrincipalId string
@description('Object ID of the deployer (optional). Granted Azure AI Developer for setup scripts.')
param deployerPrincipalId string = ''
param modelName string
param modelVersion string
param modelDeploymentName string
param modelSkuName string
param modelCapacity int
@description('Embedding model deployment (used by setup_aisearch_index.py to vectorize data/*.docx).')
param embeddingModelName string = 'text-embedding-3-small'
param embeddingModelVersion string = '1'
param embeddingDeploymentName string = 'text-embedding-3-small'
@allowed([ 'Standard', 'GlobalStandard' ])
param embeddingSkuName string = 'GlobalStandard'
param embeddingCapacity int = 50

@description('Search service name to link as a Foundry project connection (optional). Leave empty to skip.')
param searchServiceName string = ''
@description('Search service endpoint (https://<name>.search.windows.net/). Required when searchServiceName is set.')
param searchEndpoint string = ''
@description('Search service resource ID. Required when searchServiceName is set.')
param searchResourceId string = ''
@description('Name of the project connection created for the search service.')
param searchConnectionName string = ''

@description('Resource ID of a Grounding-with-Bing-Custom-Search account to link as a project connection (optional). Leave empty to skip.')
param bingAccountId string = ''
@description('Name of the Bing account — shown as the connection display name. Required when bingAccountId is set.')
param bingAccountName string = ''
@description('Name of the project connection created for the Bing account.')
param bingConnectionName string = ''

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
    allowProjectManagement: true
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: projectName
    description: 'Avatar Forge Foundry project'
  }
}

resource deployment 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: account
  name: modelDeploymentName
  sku: {
    name: modelSkuName
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    raiPolicyName: 'Microsoft.DefaultV2'
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
}

// Embedding deployment (required by scripts/setup_aisearch_index.py).
// `dependsOn: [deployment]` serializes the two creates — CS accounts return 409
// when multiple `accounts/deployments` are submitted in parallel against the
// same parent account.
resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: account
  name: embeddingDeploymentName
  sku: {
    name: embeddingSkuName
    capacity: embeddingCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModelName
      version: embeddingModelVersion
    }
    raiPolicyName: 'Microsoft.DefaultV2'
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
  dependsOn: [ deployment ]
}

// Role IDs
var cogServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908' // Cognitive Services User
var aiDeveloperRoleId = '64702f94-c441-49e6-a78b-ef80e0188fee'    // Azure AI Developer

// UAMI → Cognitive Services User (Voice Live, OpenAI data-plane)
resource uamiCogUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, uamiPrincipalId, cogServicesUserRoleId)
  scope: account
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cogServicesUserRoleId)
  }
}

// UAMI → Azure AI Developer on the project (Agents/Threads)
resource uamiAiDevProject 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, uamiPrincipalId, aiDeveloperRoleId)
  scope: project
  properties: {
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', aiDeveloperRoleId)
  }
}

// Deployer (optional) → Azure AI Developer on the project, for setup_foundry_agent.py
resource deployerAiDev 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerPrincipalId)) {
  name: guid(project.id, deployerPrincipalId, aiDeveloperRoleId)
  scope: project
  properties: {
    principalId: deployerPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', aiDeveloperRoleId)
  }
}

output projectPrincipalId string = project.identity.principalId
output accountId string = account.id
output accountName string = account.name
output accountEndpoint string = 'https://${account.name}.services.ai.azure.com/'
output projectName string = project.name
output projectEndpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${project.name}'
output modelDeploymentName string = deployment.name
output embeddingDeploymentName string = embeddingDeployment.name

// Foundry project connection to AI Search (greenfield wiring; setup_foundry_agent.py looks this up by name)
resource searchConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = if (!empty(searchServiceName) && !empty(searchEndpoint) && !empty(searchResourceId) && !empty(searchConnectionName)) {
  parent: project
  name: searchConnectionName
  properties: {
    category: 'CognitiveSearch'
    target: searchEndpoint
    authType: 'AAD'
    isSharedToAll: true
    metadata: {
      ApiType: 'Azure'
      ResourceId: searchResourceId
      Location: location
    }
  }
}

// The same wiring for Grounding with Bing Custom Search, so the web tool is
// deployed rather than click-configured. Unlike AI Search this one cannot use
// AAD — the Bing data plane is key-based only — so the account key is read at
// deploy time with listKeys. It is never emitted as an output or written to a file.
resource bingConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = if (!empty(bingAccountId) && !empty(bingAccountName) && !empty(bingConnectionName)) {
  parent: project
  name: bingConnectionName
  properties: {
    category: 'GroundingWithCustomSearch'
    target: 'https://api.bing.microsoft.com/'
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: listKeys(bingAccountId, '2025-05-01-preview').key1
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: bingAccountId
      displayName: bingAccountName
      type: 'bing_custom_search_preview'
    }
  }
}
