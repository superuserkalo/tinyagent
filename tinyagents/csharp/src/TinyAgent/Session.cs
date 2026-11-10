namespace TinyAgent;

// Shared by transport adapters. A run owns admission until delivery and cleanup finish.
internal sealed class Session(Agent agent) : IAsyncDisposable
{
    internal sealed class BusyException() : InvalidOperationException("A run or reset is already active");
    internal sealed class ClosedException() : InvalidOperationException("Session is closed");
    internal sealed record State(string? RunId, int MessageCount);

    private sealed class ActiveRun
    {
        internal readonly string Id = $"r_{Guid.NewGuid():N}";
        internal readonly CancellationTokenSource Source = new();
        internal Task? Cancellation;
        internal bool CancelRequested;
    }

    private readonly object gate = new();
    private readonly AsyncLocal<ActiveRun?> executing = new();
    private IReadOnlyList<Message> history = Array.AsReadOnly(Array.Empty<Message>());
    private ActiveRun? active;
    private ActiveRun? workerRun;
    private Task worker = Task.CompletedTask;
    private bool resetting;
    private bool closed;

    internal (string Id, Task Completion) Start(string prompt, Func<string, CancellationToken, ValueTask> beforeRun, AgentEventHandler onEvent)
    {
        Json.Text(prompt, nameof(prompt));
        ArgumentNullException.ThrowIfNull(beforeRun);
        ArgumentNullException.ThrowIfNull(onEvent);
        lock (gate)
        {
            if (closed) throw new ClosedException();
            if (active is not null || resetting) throw new BusyException();
            var run = new ActiveRun();
            var previous = history;
            active = workerRun = run;
            worker = Task.Run(() => ExecuteAsync(run, prompt, previous, beforeRun, onEvent));
            return (run.Id, worker);
        }
    }

    internal State GetState()
    {
        lock (gate) return new State(active?.Id, history.Count);
    }

    internal Task Completion
    {
        get { lock (gate) return worker; }
    }

    internal string? Cancel()
    {
        lock (gate) return RequestCancellation();
    }

    // A disconnected request may finish after another request has started a new run.
    internal void Cancel(string runId)
    {
        lock (gate)
            if (active?.Id == runId) RequestCancellation();
    }

    internal async Task<bool> ResetAsync()
    {
        string? runId;
        lock (gate)
        {
            if (closed) throw new ClosedException();
            if (resetting) throw new BusyException();
            resetting = true;
            history = Array.AsReadOnly(Array.Empty<Message>());
            runId = RequestCancellation();
        }
        try
        {
            await WaitForIdleAsync().ConfigureAwait(false);
            return runId is not null;
        }
        finally
        {
            lock (gate) resetting = false;
        }
    }

    // Stop admission first; an adapter can keep draining output before awaiting disposal.
    internal string? Stop()
    {
        lock (gate)
        {
            closed = true;
            return RequestCancellation();
        }
    }

    public async ValueTask DisposeAsync()
    {
        Stop();
        await WaitForIdleAsync().ConfigureAwait(false);
    }

    internal async Task<bool> WaitForIdleAsync()
    {
        Task pending;
        lock (gate)
        {
            // A callback may request shutdown, but cannot await its own return.
            if (executing.Value is not null && executing.Value == workerRun) return false;
            pending = worker;
        }
        await pending.ConfigureAwait(false);
        return true;
    }

    // Called under gate. CancelAsync signals immediately and schedules callbacks outside the lock.
    private string? RequestCancellation()
    {
        if (active is null) return null;
        active.CancelRequested = true;
        active.Cancellation ??= active.Source.CancelAsync();
        return active.Id;
    }

    private async Task ExecuteAsync(ActiveRun run, string prompt, IReadOnlyList<Message> previous,
        Func<string, CancellationToken, ValueTask> beforeRun, AgentEventHandler onEvent)
    {
        executing.Value = run;
        RunResult? result = null;
        try
        {
            await beforeRun(run.Id, run.Source.Token).ConfigureAwait(false);
            result = await agent.RunAsync(prompt, previous, onEvent, run.Source.Token, run.Id).ConfigureAwait(false);
        }
        catch (Exception)
        {
            // Provider errors already have a terminal event. A failed emitter has no usable peer.
        }
        finally
        {
            bool cleaned = false;
            Task cancellation;
            lock (gate) cancellation = run.Cancellation ??= run.Source.CancelAsync();
            try
            {
                await cancellation.ConfigureAwait(false);
                cleaned = true;
            }
            catch (Exception)
            {
                // Failure to release a callback also prevents committing the result.
            }
            finally
            {
                lock (gate)
                {
                    if (cleaned && result is not null && !run.CancelRequested && !closed && !resetting)
                        history = result.History;
                    run.Source.Dispose();
                    active = null;
                }
                executing.Value = null;
            }
        }
    }
}
