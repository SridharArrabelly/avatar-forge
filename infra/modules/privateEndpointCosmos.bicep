// Private endpoint onto the audit Cosmos account (#122).
//
// The reason this exists: an org policy sweep sets publicNetworkAccess to
// Disabled on the account overnight. That is the correct posture for a store
// holding full conversation transcripts — the fault was ours, in having no
// private path for the app to fall back to. With this endpoint in place,
// Disabled is the steady state rather than an outage.
//
// A VNet service endpoint would not do: service endpoints filter traffic
// arriving at the public endpoint, so disabling public access takes them down
// with it. Only a private endpoint survives publicNetworkAccess: 'Disabled'.
targetScope = 'resourceGroup'

@description('Name of the private endpoint.')
param name string

@description('Azure region for the endpoint.')
param location string = resourceGroup().location

@description('Tags applied to the endpoint.')
param tags object = {}

@description('Subnet the endpoint is placed in. Must have private endpoint network policies disabled.')
param subnetId string

@description('Virtual network the private DNS zone is linked to.')
param virtualNetworkId string

@description('Name of the Cosmos DB account to expose privately.')
param cosmosAccountName string

// privatelink.documents.azure.com is the fixed zone name for Cosmos DB for
// NoSQL. The account's normal FQDN is CNAMEd into it, so application code and
// connection settings need no change: AUDIT_COSMOS_ENDPOINT keeps working and
// simply resolves to a private address from inside the network.
var privateDnsZoneName = 'privatelink.documents.azure.com'

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = {
  name: cosmosAccountName
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    subnet: {
      id: subnetId
    }
    privateLinkServiceConnections: [
      {
        name: '${name}-connection'
        properties: {
          privateLinkServiceId: account.id
          groupIds: [
            'Sql'
          ]
        }
      }
    ]
  }
}

resource privateDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: privateDnsZoneName
  location: 'global'
  tags: tags
}

resource privateDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: privateDnsZone
  name: '${name}-link'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetworkId
    }
  }
}

// Binding the zone to the endpoint is what actually writes the A records.
// Without this group the zone stays empty and every lookup falls back to the
// public address, which is precisely what is being switched off.
resource privateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'documents'
        properties: {
          privateDnsZoneId: privateDnsZone.id
        }
      }
    ]
  }
}

output id string = privateEndpoint.id
output name string = privateEndpoint.name
output privateDnsZoneName string = privateDnsZone.name
