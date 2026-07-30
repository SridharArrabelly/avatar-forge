using System.Text.Json;
using AvatarForge.MeetingBot.Bridge;
using Xunit;

namespace AvatarForge.MeetingBot.Tests;

/// <summary>
/// Locks the wire protocol between this bot and <c>backend/acs/bridge.py</c>.
///
/// Why these tests exist: the two sides are written in different languages and
/// agree only by convention. The casing is deliberately asymmetric -- the bot
/// sends camelCase (<c>kind</c>, <c>audioData</c>) and Python replies in
/// PascalCase (<c>Kind</c>, <c>AudioData</c>) -- which is easy to "tidy up" on
/// one side and thereby break in a way nothing catches: the bot still connects,
/// the meeting still looks healthy, and the avatar simply never speaks.
///
/// Each assertion below mirrors a specific line of the Python bridge. If you
/// change one side, this suite should fail before a meeting does.
/// </summary>
public class VoiceLiveBridgeClientTests
{
    private const int SampleRate = 16000;

    private static VoiceLiveBridgeClient ClientFor(FakePythonBridge bridge) =>
        new(bridge.Uri, SampleRate, NullLogger.For<VoiceLiveBridgeClient>());

    /// <summary>
    /// Python reads the sample rate from the first frame and sizes its resampler
    /// from it (bridge.py: "once the bot's sample rate is known from the
    /// AudioMetadata frame"). Sending audio first, or omitting this, leaves the
    /// bridge unconfigured.
    /// </summary>
    [Fact]
    public async Task Connect_sends_AudioMetadata_first_describing_the_PCM()
    {
        await using var bridge = await FakePythonBridge.StartAsync();
        await using var client = ClientFor(bridge);

        await client.ConnectAsync();

        using var frame = JsonDocument.Parse(await bridge.NextFrameAsync());
        var root = frame.RootElement;

        Assert.Equal("AudioMetadata", root.GetProperty("kind").GetString());

        var meta = root.GetProperty("audioMetadata");
        Assert.Equal(SampleRate, meta.GetProperty("sampleRate").GetInt32());
        Assert.Equal(1, meta.GetProperty("channels").GetInt32());
        Assert.Equal("pcm", meta.GetProperty("encoding").GetString());
    }

    /// <summary>
    /// Room audio travels as base64 PCM16. Python matches on the lowercase key
    /// first (<c>msg.get("kind")</c>), so the casing here is part of the contract.
    /// </summary>
    [Fact]
    public async Task Room_audio_is_sent_as_base64_PCM16_under_camelCase_keys()
    {
        await using var bridge = await FakePythonBridge.StartAsync();
        await using var client = ClientFor(bridge);
        await client.ConnectAsync();
        await bridge.NextFrameAsync(); // the metadata frame

        var pcm = new byte[] { 0x01, 0x02, 0xFE, 0xFF, 0x00, 0x10 };
        await client.SendAudioFrameAsync(pcm, silent: false);

        using var frame = JsonDocument.Parse(await bridge.NextFrameAsync());
        var root = frame.RootElement;

        Assert.Equal("AudioData", root.GetProperty("kind").GetString());
        var audio = root.GetProperty("audioData");
        Assert.False(audio.GetProperty("silent").GetBoolean());
        Assert.Equal(pcm, Convert.FromBase64String(audio.GetProperty("data").GetString()!));
    }

    /// <summary>
    /// The silent flag is how Python skips silence cheaply while still counting
    /// frames (bridge.py distinguishes non-silent from silent inbound counts).
    /// Silent frames must still be sent -- the stream has to stay contiguous.
    /// </summary>
    [Fact]
    public async Task Silent_frames_are_still_sent_and_are_flagged()
    {
        await using var bridge = await FakePythonBridge.StartAsync();
        await using var client = ClientFor(bridge);
        await client.ConnectAsync();
        await bridge.NextFrameAsync();

        await client.SendAudioFrameAsync(new byte[] { 0, 0, 0, 0 }, silent: true);

        using var frame = JsonDocument.Parse(await bridge.NextFrameAsync());
        Assert.True(frame.RootElement.GetProperty("audioData").GetProperty("silent").GetBoolean());
    }

    /// <summary>
    /// The answer coming back. Python emits PascalCase here
    /// (<c>{"Kind": "AudioData", "AudioData": {"Data": ...}}</c>), which is NOT
    /// the casing the bot sends -- deserializing this with camelCase policy
    /// silently yields nulls and the avatar goes mute.
    /// </summary>
    [Fact]
    public async Task Inbound_PascalCase_AudioData_is_decoded_and_raised()
    {
        await using var bridge = await FakePythonBridge.StartAsync();
        await using var client = ClientFor(bridge);

        var expected = new byte[] { 0xAA, 0xBB, 0xCC, 0xDD };
        var got = new TaskCompletionSource<byte[]>(TaskCreationOptions.RunContinuationsAsynchronously);
        client.AudioReceived += pcm => { got.TrySetResult(pcm); return Task.CompletedTask; };

        await client.ConnectAsync();
        await bridge.SendToBotAsync(
            $$$"""{"Kind":"AudioData","AudioData":{"Data":"{{{Convert.ToBase64String(expected)}}}"}}""");

        Assert.Equal(expected, await WithTimeout(got.Task));
    }

