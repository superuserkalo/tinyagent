using System.Text.Json;
using System.Text.Json.Nodes;

namespace TinyAgent;

/// <summary>
/// Delivers a detached protocol object. A non-null token marks a run acknowledgement or event:
/// await its complete delivery. Ordinary command responses may be queued by the transport.
/// </summary>
public delegate ValueTask RpcEmitter(JsonObject value, CancellationToken? cancellationToken);

/// <summary>Command mapping and in-memory history for one session. The caller owns transport and provider.</summary>
public sealed class RpcSession : IAsyncDisposable
{
    private readonly Session session;
    private readonly RpcEmitter emit;

    public RpcSession(Agent agent, RpcEmitter emit)
    {
        ArgumentNullException.ThrowIfNull(agent);
        ArgumentNullException.ThrowIfNull(emit);
        session = new Session(agent);
        this.emit = emit;
    }

    public async ValueTask HandleAsync(JsonNode? request)
    {
        if (request is not JsonObject)
        {
            await CommandErrorAsync(null, null, "invalid_command", "RPC command must be an object").ConfigureAwait(false);
            return;
        }
        JsonObject command;
        try
        {
            ValidateText(request);
            command = (JsonObject)request.DeepClone();
        }
        catch (Exception error) when (error is ArgumentException or InvalidOperationException or JsonException)
        {
            await CommandErrorAsync(null, null, "invalid_json", "RPC input is not valid JSON").ConfigureAwait(false);
            return;
        }
        string? id = String(command["id"]);
        if (command.ContainsKey("id") && id is null)
        {
            await CommandErrorAsync(null, null, "invalid_command", "command.id must be a string").ConfigureAwait(false);
            return;
        }
        string type = String(command["type"]) ?? "unknown";
        try
        {
            switch (type)
            {
                case "prompt":
                    string? prompt = String(command["message"]);
                    if (string.IsNullOrEmpty(prompt))
                    {
                        await CommandErrorAsync(id, type, "invalid_command", "prompt.message must be a non-empty string").ConfigureAwait(false);
                        return;
                    }
                    session.Start(prompt,
                        (runId, token) => emit(Response(id, type, new JsonObject { ["runId"] = runId }), token),
                        (value, token) => emit(value.ToJson(), token));
                    break;
                case "abort":
                    string? aborted = session.Cancel();
                    await emit(Response(id, type, new JsonObject { ["accepted"] = aborted is not null, ["runId"] = aborted }), null).ConfigureAwait(false);
                    break;
                case "new_session":
                    bool reset = await session.ResetAsync().ConfigureAwait(false);
                    await emit(Response(id, type, new JsonObject { ["abortedRun"] = reset }), null).ConfigureAwait(false);
                    break;
                case "get_state":
                    await emit(Response(id, type, GetState()), null).ConfigureAwait(false);
                    break;
                default:
                    await CommandErrorAsync(id, type, "invalid_command", "Unknown command type").ConfigureAwait(false);
                    break;
            }
        }
        catch (Session.BusyException error)
        {
            await CommandErrorAsync(id, type, "busy", error.Message).ConfigureAwait(false);
        }
        catch (Session.ClosedException error)
        {
            await CommandErrorAsync(id, type, "session_closed", error.Message).ConfigureAwait(false);
        }
    }

    public JsonObject GetState()
    {
        var state = session.GetState();
        return new JsonObject
        {
            ["protocolVersion"] = 1,
            ["status"] = state.RunId is null ? "idle" : "running",
            ["runId"] = state.RunId,
            ["messageCount"] = state.MessageCount
        };
    }

    /// <summary>Requests cancellation and returns the active run ID, or null when idle.</summary>
    public string? Cancel() => session.Cancel();

    /// <summary>Closes admission and requests cancellation. Await disposal to finish cleanup.</summary>
    public string? Stop() => session.Stop();

    /// <summary>Awaits the observed run through cleanup. Returns false inside that run's callback.</summary>
    public Task<bool> WaitForIdleAsync() => session.WaitForIdleAsync();

    /// <summary>Cancels and awaits cleanup. From a run callback, only requests shutdown to avoid self-wait.</summary>
    public ValueTask DisposeAsync() => session.DisposeAsync();

    private static string? String(JsonNode? value) =>
        value is JsonValue child && child.TryGetValue<string>(out var text) ? text : null;

    private static JsonObject Response(string? id, string command, JsonObject data)
    {
        JsonObject value = new() { ["type"] = "response", ["command"] = command, ["success"] = true, ["data"] = data };
        if (id is not null) value["id"] = id;
        return value;
    }

    private ValueTask CommandErrorAsync(string? id, string? command, string code, string message)
    {
        JsonObject value = new()
        {
            ["type"] = "protocol_error",
            ["error"] = new JsonObject { ["code"] = code, ["message"] = message }
        };
        if (id is not null)
        {
            value["type"] = "response";
            value["id"] = id;
            value["command"] = command;
            value["success"] = false;
        }
        return emit(value, null);
    }

    private static void ValidateText(JsonNode? value, int depth = 0)
    {
        if (depth > 100) throw new ArgumentException("JSON nesting exceeds its depth limit.");
        if (value is JsonObject obj)
        {
            foreach (var (key, child) in obj)
            {
                Json.Text(key, "JSON property");
                ValidateText(child, depth + 1);
            }
        }
        else if (value is JsonArray array)
        {
            foreach (var child in array) ValidateText(child, depth + 1);
        }
        else if (String(value) is { } text)
        {
            Json.Text(text, "JSON string");
        }
    }
}
