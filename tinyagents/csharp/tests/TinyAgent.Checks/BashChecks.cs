using System.Runtime.InteropServices;
using System.Text;

namespace TinyAgent.Checks;

internal static class BashChecks
{
    internal static IEnumerable<(string Name, Func<Task> Run)> Cases =>
    [
        ("Bash shared blacklist and input validation", Guard),
        ("Bash native output, exit status, and closed stdin", Output),
        ("Bash exact combined output bound and overflowing processes", Bounds),
        ("Bash cancellation stops the command process group", Cancellation),
        ("Bash independent runs and background cleanup", Ownership)
    ];

    private static readonly TimeSpan Deadline = TimeSpan.FromSeconds(5);
    private static string Quote(string text) => "'" + text.Replace("'", "'\\''", StringComparison.Ordinal) + "'";
    private static async Task<string> Run(string command)
    {
        using var deadline = new CancellationTokenSource(Deadline);
        return await Bash.RunAsync(command, deadline.Token);
    }

    private static async Task Guard()
    {
        foreach (string command in new[] { "rm -rf /", "r\"\"m -rf /", "sudo echo hi", "git reset --hard HEAD", "git restore file", "git checkout -- file", "git clean -fd", "find . -delete", "mkfs.ext4 /dev/sda", ":(){ :|:& };:" })
        {
            Check.True(Bash.DeniedReason(command) is not null, "Unsafe command passed: " + command);
            Check.True((await Run(command)).StartsWith("exit_code: 126\n", StringComparison.Ordinal), "Denied command was executed.");
        }
        Check.True(Bash.DeniedReason("printf 'hello\\n'; git status") is null, "Safe command was rejected.");
        foreach (string invalid in new[] { "", "\0", "\ud800" })
            await Check.ThrowsAsync<ArgumentException>(() => Bash.RunAsync(invalid));
        using var cancelled = new CancellationTokenSource();
        await cancelled.CancelAsync();
        await Check.ThrowsAsync<OperationCanceledException>(() => Bash.RunAsync("exit 0", cancelled.Token));
        Check.True(Encoding.UTF8.GetByteCount(await Run("mkfs." + new string('a', 200_000))) < Bash.MaxOutputBytes, "Blacklist error was unbounded.");
    }

    private static async Task Output()
    {
        Check.Equal("exit_code: 7\nstdout:\nhello\ncaféstderr:\nproblem\n", await Run("printf 'hello\\ncafé'; printf 'problem\\n' >&2; exit 7"));
        Check.Equal("exit_code: 0\nstdout:\n$HOME `ignored` 'quoted'stderr:\n", await Run("printf '%s' " + Quote("$HOME `ignored` 'quoted'")));
        Check.Equal("exit_code: 0\nstdout:\neofstderr:\n", await Run("if read line; then printf input; else printf eof; fi"));
        Check.Equal("exit_code: 0\nstdout:\n�stderr:\n", await Run("printf '\\377'"));
    }

    private static async Task Bounds()
    {
        int envelope = Encoding.UTF8.GetByteCount("exit_code: 0\nstdout:\nstderr:\n");
        int count = Bash.MaxOutputBytes - envelope;
        string observation = await Run($"head -c {count} /dev/zero | tr '\\000' x");
        Check.Equal(Bash.MaxOutputBytes, Encoding.UTF8.GetByteCount(observation));
        foreach (string command in new[]
        {
            $"head -c {count + 1} /dev/zero | tr '\\000' x",
            "head -c 70000 /dev/zero; head -c 70000 /dev/zero >&2",
            "yes x"
        })
        {
            var error = await Check.ThrowsAsync<IOException>(() => Run(command));
            Check.Equal("Tool output exceeds 131072 UTF-8 bytes.", error.Message);
        }
    }

    private static async Task Cancellation()
    {
        string directory = Path.Combine(AppContext.BaseDirectory, "bash-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        string pidFile = Path.Combine(directory, "pid");
        using var cancellation = new CancellationTokenSource(Deadline);
        Task<string> running = Bash.RunAsync($"sleep 30 & child=$!; printf '%s' \"$child\" > {Quote(pidFile)}; wait", cancellation.Token);
        try
        {
            int child = await ReadPid(pidFile);
            await cancellation.CancelAsync();
            await Check.ThrowsAsync<OperationCanceledException>(() => running.WaitAsync(Deadline));
            await Gone(child);
        }
        finally
        {
            await cancellation.CancelAsync();
            try { await running.WaitAsync(Deadline); } catch (OperationCanceledException) { }
            Directory.Delete(directory, recursive: true);
        }
    }

    private static async Task Ownership()
    {
        string directory = Path.Combine(AppContext.BaseDirectory, "bash-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        string pidFile = Path.Combine(directory, "pid");
        string release = Path.Combine(directory, "release");
        using var cancellation = new CancellationTokenSource(Deadline);
        Task<string> independent = Bash.RunAsync($"until test -f {Quote(release)}; do sleep 0.01; done; printf independent", cancellation.Token);
        try
        {
            await Cancellation();
            Check.True(!independent.IsCompleted, "Cancellation stopped an independent command.");
            await File.WriteAllTextAsync(release, "ready");
            Check.Equal("exit_code: 0\nstdout:\nindependentstderr:\n", await independent.WaitAsync(Deadline));
            await Run($"sleep 30 & child=$!; printf '%s' \"$child\" > {Quote(pidFile)}");
            await Gone(await ReadPid(pidFile));
        }
        finally
        {
            await cancellation.CancelAsync();
            try { await independent.WaitAsync(Deadline); } catch (OperationCanceledException) { }
            Directory.Delete(directory, recursive: true);
        }
    }

    private static async Task<int> ReadPid(string path)
    {
        using var deadline = new CancellationTokenSource(Deadline);
        while (true)
        {
            if (File.Exists(path) && int.TryParse(await File.ReadAllTextAsync(path, deadline.Token), out int pid)) return pid;
            await Task.Delay(10, deadline.Token);
        }
    }

    private static async Task Gone(int pid)
    {
        using var deadline = new CancellationTokenSource(Deadline);
        while (Kill(pid, 0) == 0) await Task.Delay(10, deadline.Token);
        Check.Equal(3, Marshal.GetLastPInvokeError());
    }

    [DllImport("libc", EntryPoint = "kill", SetLastError = true)]
    private static extern int Kill(int pid, int signal);
}
