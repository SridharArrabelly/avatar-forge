param name string
param location string
param tags object
param containerAppsEnvironmentId string

@description('Workload profile to place the app on. Empty (default) omits the property entirely, which is required for environments that have no workload profiles. Set to "Consumption" when the environment is VNet-injected.')
param workloadProfileName string = ''
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

@description('Web IQ base URL used as the web tool in model mode. Empty falls back to the code default — the tool is gated on webIqApiKey, not on this.')
param webIqBaseUrl string = ''

@description('Comma-separated host allow-list applied to Web IQ results. Same security boundary as bingAllowedDomains — and by default the same sources: main.bicep derives these bare hosts from bingAllowedDomains, because site: cannot express the paths and boost levels Bing Custom Search enforces.')
param webIqAllowedDomains string = ''

@description('Web IQ API key. Passed as a container-app SECRET, never as a plain env var. Optional: with no key the app authenticates to Web IQ with its managed identity, and decides at startup whether that works.')
@secure()
param webIqApiKey string = ''
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
@description('Speech recognition model. Defaults to mai-transcribe-1; cascaded options include azure-speech, gpt-4o-transcribe.')
param srModel string = 'mai-transcribe-1'
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

// Model-mode env (additive). VOICE_BINDING defaults to 'agent', so a deploy
// that sets nothing is byte-identical to today: the Foundry agent stays bound
// and none of these variables are read. VOICELIVE_MODEL only matters when the
// binding is 'model' — Voice Live manages that model itself, so there is no
// model deployment and no quota request behind it.
var voiceBindingEnv = concat([
  { name: 'VOICE_BINDING', value: voiceBinding }
], empty(voiceLiveModel) ? [] : [
  { name: 'VOICELIVE_MODEL', value: voiceLiveModel }
])

// Web IQ is the web tool in model mode. Binding Voice Live to a model removes
// the Foundry agent, and its managed Bing grounding tool goes with it — the
// tools become ours to implement, so the web source has to be ours too.
//
// The key is a container-app SECRET rather than a plain env var, and the
// allow-list mirrors bingAllowedDomains — literally, since main.bicep derives it
// from that list rather than trusting anyone to retype it: a hard host
// restriction is what makes an open-web tool safe to hand an executive assistant.
//
// There is deliberately no enable flag. The app decides at startup whether the
// tool is usable by asking for a Web IQ token (web_search_available() in
// backend/voice/tools.py), because a flag can claim an entitlement a tenant does
// not have and a token cannot. That means the app can switch search_web on with
// no key present, so the allow-list must ALWAYS be here — the dangerous state is
// an enabled web tool with no host restriction, which would answer from the
// entire open web while agent mode stayed scoped to bingAllowedDomains.
var webIqKeyed = !empty(webIqApiKey)
var webIqSecrets = webIqKeyed ? [
  {
    name: 'webiq-api-key'
    value: webIqApiKey
  }
] : []
var webIqEnv = concat(webIqKeyed ? [
  { name: 'WEBIQ_API_KEY', secretRef: 'webiq-api-key' }
] : [], empty(webIqBaseUrl) ? [] : [
  { name: 'WEBIQ_BASE_URL', value: webIqBaseUrl }
], empty(webIqAllowedDomains) ? [] : [
  { name: 'WEBIQ_ALLOWED_DOMAINS', value: webIqAllowedDomains }
])

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

// Named only when the environment actually has workload profiles, which is the
// case exclusively on the private-networking path. Omitted otherwise, so the
// existing environment -- which has no profiles to name -- is untouched.
var workloadProfileProperty = empty(workloadProfileName) ? {} : {
  workloadProfileName: workloadProfileName
}

resource app 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uamiId}': {} }
  }
  properties: union({
    managedEnvironmentId: containerAppsEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: webIqSecrets
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
            { name: 'AGENT_MODEL', value: agentModel }
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
          ], concat(acsEnv, meetingBotEnv, voiceBindingEnv, webIqEnv, auditEnv))
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
  }, workloadProfileProperty)
}

output id string = app.id
output name string = app.name
output uri string = 'https://${app.properties.configuration.ingress.fqdn}'
