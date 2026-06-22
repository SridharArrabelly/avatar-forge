using AvatarForge.MeetingBot.Configuration;
using Microsoft.Extensions.Options;

// Graph Communications SDK namespaces — resolve on a Windows build host once the
// media packages are restored.
using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Graph.Communications.Client;
using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Skype.Bots.Media;
using Microsoft.Graph.Communications.Resources;

namespace AvatarForge.MeetingBot.Bot;

/// <summary>
/// The bot singleton: owns the <see cref="ICommunicationsClient"/> (Graph
/// calling + media platform) and the join logic. One instance for the process;
/// it spins up a <see cref="CallHandler"/> per joined meeting.
///
/// Mirrors the official Graph Communications "local media" sample shape, trimmed
/// to exactly what Slice 1 (audio) needs.
/// </summary>
public sealed class MeetingBotService : IDisposable
{
    private readonly BotOptions _options;
    private readonly ILoggerFactory _loggerFactory;
    private readonly ILogger<MeetingBotService> _logger;
    private readonly ICommunicationsClient _client;
    private readonly Dictionary<string, CallHandler> _handlers = new();

    public MeetingBotService(IOptions<BotOptions> options, ILoggerFactory loggerFactory)
    {
        _options = options.Value;
        _options.Validate();
        _loggerFactory = loggerFactory;
        _logger = loggerFactory.CreateLogger<MeetingBotService>();

        // Telemetry/logging sink required by the SDK.
        var graphLogger = new GraphLogger(nameof(MeetingBotService));

        // Build the calling client. The media platform is configured with our
        // public FQDN, media port and TLS cert (see BotOptions) so the
        // Real-Time Media Platform can negotiate media with Teams.
        var builder = new CommunicationsClientBuilder(
                appName: "AvatarForgeMeetingBot",
                appId: _options.AppId,
                logger: graphLogger)
            .SetAuthenticationProvider(
                new AuthenticationProvider(
                    _options.AppId,
                    _options.AppSecret,
                    _options.TenantId,
                    graphLogger))
            .SetNotificationUrl(new Uri($"https://{_options.ServiceFqdn}:{_options.SignalingPort}/api/calling"))
            .SetMediaPlatformSettings(BuildMediaPlatformSettings())
            .SetServiceBaseUrl(new Uri("https://graph.microsoft.com/v1.0"));

        _client = builder.Build();
        _client.Calls().OnIncoming += OnIncomingCall;
        _client.Calls().OnUpdated += OnCallsUpdated;
    }

    private MediaPlatformSettings BuildMediaPlatformSettings() => new()
    {
        MediaPlatformInstanceSettings = new MediaPlatformInstanceSettings
        {
            CertificateThumbprint = _options.CertificateThumbprint,
            InstanceInternalPort = _options.MediaPort,
            InstancePublicPort = _options.MediaPort,
            InstancePublicIPAddress = System.Net.IPAddress.Any,
            ServiceFqdn = _options.ServiceFqdn,
        },
        ApplicationId = _options.AppId,
    };

    /// <summary>
    /// Join a Teams meeting by its full join URL (the "Click here to join the
    /// meeting" link). Anonymous app-hosted-media join — no per-user token.
    /// Returns the call id.
    /// </summary>
    public async Task<string> JoinMeetingAsync(string joinUrl, string? displayName = null)
    {
        // Parse the join URL into the chat + meeting info the SDK needs.
        var (chatInfo, meetingInfo, meetingTenantId) = JoinInfo.ParseJoinURL(joinUrl);

        var mediaSession = CreateLocalMediaSession();

        var joinParams = new JoinMeetingParameters(chatInfo, meetingInfo, mediaSession)
        {
            // Use the MEETING's tenant (from the join URL context), not the bot's
            // home tenant. The SDK acquires its Graph token for this tenant, and
            // Graph rejects "tenant mismatch" if they differ.
            TenantId = meetingTenantId ?? _options.TenantId,
        };
        if (!string.IsNullOrWhiteSpace(displayName))
        {
            joinParams.GuestIdentity = new Microsoft.Graph.Models.Identity
            {
                DisplayName = displayName,
                Id = Guid.NewGuid().ToString(),
            };
        }

        var call = await _client.Calls().AddAsync(joinParams).ConfigureAwait(false);
        _logger.LogInformation("Joining meeting; call id = {CallId}", call.Id);

        var handler = new CallHandler(call, _options, _loggerFactory);
        _handlers[call.Id] = handler;
        await handler.StartAsync().ConfigureAwait(false);
        return call.Id;
    }

