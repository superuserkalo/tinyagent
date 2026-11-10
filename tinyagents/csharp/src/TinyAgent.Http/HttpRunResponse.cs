using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Connections.Features;
using Microsoft.AspNetCore.Http;

namespace TinyAgent;

// Each request keeps its own run ID, cancellation registrations, and bounded delivery.
internal sealed class HttpRunResponse(HttpContext context, Session session) : IAsyncDisposable
{
    private readonly CancellationTokenSource drain = new();
    private CancellationTokenRegistration disconnect;
    private CancellationTokenRegistration connectionLost;
    private CancellationTokenRegistration cancellation;
    private CancellationTokenRegistration stalled;
    private string? runId;
    private int drainRequested;
    private readonly CancellationToken connectionClosed = context.Features.Get<IConnectionLifetimeFeature>()?.ConnectionClosed
        ?? throw new InvalidOperationException("HTTP transport did not expose its connection lifetime.");
    internal Exception? Failure { get; private set; }
    internal bool Completed { get; private set; }

    internal async ValueTask StartAsync(string id, CancellationToken token)
    {
        runId = id;
        disconnect = context.RequestAborted.Register(() => session.Cancel(id));
        connectionLost = connectionClosed.Register(() => session.Cancel(id));
        cancellation = token.Register(StartDrain);
        stalled = drain.Token.Register(context.Abort);
        try
        {
            context.RequestAborted.ThrowIfCancellationRequested();
            connectionClosed.ThrowIfCancellationRequested();
            token.ThrowIfCancellationRequested();
            context.Response.StatusCode = 200;
            context.Response.ContentType = "application/jsonl; charset=utf-8";
            context.Response.Headers.CacheControl = "no-store";
            await context.Response.StartAsync(drain.Token).ConfigureAwait(false);
            context.RequestAborted.ThrowIfCancellationRequested();
            connectionClosed.ThrowIfCancellationRequested();
        }
        catch (Exception error)
        {
            Failure = error;
            throw;
        }
    }

    internal async ValueTask EmitAsync(AgentEvent value, CancellationToken token)
    {
        using var providerCancelled = token.Register(StartDrain);
        try
        {
            context.RequestAborted.ThrowIfCancellationRequested();
            connectionClosed.ThrowIfCancellationRequested();
            byte[] bytes = JsonSerializer.SerializeToUtf8Bytes(value.ToJson());
            bool oversized = bytes.Length > HttpProtocol.MaxLineBytes;
            bool terminal = value is AgentEvent.Completed or AgentEvent.Aborted or AgentEvent.Failed;
            if (oversized)
            {
                session.Cancel(value.RunId);
                bytes = JsonSerializer.SerializeToUtf8Bytes(new JsonObject
                {
                    ["type"] = "agent_end", ["runId"] = value.RunId, ["sequence"] = value.Sequence,
                    ["status"] = "failed", ["error"] = new JsonObject
                    {
                        ["code"] = "output_too_large", ["message"] = "HTTP output exceeds its byte limit", ["retryable"] = false
                    }
                });
                terminal = true;
            }
            byte[] frame = [.. bytes, (byte)'\n'];
            var flushed = await context.Response.BodyWriter.WriteAsync(frame, drain.Token).ConfigureAwait(false);
            if (flushed.IsCanceled || flushed.IsCompleted)
                throw new IOException("HTTP response ended before event delivery completed.");
            if (terminal) await context.Response.CompleteAsync().ConfigureAwait(false);
            context.RequestAborted.ThrowIfCancellationRequested();
            // Kestrel suppresses RequestAborted while sending the final HTTP chunk.
            connectionClosed.ThrowIfCancellationRequested();
            drain.Token.ThrowIfCancellationRequested();
            Completed = terminal;
            if (oversized) throw new IOException("HTTP output exceeds its byte limit.");
        }
        catch (Exception error)
        {
            Failure = error;
            if (runId is not null) session.Cancel(runId);
            throw;
        }
    }

    private void StartDrain()
    {
        if (Interlocked.Exchange(ref drainRequested, 1) == 0) drain.CancelAfter(TimeSpan.FromSeconds(1));
    }

    public async ValueTask DisposeAsync()
    {
        await disconnect.DisposeAsync().ConfigureAwait(false);
        await connectionLost.DisposeAsync().ConfigureAwait(false);
        await cancellation.DisposeAsync().ConfigureAwait(false);
        await stalled.DisposeAsync().ConfigureAwait(false);
        drain.Dispose();
    }
}
