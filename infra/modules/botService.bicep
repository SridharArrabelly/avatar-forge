// Azure Bot (Bot Service) registration + Teams channel for the channel C bot (#53).
// Conditional: only deployed when a bot app id is supplied, so existing
// (bot-less) deployments are unaffected.
//
// The bot's identity is an Entra app registration (msaAppId). This module does
// NOT create that app registration or its secret — those are an identity step
// the operator performs (portal / `az ad app`), because creating app
// registrations is outside the resource-group deployment scope. See teams/README.md.
targetScope = 'resourceGroup'

@description('Name of the Azure Bot resource.')
param name string

@description('Display name shown for the bot.')
param botDisplayName string

@description('Resource tags.')
param tags object

@description('Microsoft App ID (Entra app client id) backing the bot.')
param msaAppId string

@description('Tenant ID for the single-tenant bot app registration.')
param msaAppTenantId string

@description('Messaging endpoint — the ACA HTTPS URL + /api/messages.')
param endpoint string

// Bot Service is a global resource.
resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: name
  location: 'global'
  tags: tags
  sku: {
    name: 'F0'
  }
  kind: 'azurebot'
  properties: {
    displayName: botDisplayName
    endpoint: endpoint
    msaAppId: msaAppId
    msaAppType: 'SingleTenant'
    msaAppTenantId: msaAppTenantId
  }
}

// Enable the Microsoft Teams channel so the bot is reachable from Teams.
resource teamsChannel 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      isEnabled: true
    }
  }
}

output id string = bot.id
output name string = bot.name