    /// <summary>Leave / end a joined call.</summary>
    public async Task LeaveAsync(string callId)
    {
        if (_handlers.Remove(callId, out var handler))
        {
            try { await handler.Call.DeleteAsync().ConfigureAwait(false); }
            finally { await handler.DisposeAsync().ConfigureAwait(false); }
        }
    }

    /// <summary>
    /// Build the local media session. Audio is always present (Slice 1). When
    /// <see cref="BotOptions.EnableVideo"/> is set, an outbound NV12 VideoSocket
    /// is added so Nuru can render a synced avatar camera tile (Slice 2A); when
    /// it is unset the session is byte-for-byte the audio-only Slice 1 session.
    /// See docs/teams-meeting-bot.md §10 and docs/teams-avatar-video.md.
    /// </summary>
    private ILocalMediaSession CreateLocalMediaSession()
    {
        var audioSettings = new AudioSocketSettings
        {
            StreamDirections = StreamDirection.Sendrecv,
            // MIXED whole-room audio at 16 kHz; no per-participant unmixing.
            SupportedAudioFormat = AudioFormat.Pcm16K,
        };

        if (!_options.EnableVideo)
            return _client.CreateMediaSession(audioSettings);

        // Slice 2A: outbound-only NV12 video for the avatar's camera tile. We
        // advertise the configured format (plus a 720p fallback) so Teams can
        // negotiate a send resolution; the playout loop pushes frames sourced
        // from the same Voice Live avatar synthesis as the audio.
        var videoSettings = new VideoSocketSettings
        {
            StreamDirections = StreamDirection.Sendonly,
            ReceiveColorFormat = VideoColorFormat.NV12,
            SupportedSendVideoFormats = new List<VideoFormat>
            {
                VideoFormatFor(_options.VideoWidth, _options.VideoHeight, _options.VideoFps),
                VideoFormat.NV12_1280x720_15Fps,
            },
        };

        return _client.CreateMediaSession(audioSettings, videoSettings);
    }

    /// <summary>
    /// Map configured dimensions/fps to a supported NV12 <see cref="VideoFormat"/>.
    /// Falls back to 640x360@15 (a safe talking-head size) for unknown combos.
    /// </summary>
    internal static VideoFormat VideoFormatFor(int width, int height, int fps) =>
        (width, height, fps) switch
        {
            (1280, 720, 30) => VideoFormat.NV12_1280x720_30Fps,
            (1280, 720, 15) => VideoFormat.NV12_1280x720_15Fps,
            (960, 540, 30) => VideoFormat.NV12_960x540_30Fps,
            (640, 360, 30) => VideoFormat.NV12_640x360_30Fps,
            (640, 360, 15) => VideoFormat.NV12_640x360_15Fps,
            (480, 270, 15) => VideoFormat.NV12_480x270_15Fps,
            (320, 180, 30) => VideoFormat.NV12_180x320_30Fps,
            _ => VideoFormat.NV12_640x360_15Fps,
        };

    private void OnIncomingCall(object? sender, CollectionEventArgs<ICall> args)
    {
        // We are an outbound joiner, not an answerer, so incoming calls are
        // unexpected. Log and ignore (or redirect) per policy.
        foreach (var call in args.AddedResources)
            _logger.LogWarning("Unexpected incoming call {CallId} — ignoring.", call.Id);
    }

    private void OnCallsUpdated(object? sender, CollectionEventArgs<ICall> args)
    {
        foreach (var call in args.RemovedResources)
        {
            if (_handlers.Remove(call.Id, out var handler))
            {
                _logger.LogInformation("Call {CallId} ended; tearing down handler.", call.Id);
                _ = handler.DisposeAsync();
            }
        }
    }

    /// <summary>Expose the SDK's HTTP request processor for the calling webhook.</summary>
    public ICommunicationsClient Client => _client;

    public void Dispose()
    {
        foreach (var h in _handlers.Values) _ = h.DisposeAsync();
        _handlers.Clear();
        _client.Dispose();
    }
}
