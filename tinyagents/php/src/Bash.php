<?php
declare(strict_types=1);

namespace TinyAgent;

/** Optional guarded Bash execution on Linux and macOS. The blacklist is not a sandbox. */
final class Bash
{
    public const MAX_OUTPUT_BYTES = 131_072;
    private const DENIED_WORDS = [
        'rm', 'rmdir', 'unlink', 'shred', 'truncate', 'dd', 'mkfs', 'wipefs', 'fdisk', 'parted',
        'sudo', 'doas', 'shutdown', 'reboot', 'poweroff', 'halt', 'kill', 'pkill', 'killall',
    ];
    // A live supervisor owns the command's process group through cancellation and normal exit.
    private const SUPERVISOR = <<<'BASH'
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
        BASH;

    public static function deniedReason(string $command): ?string
    {
        $normalized = str_replace(['\\', '"', "'"], '', strtolower($command));
        preg_match_all('/[a-z0-9_.-]+/', $normalized, $matches);
        $words = array_fill_keys($matches[0], true);
        $blocked = array_values(array_filter(array_keys($words),
            static fn(string $word): bool => in_array($word, self::DENIED_WORDS, true) || str_starts_with($word, 'mkfs.')));
        sort($blocked);
        if ($blocked !== []) {
            return "'" . substr($blocked[0], 0, 80) . "' is denied";
        }
        if (isset($words['find'], $words['-delete'])) {
            return "'find -delete' is denied";
        }
        if (isset($words['git']) && (isset($words['clean']) || isset($words['restore'])
            || isset($words['reset'], $words['--hard']) || isset($words['checkout'], $words['--']))) {
            return 'destructive git command is denied';
        }
        if (str_contains((string) preg_replace('/\s/', '', $normalized), ':(){:|:&};:')) {
            return 'fork bomb is denied';
        }
        return null;
    }

    public static function run(string $command, ?CancellationToken $cancellation = null): string
    {
        Json::text($command, 'Bash command');
        if ($command === '' || str_contains($command, "\0")) {
            throw new \InvalidArgumentException('Bash command must be nonempty text without NUL.');
        }
        $cancellation ??= new CancellationToken();
        $cancellation->throwIfCancelled();
        if (($reason = self::deniedReason($command)) !== null) {
            return "exit_code: 126\nstdout:\n\nstderr:\nblocked: {$reason}\n";
        }
        if (!in_array(PHP_OS_FAMILY, ['Linux', 'Darwin'], true)) {
            throw new \RuntimeException('Bash execution requires Linux or macOS.');
        }
        $environment = getenv();
        foreach (['BASH_ENV', 'ENV', 'SHELLOPTS', 'BASHOPTS'] as $name) {
            unset($environment[$name]);
        }
        $process = null;
        $pipes = [];
        $output = [1 => '', 2 => ''];
        $failure = null;
        $exitCode = -1;
        try {
            $process = proc_open(['bash', '--noprofile', '--norc', '-c', self::SUPERVISOR, 'tinyagent-bash', $command],
                [0 => ['file', '/dev/null', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes, env_vars: $environment);
            if (!is_resource($process)) {
                throw new \RuntimeException('Could not start Bash.');
            }
            foreach ($pipes as $pipe) {
                if (!stream_set_blocking($pipe, false)) {
                    throw new \RuntimeException('Could not configure Bash output.');
                }
            }
            $readers = $pipes;
            $bytes = 0;
            do {
                $cancellation->throwIfCancelled();
                $status = proc_get_status($process);
                $read = false;
                foreach ($readers as $number => $pipe) {
                    $chunk = fread($pipe, 8192);
                    if ($chunk === false) {
                        throw new \RuntimeException('Could not read Bash output.');
                    }
                    $bytes += strlen($chunk);
                    if ($bytes > self::MAX_OUTPUT_BYTES) {
                        throw new \RuntimeException('Tool output exceeds 131072 UTF-8 bytes');
                    }
                    $output[$number] .= $chunk;
                    $read = $read || $chunk !== '';
                    if (feof($pipe)) {
                        unset($readers[$number]);
                    }
                }
                if (!$status['running'] && $readers === []) {
                    $exitCode = $status['exitcode'];
                    break;
                }
                if (!Cooperative::pause() && !$read) {
                    usleep(10_000);
                }
            } while (true);
        } catch (\Throwable $error) {
            $failure = $error;
        } finally {
            try {
                if (is_resource($process) && proc_get_status($process)['running']) {
                    proc_terminate($process);
                    while (proc_get_status($process)['running']) {
                        try {
                            if (!Cooperative::pause()) {
                                usleep(10_000);
                            }
                        } catch (\Throwable $error) {
                            // A second throw into the owner fiber must not abandon cleanup.
                            $failure ??= $error;
                        }
                    }
                }
            } finally {
                foreach ($pipes as $pipe) {
                    if (is_resource($pipe)) {
                        fclose($pipe);
                    }
                }
                if (is_resource($process)) {
                    proc_close($process);
                }
            }
        }
        if ($failure !== null) {
            throw $failure;
        }
        $cancellation->throwIfCancelled();
        $observation = "exit_code: {$exitCode}\nstdout:\n" . self::utf8($output[1]) . "stderr:\n" . self::utf8($output[2]);
        if (strlen($observation) > self::MAX_OUTPUT_BYTES) {
            throw new \RuntimeException('Tool output exceeds 131072 UTF-8 bytes');
        }
        return $observation;
    }

    private static function utf8(string $bytes): string
    {
        return json_decode(json_encode($bytes, JSON_THROW_ON_ERROR | JSON_INVALID_UTF8_SUBSTITUTE), flags: JSON_THROW_ON_ERROR);
    }
}
