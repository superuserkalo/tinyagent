using System.Buffers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Http;
using Microsoft.Net.Http.Headers;

namespace TinyAgent;

internal sealed class HttpFailure(int status, string code, string message) : Exception(message)
{
    internal int Status { get; } = status;
    internal string Code { get; } = code;
}

internal static class HttpProtocol
{
    internal const int MaxBodyBytes = 1_048_576;
    internal const int MaxLineBytes = 1_048_576;

    internal static async Task<JsonObject?> ReadObjectAsync(HttpRequest request, bool optional)
    {
        if (request.ContentLength > MaxBodyBytes)
            throw new HttpFailure(413, "body_too_large", "Request body exceeds its byte limit");
        var body = new ArrayBufferWriter<byte>();
        while (true)
        {
            int length = Math.Min(8192, MaxBodyBytes + 1 - body.WrittenCount);
            int count = await request.Body.ReadAsync(body.GetMemory(length)[..length], request.HttpContext.RequestAborted).ConfigureAwait(false);
            if (count == 0) break;
            body.Advance(count);
            if (body.WrittenCount > MaxBodyBytes)
                throw new HttpFailure(413, "body_too_large", "Request body exceeds its byte limit");
        }
        if (optional && body.WrittenCount == 0) return null;
        if (!MediaTypeHeaderValue.TryParse(request.ContentType, out var media)
            || !media.MediaType.Equals("application/json", StringComparison.OrdinalIgnoreCase)
            || (media.Charset.HasValue && !media.Charset.Equals("utf-8", StringComparison.OrdinalIgnoreCase)))
            throw new HttpFailure(415, "unsupported_media_type", "Expected application/json with UTF-8 encoding");
        try
        {
            string source = Json.Utf8.GetString(body.WrittenSpan);
            var value = Json.ParseObject(source);
            ValidateStrings(value);
            return value;
        }
        catch (DecoderFallbackException)
        {
            throw new HttpFailure(400, "invalid_utf8", "Request body is not valid UTF-8");
        }
        catch (Exception error) when (error is JsonException or ProviderException or ArgumentException or InvalidOperationException)
        {
            throw new HttpFailure(400, "invalid_request", "Request body must be a valid JSON object");
        }
    }

    private static void ValidateStrings(JsonNode? value)
    {
        if (value is JsonObject obj)
            foreach (var (key, child) in obj)
            {
                Json.Text(key, "JSON key");
                ValidateStrings(child);
            }
        else if (value is JsonArray array)
            foreach (var child in array) ValidateStrings(child);
        else if (value is JsonValue scalar && scalar.TryGetValue<string>(out var text))
            Json.Text(text, "JSON string");
    }

    internal static Task ErrorAsync(HttpContext context, int status, string code, string message) =>
        JsonAsync(context, status, new JsonObject
        {
            ["error"] = new JsonObject { ["code"] = code, ["message"] = message, ["retryable"] = status == 409 || status >= 500 }
        });

    internal static async Task JsonAsync(HttpContext context, int status, JsonObject value)
    {
        byte[] body = [.. JsonSerializer.SerializeToUtf8Bytes(value), (byte)'\n'];
        context.Response.StatusCode = status;
        context.Response.ContentType = "application/json; charset=utf-8";
        context.Response.Headers.CacheControl = "no-store";
        context.Response.ContentLength = body.Length;
        await context.Response.Body.WriteAsync(body, context.RequestAborted).ConfigureAwait(false);
        await context.Response.CompleteAsync().ConfigureAwait(false);
    }
}
