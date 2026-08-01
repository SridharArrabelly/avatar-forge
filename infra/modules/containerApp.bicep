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
param appInsightsConnectionString string
@description('Search service endpoint (https://<name>.search.windows.net/)')
param searchEndpoint string = ''
param agentModel string = ''
param embeddingDeployment string = ''
param avatarName string = ''
param customAvatarName string = ''
@description('Assistant persona / display name (e.g. "Nuru") for the bot welcome message. Purely cosmetic; does NOT select the avatar model. Empty falls back to "Avatar".')
param avatarDisplayName string = ''
@description('Identity tagline under the avatar name (e.g. "Your MTN Digital Assistant"). Empty uses the company-agnostic default.')
param avatarTagline string = ''
param photoAvatarName string = ''
@description('"true"/"false" string — frontend treats prebuilt as photo avatar when "true".')
param isPhotoAvatar string = ''
@description('"true"/"false" string — frontend treats avatar as custom when "true".')
param isCustomAvatar string = ''
param avatarBackgroundImageUrl string = ''
@description('Speech recognition model. Defaults to mai-transcribe-1; cascaded options include azure-speech, gpt-4o-transcribe.')
param srModel string = 'mai-transcribe-1'
@description('Recognition language locale (BCP-47, e.g. en-ZA). Use "auto" to let the SR model auto-detect.')
param recognitionLanguage string = 'auto'

// ───────── channel D in-call media (#27) ─────────
@description('ACS endpoint for the Call Automation media participant. Empty disables channel D in the container.')
param acsEndpoint string = ''

@description('"true"/"false" string. When "true", the .NET Teams media bot bridge (/ws/acs/audio) is served WITHOUT an ACS resource — sets MEETING_BOT_ENABLED so ACS_ENABLED is true on the Voice Live path alone.')
param meetingBotEnabled string = 'false'

@description('PCM sample rate (Hz) the media bot streams. Teams media bot uses 16000; ACS browser bridge uses 24000.')
param acsAudioSampleRate string = ''

@description('"true"/"false" string. When "true", the in-call avatar only answers after a wake phrase so she never talks over humans.')
param acsRequireWakePhrase string = ''

@description('"true"/"false" string. When "true", the in-call avatar sends an outgoing video tile so it is a visible participant instead of a faceless audio leg.')
param acsAvatarVideoEnabled string = ''

@description('Placeholder image used on first provision; azd replaces it during `azd deploy`.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

// Channel D ACS env (additive). Surfaces ACS_ENDPOINT only when enabled; the app
// reads it to construct the Call Automation client (managed identity via
// AZURE_CLIENT_ID). Empty -> channel D stays off and the container behaves as today.
var acsEnv = !empty(acsEndpoint) ? [
  {
    name: 'ACS_ENDPOINT'
    value: acsEndpoint
  }
] : []

// Channel D Teams media-bot env (additive). The .NET media bot connects to the
// /ws/acs/audio bridge, which only needs Voice Live (no ACS resource). MEETING_BOT_ENABLED
// flips ACS_ENABLED on so the bridge is served. Empty/false -> behaves as today.
var meetingBotOn = toLower(meetingBotEnabled) == 'true'
var meetingBotEnv = concat(
  meetingBotOn ? [ { name: 'MEETING_BOT_ENABLED', value: 'true' } ] : [],
  !empty(acsAudioSampleRate) ? [ { name: 'ACS_AUDIO_SAMPLE_RATE', value: acsAudioSampleRate } ] : [],
  !empty(acsRequireWakePhrase) ? [ { name: 'ACS_REQUIRE_WAKE_PHRASE', value: acsRequireWakePhrase } ] : [],
  !empty(acsAvatarVideoEnabled) ? [ { name: 'ACS_AVATAR_VIDEO_ENABLED', value: acsAvatarVideoEnabled } ] : []
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
            cpu: json('1.0')
            memory: '2.0Gi'
          }
          env: concat([
            { name: 'PORT', value: '3000' }
            { name: 'AZURE_CLIENT_ID', value: uamiClientId }
            { name: 'DEVELOPER_MODE', value: 'false' }
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
            { name: 'AVATAR_NAME', value: avatarName }
            { name: 'CUSTOM_AVATAR_NAME', value: customAvatarName }
            { name: 'AVATAR_DISPLAY_NAME', value: avatarDisplayName }
            { name: 'AVATAR_TAGLINE', value: avatarTagline }
            { name: 'PHOTO_AVATAR_NAME', value: photoAvatarName }
            { name: 'IS_PHOTO_AVATAR', value: isPhotoAvatar }
            { name: 'IS_CUSTOM_AVATAR', value: isCustomAvatar }
            { name: 'AVATAR_BACKGROUND_IMAGE_URL', value: avatarBackgroundImageUrl }
            { name: 'SR_MODEL', value: srModel }
            { name: 'RECOGNITION_LANGUAGE', value: recognitionLanguage }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
          ], concat(acsEnv, meetingBotEnv))
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
