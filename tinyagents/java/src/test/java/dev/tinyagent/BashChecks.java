package dev.tinyagent;

import static dev.tinyagent.ProviderChecks.*;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CancellationException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

final class BashChecks {
    private BashChecks() {}

    static void run() {
        check("Bash shared blacklist and input validation", BashChecks::guard);
        check("Bash native stdout, stderr, status, and closed stdin", BashChecks::output);
        check("Bash exact combined output limits and infinite output", BashChecks::bounds);
        check("Bash cancellation stops the command process group", () -> cancellation(false));
        check("Bash caller interruption retains cleanup and interrupt state", () -> cancellation(true));
        check("Bash independent commands and background cleanup", BashChecks::ownership);
    }

    private static String quote(String value) { return "'" + value.replace("'", "'\\''") + "'"; }

    private static void guard() throws Exception {
        for (String command : List.of("rm -rf /", "r\"\"m -rf /", "sudo echo hi", "git reset --hard HEAD",
                "git restore file", "git checkout -- file", "git clean -fd", "find . -delete", "mkfs.ext4 /dev/sda", ":(){ :|:& };:")) {
            truth(Bash.deniedReason(command) != null, "Unsafe command passed: " + command);
            truth(Bash.run(command, new CancellationToken()).startsWith("exit_code: 126\n"), "Denied command was executed");
        }
        truth(Bash.deniedReason("printf 'hello\\n'; git status") == null, "Safe command was rejected");
        for (String invalid : List.of("", "\0", "\ud800"))
            expect(IllegalArgumentException.class, () -> Bash.run(invalid, new CancellationToken()));
        var cancelled = new CancellationToken();
        cancelled.cancel();
        expect(CancellationException.class, () -> Bash.run("exit 0", cancelled));
        truth(Bash.run("mkfs." + "x".repeat(200_000), new CancellationToken()).length() < 200, "Blacklist error was unbounded");
    }

    private static void output() throws Exception {
        equal(Bash.run("printf 'hello\\ncafé'; printf 'problem\\n' >&2; exit 7", new CancellationToken()),
                "exit_code: 7\nstdout:\nhello\ncaféstderr:\nproblem\n");
        equal(Bash.run("printf '%s' " + quote("$HOME `ignored` 'quoted'"), new CancellationToken()),
                "exit_code: 0\nstdout:\n$HOME `ignored` 'quoted'stderr:\n");
        equal(Bash.run("if read line; then printf input; else printf eof; fi", new CancellationToken()),
                "exit_code: 0\nstdout:\neofstderr:\n");
        equal(Bash.run("printf '\\377'", new CancellationToken()), "exit_code: 0\nstdout:\n�stderr:\n");
    }

    private static void bounds() throws Exception {
        int count = Bash.MAX_OUTPUT_BYTES - "exit_code: 0\nstdout:\nstderr:\n".length();
        String result = Bash.run("head -c " + count + " /dev/zero | tr '\\000' x", new CancellationToken());
        equal(result.getBytes(StandardCharsets.UTF_8).length, Bash.MAX_OUTPUT_BYTES);
        for (String command : List.of("head -c " + (count + 1) + " /dev/zero | tr '\\000' x",
                "head -c 70000 /dev/zero; head -c 70000 /dev/zero >&2", "yes x")) {
            var failure = new AtomicReference<Throwable>();
            var token = new CancellationToken();
            var running = worker(() -> Bash.run(command, token), failure);
            try {
                join(running);
                truth(failure.get() instanceof IOException, "Overflow did not fail: " + failure.get());
                equal(failure.get().getMessage(), "Tool output exceeds 131072 UTF-8 bytes");
            } finally { token.cancel(); join(running); }
        }
    }

    private static void cancellation(boolean interrupt) throws Exception {
        Path directory = Files.createTempDirectory(Path.of("target"), "bash-");
        Path pidFile = directory.resolve("pid");
        var token = new CancellationToken();
        var failure = new AtomicReference<Throwable>();
        var interrupted = new AtomicBoolean();
        var running = worker(() -> {
            try { Bash.run("sleep 30 & child=$!; printf '%s' \"$child\" > " + quote(pidFile.toAbsolutePath().toString()) + "; wait", token); }
            finally { interrupted.set(Thread.currentThread().isInterrupted()); }
        }, failure);
        try {
            long child = readPid(pidFile);
            if (interrupt) running.interrupt(); else token.cancel();
            join(running);
            truth(failure.get() instanceof CancellationException, "Cancellation did not propagate: " + failure.get());
            if (interrupt) truth(interrupted.get(), "Caller interruption was lost");
            gone(child);
        } finally {
            token.cancel();
            join(running);
            Files.deleteIfExists(pidFile);
            Files.delete(directory);
        }
    }

    private static void ownership() throws Exception {
        Path directory = Files.createTempDirectory(Path.of("target"), "bash-");
        Path release = directory.resolve("release");
        Path pidFile = directory.resolve("pid");
        var token = new CancellationToken();
        var failure = new AtomicReference<Throwable>();
        var result = new AtomicReference<String>();
        var running = worker(() -> result.set(Bash.run("until test -f " + quote(release.toAbsolutePath().toString())
                + "; do sleep 0.01; done; printf independent", token)), failure);
        try {
            cancellation(false);
            truth(running.isAlive(), "Cancellation stopped an independent command");
            Files.writeString(release, "ready");
            join(running);
            truth(failure.get() == null, "Independent command failed: " + failure.get());
            equal(result.get(), "exit_code: 0\nstdout:\nindependentstderr:\n");
            Bash.run("sleep 30 & child=$!; printf '%s' \"$child\" > " + quote(pidFile.toAbsolutePath().toString()), new CancellationToken());
            gone(readPid(pidFile));
        } finally {
            token.cancel();
            join(running);
            Files.deleteIfExists(pidFile);
            Files.deleteIfExists(release);
            Files.delete(directory);
        }
    }

    private static long readPid(Path file) throws Exception {
        long end = System.nanoTime() + Duration.ofSeconds(2).toNanos();
        while (System.nanoTime() < end) {
            if (Files.exists(file)) {
                String text = Files.readString(file);
                if (!text.isEmpty()) return Long.parseLong(text);
            }
            Thread.sleep(10);
        }
        throw new AssertionError("Bash did not report its child PID");
    }

    private static void gone(long pid) throws Exception {
        long end = System.nanoTime() + Duration.ofSeconds(2).toNanos();
        while (System.nanoTime() < end) {
            if (ProcessHandle.of(pid).isEmpty()) return;
            Thread.sleep(10);
        }
        throw new AssertionError("Bash child is still present: " + pid);
    }
}
