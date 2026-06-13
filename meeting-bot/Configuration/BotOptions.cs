namespace AvatarForge.MeetingBot.Configuration;

/// <summary>
/// Strongly-typed configuration for the meeting media bot, bound from the
/// "Bot" section of appsettings.json / environment variables.
///
/// Secrets (AppSecret) must come from the environment or a secret store — never
/// commit them. In the Avatar-Forge MngEnv tenant the values are:
///   AppId   = 860ecee0-c226-4930-8c00-e37bae4a3ae5  (avatar-forge-meeting-bot)
///   Tenant  = 349b3dac-8649-4410-acdc-ef8bbcb7a46f
///   AppSecret -> stored in azd env BOT_CLIENT_SECRET (do not hardcode)
/// </summary>
public sealed class BotOptions
{
    public const string SectionName = "Bot";

    /// <summary>Entra application (client) id of the calling bot.</summary>
    public string AppId { get; set; } = string.Empty;

    /// <summary>Entra application client secret. Inject from the environment.</summary>
    public string AppSecret { get; set; } = string.Empty;

    /// <summary>Entra tenant id the bot is registered in.</summary>
    public string TenantId { get; set; } = string.Empty;

    /// <summary>
    /// Public FQDN of this bot's signaling endpoint (the Bot Framework calling
    /// webhook), e.g. "bot.contoso.com". Must resolve to this host and be
    /// reachable over HTTPS on <see cref="SignalingPort"/>.
    /// </summary>
    public string ServiceFqdn { get; set; } = string.Empty;

    /// <summary>HTTPS port for the calling/signaling webhook (Bot Framework).</summary>
    public int SignalingPort { get; set; } = 9441;

    /// <summary>
    /// Public TCP port range / single port for the media platform's TLS media
    /// endpoint. Must be open end-to-end (NSG + Windows firewall + load
    /// balancer) to the public internet for the Real-Time Media Platform.
    /// </summary>
    public int MediaPort { get; set; } = 8445;

    /// <summary>
    /// Certificate thumbprint (installed in LocalMachine\My) used for both the
    /// signaling endpoint and the media platform. Must be a publicly-trusted
    /// cert whose subject matches <see cref="ServiceFqdn"/>.
    /// </summary>
    public string CertificateThumbprint { get; set; } = string.Empty;

    /// <summary>
    /// WebSocket URL of the Python backend bridge endpoint that speaks the
    /// AcsVoiceBridge protocol. Example:
    ///   wss://ca-avatar-mngenv-....azurecontainerapps.io/ws/acs/audio
    /// </summary>
    public string BridgeWebSocketUrl { get; set; } = string.Empty;

    /// <summary>
    /// PCM sample rate used end-to-end on the bridge. The Graph media platform
    /// delivers 16 kHz mono PCM16; Voice Live accepts 16 kHz, so we run the seam
    /// at 16 kHz with NO resampling. The Python side must agree
    /// (ACS_AUDIO_SAMPLE_RATE=16000).
    /// </summary>
    public int BridgeSampleRate { get; set; } = 16000;

    // ── Slice 2A — the avatar's video face (camera tile) ──────────────────────
    //
    // The face is a SECOND, additive media leg. When EnableVideo is false (the
    // default) the bot is audio-only and behaves exactly like Slice 1 — no
    // VideoSocket is created, no video is negotiated, nothing changes. When true,
    // the bot negotiates an outbound NV12 video stream and pumps frames into the
    // call as a participant camera tile. The frames are sourced from the SAME
    // Voice Live avatar synthesis that produces the answer audio (forwarded from
    // Python as `VideoData` bridge frames), so the lips stay in sync with the
    // speech. Until the Python video source is wired, the loop sends a static
    // placeholder frame so the camera-tile path can be proven independently.

    /// <summary>Master switch for the avatar video face (Slice 2A). Off = audio-only.</summary>
    public bool EnableVideo { get; set; } = false;

    /// <summary>Outbound video width in pixels. Must match a supported NV12 send format.</summary>
    public int VideoWidth { get; set; } = 640;

    /// <summary>Outbound video height in pixels. Must match a supported NV12 send format.</summary>
    public int VideoHeight { get; set; } = 360;

    /// <summary>Outbound video frame rate (fps). 15 keeps CPU/bandwidth modest for a talking head.</summary>
    public int VideoFps { get; set; } = 15;

    public void Validate()
    {
        if (string.IsNullOrWhiteSpace(AppId)) throw new InvalidOperationException("Bot:AppId is required.");
        if (string.IsNullOrWhiteSpace(AppSecret)) throw new InvalidOperationException("Bot:AppSecret is required (inject from env/secret store).");
        if (string.IsNullOrWhiteSpace(TenantId)) throw new InvalidOperationException("Bot:TenantId is required.");
        if (string.IsNullOrWhiteSpace(ServiceFqdn)) throw new InvalidOperationException("Bot:ServiceFqdn is required.");
        if (string.IsNullOrWhiteSpace(CertificateThumbprint)) throw new InvalidOperationException("Bot:CertificateThumbprint is required.");
        if (string.IsNullOrWhiteSpace(BridgeWebSocketUrl)) throw new InvalidOperationException("Bot:BridgeWebSocketUrl is required.");
    }
}
