using System.Runtime.ExceptionServices;
using System.Text;

namespace TinyAgent;

public delegate ValueTask AgentEventHandler(AgentEvent agentEvent, CancellationToken cancellationToken);

/// <summary>Immutable configuration. The caller owns the provider and awaits its runs before disposal.</summary>
public sealed class Agent
{
    private readonly IProvider provider;
    private readonly string model;
    private readonly string instructions;
    private readonly Dictionary<string, Tool> tools;
    private readonly IReadOnlyList<ToolDefinition> definitions;

    public Agent(IProvider provider, string model, string instructions = "", IEnumerable<Tool>? tools = null)
    {
        ArgumentNullException.ThrowIfNull(provider);
        ArgumentException.ThrowIfNullOrEmpty(model);
        this.provider = provider;
        this.model = Json.Text(model, nameof(model));
        this.instructions = Json.Text(instructions, nameof(instructions));
        this.tools = new(StringComparer.Ordinal);
        foreach (var tool in tools ?? [])
        {
            ArgumentNullException.ThrowIfNull(tool);
            if (!this.tools.TryAdd(tool.Definition.Name, tool))
                throw new ArgumentException($"Duplicate tool name: {tool.Definition.Name}", nameof(tools));
        }
        definitions = Array.AsReadOnly(this.tools.Values.Select(tool => tool.Definition).ToArray());
    }

    public Task<RunResult> RunAsync(
        string prompt,
        IEnumerable<Message>? history = null,
        AgentEventHandler? onEvent = null,
        CancellationToken cancellationToken = default,
        string? runId = null)
    {
        Json.Text(prompt, nameof(prompt));
        string id = runId ?? $"r_{Guid.NewGuid():N}";
        ArgumentException.ThrowIfNullOrEmpty(id, nameof(runId));
        Json.Text(id, nameof(runId));
        var transcript = history?.ToList() ?? [];
        if (transcript.Any(message => message is not (UserMessage or AssistantMessage or ToolResultMessage)))
            throw new ArgumentException("History contains a null or unsupported message.", nameof(history));
        transcript.Add(new UserMessage(prompt));
        return new Run(this, transcript, onEvent, cancellationToken, id).ExecuteAsync();
    }

