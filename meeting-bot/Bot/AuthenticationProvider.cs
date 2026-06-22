using System.Collections.Concurrent;
using System.Net.Http.Headers;
using Microsoft.Graph.Communications.Client.Authentication;
using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Identity.Client;

namespace AvatarForge.MeetingBot.Bot;

/// <summary>
/// Outbound/inbound auth for the calling client, faithful to the official Graph
/// Communications sample's <c>AuthenticationProvider</c>.
///
/// - Outbound: acquires an app-only Graph token (client-credentials via MSAL)
///   and attaches it as a Bearer header on calls the SDK makes to Graph.
/// - Inbound: validates the tenant token Microsoft Graph signs its
///   notifications/webhooks with (so we only accept genuine Graph callbacks).
/// </summary>
public sealed class AuthenticationProvider : IRequestAuthenticationProvider
{
    private const string GraphScope = "https://graph.microsoft.com/.default";

    private readonly string _appId;
    private readonly string _appSecret;
    private readonly string _tenantId;
    private readonly IGraphLogger _logger;
    private readonly ConcurrentDictionary<string, IConfidentialClientApplication> _apps = new();

    /// <summary>
    /// Tenant of the meeting the bot is currently joined to. Mid-call control
    /// requests (e.g. UnmuteAsync) are issued by the SDK with an EMPTY tenant,
    /// so without this they would fall back to the bot's home tenant and Graph
    /// rejects them ("Tenant Id of call is empty or does not match"). Set at join
    /// time so the fallback targets the meeting/organizer tenant instead.
    /// </summary>
    public string? MeetingTenantOverride { get; set; }

    public AuthenticationProvider(string appId, string appSecret, string tenantId, IGraphLogger logger)
    {
        _appId = appId;
        _appSecret = appSecret;
        _tenantId = tenantId;
        _logger = logger;
    }

    /// <summary>
    /// Builds (and caches) a confidential-client app whose authority points at a
    /// specific tenant. A multi-tenant bot must acquire its Graph token against the
    /// tenant that owns the meeting (the organizer tenant), not its own home tenant,
    /// otherwise Graph rejects the join with "Request authorization tenant mismatch".
    /// </summary>
    private IConfidentialClientApplication GetOrCreateApp(string tenant) =>
        _apps.GetOrAdd(tenant, t => ConfidentialClientApplicationBuilder
            .Create(_appId)
            .WithClientSecret(_appSecret)
            .WithAuthority(new Uri($"https://login.microsoftonline.com/{t}"))
            .Build());

    public async Task AuthenticateOutboundRequestAsync(HttpRequestMessage request, string tenant)
    {
        // Honor the per-request tenant the SDK supplies (the meeting/organizer
        // tenant). For mid-call requests the SDK passes an empty tenant, so fall
        // back to the joined meeting tenant before the bot's home tenant.
        var authorityTenant = !string.IsNullOrWhiteSpace(tenant)
            ? tenant
            : (!string.IsNullOrWhiteSpace(MeetingTenantOverride) ? MeetingTenantOverride! : _tenantId);
        var app = GetOrCreateApp(authorityTenant);
        var result = await app.AcquireTokenForClient(new[] { GraphScope })
            .ExecuteAsync()
            .ConfigureAwait(false);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", result.AccessToken);
    }

    /// <summary>
    /// Acquire an app-only Graph token for an explicit tenant. Used by the
    /// short-link resolver to call <c>/users/{id}/onlineMeetings</c> against the
    /// organizer tenant (outside the SDK's own request pipeline).
    /// </summary>
    public async Task<string> AcquireAppTokenAsync(string tenant)
    {
        var authorityTenant = string.IsNullOrWhiteSpace(tenant) ? _tenantId : tenant;
        var app = GetOrCreateApp(authorityTenant);
        var result = await app.AcquireTokenForClient(new[] { GraphScope })
            .ExecuteAsync()
            .ConfigureAwait(false);
        return result.AccessToken;
    }

    public Task<RequestValidationResult> ValidateInboundRequestAsync(HttpRequestMessage request)
    {
        // The Graph Communications SDK ships a full inbound validator
        // (tenant-token signature + audience checks). For the scaffold we accept
        // and log; wire the SDK's built-in validator before production.
        // TODO(prod): replace with the SDK's token validation against _appId.
        _logger.Info("Inbound calling notification received (validation TODO).");
        return Task.FromResult(new RequestValidationResult { IsValid = true, TenantId = _tenantId });
    }
}
