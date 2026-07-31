using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;

namespace AvatarForge.MeetingBot.Bridge;

/// <summary>
/// WebSocket client that speaks the <c>AcsVoiceBridge</c> wire protocol
/// (see <c>backend/acs/bridge.py</c>) to the unchanged Python backend.
///
/// This class is the entire "contract" between the .NET media bot and the
/// Python brain. It has NO dependency on the Graph media SDK, so it is fully
/// unit-testable on any OS.
///
/// Wire protocol:
///   Outbound (bot -> Python, the room speaking):
///     1. one metadata frame:
///        {"kind":"AudioMetadata","audioMetadata":{"sampleRate":16000,"channels":1,"encoding":"pcm"}}
///     2. then audio frames (20 ms each):
///        {"kind":"AudioData","audioData":{"data":"<base64 PCM16>","silent":false}}
///   Inbound (Python -> bot, Nuru answering):
///     {"Kind":"AudioData","AudioData":{"Data":"<base64 PCM16>"}}   -> play into call
///     {"Kind":"StopAudio","StopAudio":{}}                          -> flush outbound buffer (barge-in)
///     {"Kind":"VideoData","VideoData":{"Data":"<base64 NV12>","Width":640,"Height":360}}
///                                                                  -> render as the avatar camera tile
///
/// The VideoData frames carry raw NV12 video from the SAME Voice Live avatar
/// synthesis that produced the AudioData, so audio and video stay lip-synced.
/// Video is only emitted by Python when the avatar is enabled on the bridge
/// session; an audio-only deploy never sends it and the .NET side never wires a
/// VideoSocket (see <see cref="Configuration.BotOptions.EnableVideo"/>).
/// </summary>
public sealed class VoiceLiveBridgeClient : IAsyncDisposable
{
    private readonly Uri _uri;
    private readonly int _sampleRate;
    private readonly ILogger<VoiceLiveBridgeClient> _logger;
    private ClientWebSocket? _ws;
    private CancellationTokenSource? _cts;
    private Task? _receiveLoop;

    // ClientWebSocket permits exactly ONE outstanding SendAsync. Room audio is
    // pushed from the Graph media callback (a new 20 ms frame every 20 ms, and the
    // callback is async so the SDK does not wait for the previous one to finish),
    // while control frames are sent from other threads entirely. Without this gate
    // two sends overlap, SendAsync throws InvalidOperationException, and the frame
    // of room audio is swallowed by the caller's catch — the avatar silently
    // mishears the question rather than failing visibly.
    //
    // Not covered by BridgeContract.Tests, deliberately: those run over loopback
    // where a send completes in sub-microseconds, so the overlap never occurs and
    // such a test passes with the gate removed. In production this socket crosses
    // VM -> Container App at ~3.6 ms median, which is what keeps a send in flight
    // while the next 20 ms frame arrives. Don't "prove" this with a local test.
    private readonly SemaphoreSlim _sendGate = new(1, 1);

    // Single JSON options instance (camelCase ignored — we emit explicit names).
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    /// <summary>Raised when Nuru's synthesized PCM16 arrives to be played into the call.</summary>
    public event Func<byte[], Task>? AudioReceived;

    /// <summary>Raised on barge-in: flush any buffered outbound audio immediately.</summary>
    public event Func<Task>? StopAudioRequested;

    /// <summary>
    /// Raised when a Nuru avatar video frame (raw NV12) arrives to be rendered as
    /// the bot's camera tile. Only fires when the Python bridge has the avatar
    /// enabled and is forwarding <c>VideoData</c> frames.
    /// </summary>
    public event Func<VideoFrame, Task>? VideoReceived;

    public VoiceLiveBridgeClient(Uri uri, int sampleRate, ILogger<VoiceLiveBridgeClient> logger)
    {
        _uri = uri;
        _sampleRate = sampleRate;
        _logger = logger;
    }

    public async Task ConnectAsync(CancellationToken ct = default)
    {
        _ws = new ClientWebSocket();
        await _ws.ConnectAsync(_uri, ct).ConfigureAwait(false);
        _logger.LogInformation("Bridge connected to {Uri}", _uri);

        // First frame must be the audio metadata describing the PCM we will send.
        await SendMetadataAsync(ct).ConfigureAwait(false);

        _cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        _receiveLoop = Task.Run(() => ReceiveLoopAsync(_cts.Token));
    }

    private Task SendMetadataAsync(CancellationToken ct)
    {
        var frame = new
        {
            kind = "AudioMetadata",
            audioMetadata = new { sampleRate = _sampleRate, channels = 1, encoding = "pcm" },
        };
        return SendJsonAsync(frame, ct);
    }

