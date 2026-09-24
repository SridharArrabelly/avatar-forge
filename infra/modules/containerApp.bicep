param name string
param location string
param tags object
param containerAppsEnvironmentId string
param acrLoginServer string
param uamiId string
param uamiClientId string
param voiceliveEndpoint string
param projectEndpoint string
param agentName string
param agentProjectName string
param searchConnectionName string
param searchIndexName string
param voiceLiveVoice string
param bingConnectionName string = ''
param bingCustomConfigName string = ''

@description('Voice Live binding: "agent" routes through the Foundry agent (default, unchanged behaviour); "model" binds Voice Live straight to a realtime model with in-process tools.')
param voiceBinding string = 'agent'

@description('Realtime model deployed by Voice Live when voiceBinding is "model". Voice Live manages this model itself — no model deployment or quota is needed.')
param voiceLiveModel string = ''

@description('Web IQ endpoint in model mode. Empty resolves to https://api.microsoft.ai/v3.')
param webIqBaseUrl string = ''

@description('The trusted sites (TRUSTED_WEB_SITES), as written. The app reduces them to bare hosts for Web IQ, because site: cannot express the paths and boost levels Bing enforces. Empty = the open web.')
param trustedWebSites string = ''

@description('Web IQ result language hint in model mode.')
param webIqLanguage string = 'en'

@description('Web IQ result region hint in model mode.')
param webIqRegion string = 'ZA'

@description('Web IQ API key. Passed as a container-app SECRET, never as a plain env var. Optional: with no key the app authenticates to Web IQ with its managed identity, and decides at startup whether that works.')
@secure()
param webIqApiKey string = ''

@description('Agent mode only: serve the agent\'s web tool — Web IQ behind /api/tools/search-web. resources.bicep decides this; see agentWebTool in main.bicep.')
param agentWebIq bool = false

@description('Shared key the Foundry agent presents in x-tool-key. Set = key mode; it then wins over the Entra settings below.')
@secure()
param agentWebToolKey string = ''

@description('Entra mode: Application ID URI the agent\'s token must be issued for.')
param agentWebToolAudience string = ''

@description('Entra mode, optional: the app registration\'s client ID, accepted as a v2-token audience.')
param agentWebToolAppId string = ''

@description('Entra mode: comma-separated object IDs allowed to call — the Foundry identities that sign the agent\'s calls.')
param agentWebToolCallerOids string = ''

@description('Entra mode: tenant whose tokens are accepted.')
param agentWebToolTenantId string = ''
param appInsightsConnectionString string
@description('Search service endpoint (https://<name>.search.windows.net/)')
param searchEndpoint string = ''
param agentModel string = ''
param embeddingDeployment string = ''
param avatarType string = 'standard-video'
param avatarModel string = ''
@description('Assistant persona / display name (e.g. "Nuru") for the bot welcome message. Purely cosmetic; does NOT select the avatar model. Empty falls back to "Avatar".')
param avatarDisplayName string = ''
@description('Identity tagline under the avatar name (e.g. "Your MTN Digital Assistant"). Empty uses the company-agnostic default.')
param avatarTagline string = ''
param avatarBackgroundImageUrl string = ''
param enableAvatarSpeakingStyle string = 'false'
@description('Speech recognition model. Voice Live accepts mai-transcribe (service-managed MAI model) or azure-speech.')
param srModel string = 'mai-transcribe'
@description('Recognition language locale (BCP-47, e.g. en-ZA). Use "auto" to let the SR model auto-detect.')
param recognitionLanguage string = 'auto'

// ───────── channels C/D in-call media (#27) ─────────
@description('ACS endpoint for the channel C browser guest media participant. Empty disables channel C in the container.')
param acsEndpoint string = ''

@description('"true"/"false" string. When "true", the .NET Teams media bot bridge (/ws/acs/audio) is served WITHOUT an ACS resource — sets MEETING_BOT_ENABLED so ACS_ENABLED is true on the Voice Live path alone.')
param meetingBotEnabled string = 'false'

