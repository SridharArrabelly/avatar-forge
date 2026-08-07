// Grants the Container App's user-assigned managed identity permission to write
// audit records, so the server can use DefaultAzureCredential with no stored
// credential (#30).
//
// Conditional + additive: only deployed when auditing is enabled
// (enableAudit=true), mirroring acsRoleForApp.bicep.
//
// Note this is a Cosmos DB *data-plane* role assignment
// (sqlRoleAssignments), not an Azure RBAC one. Control-plane roles such as
// Contributor do not grant document access, and because the account sets
// disableLocalAuth, this assignment is the only way in. The built-in
// "Cosmos DB Built-in Data Contributor" role (id 00000000-0000-0000-0000-
// 000000000002) is the least-privileged built-in that permits writes.
targetScope = 'resourceGroup'

@description('Name of the Cosmos DB account (in this resource group).')
param accountName string

@description('Principal id of the Container App user-assigned managed identity.')
param appPrincipalId string

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = {
  name: accountName
}

var dataContributorRoleId = '00000000-0000-0000-0000-000000000002'

resource appDataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: account
  name: guid(account.id, appPrincipalId, dataContributorRoleId)
  properties: {
    principalId: appPrincipalId
    roleDefinitionId: resourceId(
      'Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions',
      accountName,
      dataContributorRoleId
    )
    scope: account.id
  }
}
