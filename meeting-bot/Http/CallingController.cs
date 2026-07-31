using AvatarForge.MeetingBot.Bot;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Graph.Communications.Client;

namespace AvatarForge.MeetingBot.Http;

/// <summary>
/// The Bot Framework calling webhook. Microsoft Graph POSTs call notifications
/// (state changes, roster updates, media negotiation) here. We hand the raw
/// HTTP request to the SDK's request processor, which routes it into the
/// ICommunicationsClient event stream that <see cref="MeetingBot"/> subscribes
/// to.
///
/// This route MUST match SetNotificationUrl in MeetingBot:
///   https://{ServiceFqdn}:{SignalingPort}/api/calling
/// </summary>
[ApiController]
[Route("api/calling")]
public sealed class CallingController : ControllerBase
{
    private readonly MeetingBotService _bot;

    public CallingController(MeetingBotService bot) => _bot = bot;

    [HttpPost]
    public async Task<IActionResult> OnNotification()
    {
        // Hand the inbound request to the SDK. ProcessNotificationAsync reads
        // the body/headers, validates, and raises the typed call events.
        var response = await _bot.Client
            .ProcessNotificationAsync(await Request.ToHttpRequestMessageAsync().ConfigureAwait(false))
            .ConfigureAwait(false);

        // Relay the SDK's status/headers back to Graph.
        return new HttpResponseMessageResult(response);
    }
}
