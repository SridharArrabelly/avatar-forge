// Virtual network backing the private audit path (#122).
//
// Only deployed when private networking is enabled. Two subnets, because the
// Container Apps environment demands a subnet it does not share:
//
//   apps  — delegated to Microsoft.App/environments, holds the environment's
//           infrastructure and the app replicas.
//   pep   — holds private endpoints. Network policies are switched off here;
//           Azure refuses to place a private endpoint in a subnet that still
//           enforces them.
//
// Outbound internet access stays open. That is deliberate: Web IQ
// (api.microsoft.ai), Grounding with Bing and ACS have no private-link
// offering, so the app must keep a public egress path to function.
targetScope = 'resourceGroup'

@description('Name of the virtual network.')
param name string

@description('Azure region for the network.')
param location string = resourceGroup().location

@description('Tags applied to the network.')
param tags object = {}

@description('Address space of the virtual network.')
param addressPrefix string = '10.100.0.0/16'

@description('Subnet for the Container Apps environment. Workload-profile environments require /27 or larger and the subnet must be delegated to Microsoft.App/environments.')
param appsSubnetPrefix string = '10.100.0.0/23'

@description('Subnet holding private endpoints.')
param privateEndpointSubnetPrefix string = '10.100.2.0/24'

var appsSubnetName = 'snet-apps'
var privateEndpointSubnetName = 'snet-private-endpoints'

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        addressPrefix
      ]
    }
    // Subnets are declared inline rather than as child resources: declaring
    // them separately lets concurrent writes to the same parent race, and the
    // loser is silently dropped.
    subnets: [
      {
        name: appsSubnetName
        properties: {
          addressPrefix: appsSubnetPrefix
          delegations: [
            {
              name: 'Microsoft.App.environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: privateEndpointSubnetName
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

output id string = vnet.id
output name string = vnet.name
output appsSubnetId string = vnet.properties.subnets[0].id
output privateEndpointSubnetId string = vnet.properties.subnets[1].id