@description('PCM sample rate (Hz) the media bot streams. Teams media bot uses 16000; ACS browser bridge uses 24000.')
param acsAudioSampleRate string = ''

@description('"true"/"false" string. When "true", the in-call avatar only answers after a wake phrase so she never talks over humans.')
param acsRequireWakePhrase string = ''

@description('"true"/"false" string. When "true", the in-call avatar sends an outgoing video tile so it is a visible participant instead of a faceless audio leg.')
param acsAvatarVideoEnabled string = ''

@description('"true"/"false" string. When "true", the browser joiner\'s outgoing tile carries the LIVE lip-synced avatar (fragmented MP4 from Voice Live, painted onto the tile canvas) instead of the static branded placard. Requires acsAvatarVideoEnabled="true" — the placard tile has to exist before it can carry a face.')
param browserJoinVideoEnabled string = ''

@description('"true"/"false" string. When "true" the browser exposes the settings panel, live transcript and per-event logging, so settings can be changed and tried live while testing. Production default "false" hides the panel, locks settings and auto-starts an avatar-only experience.')
param developerMode string = 'false'

// ───────── conversation audit trail (#30) ─────────
@description('"true"/"false" string. When "true", every conversation turn is recorded to Cosmos. Default "false" leaves the app byte-identical to today.')
param enableAudit string = 'false'

@description('Cosmos DB account endpoint holding the audit trail. Empty disables the Cosmos sink.')
param auditCosmosEndpoint string = ''

@description('Cosmos database holding the audit container.')
param auditCosmosDatabase string = ''

@description('Cosmos container holding one document per conversation turn.')
param auditCosmosContainer string = ''

@description('Days each audit record is retained, written as a per-item Cosmos TTL.')
param auditRetentionDays string = '365'

