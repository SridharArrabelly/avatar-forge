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
    // Whether the media platform is currently accepting outbound audio. When the
    // bot is muted this is Inactive and audioSocket.Send() is silently dropped.
    private volatile bool _audioSendActive;
    private long _framesSent;

    // 20 ms of 16 kHz mono PCM16 = 16000 * 0.02 * 2 bytes = 640 bytes.
    private const int FrameBytes = 640;
    private const int BytesPerMs = 32;              // 16 kHz * 2 bytes / 1000 ms
    // Media timestamps are 100-ns ticks; one 20 ms PCM16 frame == 200,000 ticks.
    private const long FrameTicks = 200_000;
    // A pre-allocated silent frame used to fill playout underruns so the outbound
    // stream stays contiguous (see PlayoutLoopAsync).
    private static readonly byte[] Silence = new byte[FrameBytes];
    private long _underruns;
    private long _audioDropped;

    // ── Jitter buffer ──────────────────────────────────────────────────────────
    // The playout loop drains at exactly real time (one 20 ms frame per 20 ms).
    // The producer also delivers at real time, but over a network, so it arrives
    // in bursts. With no buffer the consumer sits permanently on the edge of empty
    // and any jitter is an instant underrun. Holding a small amount of audio
    // absorbs that jitter. Cheap insurance: TargetBufferMs of extra latency.
    private const int TargetBufferMs = 120;
    private const int MaxBufferMs = 400;
    private const int TargetBufferBytes = TargetBufferMs * BytesPerMs;
    private const int MaxBufferBytes = MaxBufferMs * BytesPerMs;
    private int _bufferedBytes;                     // carry + everything queued
    private bool _priming = true;                   // fill before draining
    private short _lastSample;                      // for splice ramps
    private bool _lastFrameWasSilence = true;
    // A splice between real audio and silence is a step in the waveform, which is
    // exactly what a click is. Ramp across ~2 ms instead of stepping.
    private const int RampSamples = 32;

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
    private long _videoFramesSent;
    private long _videoUnderruns;
    private long _videoFormatMismatch;
    private long _videoDropped;
    // The last real avatar frame, re-sent whenever the queue runs dry. See
    // VideoPlayoutLoopAsync for why this matters more than it sounds.
    private VideoFrame? _lastRealFrame;
    // Audio and video come from the SAME Voice Live stream, so they must be held
    // to the SAME latency or the lips drift off the voice. Sized from
    // MaxBufferMs, matching the audio jitter buffer, rather than a bare count.
    private int MaxQueuedVideoFrames =>
        Math.Max(2, Math.Clamp(_options.VideoFps, 1, 30) * MaxBufferMs / 1000);

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
            System.Threading.Interlocked.Add(ref _bufferedBytes, pcm.Length);
            return Task.CompletedTask;
        };

        // 3. Barge-in: flush queued playout so she stops mid-sentence.
        _bridge.StopAudioRequested += () =>
        {
            _flush = true;
            while (_playout.TryDequeue(out _)) { }
            System.Threading.Interlocked.Exchange(ref _bufferedBytes, 0);
            _priming = true;   // refill before the next answer starts
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

        // Track whether the platform is accepting outbound audio. If this never
        // goes Active (e.g. the bot is muted), Send() is dropped and the room
        // hears nothing even though we keep pushing frames.
        audioSocket.AudioSendStatusChanged += (_, e) =>
        {
            _audioSendActive = e.MediaSendStatus == MediaSendStatus.Active;
            _logger.LogInformation("Audio SEND status = {Status}", e.MediaSendStatus);
        };

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

                // Forward ALL audio to the bridge (never mark as silent).
                // Voice Live has its own VAD; the bridge drops silent-flagged
                // frames which fragments the audio stream and breaks STT.
                await _bridge.SendAudioFrameAsync(pcm, silent: false).ConfigureAwait(false);
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
        // Monotonic media timestamp in 100-ns ticks. The Real-Time Media Platform
        // uses this to schedule playback; frames sent with a stale/zero timestamp
        // are dropped (the room hears nothing even though Send() succeeds). One
        // 20 ms PCM16 frame == 200,000 ticks.
        long ts = System.Diagnostics.Stopwatch.GetTimestamp();
        // DIAGNOSTIC test-tone generator state (440 Hz sine @ 16 kHz).
        double phase = 0;
        const double toneHz = 440.0;
        const double phaseStep = 2 * Math.PI * toneHz / 16000.0;
        while (await timer.WaitForNextTickAsync().ConfigureAwait(false))
        {
            if (_options.TestTone)
            {
                var tone = new byte[FrameBytes];
                for (int i = 0; i < FrameBytes; i += 2)
                {
                    short s = (short)(Math.Sin(phase) * 12000);
                    tone[i] = (byte)(s & 0xFF);
                    tone[i + 1] = (byte)((s >> 8) & 0xFF);
                    phase += phaseStep;
                }
                try
                {
                    ts += FrameTicks;
                    audioSocket.Send(new AudioSendBuffer(tone, AudioFormat.Pcm16K, ts));
                    if (System.Threading.Interlocked.Increment(ref _framesSent) % 100 == 1)
                        _logger.LogInformation("TestTone: sent {Count} frames (audioSendActive={Active})",
                            _framesSent, _audioSendActive);
                }
                catch (Exception ex) { _logger.LogError(ex, "Test tone send failed."); }
                continue;
            }

            if (_flush) { carry.Clear(); }

            // ── Jitter buffer ─────────────────────────────────────────────────
            //
            // Draining at exactly real time from a queue fed at exactly real time
            // over a network means sitting permanently on the edge of empty: any
            // late packet is an instant underrun. That produced BOTH reported
            // audio defects.
            //
            // Ticking: an underrun mid-word forces a splice to silence and back.
            // Skipping the slot (the original code) left a hole; sending silence
            // (the first fix) filled the hole but the STEP in the waveform was
            // still there, so the click survived. Measured on a real call: 18% of
            // outbound VIDEO frames were underruns, in short bursts — the audio
            // path was jittering identically.
            //
            // Growing audio delay: every inserted silent frame adds 20 ms of
            // stream time WITHOUT consuming any queued audio, so it pushes all
            // subsequent speech later. Nothing ever gave that time back — a
            // one-way ratchet. Video meanwhile was capped and dropped its oldest
            // frames, so it stayed current. Hence the reported "mouth moves, voice
            // arrives later", growing over the call.
            //
            // So: hold TargetBufferMs before starting to drain, and if the backlog
            // ever exceeds MaxBufferMs, discard down to target to give the latency
            // back. Cost is TargetBufferMs of extra latency, once.
            int buffered = Volatile.Read(ref _bufferedBytes);

            if (buffered > MaxBufferBytes)
            {
                // Too far behind: drop oldest audio down to the target. Better a
                // single audible skip than permanent, growing lip-sync lag.
                while (Volatile.Read(ref _bufferedBytes) > TargetBufferBytes
                       && _playout.TryDequeue(out var stale))
                {
                    System.Threading.Interlocked.Add(ref _bufferedBytes, -stale.Length);
                    System.Threading.Interlocked.Increment(ref _audioDropped);
                }
                carry.Clear();
            }
            else if (_priming)
            {
                if (buffered < TargetBufferBytes)
                {
                    // Still filling. Emit silence so the stream stays contiguous
                    // and the media timestamps keep advancing.
                    SendFrame(audioSocket, NextSilence(), ref ts);
                    continue;
                }
                _priming = false;
            }

            while (carry.Count < FrameBytes && _playout.TryDequeue(out var chunk))
            {
                carry.AddRange(chunk);
                System.Threading.Interlocked.Add(ref _bufferedBytes, -chunk.Length);
            }

            byte[] frame;
            if (carry.Count >= FrameBytes)
            {
                frame = carry.GetRange(0, FrameBytes).ToArray();
                carry.RemoveRange(0, FrameBytes);
                if (_lastFrameWasSilence) FadeIn(frame);
                _lastSample = (short)(frame[FrameBytes - 2] | (frame[FrameBytes - 1] << 8));
                _lastFrameWasSilence = false;
            }
            else
            {
                // Genuine underrun. Emit silence and re-prime, so we refill the
                // buffer instead of stuttering into the next underrun immediately.
                frame = NextSilence();
                _priming = true;
                System.Threading.Interlocked.Increment(ref _underruns);
            }

            SendFrame(audioSocket, frame, ref ts);
        }
    }

    /// <summary>
    /// A silent 20 ms frame that ramps down from the last real sample instead of
    /// stepping to zero. The step IS the click.
    /// </summary>
    private byte[] NextSilence()
    {
        if (_lastFrameWasSilence || _lastSample == 0)
        {
            _lastFrameWasSilence = true;
            return Silence;
        }
        var buf = new byte[FrameBytes];
        for (int i = 0; i < RampSamples; i++)
        {
            short s = (short)(_lastSample * (RampSamples - i) / RampSamples);
            buf[i * 2] = (byte)(s & 0xFF);
            buf[i * 2 + 1] = (byte)((s >> 8) & 0xFF);
        }
        _lastFrameWasSilence = true;
        _lastSample = 0;
        return buf;
    }

    /// <summary>Ramp a frame up from silence, for the same reason as <see cref="NextSilence"/>.</summary>
    private static void FadeIn(byte[] frame)
    {
        for (int i = 0; i < RampSamples; i++)
        {
            short s = (short)(frame[i * 2] | (frame[i * 2 + 1] << 8));
            s = (short)(s * i / RampSamples);
            frame[i * 2] = (byte)(s & 0xFF);
            frame[i * 2 + 1] = (byte)((s >> 8) & 0xFF);
        }
    }

    private void SendFrame(IAudioSocket audioSocket, byte[] frame, ref long ts)
    {
        try
        {
            // Send one 20 ms PCM16 frame into the meeting, with a monotonic
            // media timestamp so the platform actually plays it out.
            ts += FrameTicks;
            audioSocket.Send(new AudioSendBuffer(frame, AudioFormat.Pcm16K, ts));
            if (System.Threading.Interlocked.Increment(ref _framesSent) % 500 == 1)
                _logger.LogInformation(
                    "Playout: sent {Count} frames (audioSendActive={Active}, underruns={Underruns}, "
                    + "dropped={Dropped}, bufferedMs={BufferedMs})",
                    _framesSent, _audioSendActive, _underruns, _audioDropped,
                    Volatile.Read(ref _bufferedBytes) / BytesPerMs);
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Outbound audio send failed.");
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
            // Bound the backlog: drop the OLDEST frames, never the newest, so the
            // face stays close to the audio rather than drifting further behind.
            while (_videoQueue.Count > MaxQueuedVideoFrames && _videoQueue.TryDequeue(out _))
                System.Threading.Interlocked.Increment(ref _videoDropped);
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
            if (_videoQueue.TryDequeue(out var real))
            {
                nv12 = real.Nv12;
                _lastRealFrame = real;
                // Never discard a real frame because it does not match the format
                // the platform said it preferred. That is what made the avatar
                // appear and then vanish mid-meeting: the platform raises
                // VideoSendStatusChanged with a PreferredVideoSourceFormat, we
                // adopted it, and from that moment EVERY frame Python produced
                // (fixed at VideoWidth x VideoHeight) failed the equality check and
                // was dropped in favour of the solid placeholder — a blank tile.
                // The frame itself is the source of truth: describe it accurately
                // and send it.
                if (real.Width != format.Width || real.Height != format.Height)
                {
                    format = MeetingBotService.VideoFormatFor(real.Width, real.Height, fps);
                    if (System.Threading.Interlocked.Increment(ref _videoFormatMismatch) % 150 == 1)
                    {
                        _logger.LogWarning(
                            "Avatar frames are {W}x{H} but the platform prefers {PW}x{PH}; sending the real frame at its own size ({Count} so far).",
                            real.Width, real.Height, _activeVideoFormat?.Width, _activeVideoFormat?.Height,
                            _videoFormatMismatch);
                    }
                }
            }
            else if (_lastRealFrame is { } held)
            {
                // Queue ran dry for this slot. HOLD THE LAST REAL FRAME — never
                // fall back to the placeholder once the avatar is live.
                //
                // The placeholder is a solid brand-blue fill. Sending it on every
                // underrun meant the tile alternated face / solid blue several
                // times a second: measured on a real call, 193 of 1052 recorded
                // frames (18%) were solid blue, in 84 separate runs of ~2 frames
                // each. That is the reported "background blue screen flickering".
                // A held frame is invisible at these durations — the face simply
                // pauses for 33 ms.
                nv12 = held.Nv12;
                if (held.Width != format.Width || held.Height != format.Height)
                    format = MeetingBotService.VideoFormatFor(held.Width, held.Height, fps);
                System.Threading.Interlocked.Increment(ref _videoUnderruns);
            }
            else
            {
                // No real frame has EVER arrived, so there is nothing to hold.
                // The placeholder keeps the camera tile alive and proves the path.
                nv12 = GetPlaceholder(format.Width, format.Height);
                System.Threading.Interlocked.Increment(ref _videoUnderruns);
            }

            try
            {
                // 100 ns reference timestamp the platform uses to pace the stream.
                long ts = sw.Elapsed.Ticks;
                var buffer = new VideoSendBuffer(nv12, format, ts);
                videoSocket.Send(buffer);
                if (System.Threading.Interlocked.Increment(ref _videoFramesSent) % 300 == 1)
                {
                    _logger.LogInformation(
                        "Video: sent {Count} frames at {W}x{H} (queued={Queued}, held={Underruns}, "
                        + "mismatched={Mismatch}, dropped={Dropped})",
                        _videoFramesSent, format.Width, format.Height,
                        _videoQueue.Count, _videoUnderruns, _videoFormatMismatch, _videoDropped);
                }
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
        return rms < 50; // ~ -50 dBFS; Teams meeting mix is quieter than direct mic
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
    public AudioSendBuffer(byte[] pcm, AudioFormat format, long timestamp = 0)
    {
        Length = pcm.Length;
        AudioFormat = format;
        Timestamp = timestamp;
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
