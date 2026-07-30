@description('Name of the Grounding-with-Bing-Custom-Search account.')
param name string

@description('Bing accounts are a global resource — this is always "global" and is not the deployment region.')
param location string = 'global'

@description('Pricing tier. G2 is the tier this project has run on; G1 is the lower tier.')
@allowed([ 'G1', 'G2' ])
param skuName string = 'G2'

param tags object = {}

@description('Name of the custom search configuration (the curated site allow-list). This is what BING_CUSTOM_CONFIG_NAME points at.')
param configName string

@description('''
The curated allow-list the web tool is restricted to. Each entry:
  domain          — full URL, e.g. https://www.example.com/newsroom
  includeSubPages — whether pages below that path are searchable
  boostLevel      — SuperBoost | Boosted (rank weighting within the allow-list)

This is a HARD allow-list enforced by Bing: nothing outside it is reachable, which
is the property that makes an open-web tool safe to give an executive assistant.
''')
param allowedDomains array

@description('Domains to exclude even if they sit under an allowed path.')
param blockedDomains array = []

@description('Domains always returned for a matching query.')
param pinnedDomains array = []

// The site allow-list is a first-class ARM resource (not portal-only, which is the
// usual assumption), so the whole tool — account, allow-list and the Foundry
// connection — can be deployed rather than click-configured. Bicep has no type
// definitions for Microsoft.Bing yet, so these emit BCP081 "types not available"
// warnings; the shapes below are taken from a live, working resource.
#disable-next-line BCP081
resource account 'Microsoft.Bing/accounts@2025-05-01-preview' = {
  name: name
  location: location
  tags: tags
  kind: 'Bing.GroundingCustomSearch'
  sku: {
    name: skuName
  }
}

#disable-next-line BCP081
resource configuration 'Microsoft.Bing/accounts/customSearchConfigurations@2025-05-01-preview' = {
  parent: account
  name: configName
  properties: {
    allowedDomains: allowedDomains
    blockedDomains: blockedDomains
    pinnedDomains: pinnedDomains
  }
}

output accountId string = account.id
output accountName string = account.name
output configName string = configuration.name
@description('Always https://api.bing.microsoft.com/ — the target the Foundry connection points at.')
output endpoint string = 'https://api.bing.microsoft.com/'