@description('Placeholder image used on first provision; azd replaces it during `azd deploy`.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

// Channel C ACS env (additive). Surfaces ACS_ENDPOINT only when enabled; the app
// reads it to construct the Call Automation client (managed identity via
// AZURE_CLIENT_ID). Empty -> channel C stays off and the container behaves as today.
var acsEnv = !empty(acsEndpoint) ? [
  {
    name: 'ACS_ENDPOINT'
    value: acsEndpoint
  }
] : []

// Audit env (additive). Surfaces the audit settings only when enabled, so a
// deploy that does not opt in has no AUDIT_* variables at all and the capture
// code never runs. AUDIT_SINK is pinned to cosmos here because that is the only
// place the deployed container has to write durably to.
var auditEnv = toLower(enableAudit) == 'true' ? [
  {
    name: 'ENABLE_AUDIT'
    value: 'true'
  }
  {
    name: 'AUDIT_SINK'
    value: 'cosmos'
  }
  {
    name: 'AUDIT_COSMOS_ENDPOINT'
    value: auditCosmosEndpoint
  }
  {
    name: 'AUDIT_COSMOS_DATABASE'
    value: auditCosmosDatabase
  }
  {
    name: 'AUDIT_COSMOS_CONTAINER'
    value: auditCosmosContainer
  }
  {
    name: 'AUDIT_RETENTION_DAYS'
    value: auditRetentionDays
  }
] : []

// Keep the effective model visible without exposing the other binding's setting.
var modelBinding = toLower(voiceBinding) == 'model'
var voiceBindingEnv = concat([
  { name: 'VOICE_BINDING', value: voiceBinding }
], modelBinding ? [
  { name: 'VOICELIVE_MODEL', value: empty(voiceLiveModel) ? 'gpt-realtime-2.1' : voiceLiveModel }
] : [
  { name: 'AGENT_MODEL', value: agentModel }
])

// Web IQ is the web tool in model mode. Binding Voice Live to a model removes
// the Foundry agent, and its managed Bing grounding tool goes with it — the
// tools become ours to implement, so the web source has to be ours too.
//
// Agent mode can opt into the same tool (agentWebIq): the agent calls this app's
// /api/tools/search-web, which runs the model-mode search_web() unchanged. So
// everything below applies to it too — above all the allow-list.
//
// The key is a container-app SECRET rather than a plain env var. The trusted
// sites are the same TRUSTED_WEB_SITES list Bing is built from, passed as
// written; the app reduces it to hosts. An empty list is a supported choice and
// means the open web.
//
// There is deliberately no enable flag. The app decides at startup whether the
// tool is usable by asking for a Web IQ token (web_search_available() in
// backend/voice/tools.py), because a flag can claim an entitlement a tenant does
// not have and a token cannot. That means the app can switch search_web on with
// no key present, so wherever Web IQ is on a configured site list must ALWAYS be
// included. If it hung off the key, a keyless deployment would search the open
// web although the operator had restricted it.
var webIqOn = modelBinding || agentWebIq
var webIqKeyed = webIqOn && !empty(webIqApiKey)
var webIqSecrets = webIqKeyed ? [
  {
    name: 'webiq-api-key'
    value: webIqApiKey
  }
] : []
var webIqEnv = webIqOn ? concat(webIqKeyed ? [
  { name: 'WEBIQ_API_KEY', secretRef: 'webiq-api-key' }
] : [], [
  { name: 'WEBIQ_BASE_URL', value: empty(webIqBaseUrl) ? 'https://api.microsoft.ai/v3' : webIqBaseUrl }
  { name: 'WEBIQ_LANGUAGE', value: empty(webIqLanguage) ? 'en' : webIqLanguage }
  { name: 'WEBIQ_REGION', value: empty(webIqRegion) ? 'ZA' : webIqRegion }
], empty(trustedWebSites) ? [] : [
  { name: 'TRUSTED_WEB_SITES', value: trustedWebSites }
]) : []

// How the agent's calls to /api/tools/search-web are authenticated; see
// backend/api/agent_tools.py. Exactly one mode is emitted, and a key wins, so the
// app never has to guess which one Foundry was configured with. Neither = the
// route answers 404.
var agentWebToolKeyed = agentWebIq && !empty(agentWebToolKey)
var agentWebToolEntra = agentWebIq && !agentWebToolKeyed && !empty(agentWebToolAudience) && !empty(agentWebToolCallerOids) && !empty(agentWebToolTenantId)
var agentWebToolSecrets = agentWebToolKeyed ? [
  {
    name: 'agent-web-tool-key'
    value: agentWebToolKey
  }
] : []
var agentWebToolEnv = concat(agentWebToolKeyed ? [
  { name: 'AGENT_WEB_TOOL_KEY', secretRef: 'agent-web-tool-key' }
] : [], agentWebToolEntra ? concat([
  { name: 'AGENT_WEB_TOOL_AUDIENCE', value: agentWebToolAudience }
  { name: 'AGENT_WEB_TOOL_CALLER_OIDS', value: agentWebToolCallerOids }
  { name: 'AGENT_WEB_TOOL_TENANT_ID', value: agentWebToolTenantId }
], empty(agentWebToolAppId) ? [] : [
  { name: 'AGENT_WEB_TOOL_APP_ID', value: agentWebToolAppId }
]) : [])

// Channel D Teams media-bot env (additive). The .NET media bot connects to the
// /ws/acs/audio bridge, which only needs Voice Live (no ACS resource). MEETING_BOT_ENABLED
// flips ACS_ENABLED on so the bridge is served. Empty/false -> behaves as today.
var meetingBotOn = toLower(meetingBotEnabled) == 'true'
var meetingBotEnv = concat(
  meetingBotOn ? [ { name: 'MEETING_BOT_ENABLED', value: 'true' } ] : [],
  !empty(acsAudioSampleRate) ? [ { name: 'ACS_AUDIO_SAMPLE_RATE', value: acsAudioSampleRate } ] : [],
  !empty(acsRequireWakePhrase) ? [ { name: 'ACS_REQUIRE_WAKE_PHRASE', value: acsRequireWakePhrase } ] : [],
  !empty(acsAvatarVideoEnabled) ? [ { name: 'ACS_AVATAR_VIDEO_ENABLED', value: acsAvatarVideoEnabled } ] : [],
  !empty(browserJoinVideoEnabled) ? [ { name: 'BROWSER_JOIN_VIDEO_ENABLED', value: browserJoinVideoEnabled } ] : []
)

resource app 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uamiId}': {} }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: concat(webIqSecrets, agentWebToolSecrets)
      ingress: {
        external: true
        targetPort: 3000
        transport: 'auto'
        allowInsecure: false
        corsPolicy: {
          allowedOrigins: [ '*' ]
          allowedMethods: [ 'GET','POST','PUT','DELETE','OPTIONS' ]
          allowedHeaders: [ '*' ]
        }
      }
      registries: [
        {
          server: acrLoginServer
          identity: uamiId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: containerImage
          resources: {
            // ACA Consumption requires an exact 1 vCPU : 2 GiB ratio, so these two
            // move together; raising cpu alone fails validation. This is also the
            // per-replica ceiling: the environment has no workloadProfiles, so it is
            // Consumption-only and caps at 2 vCPU / 4 GiB. Going higher means adding
            // a dedicated workload profile to containerAppsEnvironment.bicep first.
            // Headroom for concurrent Voice Live sessions, each of which bridges
            // audio in the app process.
            cpu: json('2.0')
            memory: '4.0Gi'
          }
          env: concat([
            { name: 'PORT', value: '3000' }
            { name: 'AZURE_CLIENT_ID', value: uamiClientId }
            { name: 'DEVELOPER_MODE', value: developerMode }
            { name: 'AZURE_VOICELIVE_ENDPOINT', value: voiceliveEndpoint }
            { name: 'PROJECT_ENDPOINT', value: projectEndpoint }
            { name: 'AGENT_NAME', value: agentName }
            { name: 'AGENT_PROJECT_NAME', value: agentProjectName }
            { name: 'EMBEDDING_DEPLOYMENT', value: embeddingDeployment }
            { name: 'AZURE_SEARCH_ENDPOINT', value: searchEndpoint }
            { name: 'SEARCH_CONNECTION_NAME', value: searchConnectionName }
            { name: 'SEARCH_INDEX_NAME', value: searchIndexName }
            { name: 'VOICELIVE_VOICE', value: voiceLiveVoice }
            { name: 'BING_CONNECTION_NAME', value: bingConnectionName }
            { name: 'BING_CUSTOM_CONFIG_NAME', value: bingCustomConfigName }
            { name: 'AVATAR_TYPE', value: avatarType }
            { name: 'AVATAR_MODEL', value: avatarModel }
            { name: 'AVATAR_DISPLAY_NAME', value: avatarDisplayName }
            { name: 'AVATAR_TAGLINE', value: avatarTagline }
            { name: 'AVATAR_BACKGROUND_IMAGE_URL', value: avatarBackgroundImageUrl }
            { name: 'ENABLE_AVATAR_SPEAKING_STYLE', value: enableAvatarSpeakingStyle }
            { name: 'SR_MODEL', value: srModel }
            { name: 'RECOGNITION_LANGUAGE', value: recognitionLanguage }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
          ], concat(acsEnv, meetingBotEnv, voiceBindingEnv, webIqEnv, agentWebToolEnv, auditEnv))
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/', port: 3000 }
              initialDelaySeconds: 10
              periodSeconds: 30
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
        rules: [
          {
            name: 'http-scaler'
            http: { metadata: { concurrentRequests: '10' } }
          }
        ]
      }
    }
  }
}

output id string = app.id
output name string = app.name
output uri string = 'https://${app.properties.configuration.ingress.fqdn}'
