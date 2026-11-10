import dev.tinyagent.Provider;
import dev.tinyagent.ProviderTurn;
import java.io.BufferedReader;
import java.io.FileDescriptor;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

/** Native descriptors and JVM signals; no provider credentials or listener. */
public final class CliFixture {
    private CliFixture() {}

    public static void main(String[] args) throws Exception {
        String mode = args[0];
        Path marker = Path.of(args[1]);
        try (var input = new BufferedReader(new InputStreamReader(new FileInputStream(FileDescriptor.in), StandardCharsets.UTF_8));
                var output = new FileOutputStream(FileDescriptor.out).getChannel();
                var error = new FileOutputStream(FileDescriptor.err).getChannel()) {
            Provider provider = (request, token, text) -> {
                Files.writeString(marker, "1");
                try {
                    if (mode.equals("request")) {
                        while (!token.isCancelled()) Thread.sleep(10);
                        token.check();
                    }
                    if (mode.equals("stalled")) {
                        for (;;) { token.check(); text.accept("x".repeat(16_384)); }
                    }
                    return new ProviderTurn("", List.of(), List.of(), List.of());
                } finally {
                    Files.writeString(marker, "2");
                }
            };
            if (mode.equals("idle")) {
                Files.writeString(marker, "1");
                TinyAgent.run(provider, input, output, error);
            } else if (mode.equals("bash")) {
                TinyAgent.runTurn(CliChecks.provider(CliChecks.response("", CliChecks.bash("one", CliChecks.waitCommand(marker)))),
                        List.of(), "wait", output, error);
            } else {
                TinyAgent.runTurn(provider, List.of(), "wait", output, error);
            }
        }
    }
}
