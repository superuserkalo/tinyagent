import dev.tinyagent.Agent;
import dev.tinyagent.AgentEvent;
import dev.tinyagent.Bash;
import dev.tinyagent.HttpServer;
import dev.tinyagent.Rpc;
import dev.tinyagent.Tool;
import dev.tinyagent.CancellationToken;
import dev.tinyagent.Message;
import dev.tinyagent.OpenAIProvider;
import dev.tinyagent.Provider;
import java.io.BufferedReader;
import java.io.FileDescriptor;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.net.InetSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.WritableByteChannel;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import tools.jackson.databind.json.JsonMapper;

public class TinyAgent {
    static final String MODEL = System.getenv().getOrDefault("TINYAGENT_MODEL", "gpt-5.6");
    static final String INSTRUCTIONS = """
        You are TinyAgent, a coding agent in the current directory.
        Inspect before changing files. Use bash for shell and file work. Each call is a fresh process.""";
    private static final Tool BASH = new Tool("bash",
            "Run a non-interactive bash command in the current directory.",
            JsonMapper.builder().build().readTree("""
                {"type":"object","properties":{"command":{"type":"string","minLength":1}},
                 "required":["command"],"additionalProperties":false}
                """), (arguments, token) -> Bash.run(arguments.path("command").stringValue(), token));

    static Agent agent(Provider provider) {
        return new Agent(provider, MODEL, INSTRUCTIONS, List.of(BASH));
    }

    static List<Message> turn(Provider provider, List<Message> previous, String prompt,
            CancellationToken cancellation, Provider.TextSink text, Provider.TextSink diagnostics) throws Exception {
        return agent(provider).run(prompt, previous, cancellation, event -> {
            switch (event) {
                case AgentEvent.TextDelta delta -> text.accept(delta.delta());
                case AgentEvent.ToolStarted started -> {
                    var command = started.arguments().path("command");
                    if (command.isString() && !command.stringValue().isEmpty()) {
                        diagnostics.accept("\n$ " + command.stringValue() + "\n");
                    }
                }
                case AgentEvent.ToolEnded ended -> {
                    if (!cancellation.isCancelled()) diagnostics.accept(ended.output() + "\n");
                }
                case AgentEvent.Completed ignored -> text.accept("\n");
                default -> { }
            }
        }).history();
    }

    // A hook exists only during a turn, so idle console input cannot hold up exit.
    static List<Message> runTurn(Provider provider, List<Message> history, String prompt,
            WritableByteChannel output, WritableByteChannel error) throws Exception {
        var cancellation = new CancellationToken();
        var ended = new CountDownLatch(1);
        var delivery = cancellation.onCancel(() -> {
            close(output);
            close(error);
        });
        Thread shutdown = Thread.ofPlatform().name("tinyagent-cli-shutdown").unstarted(() -> {
            try { cancellation.cancel(); }
            finally {
                boolean interrupted = false;
                for (;;) {
                    try { ended.await(); break; }
                    catch (InterruptedException ignored) { interrupted = true; }
                }
                if (interrupted) Thread.currentThread().interrupt();
            }
        });
        Runtime.getRuntime().addShutdownHook(shutdown);
        try {
            return turn(provider, history, prompt, cancellation,
                    value -> write(output, value), value -> write(error, value));
        } finally {
            delivery.close();
            ended.countDown();
            try { Runtime.getRuntime().removeShutdownHook(shutdown); }
            catch (IllegalStateException ignored) { /* JVM shutdown already owns the hook. */ }
        }
    }

    static void run(Provider provider, BufferedReader input, WritableByteChannel output,
            WritableByteChannel error) throws Exception {
        List<Message> history = List.of();
        write(output, "TinyAgent. Type a task, or Ctrl-C or Ctrl-D to exit.\n");
        while (true) {
            write(output, "\n> ");
            String line = input.readLine();
            if (line == null) break;
            String prompt = line.trim();
            if (prompt.isEmpty()) continue;
            try { history = runTurn(provider, history, prompt, output, error); }
            catch (Exception failure) {
                if (!output.isOpen() || !error.isOpen() || Thread.currentThread().isInterrupted()) throw failure;
                write(error, "error: " + failure.getMessage() + "\n");
            }
        }
        write(output, "\n");
    }

    private static void write(WritableByteChannel output, String text) throws IOException {
        var bytes = ByteBuffer.wrap(text.getBytes(StandardCharsets.UTF_8));
        while (bytes.hasRemaining()) output.write(bytes);
    }

    private static void close(WritableByteChannel channel) {
        try { channel.close(); }
        catch (IOException ignored) { /* Continue releasing the other output and provider. */ }
    }

    static void selfTest() {
        check(Bash.deniedReason("printf 'hello\\n'") == null, "safe command was denied");
        for (String command : List.of("rm -rf /", "r\"\"m -rf /", "sudo echo hi", "git reset --hard HEAD", "find . -delete", "mkfs.ext4 /dev/sda"))
            check(Bash.deniedReason(command) != null, "unsafe command was allowed: " + command);
        check(BASH.definition().parameters().path("required").get(0).stringValue().equals("command"), "Bash schema is missing command");
        System.out.println("self-test passed");
    }

    static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    static void serve(Agent agent) throws Exception {
        String host = System.getenv().getOrDefault("TINYAGENT_HOST", "127.0.0.1");
        int port = Integer.parseInt(System.getenv().getOrDefault("TINYAGENT_PORT", "8080"));
        var options = new HttpServer.Options(new InetSocketAddress(host, port),
                System.getenv("TINYAGENT_TOKEN"), "1".equals(System.getenv("TINYAGENT_ALLOW_REMOTE")));
        try (var server = HttpServer.start(agent, options)) {
            var hook = Thread.ofPlatform().name("tinyagent-http-shutdown").unstarted(() -> {
                try { server.stop(); server.awaitStopped(); }
                catch (IOException error) { System.err.println(error.getMessage()); }
            });
            Runtime.getRuntime().addShutdownHook(hook);
            try {
                System.err.println("TinyAgent listening on " + server.url());
                server.awaitStopped();
            } finally {
                try { Runtime.getRuntime().removeShutdownHook(hook); }
                catch (IllegalStateException ignored) { /* JVM shutdown already owns the hook. */ }
            }
        }
    }

    public static void main(String[] args) throws Exception {
        if (Arrays.asList(args).contains("--self-test")) {
            selfTest();
            return;
        }
        try (var provider = new OpenAIProvider()) {
            if (Arrays.equals(args, new String[]{"--mode", "rpc"})) {
                Rpc.runStdio(agent(provider));
            } else if (Arrays.equals(args, new String[]{"serve"})) {
                serve(agent(provider));
            } else if (args.length == 0) {
                try (var input = new BufferedReader(new InputStreamReader(new FileInputStream(FileDescriptor.in), StandardCharsets.UTF_8));
                        var output = new FileOutputStream(FileDescriptor.out).getChannel();
                        var error = new FileOutputStream(FileDescriptor.err).getChannel()) {
                    run(provider, input, output, error);
                }
            } else {
                throw new IllegalArgumentException("Usage: TinyAgent [--self-test | --mode rpc | serve]");
            }
        }
    }
}
