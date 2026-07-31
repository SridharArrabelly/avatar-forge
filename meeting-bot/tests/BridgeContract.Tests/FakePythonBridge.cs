using System.Net;
using System.Net.WebSockets;
using System.Text;
using System.Threading.Channels;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace AvatarForge.MeetingBot.Tests;

/// <summary>
/// A stand-in for the Python <c>AcsVoiceBridge</c> WebSocket endpoint
/// (<c>/ws/acs/audio</c>), running on a real Kestrel socket so the client under
/// test exercises its actual transport rather than a mocked abstraction.
///
/// Records every frame the bot sends, and can push frames back the way Python
/// does. Binds to port 0 so tests never collide with a real service -- notably
/// the media platform's 8445, which has its own restart race.
/// </summary>
internal sealed class FakePythonBridge : IAsyncDisposable
{
    private readonly IHost _host;
    private readonly Channel<string> _received = Channel.CreateUnbounded<string>();
    private readonly TaskCompletionSource _connected =
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    private WebSocket? _socket;

    private FakePythonBridge(IHost host) => _host = host;

    /// <summary>The <c>wss</c>-equivalent URI to hand to the client under test.</summary>
    public Uri Uri { get; private set; } = null!;

    /// <summary>Completes once the bot has opened its WebSocket.</summary>
    public Task Connected => _connected.Task;

    public static async Task<FakePythonBridge> StartAsync()
    {
        var builder = WebApplication.CreateBuilder();
        builder.WebHost.UseUrls("http://127.0.0.1:0");
        builder.Logging.ClearProviders();

        var app = builder.Build();
        var self = new FakePythonBridge(app);

        app.UseWebSockets();
        app.Run(async context =>
        {
            if (!context.WebSockets.IsWebSocketRequest)
            {
                context.Response.StatusCode = (int)HttpStatusCode.BadRequest;
                return;
            }

            var socket = await context.WebSockets.AcceptWebSocketAsync();
            self._socket = socket;
            self._connected.TrySetResult();
            await self.ReadLoopAsync(socket, context.RequestAborted);
        });

        await app.StartAsync();

        // The bound port is only knowable after start, and it lives on the
        // server's feature collection rather than in DI.
        var addresses = app.Services
            .GetRequiredService<IServer>()
            .Features.Get<IServerAddressesFeature>();
        var origin = addresses?.Addresses.FirstOrDefault()
            ?? throw new InvalidOperationException("Kestrel reported no bound address.");
        self.Uri = new Uri(origin.Replace("http://", "ws://") + "/ws/acs/audio");
        return self;
    }

    private async Task ReadLoopAsync(WebSocket socket, CancellationToken ct)
    {
        var buffer = new byte[64 * 1024];
        try
        {
            while (socket.State == WebSocketState.Open && !ct.IsCancellationRequested)
            {
                var sb = new StringBuilder();
                WebSocketReceiveResult result;
                do
                {
                    result = await socket.ReceiveAsync(buffer, ct);
                    if (result.MessageType == WebSocketMessageType.Close) return;
                    sb.Append(Encoding.UTF8.GetString(buffer, 0, result.Count));
                }
                while (!result.EndOfMessage);

                await _received.Writer.WriteAsync(sb.ToString(), ct);
            }
        }
        catch (OperationCanceledException) { /* shutting down */ }
        catch (WebSocketException) { /* client vanished; fine for a test double */ }
    }

    /// <summary>
    /// The next frame the bot sent, or a failure if it never arrives. The timeout
    /// keeps a broken contract from hanging the suite instead of failing it.
    /// </summary>
    public async Task<string> NextFrameAsync(TimeSpan? timeout = null)
    {
        using var cts = new CancellationTokenSource(timeout ?? TimeSpan.FromSeconds(10));
        return await _received.Reader.ReadAsync(cts.Token);
    }

    /// <summary>Push a frame the way the Python bridge does (PascalCase).</summary>
    public async Task SendToBotAsync(string json)
    {
        await Connected;
        var bytes = Encoding.UTF8.GetBytes(json);
        await _socket!.SendAsync(bytes, WebSocketMessageType.Text, true, CancellationToken.None);
    }

    public async ValueTask DisposeAsync()
    {
        try { await _host.StopAsync(TimeSpan.FromSeconds(2)); } catch { /* ignore */ }
        _host.Dispose();
    }
}

/// <summary>Captures log output so a test can assert on it without a real sink.</summary>
internal static class NullLogger
{
    public static ILogger<T> For<T>() =>
        Microsoft.Extensions.Logging.Abstractions.NullLogger<T>.Instance;
}
