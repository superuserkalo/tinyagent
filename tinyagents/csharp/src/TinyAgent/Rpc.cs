using System.Runtime.ExceptionServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace TinyAgent;

/// <summary>Native JSONL RPC. The connection owns its streams; the caller owns the provider.</summary>
public static class Rpc
{
    /// <summary>
    /// Runs until EOF or cancellation and disposes both streams after joining all work.
    /// Streams must honor cancellation of pending async reads and writes.
    /// </summary>
    public static Task RunAsync(Agent agent, Stream input, Stream output, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(agent);
        ArgumentNullException.ThrowIfNull(input);
        ArgumentNullException.ThrowIfNull(output);
        if (!input.CanRead || !output.CanWrite)
            throw new ArgumentException("RPC requires readable input and writable output.");
        return new Connection(agent, input, output).RunAsync(cancellationToken);
    }

    /// <summary>Owns redirected stdin/stdout on Linux and macOS. Returns 130 on Ctrl-C, otherwise 0.</summary>
    public static async Task<int> RunStdioAsync(Agent agent, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(agent);
        if (!OperatingSystem.IsLinux() && !OperatingSystem.IsMacOS())
            throw new PlatformNotSupportedException("Native RPC stdio currently supports Linux and macOS.");
        if (!Console.IsInputRedirected || !Console.IsOutputRedirected)
            throw new InvalidOperationException("RPC stdio requires redirected input and output.");
        await using var input = Stdio.Open(0, FileAccess.Read);
        await using var output = Stdio.Open(1, FileAccess.Write);
        using var interrupted = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        bool ctrlC = false;
        ConsoleCancelEventHandler handler = (_, args) =>
        {
            args.Cancel = true;
            Volatile.Write(ref ctrlC, true);
            interrupted.Cancel();
        };
        Console.CancelKeyPress += handler;
        try
        {
            await RunAsync(agent, input, output, interrupted.Token).ConfigureAwait(false);
            return 0;
        }
        catch (OperationCanceledException) when (Volatile.Read(ref ctrlC) && !cancellationToken.IsCancellationRequested)
        {
            return 130;
        }
        finally
        {
            Console.CancelKeyPress -= handler;
        }
    }

    private sealed class Connection
    {
        private readonly Stream input;
        private readonly Stream output;
        private readonly RpcOutput pending = new();
        private readonly RpcSession session;
        private readonly CancellationTokenSource stopping = new();
        private readonly object gate = new();
        private Task? cancellation;
        private ExceptionDispatchInfo? failure;

        internal Connection(Agent agent, Stream input, Stream output)
        {
            this.input = input;
            this.output = output;
            session = new RpcSession(agent, EmitAsync);
        }

        internal async Task RunAsync(CancellationToken parent)
        {
            var registration = parent.Register(Stop);
            var writer = Task.Run(WriteAsync);
            var reader = Task.Run(ReadAsync);
            try
            {
                await reader.ConfigureAwait(false);
            }
            finally
            {
                Stop();
                await session.DisposeAsync().ConfigureAwait(false);
                pending.Finish();
                await writer.ConfigureAwait(false);
                await registration.DisposeAsync().ConfigureAwait(false);
                if (cancellation is not null)
                {
                    try { await cancellation.ConfigureAwait(false); }
                    catch (Exception error) { Record(error); }
                }
                try { await input.DisposeAsync().ConfigureAwait(false); }
                catch (Exception error) { Record(error); }
                try { await output.DisposeAsync().ConfigureAwait(false); }
                catch (Exception error) { Record(error); }
                stopping.Dispose();
            }
            parent.ThrowIfCancellationRequested();
            failure?.Throw();
        }

        private void Stop()
        {
            lock (gate) cancellation ??= stopping.CancelAsync();
            session.Stop();
        }

        private void Record(Exception error)
        {
            lock (gate) failure ??= ExceptionDispatchInfo.Capture(error);
        }

        private async ValueTask EmitAsync(JsonObject value, CancellationToken? token)
        {
            try { await pending.EmitAsync(value, token).ConfigureAwait(false); }
            catch (RpcException error) when (error.Code == "output_too_large")
            {
                session.Cancel();
                throw;
            }
            catch (Exception error)
            {
                Record(error);
                Stop();
                throw;
            }
        }

        private async Task ReadAsync()
        {
            try
            {
                byte[] chunk = new byte[8192];
                byte[] line = new byte[RpcOutput.MaxLineBytes];
                int length = 0;
                while (!stopping.IsCancellationRequested)
                {
                    int count = await input.ReadAsync(chunk, stopping.Token).ConfigureAwait(false);
                    if (count == 0)
                    {
                        if (length != 0) await ProcessAsync(line.AsMemory(0, length)).ConfigureAwait(false);
                        break;
                    }
                    for (int index = 0; index < count; index++)
                    {
                        byte value = chunk[index];
                        if (value == '\n')
                        {
                            if (!await ProcessAsync(line.AsMemory(0, length)).ConfigureAwait(false) || stopping.IsCancellationRequested) return;
                            length = 0;
                        }
                        else if (length == line.Length)
                        {
                            await EmitAsync(RpcOutput.Error("line_too_large", $"RPC input exceeds {RpcOutput.MaxLineBytes} UTF-8 bytes"), null).ConfigureAwait(false);
                            return;
                        }
                        else line[length++] = value;
                    }
                }
            }
            catch (OperationCanceledException) when (stopping.IsCancellationRequested) { }
            catch (Exception error) { Record(error); }
            finally { Stop(); }
        }

        private async Task<bool> ProcessAsync(ReadOnlyMemory<byte> bytes)
        {
            string text;
            try { text = Json.Utf8.GetString(bytes.Span); }
            catch (DecoderFallbackException)
            {
                await EmitAsync(RpcOutput.Error("invalid_utf8", "RPC input is not valid UTF-8"), null).ConfigureAwait(false);
                return false;
            }
            JsonNode? command;
            try { command = JsonNode.Parse(text, documentOptions: new JsonDocumentOptions { MaxDepth = 100 }); }
            catch (JsonException)
            {
                await EmitAsync(RpcOutput.Error("invalid_json", "RPC input is not valid JSON"), null).ConfigureAwait(false);
                return true;
            }
            try { await session.HandleAsync(command).ConfigureAwait(false); }
            catch (RpcException error) when (error.Code == "output_too_large") { }
            return true;
        }

        private async Task WriteAsync()
        {
            try
            {
                while (await pending.NextAsync().ConfigureAwait(false) is { } frame)
                {
                    await WriteFrameAsync(frame).ConfigureAwait(false);
                    pending.Complete(frame);
                }
            }
            catch (Exception error)
            {
                Record(error);
                pending.Fail(error);
                Stop();
            }
        }

        private async Task WriteFrameAsync(RpcOutput.Frame frame)
        {
            using var deadline = new CancellationTokenSource();
            int armed = 0;
            void Arm()
            {
                if (Interlocked.Exchange(ref armed, 1) == 0) deadline.CancelAfter(TimeSpan.FromSeconds(1));
            }
            using var shutdown = stopping.Token.Register(Arm);
            using var run = frame.Token.GetValueOrDefault().Register(Arm);
            try
            {
                await output.WriteAsync(frame.Bytes, deadline.Token).ConfigureAwait(false);
                await output.FlushAsync(deadline.Token).ConfigureAwait(false);
                deadline.Token.ThrowIfCancellationRequested();
            }
            catch (OperationCanceledException) when (deadline.IsCancellationRequested)
            {
                throw new RpcException("output_stalled", "Cancelled RPC output did not drain");
            }
        }
    }
}
