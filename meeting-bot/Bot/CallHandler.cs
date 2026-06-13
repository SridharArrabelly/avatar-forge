using System.Collections.Concurrent;
using System.Runtime.InteropServices;
using AvatarForge.MeetingBot.Bridge;
using AvatarForge.MeetingBot.Configuration;

// NOTE: these usings resolve only once the Graph Communications media packages
// are restored on a Windows build host. They are the real SDK namespaces used
// by the official local-media samples (HueBot / PsiBot).
using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Skype.Bots.Media;

namespace AvatarForge.MeetingBot.Bot;

/// <summary>
/// Owns the media plumbing for a single joined call: pumps inbound MIXED
/// participant audio from the Graph <see cref="IAudioSocket"/> to the Python
/// bridge, and plays Nuru's PCM answer (received from the bridge) back into the
/// call. It contains NO answering logic — that all lives in Python.
///
/// ── Audio format ──
/// The Real-Time Media Platform delivers/accepts 16 kHz mono PCM16 (1 channel,
/// 20 ms frames = 640 bytes). We run the bridge at the same rate, so there is
/// no resampling. Keep <see cref="BotOptions.BridgeSampleRate"/> == 16000.
/// </summary>
public sealed class CallHandler : IAsyncDisposable
{
    private readonly ICall _call;
    private readonly BotOptions _options;
    private readonly ILogger<CallHandler> _logger;
    private readonly VoiceLiveBridgeClient _bridge;

    /// <summary>The Graph call this handler owns (used by the bot to leave).</summary>
    public ICall Call => _call;

    // Outbound playout queue: PCM16 chunks from Voice Live, drained at frame
    // cadence onto the AudioSocket. A queue (not direct send) lets barge-in
    // flush everything instantly.
    private readonly ConcurrentQueue<byte[]> _playout = new();
    private volatile bool _flush;

    // 20 ms of 16 kHz mono PCM16 = 16000 * 0.02 * 2 bytes = 640 bytes.
    private const int FrameBytes = 640;

    // ── Slice 2A — outbound avatar video (only used when EnableVideo) ──
    // Real NV12 frames from Voice Live (forwarded by Python as VideoData) land
    // here; the playout loop drains them at frame cadence. While none have
    // arrived yet — or whenever the queue runs dry — the loop sends a static
    // placeholder so the camera tile stays alive and the path is provable even
    // before the Python video source is wired.
    private readonly ConcurrentQueue<VideoFrame> _videoQueue = new();
    private volatile bool _videoActive;        // set from VideoSendStatusChanged
    private byte[]? _placeholderNv12;            // cached solid-colour frame
    private VideoFormat? _activeVideoFormat;     // negotiated send format

    public CallHandler(ICall call, BotOptions options, ILoggerFactory loggerFactory)
    {
        _call = call;        _options = options;
        _logger = loggerFactory.CreateLogger<CallHandler>();
        _bridge = new VoiceLiveBridgeClient(
            new Uri(options.BridgeWebSocketUrl),
            options.BridgeSampleRate,
            loggerFactory.CreateLogger<VoiceLiveBridgeClient>());
    }

    public async Task StartAsync(CancellationToken ct = default)
    {
        // 1. Connect the bridge to Python (sends AudioMetadata up-front).
        await _bridge.ConnectAsync(ct).ConfigureAwait(false);

        // 2. Nuru's answer audio -> enqueue for playout into the call.
        _bridge.AudioReceived += pcm =>
        {
            _playout.Enqueue(pcm);
            return Task.CompletedTask;
        };

        // 3. Barge-in: flush queued playout so she stops mid-sentence.
        _bridge.StopAudioRequested += () =>
        {
            _flush = true;
            while (_playout.TryDequeue(out _)) { }
            _flush = false;
            return Task.CompletedTask;
        };

        // 4. Wire the Graph AudioSocket (inbound + outbound).
        WireAudioSocket();

        // 5. Slice 2A: if the avatar face is enabled, wire the outbound VideoSocket
        //    and start pumping NV12 frames (real ones from Voice Live, else a
        //    placeholder). Audio is never blocked on this.
        if (_options.EnableVideo)
            WireVideoSocket();
    }