    /// <summary>
    /// Forward one PCM16 frame of meeting audio to Python. <paramref name="silent"/>
    /// lets the Python side skip silence cheaply (it still counts frames).
    /// </summary>
    public Task SendAudioFrameAsync(ReadOnlyMemory<byte> pcm16, bool silent, CancellationToken ct = default)
    {
        var frame = new
        {
            kind = "AudioData",
            audioData = new { data = Convert.ToBase64String(pcm16.Span), silent },
        };
        return SendJsonAsync(frame, ct);
    }

    private async Task SendJsonAsync(object frame, CancellationToken ct)
    {
        if (_ws is not { State: WebSocketState.Open }) return;
        var json = JsonSerializer.SerializeToUtf8Bytes(frame, JsonOpts);
        await _sendGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            // Re-check: the socket can close while we were queued on the gate.
            if (_ws is not { State: WebSocketState.Open }) return;
            await _ws.SendAsync(json, WebSocketMessageType.Text, endOfMessage: true, ct).ConfigureAwait(false);
        }
        finally
        {
            _sendGate.Release();
        }
    }

    private async Task ReceiveLoopAsync(CancellationToken ct)
    {
        var buffer = new byte[64 * 1024];
        var sb = new StringBuilder();
        try
        {
            while (_ws is { State: WebSocketState.Open } && !ct.IsCancellationRequested)
            {
                sb.Clear();
                WebSocketReceiveResult result;
                do
                {
                    result = await _ws.ReceiveAsync(buffer, ct).ConfigureAwait(false);
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        _logger.LogInformation("Bridge closed by server.");
                        return;
                    }
                    sb.Append(Encoding.UTF8.GetString(buffer, 0, result.Count));
                }
                while (!result.EndOfMessage);

                await DispatchAsync(sb.ToString()).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException) { /* shutting down */ }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Bridge receive loop failed.");
        }
    }

    private async Task DispatchAsync(string message)
    {
        BridgeInbound? frame;
        try
        {
            frame = JsonSerializer.Deserialize<BridgeInbound>(message, JsonOpts);
        }
        catch (JsonException ex)
        {
            _logger.LogWarning(ex, "Unparseable bridge frame dropped.");
            return;
        }
        if (frame is null) return;

        switch (frame.Kind)
        {
            case "AudioData" when frame.AudioData?.Data is { Length: > 0 } b64:
                var pcm = Convert.FromBase64String(b64);
                if (AudioReceived is not null) await AudioReceived(pcm).ConfigureAwait(false);
                break;

            case "StopAudio":
                if (StopAudioRequested is not null) await StopAudioRequested().ConfigureAwait(false);
                break;

            case "VideoData" when frame.VideoData?.Data is { Length: > 0 } vb64:
                if (VideoReceived is not null)
                {
                    var nv12 = Convert.FromBase64String(vb64);
                    await VideoReceived(new VideoFrame(nv12, frame.VideoData.Width, frame.VideoData.Height))
                        .ConfigureAwait(false);
                }
                break;
        }
    }

    public async ValueTask DisposeAsync()
    {
        try { _cts?.Cancel(); } catch { /* ignore */ }
        if (_receiveLoop is not null)
        {
            try { await _receiveLoop.ConfigureAwait(false); } catch { /* ignore */ }
        }
        if (_ws is not null)
        {
            try
            {
                if (_ws.State == WebSocketState.Open)
                    await _ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "bye", CancellationToken.None).ConfigureAwait(false);
            }
            catch { /* ignore */ }
            _ws.Dispose();
        }
        _cts?.Dispose();
        _sendGate.Dispose();
    }

    // ── inbound DTOs (PascalCase, matching the Python outbound frames) ──
    private sealed class BridgeInbound
    {
        [JsonPropertyName("Kind")] public string? Kind { get; set; }
        [JsonPropertyName("AudioData")] public AudioDataPayload? AudioData { get; set; }
        [JsonPropertyName("VideoData")] public VideoDataPayload? VideoData { get; set; }
    }

    private sealed class AudioDataPayload
    {
        [JsonPropertyName("Data")] public string? Data { get; set; }
    }

    private sealed class VideoDataPayload
    {
        [JsonPropertyName("Data")] public string? Data { get; set; }
        [JsonPropertyName("Width")] public int Width { get; set; }
        [JsonPropertyName("Height")] public int Height { get; set; }
    }
}

/// <summary>
/// One decoded avatar video frame in NV12 (Y plane of Width*Height bytes followed
/// by an interleaved UV plane of Width*Height/2 bytes). Transport-only; carries no
/// media-SDK dependency so it stays unit-testable on any OS.
/// </summary>
public readonly record struct VideoFrame(byte[] Nv12, int Width, int Height);
