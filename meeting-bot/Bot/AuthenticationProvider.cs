using System.Collections.Concurrent;
using System.Net.Http.Headers;
using Microsoft.Graph.Communications.Client.Authentication;
using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Identity.Client;
using Microsoft.IdentityModel.Protocols;
using Microsoft.IdentityModel.Protocols.OpenIdConnect;
using Microsoft.IdentityModel.Tokens;
using Microsoft.IdentityModel.JsonWebTokens;

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

    /// <summary>
    /// OpenID metadata for the Microsoft calling service that signs the inbound
    /// call notifications. Static so the signing keys are fetched once per process
    /// and refreshed on the manager's own schedule rather than per request.
    /// </summary>
    private const string CallingOpenIdConfigUrl =
        "https://api.aps.skype.com/v1/.well-known/OpenIdConfiguration";

    private static readonly ConfigurationManager<OpenIdConnectConfiguration> _openIdConfig =
        new(CallingOpenIdConfigUrl, new OpenIdConnectConfigurationRetriever());

    private static readonly JsonWebTokenHandler _tokenHandler = new();

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

    /// <summary>
    /// When false, inbound notifications are accepted without token validation.
    /// Escape hatch only — see <c>BotOptions.ValidateInboundRequests</c>.
    /// </summary>
    public bool ValidateInboundRequests { get; init; } = true;

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

    /// <summary>
    /// Validates the bearer token Microsoft's calling service signs its inbound
    /// notifications with, so the public <c>/api/calling</c> endpoint only acts on
    /// genuine callbacks.
    ///
    /// Without this any host that can reach the endpoint could inject fabricated
    /// call notifications, which matters because the media host is deliberately
    /// exposed to the internet (Teams must be able to reach it).
    ///
    /// The signing keys are published as OpenID metadata by the calling service;
    /// they are fetched once and refreshed on the configuration manager's own
    /// schedule. A token is accepted only when the signature validates against
    /// those keys AND the audience is this bot's app id.
    /// </summary>
    public async Task<RequestValidationResult> ValidateInboundRequestAsync(HttpRequestMessage request)
    {
        if (!ValidateInboundRequests)
        {
            _logger.Warn("Inbound calling notification accepted WITHOUT validation " +
                         "(Bot:ValidateInboundRequests=false).");
            return new RequestValidationResult { IsValid = true, TenantId = MeetingTenantOverride ?? _tenantId };
        }

        var token = request.Headers.Authorization?.Parameter;
        if (string.IsNullOrWhiteSpace(token) ||
            !string.Equals(request.Headers.Authorization?.Scheme, "Bearer", StringComparison.OrdinalIgnoreCase))
        {
            _logger.Warn("Rejected inbound calling notification: no bearer token.");
            return new RequestValidationResult { IsValid = false };
        }

        try
        {
            var config = await _openIdConfig
                .GetConfigurationAsync(CancellationToken.None)
                .ConfigureAwait(false);

            var parameters = new TokenValidationParameters
            {
                ValidAudiences = new[] { _appId },
                ValidIssuers = config.Issuer is { Length: > 0 }
                    ? new[] { config.Issuer }
                    : null,
                // The calling service's metadata does not always advertise an
                // issuer; the signing-key check plus the audience check are what
                // actually bind the token to us.
                ValidateIssuer = config.Issuer is { Length: > 0 },
                IssuerSigningKeys = config.SigningKeys,
                ValidateIssuerSigningKey = true,
                ValidateAudience = true,
                ValidateLifetime = true,
                ClockSkew = TimeSpan.FromMinutes(5),
            };

            var result = await _tokenHandler
                .ValidateTokenAsync(token, parameters)
                .ConfigureAwait(false);

            if (!result.IsValid)
            {
                _logger.Warn($"Rejected inbound calling notification: {result.Exception?.Message}");
                return new RequestValidationResult { IsValid = false };
            }

            // The tenant the notification is about — mid-call requests must be
            // issued against it, not the bot's home tenant.
            result.Claims.TryGetValue("tid", out var tid);
            var tenantId = tid?.ToString();
            if (string.IsNullOrWhiteSpace(tenantId))
                tenantId = MeetingTenantOverride ?? _tenantId;

            return new RequestValidationResult { IsValid = true, TenantId = tenantId };
        }
        catch (Exception ex)
        {
            // Never let a validation fault crash the notification pipeline; a
            // rejected notification is retried by the service.
            _logger.Error(ex, "Inbound calling notification validation failed.");
            return new RequestValidationResult { IsValid = false };
        }
    }
}
