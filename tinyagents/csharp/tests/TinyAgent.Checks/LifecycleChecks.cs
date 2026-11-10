using System.Net;
using System.Text;
using System.Text.Json.Nodes;

namespace TinyAgent.Checks;

internal static class LifecycleChecks
{
    internal static async Task TerminalWithoutEof()
    {
        foreach (bool anthropic in new[] { false, true })
        {
            using var fixture = new HttpFixture();
            var stream = fixture.Enqueue(
                HttpFixture.Wire(HttpFixture.Sse(anthropic ? "anthropic_text" : "openai_text"), ending: false), eof: false);
            using var provider = ProviderChecks.Provider(anthropic, fixture.Client, TimeSpan.FromSeconds(2));
            var result = await provider.StreamAsync(ProviderChecks.Request()).WaitAsync(TimeSpan.FromSeconds(1));
            Check.Equal("done\ncafé", result.Text);
            await stream.Closed.Task.WaitAsync(TimeSpan.FromSeconds(1));
        }
    }

    internal static async Task Cancellation()
    {
        foreach (bool anthropic in new[] { false, true })
        {
            foreach (byte[] prefix in Prefixes())
            {
                using var fixture = new HttpFixture();
                var stream = fixture.Enqueue(prefix, eof: false);
                using var provider = ProviderChecks.Provider(anthropic, fixture.Client);
                using var cancellation = new CancellationTokenSource();
                var pending = provider.StreamAsync(ProviderChecks.Request(), cancellationToken: cancellation.Token);
                try
                {
                    await stream.Waiting.Task.WaitAsync(TimeSpan.FromSeconds(2));
                    cancellation.Cancel();
                    await Check.ThrowsAsync<OperationCanceledException>(() => pending.WaitAsync(TimeSpan.FromSeconds(2)));
                    await stream.Closed.Task.WaitAsync(TimeSpan.FromSeconds(1));
                }
                finally
                {
                    cancellation.Cancel();
                    fixture.Dispose();
                    try { await pending; } catch (OperationCanceledException) { }
                }
            }
        }
    }

    internal static async Task Deadlines()
    {
        foreach (bool anthropic in new[] { false, true })
        {
            foreach (byte[] prefix in Prefixes())
            {
                using var fixture = new HttpFixture();
                var stream = fixture.Enqueue(prefix, eof: false);
                using var provider = ProviderChecks.Provider(anthropic, fixture.Client, TimeSpan.FromMilliseconds(80));
                var error = await Check.ThrowsAsync<ProviderException>(() =>
                    provider.StreamAsync(ProviderChecks.Request()).WaitAsync(TimeSpan.FromSeconds(2)));
                Check.True(error.Message.Contains("timed out", StringComparison.Ordinal), "The deadline was not reported.");
                await stream.Closed.Task.WaitAsync(TimeSpan.FromSeconds(1));
            }
        }
    }

    internal static async Task CallbackLifetime()
    {
        using var fixture = new HttpFixture();
        var stream = fixture.Enqueue(HttpFixture.Wire(HttpFixture.Sse("openai_text")));
        using var provider = ProviderChecks.Provider(false, fixture.Client, TimeSpan.FromMilliseconds(100));
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var expired = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var pending = provider.StreamAsync(ProviderChecks.Request(), async (_, token) =>
        {
            using var registration = token.Register(() => expired.TrySetResult());
            entered.TrySetResult();
            await release.Task.ConfigureAwait(false);
        });
        try
        {
            await entered.Task.WaitAsync(TimeSpan.FromSeconds(2));
            await expired.Task.WaitAsync(TimeSpan.FromSeconds(2));
            Check.True(!pending.IsCompleted, "The callback was abandoned during cancellation.");
        }
        finally
        {
            release.TrySetResult();
        }
        var error = await Check.ThrowsAsync<ProviderException>(() => pending);
        Check.True(error.Message.Contains("timed out", StringComparison.Ordinal), "The callback deadline was lost.");
        await stream.Closed.Task.WaitAsync(TimeSpan.FromSeconds(1));
    }

    internal static async Task CallbackFailure()
    {
        string body = HttpFixture.Events(new JsonObject { ["type"] = "response.output_text.delta", ["delta"] = "hello" });
        byte[] bytes = [
            .. "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n100000\r\n"u8.ToArray(),
            .. Encoding.UTF8.GetBytes(body)
        ];
        using var fixture = new HttpFixture();
        var stream = fixture.Enqueue(bytes, eof: false);
        using var provider = ProviderChecks.Provider(false, fixture.Client, TimeSpan.FromSeconds(2));
        var failure = new InvalidOperationException("consumer stopped");
        var error = await Check.ThrowsAsync<ProviderException>(() => provider.StreamAsync(
            ProviderChecks.Request(), (_, _) => ValueTask.FromException(failure)).WaitAsync(TimeSpan.FromSeconds(1)));
        Check.True(ReferenceEquals(failure, error.InnerException), "The consumer error was replaced.");
        await stream.Closed.Task.WaitAsync(TimeSpan.FromSeconds(1));
    }