    /// <summary>
    /// Wires the call's audio socket. Inbound MIXED audio -> bridge. Outbound
    /// playout queue -> socket. This is the only code that touches the media
    /// SDK; everything above is transport-agnostic.
    /// </summary>
    private void WireAudioSocket()
    {
        var mediaSession = _call.GetLocalMediaSession();
        IAudioSocket audioSocket = mediaSession.AudioSocket;

        // ── Inbound: room -> Python ──
        audioSocket.AudioMediaReceived += async (_, e) =>
        {
            try
            {
                // e.Buffer is unmanaged PCM16; copy to managed before the SDK
                // recycles it, then forward to the bridge.
                var len = (int)e.Buffer.Length;
                var pcm = new byte[len];
                System.Runtime.InteropServices.Marshal.Copy(e.Buffer.Data, pcm, 0, len);

                // Cheap silence flag so Python can short-circuit (it re-checks).
                bool silent = IsSilent(pcm);
                await _bridge.SendAudioFrameAsync(pcm, silent).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Inbound audio forward failed.");
            }
            finally
            {
                e.Buffer.Dispose();
            }
        };

        // ── Outbound: Python -> room ──
        // Drain the playout queue at 20 ms cadence onto the AudioSocket.
        _ = Task.Run(() => PlayoutLoopAsync(audioSocket));
    }

    private async Task PlayoutLoopAsync(IAudioSocket audioSocket)
    {
        var carry = new List<byte>(FrameBytes * 2);
        var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(20));
        while (await timer.WaitForNextTickAsync().ConfigureAwait(false))
        {
            if (_flush) { carry.Clear(); continue; }

            while (carry.Count < FrameBytes && _playout.TryDequeue(out var chunk))
                carry.AddRange(chunk);

            if (carry.Count < FrameBytes) continue; // not enough buffered yet

            var frame = carry.GetRange(0, FrameBytes).ToArray();
            carry.RemoveRange(0, FrameBytes);

            try
            {
                // Send one 20 ms PCM16 frame into the meeting.
                var buffer = new AudioSendBuffer(frame, AudioFormat.Pcm16K);
                audioSocket.Send(buffer);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Outbound audio send failed.");
            }
        }
    }

    /// <summary>
    /// Wires the call's outbound video socket (Slice 2A). Real avatar NV12 frames
    /// arrive from the bridge (<c>VideoData</c>) and are queued; a playout loop
    /// pushes them — or a placeholder — into the call as a camera tile. Only
    /// called when <see cref="BotOptions.EnableVideo"/> is set.
    /// </summary>
    private void WireVideoSocket()
    {
        var mediaSession = _call.GetLocalMediaSession();
        IVideoSocket? videoSocket = mediaSession.VideoSocket;
        if (videoSocket is null)
        {
            _logger.LogWarning("EnableVideo set but the media session has no VideoSocket; skipping the avatar face.");
            return;
        }

        _activeVideoFormat = MeetingBotService.VideoFormatFor(_options.VideoWidth, _options.VideoHeight, _options.VideoFps);

        // The platform tells us when it is ready to receive frames (and the
        // resolution it prefers). Only send while Active to avoid wasted frames.
        videoSocket.VideoSendStatusChanged += (_, e) =>
        {
            _videoActive = e.MediaSendStatus == MediaSendStatus.Active;
            if (_videoActive && e.PreferredVideoSourceFormat is { } pref)
            {
                _activeVideoFormat = pref;
                _placeholderNv12 = null; // rebuild at the new size on next tick
            }
            _logger.LogInformation("Video send status = {Status}", e.MediaSendStatus);
        };

        // ── Inbound bridge video (Nuru's synced avatar) -> queue ──
        _bridge.VideoReceived += frame =>
        {
            _videoQueue.Enqueue(frame);
            return Task.CompletedTask;
        };

        // Flush the queued video too on barge-in so a cancelled answer's tail
        // frames don't linger (audio flush is wired in StartAsync).
        _bridge.StopAudioRequested += () =>
        {
            while (_videoQueue.TryDequeue(out _)) { }
            return Task.CompletedTask;
        };

        _ = Task.Run(() => VideoPlayoutLoopAsync(videoSocket));
    }

    private async Task VideoPlayoutLoopAsync(IVideoSocket videoSocket)
    {
        int fps = Math.Clamp(_options.VideoFps, 1, 30);
        var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(1000.0 / fps));
        var sw = System.Diagnostics.Stopwatch.StartNew();

        while (await timer.WaitForNextTickAsync().ConfigureAwait(false))
        {
            if (!_videoActive) continue;

            var format = _activeVideoFormat ?? VideoFormat.NV12_640x360_15Fps;

            byte[] nv12;
            if (_videoQueue.TryDequeue(out var real) &&
                real.Width == format.Width && real.Height == format.Height)
            {
                nv12 = real.Nv12;
            }
            else
            {
                nv12 = GetPlaceholder(format.Width, format.Height);
            }

            try
            {
                // 100 ns reference timestamp the platform uses to pace the stream.
                long ts = sw.Elapsed.Ticks;
                var buffer = new VideoSendBuffer(nv12, format, ts);
                videoSocket.Send(buffer);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Outbound video send failed.");
            }
        }
    }

    /// <summary>
    /// A cached solid-colour NV12 placeholder (Nuru brand tone) sized to the
    /// negotiated format. Used until real avatar frames flow so the camera-tile
    /// path is provable on its own.
    /// </summary>
    private byte[] GetPlaceholder(int width, int height)
    {
        var cached = _placeholderNv12;
        if (cached is not null && cached.Length == width * height * 3 / 2)
            return cached;

        // NV12: full-res Y plane, then half-res interleaved U/V plane.
        // Solid colour from RGB(60,90,150) -> Y=88, U=163, V=108.
        const byte Y = 88, U = 163, V = 108;
        int ySize = width * height;
        var buf = new byte[ySize + ySize / 2];
        Array.Fill(buf, Y, 0, ySize);
        for (int i = ySize; i + 1 < buf.Length; i += 2)
        {
            buf[i] = U;
            buf[i + 1] = V;
        }
        _placeholderNv12 = buf;
        return buf;
    }

    private static bool IsSilent(byte[] pcm16)
    {
        // Quick RMS check; threshold chosen for 16-bit samples.
        long sumSq = 0;
        for (int i = 0; i + 1 < pcm16.Length; i += 2)
        {
            short s = (short)(pcm16[i] | (pcm16[i + 1] << 8));
            sumSq += (long)s * s;
        }
        int samples = pcm16.Length / 2;
        if (samples == 0) return true;
        double rms = Math.Sqrt(sumSq / (double)samples);
        return rms < 200; // ~ -40 dBFS
    }

    public async ValueTask DisposeAsync()
    {
        await _bridge.DisposeAsync().ConfigureAwait(false);
    }
}

