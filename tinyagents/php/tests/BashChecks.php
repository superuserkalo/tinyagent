<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\Bash;
use TinyAgent\Cancelled;
use TinyAgent\CancellationToken;

function bashGuard(): void
{
    foreach (['rm -rf /', 'r""m -rf /', 'sudo echo hi', 'git reset --hard HEAD', 'git restore file',
        'git checkout -- file', 'git clean -fd', 'find . -delete', 'mkfs.ext4 /dev/sda', ':(){ :|:& };:'] as $command) {
        same(true, Bash::deniedReason($command) !== null);
        same(true, str_starts_with(Bash::run($command), "exit_code: 126\n"));
    }
    same(null, Bash::deniedReason("printf hello; git status"));
    foreach (['', "\0", "\xff"] as $invalid) {
        raises(\InvalidArgumentException::class, static fn() => Bash::run($invalid));
    }
    $token = new CancellationToken();
    $token->cancel();
    raises(Cancelled::class, static fn() => Bash::run('exit 0', $token));
    same(true, strlen(Bash::run('mkfs.' . str_repeat('x', 200_000))) < 200);
}

function bashOutput(): void
{
    same("exit_code: 7\nstdout:\nhello\ncaféstderr:\nproblem\n",
        Bash::run("printf 'hello\\ncafé'; printf 'problem\\n' >&2; exit 7"));
    $literal = '$HOME `ignored` \'quoted\'';
    same("exit_code: 0\nstdout:\n{$literal}stderr:\n", Bash::run("printf '%s' " . escapeshellarg($literal)));
    same("exit_code: 0\nstdout:\neofstderr:\n", Bash::run('if read line; then printf input; else printf eof; fi'));
    same("exit_code: 0\nstdout:\n�stderr:\n", Bash::run("printf '\\377'"));
}

function bashBounds(): void
{
    $count = Bash::MAX_OUTPUT_BYTES - strlen("exit_code: 0\nstdout:\nstderr:\n");
    same(Bash::MAX_OUTPUT_BYTES, strlen(Bash::run("head -c {$count} /dev/zero | tr '\\000' x")));
    foreach (["head -c " . ($count + 1) . " /dev/zero | tr '\\000' x",
        'head -c 70000 /dev/zero; head -c 70000 /dev/zero >&2', 'yes x'] as $command) {
        $task = new CooperativeTask(static fn(CancellationToken $token): string => Bash::run($command, $token));
        try {
            $task->finish();
            same(\RuntimeException::class, get_class($task->error));
            same('Tool output exceeds 131072 UTF-8 bytes', $task->error->getMessage());
        } finally {
            $task->close();
        }
    }
}

function bashCancellation(bool $interrupt): void
{
    withProcessDirectory(static function (string $directory) use ($interrupt): void {
        $path = $directory . '/pid';
        $task = new CooperativeTask(static fn(CancellationToken $token): string => Bash::run(waitingCommand($path), $token));
        try {
            $task->until(static fn(): bool => processPidReady($path));
            $child = (int) file_get_contents($path);
            $failure = new \LogicException('Caller stopped Bash.');
            if ($interrupt) {
                $task->fiber->throw($failure);
                if ($task->fiber->isSuspended()) {
                    $task->fiber->throw(new \LogicException('Second cleanup interruption.'));
                }
            } else {
                $task->token->cancel();
            }
            $task->finish();
            if ($interrupt) {
                same($failure, $task->error);
            } else {
                same(Cancelled::class, get_class($task->error));
            }
            processGone($child);
        } finally {
            $task->close();
        }
    });
}

function bashOwnership(): void
{
    withProcessDirectory(static function (string $directory): void {
        $release = $directory . '/release';
        $path = $directory . '/pid';
        $task = new CooperativeTask(static fn(CancellationToken $token): string => Bash::run(
            'until test -f ' . escapeshellarg($release) . '; do sleep 0.01; done; printf independent', $token));
        try {
            $task->fiber->start();
            bashCancellation(false);
            same(true, $task->fiber->isSuspended());
            file_put_contents($release, 'ready');
            $task->finish();
            same(null, $task->error);
            same("exit_code: 0\nstdout:\nindependentstderr:\n", $task->value);
            Bash::run('sleep 30 & child=$!; printf \'%s\' "$child" > ' . escapeshellarg($path));
            processGone((int) file_get_contents($path));
        } finally {
            $task->close();
        }
    });
}

function bashCooperativeScope(): void
{
    $fiber = new \Fiber(static fn(): string => Bash::run('printf synchronous'));
    $fiber->start();
    same(true, $fiber->isTerminated());
    same("exit_code: 0\nstdout:\nsynchronousstderr:\n", $fiber->getReturn());
    $task = new CooperativeTask(static fn(CancellationToken $token): string => Bash::run('sleep 30', $token));
    try {
        $task->fiber->start();
        same(true, $task->fiber->isSuspended());
    } finally {
        $task->close();
    }
    same(Cancelled::class, get_class($task->error));
}