    internal static async Task CleanupBeforeReturn()
    {
        foreach (string outcome in new[] { "complete", "cancel", "deadline" })
        {
            await using var stream = new CleanupStream(Encoding.UTF8.GetBytes(HttpFixture.Sse("openai_text")));
            using var client = new HttpClient(new ContentHandler(stream)) { Timeout = Timeout.InfiniteTimeSpan };
            using var provider = ProviderChecks.Provider(false, client,
                outcome == "deadline" ? TimeSpan.FromMilliseconds(100) : TimeSpan.FromSeconds(3));
            using var cancellation = new CancellationTokenSource();
            CancellationToken requestToken = default;
            var pending = provider.StreamAsync(ProviderChecks.Request(), (_, token) =>
            {
                requestToken = token;
                return ValueTask.CompletedTask;
            }, cancellation.Token);
            try
            {
                await stream.Started.Task.WaitAsync(TimeSpan.FromSeconds(2));
                Check.True(!pending.IsCompleted, "Provider returned before asynchronous body cleanup.");
                if (outcome == "cancel")
                    cancellation.Cancel();
                if (outcome == "deadline")
                    await Check.ThrowsAsync<OperationCanceledException>(() =>
                        Task.Delay(Timeout.InfiniteTimeSpan, requestToken).WaitAsync(TimeSpan.FromSeconds(2)));
            }
            finally
            {
                stream.Release.TrySetResult();
            }
            if (outcome == "cancel")
                await Check.ThrowsAsync<OperationCanceledException>(() => pending);
            else if (outcome == "deadline")
            {
                var error = await Check.ThrowsAsync<ProviderException>(() => pending);
                Check.True(error.Message.Contains("timed out", StringComparison.Ordinal), "The cleanup deadline was lost.");
            }
            else
                Check.Equal("done\ncafé", (await pending).Text);
            Check.True(stream.Finished, "Body cleanup did not finish.");
        }
    }

    internal static async Task ClientOwnershipAndDispose()
    {
        using var fixture = new HttpFixture();
        var pendingStream = fixture.Enqueue([], eof: false);
        fixture.Enqueue(HttpFixture.Wire(HttpFixture.Sse("openai_text")));
        var provider = ProviderChecks.Provider(false, fixture.Client);
        var pending = provider.StreamAsync(ProviderChecks.Request());
        try
        {
            await pendingStream.Waiting.Task.WaitAsync(TimeSpan.FromSeconds(2));
            provider.Dispose();
            await Check.ThrowsAsync<OperationCanceledException>(() => pending.WaitAsync(TimeSpan.FromSeconds(1)));
            await pendingStream.Closed.Task.WaitAsync(TimeSpan.FromSeconds(1));
            using var another = ProviderChecks.Provider(false, fixture.Client);
            Check.Equal("done\ncafé", (await another.StreamAsync(ProviderChecks.Request())).Text);
            await Check.ThrowsAsync<ObjectDisposedException>(() => provider.StreamAsync(ProviderChecks.Request()));
        }
        finally
        {
            provider.Dispose();
            fixture.Dispose();
            try { await pending; } catch (OperationCanceledException) { }
        }
    }

    internal static async Task CredentialsAndHttpErrors()
    {
        using var fixture = new HttpFixture();
        string? key = Environment.GetEnvironmentVariable("OPENAI_API_KEY");
        Environment.SetEnvironmentVariable("OPENAI_API_KEY", null);
        try
        {
            using var provider = new OpenAIProvider(client: fixture.Client);
            await Check.ThrowsAsync<ProviderException>(() => provider.StreamAsync(ProviderChecks.Request()));
            using var cancellation = new CancellationTokenSource();
            cancellation.Cancel();
            await Check.ThrowsAsync<OperationCanceledException>(() =>
                provider.StreamAsync(ProviderChecks.Request(), cancellationToken: cancellation.Token));
            Check.Equal(0, fixture.Connections);
        }
        finally
        {
            Environment.SetEnvironmentVariable("OPENAI_API_KEY", key);
        }
        foreach (int status in new[] { 302, 401, 429, 500 })
        {
            var stream = fixture.Enqueue(HttpFixture.Wire("unread response", status: status, ending: false), eof: false);
            using var provider = ProviderChecks.Provider(false, fixture.Client);
            var error = await Check.ThrowsAsync<ProviderException>(() => provider.StreamAsync(ProviderChecks.Request()));
            Check.True(error.Message.Contains(status.ToString(), StringComparison.Ordinal), "HTTP status was lost.");
            await stream.Closed.Task.WaitAsync(TimeSpan.FromSeconds(1));
        }
        foreach (string endpoint in new[] { "file:///fixture", "https://user:password@example.com", "https://example.com/#fragment" })
            await Check.ThrowsAsync<ArgumentException>(() =>
            {
                using var value = new OpenAIProvider(endpoint: endpoint);
                return Task.CompletedTask;
            });
        await Check.ThrowsAsync<ArgumentOutOfRangeException>(() =>
        {
            using var value = new OpenAIProvider(timeout: TimeSpan.Zero);
            return Task.CompletedTask;
        });
    }

    private static byte[][] Prefixes() =>
    [
        [],
        HttpFixture.Wire(HttpFixture.Events(new JsonObject { ["type"] = "ping" }), ending: false)
    ];
}

internal sealed class CleanupStream(byte[] bytes) : MemoryStream(bytes)
{
    internal TaskCompletionSource Started { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    internal TaskCompletionSource Release { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    internal bool Finished { get; private set; }

    public override async ValueTask DisposeAsync()
    {
        Started.TrySetResult();
        await Release.Task.ConfigureAwait(false);
        await base.DisposeAsync().ConfigureAwait(false);
        Finished = true;
    }
}

internal sealed class ContentHandler(Stream stream) : HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
        Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new DirectContent(stream) });
}

internal sealed class DirectContent(Stream stream) : HttpContent
{
    protected override Task<Stream> CreateContentReadStreamAsync() => Task.FromResult(stream);
    protected override Task<Stream> CreateContentReadStreamAsync(CancellationToken cancellationToken) => Task.FromResult(stream);
    protected override Task SerializeToStreamAsync(Stream target, TransportContext? context) => throw new NotSupportedException();
    protected override bool TryComputeLength(out long length) { length = 0; return false; }
}