/// <summary>
/// Minimal AudioSendBuffer wrapper. The real SDK provides
/// <c>Microsoft.Skype.Bots.Media.AudioSendBuffer</c>; this thin subclass just
/// adapts a managed byte[] into the unmanaged buffer the platform expects.
/// (Faithful to the HueBot sample's AudioSendBuffer pattern.)
/// </summary>
internal sealed class AudioSendBuffer : Microsoft.Skype.Bots.Media.AudioMediaBuffer
{
    public AudioSendBuffer(byte[] pcm, AudioFormat format)
    {
        Length = pcm.Length;
        AudioFormat = format;
        Data = System.Runtime.InteropServices.Marshal.AllocHGlobal(pcm.Length);
        System.Runtime.InteropServices.Marshal.Copy(pcm, 0, Data, pcm.Length);
    }

    protected override void Dispose(bool disposing)
    {
        if (Data != IntPtr.Zero)
        {
            System.Runtime.InteropServices.Marshal.FreeHGlobal(Data);
            Data = IntPtr.Zero;
        }
    }
}

/// <summary>
/// Minimal NV12 <see cref="Microsoft.Skype.Bots.Media.VideoMediaBuffer"/> wrapper
/// (Slice 2A). Adapts a managed NV12 byte[] into the unmanaged buffer the media
/// platform sends as the bot's outbound camera tile, mirroring the
/// <see cref="AudioSendBuffer"/> pattern. <paramref name="timestamp"/> is the
/// 100 ns reference clock the platform uses to pace the video stream.
/// </summary>
internal sealed class VideoSendBuffer : Microsoft.Skype.Bots.Media.VideoMediaBuffer
{
    public VideoSendBuffer(byte[] nv12, Microsoft.Skype.Bots.Media.VideoFormat format, long timestamp)
    {
        Length = nv12.Length;
        VideoFormat = format;
        Timestamp = timestamp;
        Data = Marshal.AllocHGlobal(nv12.Length);
        Marshal.Copy(nv12, 0, Data, nv12.Length);
    }

    protected override void Dispose(bool disposing)
    {
        if (Data != IntPtr.Zero)
        {
            Marshal.FreeHGlobal(Data);
            Data = IntPtr.Zero;
        }
    }
}
