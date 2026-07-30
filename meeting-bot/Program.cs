using AvatarForge.MeetingBot.Bot;
using AvatarForge.MeetingBot.Configuration;
using System.Runtime.Loader;

// ── Side-by-side resolver for the Skype media SDK's managed helper assemblies ──
//
// Microsoft.Skype.Bots.Media ships Microsoft.Skype.Internal.Media.H264NetCore.dll
// and ...AudioLibNetCore.dll under <pkg>\src\net6.0\ as *content*, not as package
// references. The CopySkypeNativeMedia target in MeetingBot.csproj drops them next
// to the app, but because they are not references they never appear in
// AvatarForge.MeetingBot.deps.json — and for a self-contained app the host builds
// its trusted-assembly list purely from deps.json. The media platform loads them
// dynamically by name during MediaPlatform.Initialize, so the load failed with
//   "Could not load file or assembly 'Microsoft.Skype.Internal.Media.H264NetCore,
//    Version=1.26.0.94 ...'. The system cannot find the file specified."
// even though the file sits right there in the app directory.
//
// The native DLLs (NativeMedia.dll, skypert.dll, ...) are unaffected: P/Invoke
// probes the app directory directly. Only managed assemblies need this hook.
//
// Registered before anything else so it is in place well ahead of media init.
AssemblyLoadContext.Default.Resolving += (context, name) =>
{
    if (string.IsNullOrEmpty(name.Name)) return null;
    var candidate = Path.Combine(AppContext.BaseDirectory, name.Name + ".dll");
    if (!File.Exists(candidate)) return null;
    try
    {
        return context.LoadFromAssemblyPath(candidate);
    }
    catch (Exception ex)
    {
        Console.Error.WriteLine($"[assembly-resolve] {name.Name}: {ex.Message}");
        return null;
    }
};

var builder = WebApplication.CreateBuilder(args);

// Run under the Windows Service Control Manager when launched as a service
// (no-op when run as a plain console process), so `Start-Service` works.
builder.Host.UseWindowsService();

// ── Configuration ──
// Bind Bot:* from appsettings + environment. AppSecret should come from the
// environment (Bot__AppSecret) — never appsettings.json. The host setup script
// writes it from the azd env value BOT_CLIENT_SECRET.
builder.Services.Configure<BotOptions>(builder.Configuration.GetSection(BotOptions.SectionName));

// Allow the standard env var BOT_CLIENT_SECRET to populate Bot:AppSecret.
var envSecret = Environment.GetEnvironmentVariable("BOT_CLIENT_SECRET");
if (!string.IsNullOrWhiteSpace(envSecret))
{
    builder.Services.PostConfigure<BotOptions>(o => o.AppSecret = envSecret);
}

// The avatar's brand name is shared with the Python backend through the single
// AVATAR_DISPLAY_NAME env var — never hardcode the custom name in the bot.
var envAvatarName = Environment.GetEnvironmentVariable("AVATAR_DISPLAY_NAME");
if (!string.IsNullOrWhiteSpace(envAvatarName))
{
    builder.Services.PostConfigure<BotOptions>(o => o.AvatarDisplayName = envAvatarName.Trim());
}

builder.Services.AddControllers();
builder.Services.AddSingleton<MeetingBotService>();

// ── Kestrel ──
// Two public surfaces (see docs/channels/d-design-media-bot.md):
//   - HTTPS signaling/webhook + operator API on SignalingPort
//   - the media platform binds its own TLS media port (MediaPort) internally
// Both require the publicly-trusted cert (CertificateThumbprint) on a Windows
// host. We let the media SDK own the media port; Kestrel owns signaling.
var botSection = builder.Configuration.GetSection(BotOptions.SectionName);
var signalingPort = botSection.GetValue<int?>("SignalingPort") ?? 9441;
var certThumbprint = botSection.GetValue<string>("CertificateThumbprint");

builder.WebHost.ConfigureKestrel(kestrel =>
{
    // Graph requires HTTPS for the calling webhook. Bind the publicly-trusted
    // cert (LocalMachine\My) matching ServiceFqdn by thumbprint. The media
    // platform binds its own TLS media port internally via
    // MediaPlatformInstanceSettings.CertificateThumbprint (see MeetingBot).
    kestrel.ListenAnyIP(signalingPort, lo =>
    {
        if (!string.IsNullOrWhiteSpace(certThumbprint))
        {
            var cert = FindCertByThumbprint(certThumbprint!);
            if (cert is not null)
            {
                lo.UseHttps(cert);
                return;
            }
        }
        // No cert configured/found: fall back to HTTP so the host still starts
        // for local wiring tests. Graph callbacks will NOT work over HTTP.
    });
});

static System.Security.Cryptography.X509Certificates.X509Certificate2? FindCertByThumbprint(string thumbprint)
{
    var clean = thumbprint.Replace(" ", string.Empty).ToUpperInvariant();
    foreach (var location in new[]
             {
                 System.Security.Cryptography.X509Certificates.StoreLocation.LocalMachine,
                 System.Security.Cryptography.X509Certificates.StoreLocation.CurrentUser,
             })
    {
        using var store = new System.Security.Cryptography.X509Certificates.X509Store(
            System.Security.Cryptography.X509Certificates.StoreName.My, location);
        store.Open(System.Security.Cryptography.X509Certificates.OpenFlags.ReadOnly);
        foreach (var c in store.Certificates)
        {
            if (string.Equals(c.Thumbprint, clean, StringComparison.OrdinalIgnoreCase))
                return c;
        }
    }
    return null;
}

var app = builder.Build();

// Eagerly construct the bot so the calling client + media platform initialize
// at startup (and fail fast on bad config).
_ = app.Services.GetRequiredService<MeetingBotService>();

app.MapControllers();

app.Logger.LogInformation(
    "Avatar-Forge meeting bot started. Signaling on :{Port}/api/calling, operator API at /api/join.",
    signalingPort);

app.Run();
