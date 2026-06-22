using AvatarForge.MeetingBot.Bot;
using Microsoft.AspNetCore.Mvc;

namespace AvatarForge.MeetingBot.Http;

/// <summary>
/// Operator API to bring Nuru into / out of a meeting. This is what the Python
/// backend (or a human/launcher) calls to make the bot join — replacing the
/// browser-joiner interim for the "hear everyone" path.
///
/// POST /api/join   { "joinUrl": "https://teams.microsoft.com/l/meetup-join/...", "displayName": "Nuru" }
/// POST /api/leave  { "callId": "..." }
/// </summary>
[ApiController]
[Route("api")]
public sealed class JoinController : ControllerBase
{
    private readonly MeetingBotService _bot;
    private readonly ILogger<JoinController> _logger;

    public JoinController(MeetingBotService bot, ILogger<JoinController> logger)
    {
        _bot = bot;
        _logger = logger;
    }

    public sealed record JoinRequest(string JoinUrl, string? DisplayName);
    public sealed record LeaveRequest(string CallId);

    [HttpPost("join")]
    public async Task<IActionResult> Join([FromBody] JoinRequest req)
    {
        if (string.IsNullOrWhiteSpace(req.JoinUrl))
            return BadRequest(new { error = "joinUrl is required" });
        try
        {
            // Display name defaults to the configured avatar name (AVATAR_DISPLAY_NAME)
            // inside JoinMeetingAsync — never hardcode the custom name here.
            var callId = await _bot.JoinMeetingAsync(req.JoinUrl, req.DisplayName);
            return Ok(new { callId });
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Join failed.");
            return StatusCode(500, new { error = ex.Message });
        }
    }

    [HttpPost("leave")]
    public async Task<IActionResult> Leave([FromBody] LeaveRequest req)
    {
        if (string.IsNullOrWhiteSpace(req.CallId))
            return BadRequest(new { error = "callId is required" });
        await _bot.LeaveAsync(req.CallId);
        return Ok(new { left = req.CallId });
    }

    [HttpGet("health")]
    public IActionResult Health() => Ok(new { status = "ok" });
}
