// The Foundry project connection that holds the agent web tool's shared key.
//
// Key mode only (see agentWebTool in main.bicep). Foundry injects the key as the
// x-tool-key header on every OpenAPI tool call; the container app holds the same
// value as a secret and compares the two (backend/api/agent_tools.py).
//
// Its own module rather than a resource in foundry.bicep because the target is
// the container app's URL, and the app takes its endpoints from the Foundry
// module: declaring it there would be a cycle.

@description('Foundry account (kind=AIServices) name.')
param accountName string
@description('Foundry project name.')
param projectName string
@description('Connection name. setup_foundry_agent.py resolves it by name.')
param connectionName string
@description('What the key opens — the container app\'s URL. Informational: the OpenAPI spec\'s servers entry decides where calls go.')
param target string
@description('Header the key is sent in. Must match the OpenAPI spec\'s apiKey scheme and backend/api/agent_tools.py.')
param header string = 'x-tool-key'
@secure()
param key string

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' existing = {
  parent: account
  name: projectName
}

resource connection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: project
  name: connectionName
  properties: {
    category: 'CustomKeys'
    authType: 'CustomKeys'
    target: target
    // Shared like the Search and Bing connections: the agent runs under
    // whichever identity opens the Voice Live session, not the deployer's.
    isSharedToAll: true
    credentials: {
      keys: {
        '${header}': key
      }
    }
    metadata: {
      purpose: 'agent-web-tool'
    }
  }
}

output name string = connection.name
