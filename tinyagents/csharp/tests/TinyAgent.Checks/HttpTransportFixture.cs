using System.IO.Pipelines;
using System.Net;
using System.Threading.Channels;
using Microsoft.AspNetCore.Connections;

namespace TinyAgent.Checks;

// Real Kestrel and SocketsHttpHandler use bounded pipes; no socket listener is bound.
internal sealed class HttpTransportFixture : IConnectionListenerFactory, IConnectionListener
{
    internal bool FailFinalChunk { get; set; }
    private readonly Channel<ConnectionContext> connections = Channel.CreateUnbounded<ConnectionContext>();
    public EndPoint EndPoint { get; } = new IPEndPoint(IPAddress.Loopback, 12345);

    public ValueTask<IConnectionListener> BindAsync(EndPoint endpoint, CancellationToken cancellationToken = default) =>
        ValueTask.FromResult<IConnectionListener>(this);

    public async ValueTask<ConnectionContext?> AcceptAsync(CancellationToken cancellationToken = default)
    {
        try { return await connections.Reader.ReadAsync(cancellationToken).ConfigureAwait(false); }
        catch (ChannelClosedException) { return null; }
    }

    public ValueTask UnbindAsync(CancellationToken cancellationToken = default)
    {
        connections.Writer.TryComplete();
        return ValueTask.CompletedTask;
    }

    public ValueTask DisposeAsync() => UnbindAsync();

    internal HttpClient Client()
    {
        var handler = new SocketsHttpHandler
        {
            UseProxy = false,
            UseCookies = false,
            ConnectCallback = (_, _) => ValueTask.FromResult(Connect()),
            MaxResponseDrainSize = 0
        };
        return new HttpClient(handler) { BaseAddress = new Uri("http://127.0.0.1:12345"), Timeout = TimeSpan.FromSeconds(5) };
    }

    internal Stream Connect()
    {
        var input = new Pipe(new PipeOptions(pauseWriterThreshold: 65_536, resumeWriterThreshold: 32_768));
        var output = new Pipe(new PipeOptions(pauseWriterThreshold: 1, resumeWriterThreshold: 1));
        var connection = new FixtureConnection(
            new Duplex(input.Reader, output.Writer), new Duplex(output.Reader, input.Writer));
        connection.Transport = new Duplex(input.Reader, new CheckedWriter(output.Writer, this,
            () => connection.Abort(new ConnectionAbortedException("Fixture final chunk failed."))));
        if (!connections.Writer.TryWrite(connection)) throw new IOException("Fixture listener is closed.");
        return new ClientStream(connection, output.Reader.AsStream(), input.Writer.AsStream());
    }

    private sealed class Duplex(PipeReader input, PipeWriter output) : IDuplexPipe
    {
        public PipeReader Input { get; } = input;
        public PipeWriter Output { get; } = output;
    }

    // DefaultConnectionContext queues cancellation without joining it on disposal.
    // The fixture owns and joins its cancellation callbacks like a native transport.
    private sealed class FixtureConnection : DefaultConnectionContext
    {
        private readonly CancellationTokenSource lifetime = new();
        private readonly object gate = new();
        private bool disposed;
        private bool aborted;
        internal FixtureConnection(IDuplexPipe transport, IDuplexPipe application)
            : base(Guid.NewGuid().ToString(), transport, application) => ConnectionClosed = lifetime.Token;
        public override void Abort(ConnectionAbortedException reason)
        {
            lock (gate)
                if (!disposed && !aborted)
                {
                    aborted = true;
                    lifetime.Cancel();
                    Transport.Input.CancelPendingRead();
                    Transport.Output.CancelPendingFlush();
                    Transport.Output.Complete(reason);
                }
        }
        public override async ValueTask DisposeAsync()
        {
            lock (gate)
            {
                if (!disposed)
                {
                    disposed = true;
                    lifetime.Cancel();
                    lifetime.Dispose();
                }
            }
            await Transport.Input.CompleteAsync().ConfigureAwait(false);
            await Transport.Output.CompleteAsync().ConfigureAwait(false);
            await base.DisposeAsync().ConfigureAwait(false);
        }
    }

    private sealed class CheckedWriter(PipeWriter inner, HttpTransportFixture fixture, Action abort) : PipeWriter
    {
        private Memory<byte> memory;
        private bool reject;
        public override Memory<byte> GetMemory(int sizeHint = 0) => memory = inner.GetMemory(sizeHint);
        public override Span<byte> GetSpan(int sizeHint = 0) => GetMemory(sizeHint).Span;
        public override void Advance(int bytes)
        {
            if (fixture.FailFinalChunk && memory.Span[..bytes].IndexOf("0\r\n\r\n"u8) >= 0)
            {
                reject = true;
                return;
            }
            inner.Advance(bytes);
        }
        public override ValueTask<System.IO.Pipelines.FlushResult> FlushAsync(CancellationToken cancellationToken = default)
        {
            if (!reject) return inner.FlushAsync(cancellationToken);
            abort();
            return ValueTask.FromException<System.IO.Pipelines.FlushResult>(new IOException("Fixture final chunk failed."));
        }
        public override void CancelPendingFlush() => inner.CancelPendingFlush();
        public override void Complete(Exception? exception = null) => inner.Complete(exception);
    }

    private sealed class ClientStream(DefaultConnectionContext connection, Stream input, Stream output) : Stream
    {
        private bool disposed;
        public override bool CanRead => true;
        public override bool CanWrite => true;
        public override bool CanSeek => false;
        public override long Length => throw new NotSupportedException();
        public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
        public override int Read(byte[] buffer, int offset, int count) => input.Read(buffer, offset, count);
        public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default) => input.ReadAsync(buffer, cancellationToken);
        public override Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken) => input.ReadAsync(buffer, offset, count, cancellationToken);
        public override void Write(byte[] buffer, int offset, int count) => output.Write(buffer, offset, count);
        public override ValueTask WriteAsync(ReadOnlyMemory<byte> buffer, CancellationToken cancellationToken = default) => output.WriteAsync(buffer, cancellationToken);
        public override Task WriteAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken) => output.WriteAsync(buffer, offset, count, cancellationToken);
        public override void Flush() => output.Flush();
        public override Task FlushAsync(CancellationToken cancellationToken) => output.FlushAsync(cancellationToken);
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        protected override void Dispose(bool disposing)
        {
            if (!disposed)
            {
                disposed = true;
                // Socket transport reports connection loss independently of request-body EOF.
                connection.Abort(new ConnectionAbortedException("Fixture peer disconnected."));
                input.Dispose();
                output.Dispose();
            }
            base.Dispose(disposing);
        }
    }
}
