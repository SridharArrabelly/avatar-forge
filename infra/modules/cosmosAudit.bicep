// Audit store for the conversation trail (#30).
//
// Only provisioned when auditing is explicitly enabled (enableAudit=true).
// Additive + conditional, mirroring the ACS opt-in: a deploy with
// enableAudit=false never creates this account and costs nothing.
//
// Cosmos DB for NoSQL, serverless. Chosen over MongoDB / Cosmos for MongoDB
// vCore on one decisive point: it authenticates on the data plane with Entra
// RBAC, so the most sensitive data in the system is not guarded by a stored
// connection string. Serverless bills per request, which suits the bursty,
// low-volume write pattern of one document per conversation turn.
targetScope = 'resourceGroup'

@description('Name of the Cosmos DB account.')
param name string

@description('Azure region for the account.')
param location string = resourceGroup().location

@description('Tags applied to the account.')
param tags object = {}

@description('Database holding the audit container.')
param databaseName string = 'audit'

@description('Container holding one document per conversation turn.')
param containerName string = 'turns'

@description('"Enabled" (default) or "Disabled". Set to "Disabled" only when a private endpoint reaches the account, otherwise the app cannot start: warm() fails, the fail-closed audit sink raises, and the revision never becomes healthy.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Enabled'

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: name
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    // Serverless: no always-on cluster to pay for at this volume.
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    // Data-plane keys are disabled outright. Every reader and writer must come
    // through Entra, which is the whole reason this backend was chosen.
    disableLocalAuth: true
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    publicNetworkAccess: publicNetworkAccess
    minimalTlsVersion: 'Tls12'
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: account
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
  }
}

resource container 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: containerName
  properties: {
    resource: {
      id: containerName
      // sessionId as the partition key makes "replay this conversation" a
      // single-partition query -- the cheapest read Cosmos performs.
      partitionKey: {
        paths: [
          '/sessionId'
        ]
        kind: 'Hash'
      }
      // TTL is enabled but not defaulted (-1 means "honour each item's own ttl
      // field"), so retention is set per record by AUDIT_RETENTION_DAYS and
      // needs no cleanup job.
      defaultTtl: -1
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          {
            path: '/sessionId/?'
          }
          {
            path: '/startedAt/?'
          }
          {
            path: '/turnIndex/?'
          }
        ]
        // Transcripts and retrieved passages are large and never filtered on.
        // Indexing them would inflate both write cost and storage for nothing.
        excludedPaths: [
          {
            path: '/*'
          }
          {
            path: '/"_etag"/?'
          }
        ]
      }
    }
  }
}

output accountName string = account.name
output endpoint string = account.properties.documentEndpoint
output databaseName string = databaseName
output containerName string = containerName
