using Microsoft.AspNetCore.Mvc;

namespace AvatarForge.MeetingBot.Http;

/// <summary>
/// Small adapters between ASP.NET Core's <see cref="HttpRequest"/>/response and
/// the <see cref="HttpRequestMessage"/>/<see cref="HttpResponseMessage"/> types
/// the Graph Communications SDK's request processor speaks. The official sample
/// ships equivalents; these are minimal, faithful versions.
/// </summary>
public static class HttpInterop
{
    public static async Task<HttpRequestMessage> ToHttpRequestMessageAsync(this HttpRequest request)
    {
        var message = new HttpRequestMessage(new HttpMethod(request.Method), request.GetEncodedUrl());

        if (request.ContentLength > 0)
        {
            // Read asynchronously. Blocking on the body here occupies a thread-pool
            // thread for the duration of a network read, on the one endpoint Teams
            // calls for every signalling notification of every call.
            using var reader = new StreamReader(request.Body);
            var body = await reader.ReadToEndAsync().ConfigureAwait(false);
            message.Content = new StringContent(body);
        }

        foreach (var header in request.Headers)
        {
            if (!message.Headers.TryAddWithoutValidation(header.Key, header.Value.ToArray()))
                message.Content?.Headers.TryAddWithoutValidation(header.Key, header.Value.ToArray());
        }
        return message;
    }

    private static string GetEncodedUrl(this HttpRequest request) =>
        $"{request.Scheme}://{request.Host}{request.PathBase}{request.Path}{request.QueryString}";
}

/// <summary>
/// Relays an <see cref="HttpResponseMessage"/> (from the SDK) back through ASP.NET.
/// </summary>
public sealed class HttpResponseMessageResult : IActionResult
{
    private readonly HttpResponseMessage _message;
    public HttpResponseMessageResult(HttpResponseMessage message) => _message = message;

    public async Task ExecuteResultAsync(ActionContext context)
    {
        var response = context.HttpContext.Response;
        response.StatusCode = (int)_message.StatusCode;
        if (_message.Content is not null)
        {
            var body = await _message.Content.ReadAsByteArrayAsync().ConfigureAwait(false);
            await response.Body.WriteAsync(body).ConfigureAwait(false);
        }
    }
}
