using System.Text.Json;
using System.Text.Json.Nodes;

namespace TinyAgent;

// One serialized queue, including the frame currently being written.
internal sealed class RpcOutput
{
    internal const int MaxLineBytes = 1_048_576;
    private const int MaxQueueBytes = 4_194_304;

    internal sealed class Frame(byte[] bytes, CancellationToken? token, Exception? error)
    {
        internal readonly byte[] Bytes = bytes;
        internal readonly CancellationToken? Token = token;
        internal readonly TaskCompletionSource Delivered = Signal();
        internal Exception? Error = error;
    }

    private readonly object gate = new();
    private readonly Queue<Frame> frames = new();
    private TaskCompletionSource changed = Signal();
    private Frame? current;
    private int queuedBytes;
    private bool finished;
    private Exception? failure;

    internal async ValueTask EmitAsync(JsonObject value, CancellationToken? token)
    {
        byte[] bytes = JsonSerializer.SerializeToUtf8Bytes(value);
        RpcException? error = null;
        if (bytes.Length > MaxLineBytes)
        {
            error = new RpcException("output_too_large", $"RPC output exceeds {MaxLineBytes} UTF-8 bytes");
            bytes = JsonSerializer.SerializeToUtf8Bytes(Error(error.Code, error.Message));
        }
        var frame = new Frame([.. bytes, (byte)'\n'], token, error);
        lock (gate)
        {
            if (failure is not null) throw failure;
            if (finished) throw new IOException("RPC output is closed");
            if (queuedBytes + frame.Bytes.Length > MaxQueueBytes)
            {
                var overflow = new RpcException("output_queue_full", $"RPC output queue exceeds {MaxQueueBytes} UTF-8 bytes");
                failure = overflow;
                finished = true;
                foreach (var pending in frames) Reject(pending, overflow);
                frames.Clear();
                if (current is not null) Reject(current, overflow);
                var last = new Frame([.. JsonSerializer.SerializeToUtf8Bytes(Error(overflow.Code, overflow.Message)), (byte)'\n'], null, null);
                frames.Enqueue(last);
                queuedBytes = (current?.Bytes.Length ?? 0) + last.Bytes.Length;
                changed.TrySetResult();
                throw overflow;
            }
            frames.Enqueue(frame);
            queuedBytes += frame.Bytes.Length;
            changed.TrySetResult();
        }
        if (token is not null) await frame.Delivered.Task.ConfigureAwait(false);
        if (frame.Error is not null) throw frame.Error;
    }

    internal async Task<Frame?> NextAsync()
    {
        while (true)
        {
            Task pending;
            lock (gate)
            {
                if (frames.TryDequeue(out var frame))
                {
                    current = frame;
                    if (frames.Count == 0) changed = Signal();
                    return frame;
                }
                if (finished) return null;
                pending = changed.Task;
            }
            await pending.ConfigureAwait(false);
        }
    }

    internal void Complete(Frame frame)
    {
        lock (gate)
        {
            queuedBytes -= frame.Bytes.Length;
            current = null;
            frame.Delivered.TrySetResult();
        }
    }

    internal void Finish()
    {
        lock (gate)
        {
            finished = true;
            changed.TrySetResult();
        }
    }

    internal void Fail(Exception error)
    {
        lock (gate)
        {
            failure ??= error;
            if (current is not null) Reject(current, failure);
            foreach (var frame in frames) Reject(frame, failure);
            frames.Clear();
            current = null;
            queuedBytes = 0;
            finished = true;
            changed.TrySetResult();
        }
    }

    internal static JsonObject Error(string code, string message) => new()
    {
        ["type"] = "protocol_error",
        ["error"] = new JsonObject { ["code"] = code, ["message"] = message }
    };

    private static TaskCompletionSource Signal() => new(TaskCreationOptions.RunContinuationsAsynchronously);

    private static void Reject(Frame frame, Exception error)
    {
        frame.Error = error;
        frame.Delivered.TrySetResult();
    }
}
