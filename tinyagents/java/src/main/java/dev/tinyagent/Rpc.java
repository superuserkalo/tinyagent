package dev.tinyagent;

import java.io.FileDescriptor;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.Channel;
import java.nio.channels.InterruptibleChannel;
import java.nio.channels.ReadableByteChannel;
import java.nio.channels.SelectableChannel;
import java.nio.channels.WritableByteChannel;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.StandardCharsets;
import java.util.Objects;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/** Native JSONL RPC. The connection takes ownership of its blocking channels. */
public final class Rpc {
    private Rpc() {}

    /** Closes both channels and joins all owned work before returning. */
    public static void run(Agent agent, ReadableByteChannel input, WritableByteChannel output)
            throws IOException, InterruptedException {
        new Connection(agent, input, output).run();
    }

    /** Owns process stdin/stdout and waits for cooperative cleanup during JVM shutdown. */
    public static void runStdio(Agent agent) throws IOException, InterruptedException {
        var connection = new Connection(agent, new FileInputStream(FileDescriptor.in).getChannel(),
                new FileOutputStream(FileDescriptor.out).getChannel());
        var hook = Thread.ofPlatform().name("tinyagent-rpc-shutdown").unstarted(() -> {
            connection.shutdown();
            boolean interrupted = false;
            for (;;) {
                try { connection.ended.await(); break; }
                catch (InterruptedException ignored) { interrupted = true; }
            }
            if (interrupted) Thread.currentThread().interrupt();
        });
        Runtime.getRuntime().addShutdownHook(hook);
        try { connection.run(); }
        finally {
            try { Runtime.getRuntime().removeShutdownHook(hook); }
            catch (IllegalStateException ignored) { /* JVM shutdown already owns the hook. */ }
        }
    }

    private static final class Connection {
        private final ReadableByteChannel input;
        private final WritableByteChannel output;
        private final RpcOutput pending = new RpcOutput();
        private final CancellationToken stopping = new CancellationToken();
        private final AtomicReference<IOException> failure = new AtomicReference<>();
        private final CountDownLatch ended = new CountDownLatch(1);
        private final RpcSession session;
        private final Thread reader;
        private final Thread writer;
        private final ScheduledThreadPoolExecutor timer;

        Connection(Agent agent, ReadableByteChannel input, WritableByteChannel output) {
            requireBlocking(input);
            requireBlocking(output);
            this.input = input;
            this.output = output;
            this.session = new RpcSession(agent, this::emit);
            // Native stdio FileChannel operations must run on platform threads to be interruptible.
            reader = Thread.ofPlatform().name("tinyagent-rpc-input").unstarted(this::read);
            writer = Thread.ofPlatform().name("tinyagent-rpc-output").unstarted(this::write);
            timer = new ScheduledThreadPoolExecutor(1,
                    Thread.ofPlatform().name("tinyagent-rpc-deadline").daemon(true).factory());
            timer.setRemoveOnCancelPolicy(true);
        }

        void run() throws IOException, InterruptedException {
            InterruptedException interrupted = null;
            try {
                writer.start();
                reader.start();
                reader.join();
                writer.join();
            } catch (InterruptedException error) {
                interrupted = error;
            } finally {
                shutdown();
                join(reader);
                session.close();
                pending.finish();
                join(writer);
                timer.shutdownNow();
                boolean interruptedDuringCleanup = false;
                while (!timer.isTerminated()) {
                    try { timer.awaitTermination(1, TimeUnit.SECONDS); }
                    catch (InterruptedException ignored) { interruptedDuringCleanup = true; }
                }
                close(input);
                close(output);
                ended.countDown();
                if (interrupted != null || interruptedDuringCleanup) Thread.currentThread().interrupt();
            }
            if (interrupted != null) throw interrupted;
            IOException error = failure.get();
            if (error == null) error = pending.failure();
            if (error != null) throw error;
        }

        void shutdown() {
            stop();
            reader.interrupt();
        }

        private void stop() {
            try { stopping.cancel(); }
            catch (RuntimeException error) { record(error); }
            try { session.stop(); }
            catch (RuntimeException error) { record(error); }
        }

