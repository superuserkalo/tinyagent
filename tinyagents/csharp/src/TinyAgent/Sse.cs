using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json.Nodes;

namespace TinyAgent;

internal sealed class Sse : IDisposable
{
    private readonly MemoryStream pending = new();
    private readonly StringBuilder data = new();
    private int dataBytes;
    private bool hasData;

    internal static async IAsyncEnumerable<JsonObject> ReadAsync(
        Stream stream,
        [EnumeratorCancellation] CancellationToken cancellationToken)
    {
        using var parser = new Sse();
        byte[] buffer = new byte[8192];
        while (true)
        {
            int count = await stream.ReadAsync(buffer, cancellationToken).ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            if (count == 0)
                break;
            int offset = 0;
            while (offset < count)
            {
                int newline = Array.IndexOf(buffer, (byte)'\n', offset, count - offset);
                int length = (newline < 0 ? count : newline) - offset;
                if (length > Json.MaxSseBytes - parser.pending.Length)
                    throw new ProviderException("SSE line exceeds its byte limit.");
                parser.pending.Write(buffer, offset, length);
                offset += length;
                if (newline < 0)
                    break;
                offset++;
                var value = parser.ReadLine();
                if (value is not null)
                    yield return value;
                cancellationToken.ThrowIfCancellationRequested();
            }
        }
        if (parser.pending.Length != 0)
        {
            var value = parser.ReadLine();
            if (value is not null)
                yield return value;
        }
        if (parser.hasData)
            yield return parser.Dispatch();
        cancellationToken.ThrowIfCancellationRequested();
    }

    private JsonObject? ReadLine()
    {
        string line;
        try
        {
            line = Json.Utf8.GetString(pending.GetBuffer(), 0, checked((int)pending.Length));
        }
        catch (DecoderFallbackException error)
        {
            throw new ProviderException("Provider stream is not valid UTF-8.", error);
        }
        pending.SetLength(0);
        if (line.EndsWith('\r'))
            line = line[..^1];
        if (line.Length == 0)
            return hasData ? Dispatch() : null;
        if (line != "data" && !line.StartsWith("data:", StringComparison.Ordinal))
            return null;
        string text = line == "data" ? "" : line[5..];
        if (text.StartsWith(' '))
            text = text[1..];
        int added = Json.Utf8.GetByteCount(text) + (hasData ? 1 : 0);
        if (added > Json.MaxSseBytes - dataBytes)
            throw new ProviderException("SSE event exceeds its byte limit.");
        if (hasData)
            data.Append('\n');
        data.Append(text);
        dataBytes += added;
        hasData = true;
        return null;
    }

    private JsonObject Dispatch()
    {
        string source = data.ToString();
        data.Clear();
        dataBytes = 0;
        hasData = false;
        if (source == "[DONE]")
            throw new ProviderException("Provider stream ended without a complete turn.");
        return Json.ParseObject(source);
    }

    public void Dispose() => pending.Dispose();
}
