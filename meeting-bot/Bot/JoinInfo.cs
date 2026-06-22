using System.Text.RegularExpressions;
using Microsoft.Graph.Models;

namespace AvatarForge.MeetingBot.Bot;

/// <summary>
/// Parses a Teams "meetup-join" URL into the <see cref="ChatInfo"/> +
/// <see cref="MeetingInfo"/> the Graph Communications SDK needs to join a meeting
/// with application-hosted media.
///
/// The Graph Communications SDK does NOT ship this helper — the official
/// local-media samples (HueBot / PsiBot) each carry their own copy. This is a
/// faithful, dependency-free port.
///
/// A join URL looks like:
///   https://teams.microsoft.com/l/meetup-join/19%3ameeting_NNN%40thread.v2/0
///       ?context=%7b%22Tid%22%3a%22&lt;tenant&gt;%22%2c%22Oid%22%3a%22&lt;organizer&gt;%22%7d
/// </summary>
public static class JoinInfo
{
    public static (ChatInfo chatInfo, MeetingInfo meetingInfo, string? tenantId) ParseJoinURL(string joinUrl)
    {
        if (string.IsNullOrWhiteSpace(joinUrl))
            throw new ArgumentException("Join URL is required.", nameof(joinUrl));

        var decoded = Uri.UnescapeDataString(joinUrl);

        // Thread id: 19:meeting_<base64>@thread.v2  (also tolerate @thread.skype).
        var threadMatch = Regex.Match(decoded, @"(19:meeting_[^/]+?@thread\.(?:v2|skype))");
        if (!threadMatch.Success)
            throw new ArgumentException("Join URL does not contain a recognizable meeting thread id.", nameof(joinUrl));
        var threadId = threadMatch.Groups[1].Value;

        // The "context" query param carries a JSON blob with Tid (tenant) + Oid
        // (organizer object id) and sometimes MessageId.
        string? organizerId = null, tenantId = null, messageId = "0";
        var ctxMatch = Regex.Match(joinUrl, @"context=([^&]+)");
        if (ctxMatch.Success)
        {
            var ctxJson = Uri.UnescapeDataString(ctxMatch.Groups[1].Value);
            organizerId = MatchJson(ctxJson, "Oid") ?? organizerId;
            tenantId = MatchJson(ctxJson, "Tid") ?? tenantId;
            messageId = MatchJson(ctxJson, "MessageId") ?? messageId;
        }

        var chatInfo = new ChatInfo
        {
            ThreadId = threadId,
            MessageId = messageId,
            ReplyChainMessageId = messageId,
        };

        var meetingInfo = new OrganizerMeetingInfo
        {
            Organizer = new IdentitySet
            {
                User = new Identity { Id = organizerId },
            },
        };
        // The SDK reads the organizer tenant from AdditionalData["tenantId"].
        if (!string.IsNullOrEmpty(tenantId))
        {
            meetingInfo.Organizer.User!.AdditionalData = new Dictionary<string, object>
            {
                ["tenantId"] = tenantId,
            };
        }

        return (chatInfo, meetingInfo, tenantId);
    }

    private static string? MatchJson(string json, string key)
    {
        var m = Regex.Match(json, "\"" + key + "\"\\s*:\\s*\"([^\"]+)\"");
        return m.Success ? m.Groups[1].Value : null;
    }
}