    private sealed class Run(
        Agent agent, List<Message> history, AgentEventHandler? onEvent, CancellationToken parent, string id)
    {
        private long sequence;
        private ExceptionDispatchInfo? consumerFailure;

        internal async Task<RunResult> ExecuteAsync()
        {
            var scope = new RunCancellation(parent);
            ExceptionDispatchInfo? failure = null;
            RunResult? result = null;
            bool cancelled;
            try
            {
                await EmitAsync(new AgentEvent.Start(id, ++sequence), scope.Token).ConfigureAwait(false);
                scope.Token.ThrowIfCancellationRequested();
                result = await LoopAsync(scope.Token).ConfigureAwait(false);
            }
            catch (Exception error)
            {
                failure = consumerFailure ?? ExceptionDispatchInfo.Capture(error);
            }
            finally
            {
                cancelled = scope.Token.IsCancellationRequested;
                try
                {
                    await scope.DisposeAsync().ConfigureAwait(false);
                }
                catch (Exception error)
                {
                    failure ??= ExceptionDispatchInfo.Capture(error);
                }
            }
            cancelled |= parent.IsCancellationRequested;
            if (cancelled && consumerFailure is null)
                failure = ExceptionDispatchInfo.Capture(new OperationCanceledException("Run cancelled.", failure?.SourceException, parent));
            if (failure is not null)
            {
                if (consumerFailure is null)
                {
                    AgentEvent terminal;
                    if (failure.SourceException is OperationCanceledException)
                        terminal = new AgentEvent.Aborted(id, ++sequence);
                    else
                    {
                        string message = failure.SourceException.Message ?? "Provider request failed.";
                        try { Json.Utf8.GetByteCount(message); }
                        catch (EncoderFallbackException) { message = "Provider request failed."; }
                        terminal = new AgentEvent.Failed(id, ++sequence, message);
                    }
                    await EmitAsync(terminal, parent).ConfigureAwait(false);
                }
                failure.Throw();
            }
            if (result is null)
                throw new InvalidOperationException("Run completed without a result.");
            await EmitAsync(new AgentEvent.Completed(id, ++sequence, result.Text), parent).ConfigureAwait(false);
            parent.ThrowIfCancellationRequested();
            return result;
        }

        private async Task<RunResult> LoopAsync(CancellationToken token)
        {
            while (true)
            {
                token.ThrowIfCancellationRequested();
                bool streamed = false;
                var turn = await agent.provider.StreamAsync(
                    new ProviderRequest(agent.model, agent.instructions, history, agent.definitions),
                    async (delta, providerToken) =>
                    {
                        consumerFailure?.Throw();
                        token.ThrowIfCancellationRequested();
                        providerToken.ThrowIfCancellationRequested();
                        Json.Text(delta, "provider text delta");
                        if (delta.Length != 0)
                        {
                            streamed = true;
                            await EmitAsync(new AgentEvent.TextDelta(id, ++sequence, delta), providerToken).ConfigureAwait(false);
                        }
                        token.ThrowIfCancellationRequested();
                        providerToken.ThrowIfCancellationRequested();
                    }, token).ConfigureAwait(false);
                consumerFailure?.Throw();
                token.ThrowIfCancellationRequested();
                if (turn is null) throw new ProviderException("Provider did not return a completed turn.");
                Json.Text(turn.Text, "provider final text");
                foreach (var call in turn.ToolCalls)
                    if (call is null || !agent.tools.ContainsKey(call.Name))
                        throw new ProviderException($"Provider requested an unregistered tool: {call?.Name}");
                if (!streamed && turn.Text.Length != 0)
                    await EmitAsync(new AgentEvent.TextDelta(id, ++sequence, turn.Text), token).ConfigureAwait(false);
                token.ThrowIfCancellationRequested();
                history.Add(turn.Assistant);
                if (turn.ToolCalls.Count == 0) return new RunResult(turn.Text, history, turn.Output);
                foreach (var call in turn.ToolCalls)
                    await ExecuteToolAsync(call, token).ConfigureAwait(false);
            }
        }

        private async Task ExecuteToolAsync(ToolCall call, CancellationToken token)
        {
            token.ThrowIfCancellationRequested();
            await EmitAsync(new AgentEvent.ToolExecutionStart(id, ++sequence, call), token).ConfigureAwait(false);
            string output = "Tool cancelled.";
            bool isError = true;
            try
            {
                output = await agent.tools[call.Name].ExecuteAsync(call.Arguments, token).ConfigureAwait(false);
                token.ThrowIfCancellationRequested();
                Json.Text(output, "tool output");
                if (Json.Utf8.GetByteCount(output) > Tool.MaxOutputBytes)
                    throw new InvalidOperationException($"Tool output exceeds {Tool.MaxOutputBytes} UTF-8 bytes.");
                isError = false;
            }
            catch (OperationCanceledException) { output = "Tool cancelled."; throw; }
            catch (Exception error)
            {
                // A handler may throw arbitrary text. Never retain unbounded or invalid UTF-8 observations.
                var detail = new StringBuilder("Tool failed: ");
                int bytes = detail.Length;
                foreach (var rune in error.Message.EnumerateRunes())
                {
                    if (bytes + rune.Utf8SequenceLength > 2000) break;
                    detail.Append(rune);
                    bytes += rune.Utf8SequenceLength;
                }
                output = token.IsCancellationRequested ? "Tool cancelled." : detail.ToString();
            }
            finally
            {
                await EmitAsync(new AgentEvent.ToolExecutionEnd(id, ++sequence, call.CallId, call.Name, output, isError), token)
                    .ConfigureAwait(false);
            }
            token.ThrowIfCancellationRequested();
            history.Add(new ToolResultMessage(call.CallId, call.Name, output, isError));
        }

        private async ValueTask EmitAsync(AgentEvent value, CancellationToken token)
        {
            if (onEvent is null)
                return;
            try
            {
                await onEvent(value, token).ConfigureAwait(false);
            }
            catch (Exception error)
            {
                consumerFailure = ExceptionDispatchInfo.Capture(error);
                throw;
            }
        }
    }

    // Retain the first cancellation task so cleanup also awaits callbacks started by the parent.
    private sealed class RunCancellation : IAsyncDisposable
    {
        private readonly CancellationTokenSource source = new();
        private readonly object gate = new();
        private readonly CancellationTokenRegistration registration;
        private Task? cancellation;
        internal CancellationToken Token { get; }

        internal RunCancellation(CancellationToken parent)
        {
            Token = source.Token;
            registration = parent.Register(() => { RequestCancellation(); });
        }

        private Task RequestCancellation()
        {
            lock (gate)
                return cancellation ??= source.CancelAsync();
        }

        public async ValueTask DisposeAsync()
        {
            await registration.DisposeAsync().ConfigureAwait(false);
            try { await RequestCancellation().ConfigureAwait(false); }
            finally { source.Dispose(); }
        }
    }
}