    /// <summary>
    /// Barge-in. When a human starts speaking, Python sends StopAudio and the bot
    /// must flush whatever it has buffered -- otherwise the avatar keeps talking
    /// over the room, which is the single most damaging failure mode in a meeting.
    /// </summary>
    [Fact]
    public async Task Inbound_StopAudio_raises_barge_in()
    {
        await using var bridge = await FakePythonBridge.StartAsync();
        await using var client = ClientFor(bridge);

        var stopped = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        client.StopAudioRequested += () => { stopped.TrySetResult(); return Task.CompletedTask; };

        await client.ConnectAsync();
        await bridge.SendToBotAsync("""{"Kind":"StopAudio","StopAudio":{}}""");

        await WithTimeout(stopped.Task);
    }

    /// <summary>
    /// The avatar's face. Width and height must survive the hop: the bot renders
    /// with a format derived from these, and frames whose dimensions disagree with
    /// what the platform negotiated are dropped in favour of the placeholder tile.
    /// </summary>
    [Fact]
    public async Task Inbound_VideoData_carries_NV12_bytes_and_its_dimensions()
    {
        await using var bridge = await FakePythonBridge.StartAsync();
        await using var client = ClientFor(bridge);

        var nv12 = new byte[] { 1, 2, 3, 4, 5, 6 };
        var got = new TaskCompletionSource<VideoFrame>(TaskCreationOptions.RunContinuationsAsynchronously);
        client.VideoReceived += f => { got.TrySetResult(f); return Task.CompletedTask; };

        await client.ConnectAsync();
        await bridge.SendToBotAsync(
            $$$"""{"Kind":"VideoData","VideoData":{"Data":"{{{Convert.ToBase64String(nv12)}}}","Width":640,"Height":360}}""");

        var frame = await WithTimeout(got.Task);
        Assert.Equal(nv12, frame.Nv12);
        Assert.Equal(640, frame.Width);
        Assert.Equal(360, frame.Height);
    }

    /// <summary>
    /// One bad frame must not end the call. The receive loop runs for the whole
    /// meeting, so an unparseable or unknown frame has to be dropped and the loop
    /// kept alive -- otherwise a single malformed message silences the avatar for
    /// the rest of the session with no visible error.
    /// </summary>
    [Fact]
    public async Task A_malformed_or_unknown_frame_does_not_kill_the_receive_loop()
    {
        await using var bridge = await FakePythonBridge.StartAsync();
        await using var client = ClientFor(bridge);

        var got = new TaskCompletionSource<byte[]>(TaskCreationOptions.RunContinuationsAsynchronously);
        client.AudioReceived += pcm => { got.TrySetResult(pcm); return Task.CompletedTask; };

        await client.ConnectAsync();
        await bridge.SendToBotAsync("{ this is not json");
        await bridge.SendToBotAsync("""{"Kind":"SomethingWeHaveNeverHeardOf","Payload":{}}""");

        var expected = new byte[] { 0x7F, 0x80 };
        await bridge.SendToBotAsync(
            $$$"""{"Kind":"AudioData","AudioData":{"Data":"{{{Convert.ToBase64String(expected)}}}"}}""");

        Assert.Equal(expected, await WithTimeout(got.Task));
    }

    /// <summary>
    /// Sending before connecting is a no-op rather than a crash: CallHandler can
    /// receive media from the platform before the bridge socket is established.
    /// </summary>
    [Fact]
    public async Task Sending_before_connect_is_ignored_rather_than_throwing()
    {
        await using var bridge = await FakePythonBridge.StartAsync();
        await using var client = ClientFor(bridge);

        await client.SendAudioFrameAsync(new byte[] { 1, 2 }, silent: false);
    }

    private static async Task<T> WithTimeout<T>(Task<T> task)
    {
        var completed = await Task.WhenAny(task, Task.Delay(TimeSpan.FromSeconds(10)));
        Assert.True(completed == task, "the expected bridge event never fired");
        return await task;
    }

    private static async Task WithTimeout(Task task)
    {
        var completed = await Task.WhenAny(task, Task.Delay(TimeSpan.FromSeconds(10)));
        Assert.True(completed == task, "the expected bridge event never fired");
        await task;
    }
}