        private void emit(ObjectNode value, CancellationToken token) throws Exception {
            try { pending.emit(value, token); }
            catch (RpcException error) {
                session.cancel();
                if (!error.code().equals("output_too_large")) {
                    record(error);
                    shutdown();
                }
                throw error;
            } catch (IOException error) {
                record(error);
                shutdown();
                throw error;
            }
        }

        private void read() {
            try {
                var chunk = ByteBuffer.allocate(8192);
                byte[] line = new byte[RpcProtocol.MAX_LINE_BYTES];
                int length = 0;
                while (!stopping.isCancelled()) {
                    int count = input.read(chunk);
                    if (count < 0) {
                        if (length != 0) processLine(line, length);
                        break;
                    }
                    chunk.flip();
                    while (chunk.hasRemaining()) {
                        byte value = chunk.get();
                        if (value == '\n') {
                            if (!processLine(line, length) || stopping.isCancelled()) return;
                            length = 0;
                        } else if (length == line.length) {
                            emit(RpcProtocol.error("line_too_large", "RPC input exceeds "
                                    + RpcProtocol.MAX_LINE_BYTES + " UTF-8 bytes"), null);
                            return;
                        } else {
                            line[length++] = value;
                        }
                    }
                    chunk.clear();
                    if (count == 0) Thread.sleep(1);
                }
            } catch (Exception error) {
                if (!stopping.isCancelled()) record(error);
            } finally {
                stop();
                session.awaitIdle();
                pending.finish();
            }
        }

        private boolean processLine(byte[] bytes, int length) throws Exception {
            String line;
            try { line = StandardCharsets.UTF_8.newDecoder().decode(ByteBuffer.wrap(bytes, 0, length)).toString(); }
            catch (CharacterCodingException error) {
                emit(RpcProtocol.error("invalid_utf8", "RPC input is not valid UTF-8"), null);
                return false;
            }
            JsonNode request;
            try { request = Json.parse(line); }
            catch (ProviderException error) {
                emit(RpcProtocol.error("invalid_json", "RPC input is not valid JSON"), null);
                return true;
            }
            try { session.handle(request); }
            catch (RpcException error) {
                if (!error.code().equals("output_too_large")) throw error;
            }
            return true;
        }

        private void write() {
            try {
                RpcOutput.Line line;
                while ((line = pending.next()) != null) writeLine(line);
            } catch (Exception error) {
                record(error);
                pending.fail(error instanceof IOException io ? io : new IOException("RPC output failed", error));
                shutdown();
            }
        }

        private void writeLine(RpcOutput.Line line) throws Exception {
            var deadline = new WriteDeadline(line);
            var global = stopping.onCancel(deadline::arm);
            var run = line.token == null ? null : line.token.onCancel(deadline::arm);
            try {
                while (line.bytes.hasRemaining()) {
                    if (output.write(line.bytes) == 0) Thread.sleep(1);
                }
                pending.complete(line);
            } finally {
                deadline.close();
                global.close();
                if (run != null) run.close();
            }
        }

        private final class WriteDeadline {
            private final RpcOutput.Line line;
            private ScheduledFuture<?> task;
            private boolean closed;

            WriteDeadline(RpcOutput.Line line) { this.line = line; }

            synchronized void arm() {
                if (closed || task != null) return;
                task = timer.schedule(() -> {
                    if (pending.expire(line)) {
                        shutdown();
                        writer.interrupt();
                    }
                }, 1, TimeUnit.SECONDS);
            }

            synchronized void close() {
                closed = true;
                if (task != null) task.cancel(false);
            }
        }

        private void record(Exception error) {
            failure.compareAndSet(null, error instanceof IOException io ? io : new IOException("RPC connection failed", error));
        }

        private void close(Channel channel) {
            try { channel.close(); }
            catch (IOException error) { record(error); }
        }
    }

    private static void requireBlocking(Channel channel) {
        Objects.requireNonNull(channel);
        if (!(channel instanceof InterruptibleChannel)
                || channel instanceof SelectableChannel selectable && !selectable.isBlocking()) {
            throw new IllegalArgumentException("RPC requires blocking interruptible channels");
        }
    }

    private static void join(Thread thread) {
        boolean interrupted = false;
        while (thread.isAlive()) {
            try { thread.join(); }
            catch (InterruptedException ignored) { interrupted = true; }
        }
        if (interrupted) Thread.currentThread().interrupt();
    }
}
