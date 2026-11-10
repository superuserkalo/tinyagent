using Microsoft.AspNetCore.Connections;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Http.Features;
using Microsoft.AspNetCore.Server.Kestrel.Core;
using Microsoft.AspNetCore.Server.Kestrel.Transport.Sockets;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;

namespace TinyAgent;

/// <summary>Owns the listener and sessions. The caller owns the configured agent and provider.</summary>
public sealed class HttpServer : IAsyncDisposable
{
    private readonly KestrelServer server;
    private readonly HttpSessions sessions;
    private readonly object gate = new();
    private Task? shutdown;
    public Uri Address { get; }

    private HttpServer(KestrelServer server, HttpSessions sessions)
    {
        this.server = server;
        this.sessions = sessions;
        Address = new Uri(server.Features.Get<IServerAddressesFeature>()!.Addresses.Single());
    }

    public static Task<HttpServer> StartAsync(Agent agent, HttpServerOptions? options = null,
        CancellationToken cancellationToken = default) =>
        StartAsync(agent, options ?? new(), null, cancellationToken);

    internal static async Task<HttpServer> StartAsync(Agent agent, HttpServerOptions options,
        IConnectionListenerFactory? transport, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(agent);
        var address = options.Validate();
        cancellationToken.ThrowIfCancellationRequested();
        var configuration = new KestrelServerOptions { AddServerHeader = false };
        configuration.Listen(address, options.Port, endpoint => endpoint.Protocols = HttpProtocols.Http1);
        configuration.Limits.MaxRequestBodySize = HttpProtocol.MaxBodyBytes;
        configuration.Limits.MaxRequestHeadersTotalSize = 65_536;
        configuration.Limits.RequestHeadersTimeout = TimeSpan.FromSeconds(10);
        configuration.Limits.MaxResponseBufferSize = 0;
        transport ??= new SocketTransportFactory(Options.Create(new SocketTransportOptions()), NullLoggerFactory.Instance);
        var server = new KestrelServer(Options.Create(configuration), transport, NullLoggerFactory.Instance);
        var sessions = new HttpSessions(agent, options.Token);
        try
        {
            await server.StartAsync(new Application(sessions), cancellationToken).ConfigureAwait(false);
            return new HttpServer(server, sessions);
        }
        catch
        {
            await sessions.DisposeAsync().ConfigureAwait(false);
            server.Dispose();
            throw;
        }
    }

    /// <summary>Stops admission immediately. Safe to call from a provider callback.</summary>
    public void RequestStop()
    {
        lock (gate) shutdown ??= StopAsync();
    }

    /// <summary>Waits for transport shutdown and all cooperative run cleanup. Call outside provider callbacks.</summary>
    public ValueTask DisposeAsync()
    {
        lock (gate) return new ValueTask(shutdown ??= StopAsync());
    }

    private async Task StopAsync()
    {
        sessions.Stop();
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(1));
        Task transport = server.StopAsync(deadline.Token);
        try
        {
            await sessions.DisposeAsync().ConfigureAwait(false);
        }
        finally
        {
            // Kestrel still owns native I/O until its stop task ends.
            try { await transport.ConfigureAwait(false); }
            finally { server.Dispose(); }
        }
    }

    private sealed class Application(HttpSessions sessions) : IHttpApplication<DefaultHttpContext>
    {
        public DefaultHttpContext CreateContext(IFeatureCollection features) => new(features);
        public Task ProcessRequestAsync(DefaultHttpContext context) => sessions.HandleAsync(context);
        public void DisposeContext(DefaultHttpContext context, Exception? exception) { }
    }
}
