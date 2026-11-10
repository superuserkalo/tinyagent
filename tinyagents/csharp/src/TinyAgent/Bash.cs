using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;

namespace TinyAgent;

/// <summary>Optional guarded Bash execution on Linux and macOS. The blacklist is not a sandbox.</summary>
public static class Bash
{
    public const int MaxOutputBytes = 131_072;
    private static readonly HashSet<string> DeniedWords =
    [
        "rm", "rmdir", "unlink", "shred", "truncate", "dd", "mkfs", "wipefs", "fdisk", "parted",
        "sudo", "doas", "shutdown", "reboot", "poweroff", "halt", "kill", "pkill", "killall"
    ];

    // Bash job control gives the command its own process group without a second language runtime.
    // The supervisor handles cancellation even between starting the job and recording its PID.
    private const string Supervisor = """
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

    public static string? DeniedReason(string command)
    {
        Json.Text(command, nameof(command));
        string normalized = command.ToLowerInvariant().Replace("\\", "").Replace("\"", "").Replace("'", "");
        var words = Regex.Matches(normalized, "[a-z0-9_.-]+").Select(match => match.Value).ToHashSet(StringComparer.Ordinal);
        string? blocked = words.Order(StringComparer.Ordinal).FirstOrDefault(word => DeniedWords.Contains(word) || word.StartsWith("mkfs.", StringComparison.Ordinal));
        if (blocked is not null) return $"'{blocked[..Math.Min(blocked.Length, 80)]}' is denied";
        if (words.Contains("find") && words.Contains("-delete")) return "'find -delete' is denied";
        if (words.Contains("git") && (words.Contains("clean") || words.Contains("restore") ||
            (words.Contains("reset") && words.Contains("--hard")) || (words.Contains("checkout") && words.Contains("--"))))
            return "destructive git command is denied";
        if (string.Concat(normalized.Where(character => !char.IsWhiteSpace(character))).Contains(":(){:|:&};:", StringComparison.Ordinal))
            return "fork bomb is denied";
        return null;
    }

    /// <summary>Captures bounded output and joins process cleanup before returning or throwing.</summary>
    public static async Task<string> RunAsync(string command, CancellationToken cancellationToken = default)
    {
        Json.Text(command, nameof(command));
        if (command.Length == 0 || command.Contains('\0'))
            throw new ArgumentException("Bash command must be non-empty and contain no NUL.", nameof(command));
        cancellationToken.ThrowIfCancellationRequested();
        if (DeniedReason(command) is { } reason)
            return $"exit_code: 126\nstdout:\n\nstderr:\nblocked: {reason}\n";
        if (!OperatingSystem.IsLinux() && !OperatingSystem.IsMacOS())
            throw new PlatformNotSupportedException("Bash execution requires Linux or macOS.");
        using var process = new Process
        {
            StartInfo = new ProcessStartInfo("bash")
            {
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false
            }
        };
        process.StartInfo.ArgumentList.Add("--noprofile");
        process.StartInfo.ArgumentList.Add("--norc");
        process.StartInfo.ArgumentList.Add("-c");
        process.StartInfo.ArgumentList.Add(Supervisor);
        process.StartInfo.ArgumentList.Add("tinyagent-bash");
        process.StartInfo.ArgumentList.Add(command);
        // Ambient Bash startup code must not alter the supervisor's traps or job control.
        foreach (string name in new[] { "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS" })
            process.StartInfo.Environment.Remove(name);
        process.Start();
        process.StandardInput.Close();
        using var stop = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        Exception? signalFailure = null;
        var registration = stop.Token.Register(() =>
        {
            try
            {
                if (!process.HasExited && Kill(process.Id, 15) != 0 && Marshal.GetLastPInvokeError() != 3)
                    throw new Win32Exception(Marshal.GetLastPInvokeError(), "Could not cancel Bash.");
            }
            catch (Exception error) { signalFailure = error; }
        });
        using var stdout = new MemoryStream();
        using var stderr = new MemoryStream();
        var gate = new object();
        int bytes = 0;
        bool overflow = false;
        async Task Capture(Stream stream, MemoryStream destination)
        {
            byte[] buffer = new byte[8192];
            try
            {
                while (true)
                {
                    int count = await stream.ReadAsync(buffer).ConfigureAwait(false);
                    if (count == 0) return;
                    bool exceeded;
                    lock (gate)
                    {
                        exceeded = count > MaxOutputBytes - bytes;
                        if (exceeded) overflow = true;
                        if (!overflow)
                        {
                            bytes += count;
                            destination.Write(buffer, 0, count);
                        }
                    }
                    if (exceeded) await stop.CancelAsync().ConfigureAwait(false);
                }
            }
            catch
            {
                await stop.CancelAsync().ConfigureAwait(false);
                throw;
            }
        }
        try
        {
            await Task.WhenAll(Capture(process.StandardOutput.BaseStream, stdout), Capture(process.StandardError.BaseStream, stderr)).ConfigureAwait(false);
        }
        finally
        {
            try { await process.WaitForExitAsync().ConfigureAwait(false); }
            finally { await registration.DisposeAsync().ConfigureAwait(false); }
        }
        cancellationToken.ThrowIfCancellationRequested();
        if (signalFailure is not null) throw signalFailure;
        string observation = $"exit_code: {process.ExitCode}\nstdout:\n{Encoding.UTF8.GetString(stdout.GetBuffer(), 0, (int)stdout.Length)}stderr:\n{Encoding.UTF8.GetString(stderr.GetBuffer(), 0, (int)stderr.Length)}";
        if (overflow || Encoding.UTF8.GetByteCount(observation) > MaxOutputBytes)
            throw new IOException($"Tool output exceeds {MaxOutputBytes} UTF-8 bytes.");
        return observation;
    }

    [DllImport("libc", EntryPoint = "kill", SetLastError = true)]
    private static extern int Kill(int pid, int signal);
}
