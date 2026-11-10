using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;

namespace TinyAgent.Checks;

internal static class ProviderTcpChecks
{
    internal static async Task Run(bool anthropic)
    {
        foreach (var (cancel, reset) in new[] { (false, false), (true, false), (false, true), (true, true) })
        {
            using var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            using var cancellation = new CancellationTokenSource();
            string body = HttpFixture.Sse(anthropic ? "anthropic_text" : "openai_text");
            if (cancel)
            {
                string terminal = anthropic ? "data: {\"type\":\"message_stop\"}" : "data: {\"type\":\"response.completed\"";
                body = body[..body.IndexOf(terminal, StringComparison.Ordinal)];
            }
            var server = ServeAsync(listener, body, anthropic, deadline.Token);
            int port = ((IPEndPoint)listener.LocalEndpoint).Port;
            string endpoint = $"http://127.0.0.1:{port}/fixture";
            using var client = reset ? ResetOnCloseClient() : null;
            using IProvider provider = anthropic
                ? new AnthropicProvider("fixture-key", endpoint, TimeSpan.FromSeconds(3), client: client)
                : new OpenAIProvider("fixture-key", endpoint, TimeSpan.FromSeconds(3), client: client);
            var observed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var deltas = new StringBuilder();
            var pending = provider.StreamAsync(ProviderChecks.Request(), (text, _) =>
            {
                deltas.Append(text);
                observed.TrySetResult();
                return ValueTask.CompletedTask;
            }, cancellation.Token);
            try
            {
                if (cancel)
                {
                    await observed.Task.WaitAsync(deadline.Token);
                    cancellation.Cancel();
                    await Check.ThrowsAsync<OperationCanceledException>(() => pending.WaitAsync(deadline.Token));
                }
                else
                {
                    Check.Equal("done\ncafé", (await pending.WaitAsync(deadline.Token)).Text);
                    Check.Equal("done\ncafé", deltas.ToString());
                }
                await server;
            }
            finally
            {
                cancellation.Cancel();
                deadline.Cancel();
                provider.Dispose();
                listener.Stop();
                try { await pending; } catch (OperationCanceledException) { }
                try { await server; } catch (OperationCanceledException) { }
            }
        }
    }

    private static HttpClient ResetOnCloseClient() => new(new SocketsHttpHandler
    {
        UseProxy = false,
        MaxResponseDrainSize = 0,
        ConnectCallback = async (context, token) =>
        {
            // SO_LINGER=0 forces a reset so both peer-closure forms stay covered.
            var socket = new Socket(SocketType.Stream, ProtocolType.Tcp) { LingerState = new LingerOption(true, 0) };
            try
            {
                await socket.ConnectAsync(context.DnsEndPoint, token);
                return new NetworkStream(socket, ownsSocket: true);
            }
            catch { socket.Dispose(); throw; }
        }
    });

    private static async Task ServeAsync(TcpListener listener, string body, bool anthropic, CancellationToken token)
    {
        using var connection = await listener.AcceptTcpClientAsync(token);
        await using var stream = connection.GetStream();
        using var header = new MemoryStream();
        byte[] next = new byte[1];
        while (true)
        {
            Check.Equal(1, await stream.ReadAsync(next, token));
            header.Write(next);
            Check.True(header.Length <= 16_384, "Request headers exceeded the fixture limit.");
            if (header.Length >= 4 && header.GetBuffer().AsSpan((int)header.Length - 4, 4).SequenceEqual("\r\n\r\n"u8))
                break;
        }
        string[] lines = Encoding.ASCII.GetString(header.ToArray()).Split("\r\n", StringSplitOptions.RemoveEmptyEntries);
        Check.Equal("POST /fixture HTTP/1.1", lines[0]);
        var headers = lines.Skip(1).Select(line => line.Split(':', 2))
            .ToDictionary(parts => parts[0], parts => parts[1].Trim(), StringComparer.OrdinalIgnoreCase);
        Check.Equal(anthropic ? "fixture-key" : "Bearer fixture-key", headers[anthropic ? "x-api-key" : "Authorization"]);
        int length = int.Parse(headers["Content-Length"], CultureInfo.InvariantCulture);
        Check.True(length is > 0 and <= 4_194_304, "Invalid fixture request size.");
        byte[] request = new byte[length];
        await stream.ReadExactlyAsync(request, token);
        using var document = JsonDocument.Parse(request);
        Check.True(document.RootElement.GetProperty("stream").GetBoolean(), "Streaming was not requested.");
        byte[] response = HttpFixture.Wire(body, ending: false);
        try
        {
            for (int offset = 0; offset < response.Length; offset += 7)
                await stream.WriteAsync(response.AsMemory(offset, Math.Min(7, response.Length - offset)), token);
            // Keep the chunk unfinished. Completion or cancellation must close the client connection.
            Check.Equal(0, await stream.ReadAsync(next, token));
        }
        catch (IOException error) when (error.InnerException is SocketException { SocketErrorCode: SocketError.ConnectionReset })
        {
            // Disposing an unread HTTP response may reset the peer instead of sending FIN.
        }
    }
}
