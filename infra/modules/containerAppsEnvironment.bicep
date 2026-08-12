param name string
param location string
param tags object
param logAnalyticsWorkspaceName string

@description('Subnet the environment is injected into. Empty (default) leaves the environment on the Azure-managed network, exactly as before. Supplying a subnet is a create-time decision: the network type of an existing environment cannot be changed in place.')
param infrastructureSubnetId string = ''

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

// Folded in with union() rather than written inline so the default path emits
// the same properties it always has. An environment cannot move between the
// managed network and a custom VNet, so a stray key here would force existing
// deployments to be torn down and recreated.
//
// Workload profiles (rather than the legacy Consumption-only environment) cost
// one extra public IP a month and are not on a deprecation path; since the
// choice is also permanent, that is the cheaper mistake to make.
var networkProperties = empty(infrastructureSubnetId) ? {} : {
  vnetConfiguration: {
    infrastructureSubnetId: infrastructureSubnetId
    // External: ingress stays reachable from the internet. This VNet exists to
    // give the app a private route *out* to Cosmos, not to hide the app.
    internal: false
  }
  workloadProfiles: [
    {
      name: 'Consumption'
      workloadProfileType: 'Consumption'
    }
  ]
}

resource env 'Microsoft.App/managedEnvironments@2024-10-02-preview' = {
  name: name
  location: location
  tags: tags
  properties: union({
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
    zoneRedundant: false
  }, networkProperties)
}

output id string = env.id
output name string = env.name
output defaultDomain string = env.properties.defaultDomain