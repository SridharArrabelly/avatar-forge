// Azure Communication Services resource for the channel D in-call media participant (#27).
//
// Conditional + additive (mirrors botService.bicep): only deployed when channel D is
// explicitly enabled (enableAcs=true). A deploy WITHOUT channel D never creates this
// resource, so existing (media-less) deployments behave exactly as today.
//
// SCOPE OF THIS MODULE: it provisions ONLY the ACS resource itself. It does NOT:
//   * link the ACS resource to a Teams tenant for interop (that is a Teams-admin /
//     tenant-policy action — see docs/admin-checklist.md), or
//   * create any Entra app registration / grant admin consent.
// Those are deliberately out of the resource-group deployment scope because they
// require directory / Teams-admin rights this project does not assume.
targetScope = 'resourceGroup'

@description('Name of the Azure Communication Services resource.')
param name string

@description('Resource tags.')
param tags object

@description('Data residency / geography for the ACS resource (NOT an Azure region). e.g. "United States", "Europe", "Africa".')
param dataLocation string = 'United States'

// ACS is a global resource; only its DATA residency (dataLocation) is geographic.
resource acs 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: name
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
  }
}

output id string = acs.id
output name string = acs.name
// Endpoint used by the Call Automation client (https://<name>.communication.azure.com/).
output endpoint string = 'https://${acs.properties.hostName}'
