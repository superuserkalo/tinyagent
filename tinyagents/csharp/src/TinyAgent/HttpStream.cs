using System.Net.Http.Headers;
using System.Text.Json.Nodes;

namespace TinyAgent;

internal sealed class HttpStream : IDisposable
{
    private readonly HttpClient client;
    private readonly bool ownsClient;
    private readonly Uri endpoint;
    private readonly TimeSpan timeout;
    private readonly CancellationTokenSource lifetime = new();
    private int disposed;

    internal HttpStream(string endpoint, TimeSpan? timeout, HttpClient? client)
    {
        if (!Uri.TryCreate(endpoint, UriKind.Absolute, out var address) ||
            address.Scheme is not ("http" or "https") || address.UserInfo.Length != 0 || address.Fragment.Length != 0)
            throw new ArgumentException("Provider endpoint must be an HTTP or HTTPS URL.", nameof(endpoint));
        this.endpoint = address;
        this.timeout = timeout ?? TimeSpan.FromSeconds(180);
        if (this.timeout <= TimeSpan.Zero || this.timeout.TotalMilliseconds > uint.MaxValue - 1)
            throw new ArgumentOutOfRangeException(nameof(timeout), "Provider timeout must be positive and at most 49 days.");
        ownsClient = client is null;
        this.client = client ?? new HttpClient(new SocketsHttpHandler
        {
            AllowAutoRedirect = false,
            UseCookies = false,
            ConnectTimeout = TimeSpan.FromSeconds(30),
            MaxResponseDrainSize = 0
        }) { Timeout = Timeout.InfiniteTimeSpan };
    }

    internal async Task<ProviderTurn> SendAsync(
        JsonObject payload,
        IReadOnlyDictionary<string, string> headers,
        IResponseParser parser,
        TextDeltaHandler? onText,
        CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(Volatile.Read(ref disposed) != 0, this);
        cancellationToken.ThrowIfCancellationRequested();
        using var deadline = new CancellationTokenSource(timeout);
        using var scope = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, deadline.Token, lifetime.Token);
        using var request = new HttpRequestMessage(HttpMethod.Post, endpoint)
        {
            Content = new ByteArrayContent(Json.Utf8.GetBytes(payload.ToJsonString()))
        };
        request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/json");
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("text/event-stream"));
        foreach (var header in headers)
            request.Headers.Add(header.Key, header.Value);
        try
        {
            ProviderTurn? completed = null;
            using (var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, scope.Token)
                .ConfigureAwait(false))
            {
                scope.Token.ThrowIfCancellationRequested();
                if (!response.IsSuccessStatusCode)
                    throw new ProviderException($"Provider HTTP {(int)response.StatusCode}.");
                await using var body = await response.Content.ReadAsStreamAsync(scope.Token).ConfigureAwait(false);
                await foreach (var value in Sse.ReadAsync(body, scope.Token).ConfigureAwait(false))
                {
                    completed = await parser.ConsumeAsync(value, onText, scope.Token).ConfigureAwait(false);
                    scope.Token.ThrowIfCancellationRequested();
                    if (completed is not null)
                        break;
                }
            }
            scope.Token.ThrowIfCancellationRequested();
            return completed ?? throw new ProviderException("Provider stream ended before a complete turn.");
        }
        catch (Exception error)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (deadline.IsCancellationRequested && !lifetime.IsCancellationRequested)
                throw new ProviderException("Provider request timed out.", error);
            scope.Token.ThrowIfCancellationRequested();
            if (error is OperationCanceledException or ProviderException)
                throw;
            throw new ProviderException("Provider request failed.", error);
        }
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref disposed, 1) != 0)
            return;
        try
        {
            lifetime.Cancel();
        }
        finally
        {
            if (ownsClient)
                client.Dispose();
            lifetime.Dispose();
        }
    }
}
