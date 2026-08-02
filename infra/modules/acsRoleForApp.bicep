// Grants the Container App's user-assigned managed identity access to the ACS
// resource so the server can construct the Call Automation / Identity clients
// with DefaultAzureCredential (ACS_ENDPOINT path) — no connection string needed.
//
// Conditional + additive: only deployed when channel C is enabled (enableAcs=true).
// Scoped to the single ACS resource (least privilege at resource scope). ACS has no
// granular data-plane built-in role, so Contributor on the resource is the supported
// grant for Entra-authenticated data-plane access (token mint + connect_call).
targetScope = 'resourceGroup'

@description('Name of the ACS resource (in this resource group).')
param acsName string

@description('Principal id of the Container App user-assigned managed identity.')
param appPrincipalId string

var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c' // Contributor

resource acs 'Microsoft.Communication/communicationServices@2023-04-01' existing = {
  name: acsName
}

resource appAcsContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acs.id, appPrincipalId, contributorRoleId)
  scope: acs
  properties: {
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
  }
}
