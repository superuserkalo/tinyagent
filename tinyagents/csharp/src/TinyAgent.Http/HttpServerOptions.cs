using System.Net;

namespace TinyAgent;

/// <summary>One token owns all sessions. Remote exposure requires both explicit opt-in and a token.</summary>
public sealed record HttpServerOptions
{
    public string Host { get; init; } = "127.0.0.1";
    public int Port { get; init; } = 8080;
    public string Token { get; init; } = "";
    public bool AllowRemote { get; init; }

    internal IPAddress Validate()
    {
        if (Port is < 0 or > 65535) throw new ArgumentOutOfRangeException(nameof(Port));
        ArgumentNullException.ThrowIfNull(Token);
        if (Token.Any(value => value is < '!' or > '~'))
            throw new ArgumentException("Token must contain only printable non-space ASCII characters.", nameof(Token));
        if (!IPAddress.TryParse(Host == "localhost" ? "127.0.0.1" : Host, out var address))
            throw new ArgumentException("Host must be an IP address or localhost.", nameof(Host));
        var resolved = address.IsIPv4MappedToIPv6 ? address.MapToIPv4() : address;
        if (!IPAddress.IsLoopback(resolved) && (!AllowRemote || Token.Length == 0))
            throw new ArgumentException("Non-loopback binding requires AllowRemote and a bearer token.");
        return address;
    }
}
