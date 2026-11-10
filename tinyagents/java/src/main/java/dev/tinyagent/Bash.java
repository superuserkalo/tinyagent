package dev.tinyagent;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.Objects;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Pattern;

/** Optional guarded Bash execution on Linux and macOS. The blacklist is not a sandbox. */
public final class Bash {
    public static final int MAX_OUTPUT_BYTES = 131_072;
    private static final Pattern WORD = Pattern.compile("[a-z0-9_.-]+");
    private static final Set<String> DENIED_WORDS = Set.of(
            "rm", "rmdir", "unlink", "shred", "truncate", "dd", "mkfs", "wipefs", "fdisk", "parted",
            "sudo", "doas", "shutdown", "reboot", "poweroff", "halt", "kill", "pkill", "killall");

    // Job control gives the command a separate process group. Only its stderr reaches the caller.
    private static final String SUPERVISOR = """
            exec 3>&2 2>/dev/null
            set -m
            job=
            cancelled=
            stop() {
                cancelled=1
                if [ -n "$job" ]; then kill -KILL -- "-$job" 2>/dev/null; fi
            }
            trap stop TERM INT
            bash -c "$1" </dev/null 2>&3 3>&- &
            job=$!
            if [ -n "$cancelled" ]; then stop; fi
            wait "$job"
            result=$?
            kill -KILL -- "-$job" 2>/dev/null
            if [ -n "$cancelled" ]; then wait "$job" 2>/dev/null; exit 130; fi
            exit "$result"
            """;

    private Bash() {}

    public static String deniedReason(String command) {
        Objects.requireNonNull(command, "command");
        String normalized = command.toLowerCase(Locale.ROOT).replace("\\", "").replace("\"", "").replace("'", "");
        var words = new TreeSet<String>();
        var matcher = WORD.matcher(normalized);
        while (matcher.find()) words.add(matcher.group());
        for (String word : words) {
            if (DENIED_WORDS.contains(word) || word.startsWith("mkfs."))
                return "'" + word.substring(0, Math.min(word.length(), 80)) + "' is denied";
        }
        if (words.contains("find") && words.contains("-delete")) return "'find -delete' is denied";
        if (words.contains("git") && (words.contains("clean") || words.contains("restore")
                || (words.contains("reset") && words.contains("--hard"))
                || (words.contains("checkout") && words.contains("--"))))
            return "destructive git command is denied";
        if (normalized.replaceAll("\\s", "").contains(":(){:|:&};:")) return "fork bomb is denied";
        return null;
    }

    /** Captures bounded output and joins process and reader cleanup before returning or throwing. */
    public static String run(String command, CancellationToken cancellation) throws IOException {
        Objects.requireNonNull(command, "command");
        Objects.requireNonNull(cancellation, "cancellation");
        if (command.isEmpty() || command.indexOf('\0') >= 0 || !StandardCharsets.UTF_8.newEncoder().canEncode(command))
            throw new IllegalArgumentException("Bash command must be non-empty Unicode text without NUL");
        cancellation.check();
        String reason = deniedReason(command);
        if (reason != null) return "exit_code: 126\nstdout:\n\nstderr:\nblocked: " + reason + "\n";
        String os = System.getProperty("os.name");
        if (!os.equals("Linux") && !os.equals("Mac OS X"))
            throw new UnsupportedOperationException("Bash execution requires Linux or macOS");
        var builder = new ProcessBuilder("bash", "--noprofile", "--norc", "-c", SUPERVISOR, "tinyagent-bash", command);
        // Ambient Bash startup code must not alter supervisor traps or job control.
        for (String name : Set.of("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS")) builder.environment().remove(name);
        Process process = builder.start();
        var stop = new Stopper(process);
        var capture = new Capture(stop);
        Thread stdout = Thread.ofPlatform().name("tinyagent-bash-stdout")
                .unstarted(() -> capture.read(process.getInputStream(), capture.stdout));
        Thread stderr = Thread.ofPlatform().name("tinyagent-bash-stderr")
                .unstarted(() -> capture.read(process.getErrorStream(), capture.stderr));
        var registration = cancellation.onCancel(stop::request);
        boolean interrupted = false;
        try {
            stdout.start();
            stderr.start();
            process.getOutputStream().close();
            try { process.waitFor(); }
            catch (InterruptedException error) { interrupted = true; stop.request(); }
        } finally {
            if (process.isAlive()) stop.request();
            for (;;) {
                try { process.waitFor(); break; }
                catch (InterruptedException ignored) { interrupted = true; stop.request(); }
            }
            for (Thread reader : new Thread[] {stdout, stderr}) {
                while (reader.isAlive()) {
                    try { reader.join(); }
                    catch (InterruptedException ignored) { interrupted = true; stop.request(); }
                }
            }
            stop.close();
            registration.close();
            if (interrupted) Thread.currentThread().interrupt();
        }
        cancellation.check();
        if (capture.overflow) throw new IOException("Tool output exceeds 131072 UTF-8 bytes");
        if (capture.failure != null) throw capture.failure;
        String observation = "exit_code: " + process.exitValue() + "\nstdout:\n"
                + capture.stdout.toString(StandardCharsets.UTF_8) + "stderr:\n" + capture.stderr.toString(StandardCharsets.UTF_8);
        if (observation.getBytes(StandardCharsets.UTF_8).length > MAX_OUTPUT_BYTES)
            throw new IOException("Tool output exceeds 131072 UTF-8 bytes");
        return observation;
    }

    // Registration removal cannot retract a callback already copied by CancellationToken.cancel().
    private static final class Stopper {
        private final Process process;
        private boolean closed;

        Stopper(Process process) { this.process = process; }

        synchronized void request() {
            if (!closed && process.isAlive()) process.destroy();
        }

        synchronized void close() { closed = true; }
    }

    private static final class Capture {
        private final Stopper stop;
        final ByteArrayOutputStream stdout = new ByteArrayOutputStream();
        final ByteArrayOutputStream stderr = new ByteArrayOutputStream();
        private int bytes;
        boolean overflow;
        IOException failure;

        Capture(Stopper stop) { this.stop = stop; }

        void read(InputStream source, ByteArrayOutputStream destination) {
            try (source) {
                byte[] buffer = new byte[8192];
                int count;
                while ((count = source.read(buffer)) != -1) {
                    boolean exceeded;
                    synchronized (this) {
                        exceeded = count > MAX_OUTPUT_BYTES - bytes;
                        if (exceeded) overflow = true;
                        if (!overflow) {
                            bytes += count;
                            destination.write(buffer, 0, count);
                        }
                    }
                    if (exceeded) stop.request();
                }
            } catch (IOException error) {
                synchronized (this) { if (failure == null) failure = error; }
                stop.request();
            }
        }
    }
}
