// ─────────────────────────────────────────────────────────────────────────────
// Avatar-Forge Teams meeting media bot — Windows host + calling registration.
//
// Channel D, issue #27. Deployed by `azd up` ONLY when the in-call channel is
// selected — either `DEPLOY_PROFILE=in-call` or `DEPLOY_MEETING_BOT_HOST=true`,
// and only once the required inputs (app id, DNS label, admin password) are
// present. A deploy that does not opt in never instantiates this module, so it
// behaves exactly as it did before the module existed.
//
// What it provisions:
//   1. A Windows Server VM (the only OS the Real-Time Media Platform supports)
//      with a public IP + DNS label, sized for a single concurrent meeting POC.
//   2. An NSG opening the signaling port (Bot Framework calling webhook) and the
//      media port range to the public internet (Teams media negotiation needs it).
//   3. An Azure Bot registration with the Teams channel CALLING webhook enabled,
//      pointing at this host's signaling endpoint.
//
// NOTE: this registration needs its OWN Entra app — an app can back only one
// Azure Bot resource, so it cannot be the same app as the channel C chat bot.
// `scripts/preflight.py` checks for that collision before you deploy.
//
// Operational docs: docs/channels/d-in-call-media-bot.md
// Design record:    docs/channels/d-design-media-bot.md
// ─────────────────────────────────────────────────────────────────────────────
targetScope = 'resourceGroup'

@description('Location for the Windows host and networking.')
param location string = resourceGroup().location

@description('Entra app client id (Microsoft App ID) of the calling bot.')
param botAppId string

@description('Tenant id of the single-tenant calling bot app registration.')
param botAppTenantId string

@description('The avatar brand/display name (sourced from AVATAR_DISPLAY_NAME). Becomes the meeting roster name via the Azure Bot resource. Never hardcode the custom avatar name.')
param avatarDisplayName string = 'Avatar'

@description('Display name for the Azure Bot resource. Defaults to the avatar name so the meeting roster shows the avatar brand.')
param botDisplayName string = avatarDisplayName

@description('Public URL of the bot icon (the avatar logo). Shown for the bot in Teams. Leave empty to use the default Bot Framework icon.')
param botIconUrl string = ''

@description('Local administrator username for the Windows VM.')
param adminUsername string = 'avatarbot'

@description('Local administrator password for the Windows VM.')
@secure()
param adminPassword string

@description('VM size. Standard_D4s_v5 (4 vCPU) is the smallest size the Teams Real-Time Media Platform runs reliably on — a 2-vCPU host was tried and had to be resized. Do not lower this without re-testing a live meeting.')
param vmSize string = 'Standard_D4s_v5'

@description('Globally-unique DNS label for the public IP (becomes <label>.<region>.cloudapp.azure.com).')
param dnsLabel string

@description('HTTPS signaling/webhook port (Bot Framework calling notifications).')
param signalingPort int = 9441

@description('Media platform public TCP port (Real-Time Media Platform).')
param mediaPort int = 8445

@description('Resource tags.')
param tags object = {}

var prefix = 'avatar-meetingbot'
var publicFqdn = '${dnsLabel}.${location}.cloudapp.azure.com'

// ───────── Networking ─────────
resource nsg 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: '${prefix}-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'Allow-Signaling-HTTPS'
        properties: {
          priority: 1000
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          sourceAddressPrefix: 'Internet'
          destinationAddressPrefix: '*'
          destinationPortRange: string(signalingPort)
        }
      }
      {
        name: 'Allow-Media'
        properties: {
          priority: 1010
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          sourceAddressPrefix: 'Internet'
          destinationAddressPrefix: '*'
          destinationPortRange: string(mediaPort)
        }
      }
      {
        name: 'Allow-ACME-HTTP'
        properties: {
          priority: 1015
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          // Port 80 for Let's Encrypt HTTP-01 validation (win-acme). Used only
          // during cert issuance/renewal; the bot itself serves HTTPS.
          sourceAddressPrefix: 'Internet'
          destinationAddressPrefix: '*'
          destinationPortRange: '80'
        }
      }
      {
        name: 'Allow-RDP'
        properties: {
          priority: 1020
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourcePortRange: '*'
          // NOTE: tighten this to your admin IP before production.
          sourceAddressPrefix: 'Internet'
          destinationAddressPrefix: '*'
          destinationPortRange: '3389'
        }
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2023-09-01' = {
  name: '${prefix}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.20.0.0/24'] }
    subnets: [
      {
        name: 'default'
        properties: {
          addressPrefix: '10.20.0.0/25'
          networkSecurityGroup: { id: nsg.id }
        }
      }
    ]
  }
}

resource pip 'Microsoft.Network/publicIPAddresses@2023-09-01' = {
  name: '${prefix}-pip'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: {
    publicIPAllocationMethod: 'Static'
    dnsSettings: { domainNameLabel: dnsLabel }
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2023-09-01' = {
  name: '${prefix}-nic'
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          subnet: { id: vnet.properties.subnets[0].id }
          privateIPAllocationMethod: 'Dynamic'
          publicIPAddress: { id: pip.id }
        }
      }
    ]
  }
}

// ───────── Windows VM (Real-Time Media Platform host) ─────────
resource vm 'Microsoft.Compute/virtualMachines@2023-09-01' = {
  name: '${prefix}-vm'
  location: location
  tags: tags
  properties: {
    hardwareProfile: { vmSize: vmSize }
    osProfile: {
      computerName: 'avatarbot'
      adminUsername: adminUsername
      adminPassword: adminPassword
    }
    storageProfile: {
      imageReference: {
        publisher: 'MicrosoftWindowsServer'
        offer: 'WindowsServer'
        sku: '2022-datacenter-azure-edition'
        version: 'latest'
      }
      osDisk: {
        createOption: 'FromImage'
        managedDisk: { storageAccountType: 'Premium_LRS' }
      }
    }
    networkProfile: {
      networkInterfaces: [{ id: nic.id }]
    }
  }
}

// ───────── Azure Bot registration with CALLING webhook ─────────
// The calling webhook is what makes this a Teams *calling* bot (vs the channel C
// chat bot). It must point at the media bot's HTTPS signaling endpoint.
resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: '${prefix}-registration'
  location: 'global'
  tags: tags
  sku: { name: 'F0' }
  kind: 'azurebot'
  properties: {
    displayName: botDisplayName
    iconUrl: empty(botIconUrl) ? null : botIconUrl
    // The chat messaging endpoint is unused by the media bot; calling uses the
    // channel webhook below. Point it at the same host for completeness.
    endpoint: 'https://${publicFqdn}:${signalingPort}/api/messages'
    msaAppId: botAppId
    msaAppType: 'SingleTenant'
    msaAppTenantId: botAppTenantId
  }
}

resource teamsChannel 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      isEnabled: true
      // Enable Teams *calling* and point at the media bot's calling webhook.
      enableCalling: true
      callingWebhook: 'https://${publicFqdn}:${signalingPort}/api/calling'
    }
  }
}

output botRegistrationId string = bot.id
output publicFqdn string = publicFqdn
output signalingEndpoint string = 'https://${publicFqdn}:${signalingPort}/api/calling'
output operatorApi string = 'https://${publicFqdn}:${signalingPort}/api/join'
