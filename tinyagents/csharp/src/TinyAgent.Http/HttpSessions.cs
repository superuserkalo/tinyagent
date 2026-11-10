using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Http;

namespace TinyAgent;

internal sealed class HttpSessions(Agent agent, string token) : IAsyncDisposable
{
    private readonly object gate = new();
    private readonly Dictionary<string, Session> sessions = new(StringComparer.Ordinal);
    private readonly HashSet<Session> owned = [];
    private readonly byte[] authorization = Encoding.ASCII.GetBytes("Bearer " + token);
    private bool closing;

    internal async Task HandleAsync(HttpContext context)
    {
        try
        {
            if (token.Length != 0 && (context.Request.Headers.Authorization.Count != 1
                || !CryptographicOperations.FixedTimeEquals(authorization, Encoding.UTF8.GetBytes(context.Request.Headers.Authorization.ToString()))))
            {
                context.Response.Headers.WWWAuthenticate = "Bearer";
                throw new HttpFailure(401, "unauthorized", "Missing or invalid bearer token");
            }
            lock (gate)
                if (closing) throw new HttpFailure(503, "server_closed", "Server is shutting down");
            string method = context.Request.Method;
            string path = context.Request.Path.Value ?? "";
            if (method == "GET" && path == "/health")
            {
                await HttpProtocol.JsonAsync(context, 200, new JsonObject { ["status"] = "ok", ["protocolVersion"] = 1 }).ConfigureAwait(false);
                return;
            }
            if (method == "POST" && path == "/sessions")
            {
                await HttpProtocol.ReadObjectAsync(context.Request, optional: true).ConfigureAwait(false);
                string id = "s_" + Convert.ToHexString(RandomNumberGenerator.GetBytes(24)).ToLowerInvariant();
                lock (gate)
                {
                    if (closing) throw new HttpFailure(503, "server_closed", "Server is shutting down");
                    var created = new Session(agent);
                    sessions.Add(id, created);
                    owned.Add(created);
                }
                await HttpProtocol.JsonAsync(context, 201, new JsonObject { ["sessionId"] = id }).ConfigureAwait(false);
                return;
            }
            string[] parts = path.Split('/');
            if (parts.Length is < 3 or > 4 || parts[0] != "" || parts[1] != "sessions")
                throw new HttpFailure(404, "not_found", "Unknown route");
            Session session;
            lock (gate)
            {
                if (!sessions.TryGetValue(parts[2], out session!))
                    throw new HttpFailure(404, "session_not_found", "Unknown session");
                if (method == "DELETE" && parts.Length == 3)
                {
                    sessions.Remove(parts[2]);
                    session.Stop();
                }
            }
            if (method == "DELETE" && parts.Length == 3)
            {
                await session.Completion.ConfigureAwait(false);
                lock (gate) owned.Remove(session);
                context.Response.StatusCode = 204;
                await context.Response.CompleteAsync().ConfigureAwait(false);
                return;
            }
            if (method == "POST" && parts.Length == 4 && parts[3] == "cancel")
            {
                await HttpProtocol.ReadObjectAsync(context.Request, optional: true).ConfigureAwait(false);
                string? id = session.Cancel();
                await HttpProtocol.JsonAsync(context, 200, new JsonObject { ["accepted"] = id is not null, ["runId"] = id }).ConfigureAwait(false);
                return;
            }
            if (method == "POST" && parts.Length == 4 && parts[3] == "prompts")
            {
                var body = await HttpProtocol.ReadObjectAsync(context.Request, optional: false).ConfigureAwait(false);
                if (body?["message"] is not JsonValue value || !value.TryGetValue<string>(out var message) || string.IsNullOrEmpty(message))
                    throw new HttpFailure(400, "invalid_request", "message must be a non-empty string");
                await using var response = new HttpRunResponse(context, session);
                var run = session.Start(message, response.StartAsync, response.EmitAsync);
                await run.Completion.ConfigureAwait(false);
                if (response.Failure is not null && !response.Completed)
                {
                    if (context.Response.HasStarted) context.Abort();
                    else throw new HttpFailure(500, "internal_error", "Could not start prompt response");
                }
                return;
            }
            throw new HttpFailure(404, "not_found", "Unknown route");
        }
        catch (Session.BusyException)
        {
            await HttpProtocol.ErrorAsync(context, 409, "session_busy", "The session already has an active run").ConfigureAwait(false);
        }
        catch (Session.ClosedException)
        {
            await HttpProtocol.ErrorAsync(context, 404, "session_not_found", "Unknown session").ConfigureAwait(false);
        }
        catch (HttpFailure error)
        {
            await HttpProtocol.ErrorAsync(context, error.Status, error.Code, error.Message).ConfigureAwait(false);
        }
        catch (BadHttpRequestException error)
        {
            await HttpProtocol.ErrorAsync(context, error.StatusCode, error.StatusCode == 413 ? "body_too_large" : "invalid_request", "Invalid HTTP request body").ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (context.RequestAborted.IsCancellationRequested) { context.Abort(); }
        catch (Exception)
        {
            if (context.Response.HasStarted) context.Abort();
            else await HttpProtocol.ErrorAsync(context, 500, "internal_error", "Request failed").ConfigureAwait(false);
        }
    }

    internal void Stop()
    {
        lock (gate)
        {
            closing = true;
            foreach (var session in owned) session.Stop();
        }
    }

    public async ValueTask DisposeAsync()
    {
        Session[] pending;
        lock (gate)
        {
            closing = true;
            pending = owned.ToArray();
            foreach (var session in pending) session.Stop();
        }
        // Join exact worker tasks even when shutdown was requested from a run callback.
        await Task.WhenAll(pending.Select(session => session.Completion)).ConfigureAwait(false);
        lock (gate)
        {
            sessions.Clear();
            owned.Clear();
        }
    }
}
