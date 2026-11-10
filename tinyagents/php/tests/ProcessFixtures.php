<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\CancellationToken;
use TinyAgent\Cooperative;

final class CooperativeTask
{
    public readonly \Fiber $fiber;
    public readonly CancellationToken $token;
    public ?\Throwable $error = null;
    public mixed $value = null;

    public function __construct(callable $work)
    {
        $this->token = new CancellationToken();
        $this->fiber = Cooperative::fiber(function () use ($work): void {
            try {
                $this->value = $work($this->token);
            } catch (\Throwable $error) {
                $this->error = $error;
            }
        });
    }

    public function until(callable $done): void
    {
        $deadline = microtime(true) + 3;
        while (!$done()) {
            if (!$this->fiber->isStarted()) {
                $this->fiber->start();
            } elseif ($this->fiber->isSuspended()) {
                $this->fiber->resume();
            }
            if (microtime(true) >= $deadline) {
                throw new \RuntimeException('Native process fixture did not reach its expected state.');
            }
            usleep(1000);
        }
    }

    public function finish(): void { $this->until(fn(): bool => $this->fiber->isTerminated()); }

    public function close(): void
    {
        $this->token->cancel();
        $this->finish();
    }
}

function withProcessDirectory(callable $work): void
{
    $root = __DIR__ . '/../dist';
    if (!is_dir($root)) {
        mkdir($root);
    }
    $directory = $root . '/process-' . bin2hex(random_bytes(8));
    mkdir($directory);
    try {
        $work($directory);
    } finally {
        foreach (glob($directory . '/*') as $file) {
            unlink($file);
        }
        rmdir($directory);
    }
}

function waitingCommand(string $path): string
{
    return 'sleep 30 & child=$!; printf \'%s\' "$child" > ' . escapeshellarg($path) . '; wait';
}

function processPidReady(string $path): bool
{
    clearstatcache(true, $path);
    return is_file($path) && filesize($path) > 0;
}

function processGone(int $pid): void
{
    $deadline = microtime(true) + 2;
    while (posix_kill($pid, 0)) {
        if (microtime(true) >= $deadline) {
            throw new \RuntimeException("Bash child {$pid} survived cleanup.");
        }
        usleep(1000);
    }
    same(3, posix_get_last_error()); // ESRCH on the supported Linux/macOS platforms.
}
