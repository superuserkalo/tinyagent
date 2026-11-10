using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace TinyAgent.Checks;

// Native SocketsHttpHandler serialization and parsing over memory, without TCP.
internal sealed class HttpFixture : IDisposable
{
    private readonly Queue<FixtureStream> pending = new();
    private readonly List<FixtureStream> streams = new();
    internal HttpClient Client { get; }
    internal int Connections { get; private set; }

    internal HttpFixture()
    {
        Client = new HttpClient(new SocketsHttpHandler
        {
            UseProxy = false,
            UseCookies = false,
            AllowAutoRedirect = false,
            MaxResponseDrainSize = 0,
            ConnectCallback = (_, token) =>
            {
                token.ThrowIfCancellationRequested();
                lock (pending)
                {
                    Connections++;
                    return ValueTask.FromResult<Stream>(pending.Dequeue());
                }
            }
        }) { Timeout = Timeout.InfiniteTimeSpan };
    }

    internal FixtureStream Enqueue(byte[] bytes, bool eof = true, int fragment = 1)
    {
        var stream = new FixtureStream(bytes, eof, fragment);
        streams.Add(stream);
        lock (pending)
            pending.Enqueue(stream);
        return stream;
    }

    internal static string Sse(string name) =>
        File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "fixtures", name + ".sse"), Encoding.UTF8);

    internal static byte[] Wire(string body, int status = 200, bool ending = true, bool chunked = true)
    {
        byte[] data = Encoding.UTF8.GetBytes(body);
        string headers = $"HTTP/1.1 {status} Fixture\r\nContent-Type: text/event-stream\r\nConnection: close\r\n";
        if (!chunked)
            return [.. Encoding.ASCII.GetBytes(headers + $"Content-Length: {data.Length}\r\n\r\n"), .. data];
        return [
            .. Encoding.ASCII.GetBytes(headers + $"Transfer-Encoding: chunked\r\n\r\n{data.Length:x}\r\n"),
            .. data, .. (ending ? "\r\n0\r\n\r\n"u8.ToArray() : Array.Empty<byte>())
        ];
    }

    internal static string Events(params JsonObject[] values) =>
        string.Concat(values.Select(value => "data: " + value.ToJsonString() + "\n\n"));

    public void Dispose()
    {
        Client.Dispose();
        foreach (var stream in streams)
            stream.Dispose();
    }
}

internal sealed class FixtureStream(byte[] bytes, bool eof, int fragment) : Stream
{
    private readonly MemoryStream written = new();
    private int offset;
    private readonly object gate = new();
    internal TaskCompletionSource Waiting { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    internal TaskCompletionSource Closed { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

    internal (string Line, IReadOnlyDictionary<string, string> Headers, JsonElement Body) Request()
    {
        string text;
        lock (gate)
            text = Encoding.UTF8.GetString(written.ToArray());
        int end = text.IndexOf("\r\n\r\n", StringComparison.Ordinal);
        string[] lines = text[..end].Split("\r\n", StringSplitOptions.None);
        var headers = lines.Skip(1).Select(line => line.Split(": ", 2))
            .ToDictionary(parts => parts[0].ToLowerInvariant(), parts => parts[1]);
        using var document = JsonDocument.Parse(text[(end + 4)..]);
        return (lines[0], headers, document.RootElement.Clone());
    }

    public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            ObjectDisposedException.ThrowIf(Closed.Task.IsCompleted, this);
            int size = Math.Min(Math.Min(buffer.Length, fragment), bytes.Length - offset);
            if (size > 0)
            {
                bytes.AsMemory(offset, size).CopyTo(buffer);
                offset += size;
                return size;
            }
            if (eof || buffer.Length == 0)
                return 0;
            Waiting.TrySetResult();
        }
        await Closed.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
        throw new ObjectDisposedException(nameof(FixtureStream));
    }

    public override ValueTask WriteAsync(ReadOnlyMemory<byte> buffer, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            ObjectDisposedException.ThrowIf(Closed.Task.IsCompleted, this);
            written.Write(buffer.Span);
        }
        return ValueTask.CompletedTask;
    }

    protected override void Dispose(bool disposing)
    {
        Closed.TrySetResult();
        base.Dispose(disposing);
    }

    public override int Read(byte[] buffer, int offset, int count) =>
        ReadAsync(buffer.AsMemory(offset, count)).AsTask().GetAwaiter().GetResult();
    public override void Write(byte[] buffer, int offset, int count) =>
        WriteAsync(buffer.AsMemory(offset, count)).GetAwaiter().GetResult();
    public override Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken) =>
        ReadAsync(buffer.AsMemory(offset, count), cancellationToken).AsTask();
    public override Task WriteAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken) =>
        WriteAsync(buffer.AsMemory(offset, count), cancellationToken).AsTask();
    public override bool CanRead => true;
    public override bool CanWrite => true;
    public override bool CanSeek => false;
    public override long Length => throw new NotSupportedException();
    public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
    public override void Flush() { }
    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
}
