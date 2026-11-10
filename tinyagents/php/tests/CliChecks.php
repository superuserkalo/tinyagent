<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\Cancelled;
use TinyAgent\CancellationToken;
use TinyAgent\Cooperative;
use TinyAgent\Json;
use TinyAgent\ProviderException;
use TinyAgent\ProviderRequest;
use TinyAgent\ProviderTurn;
use TinyAgent\UserMessage;
use function TinyAgent\Cli\run;
use function TinyAgent\Cli\turn;

require_once getenv('TINYAGENT_PHP_CLI') ?: __DIR__ . '/../tiny_agent.php';
require_once __DIR__ . '/CliFixture.php';

function cliIgnore(string $text, CancellationToken $token): void { $token->throwIfCancelled(); }

function cliStreaming(): void
{
    withProcessDirectory(static function (string $directory): void {
        $path = escapeshellarg($directory . '/order');
        $client = new FixtureClient();
        $first = $client->enqueue(events((object) ['type' => 'response.output_text.delta', 'delta' => 'working '])
            . cliAnswer('', (object) ['type' => 'reasoning', 'encrypted_content' => 'opaque'],
                cliCommand("printf first > {$path}", 'first'), cliCommand("cat {$path}; printf 'café\\n'", 'second')));
        $second = $client->enqueue(sse('openai_text'));
        $earlier = new UserMessage('earlier');
        $previous = [&$earlier];
        $text = $diagnostic = '';
        $history = turn($previous, provider(false, $client), 'go',
            static function (string $delta) use (&$text): void { $text .= $delta; },
            static function (string $delta) use (&$diagnostic): void { $diagnostic .= $delta; });
        same(6, count($history));
        same(1, count($previous));
        same("working done\ncafé\n", $text);
        same(true, str_contains($diagnostic, "firstcafé\n"));
        same(true, $first->closed && $second->closed);
        $request = json_decode($client->requests[0]->body);
        same(true, $request->stream);
        same(false, $request->store);
        same(false, $request->parallel_tool_calls);
        same('bash', $request->tools[0]->name);
        same(['command'], $request->tools[0]->parameters->required);
        same(false, $request->tools[0]->parameters->additionalProperties);
        $input = json_decode($client->requests[1]->body)->input;
        same('opaque', $input[2]->encrypted_content);
        same('second', $input[6]->call_id);
        same("exit_code: 0\nstdout:\nfirstcafé\nstderr:\n", $input[6]->output);
        $earlier = new UserMessage('changed');
        same('earlier', $history[0]->text);
        $replay = $history[2]->replay();
        $replay[0]->encrypted_content = 'changed';
        same('opaque', $history[2]->replay()[0]->encrypted_content);
        raises(\Error::class, static function () use ($history): void { $history[2]->toolCalls = []; });
    });
}

function cliBashObservations(): void
{
    foreach ([
        'printf hello; printf problem >&2; exit 7' => "exit_code: 7\nstdout:\nhellostderr:\nproblem",
        'rm forbidden' => "exit_code: 126\nstdout:\n\nstderr:\nblocked: 'rm' is denied\n",
        'yes x' => 'Tool failed: Tool output exceeds 131072 UTF-8 bytes',
        "\0" => 'Tool failed: Bash command must be nonempty text without NUL.',
    ] as $command => $expected) {
        $client = new FixtureClient();
        $client->enqueue(cliAnswer('', cliCommand($command), cliCommand('printf recovered', 'later')));
        $client->enqueue(cliAnswer());
        $history = turn([], provider(false, $client), 'run', cliIgnore(...), cliIgnore(...));
        same($expected, $history[2]->output);
        same(str_starts_with($expected, 'Tool failed:'), $history[2]->isError);
        same(true, str_contains($history[3]->output, 'recovered'));
    }
    foreach (['{}', '{"command":3}', '{"command":""}', '{"command":"exit 99","extra":true}'] as $arguments) {
        $client = new FixtureClient();
        $call = cliCommand('unused');
        $call->arguments = $arguments;
        $client->enqueue(cliAnswer('', $call));
        $client->enqueue(cliAnswer());
        $diagnostics = [];
        $history = turn([], provider(false, $client), 'run', cliIgnore(...),
            static function (string $text) use (&$diagnostics): void { $diagnostics[] = $text; });
        same(true, $history[2]->isError);
        same("Tool failed: Invalid arguments for tool 'bash'.", $history[2]->output);
        same(str_contains($arguments, 'exit 99') ? 2 : 1, count($diagnostics));
    }
}

function cliFailures(): void
{
    $previous = [new UserMessage('earlier')];
    foreach (['provider', 'malformed', 'bounds', 'unknown'] as $phase) {
        $client = new FixtureClient();
        if ($phase === 'provider') {
            $client->enqueue(cliAnswer('', cliCommand('printf partial')));
            $response = $client->enqueue('');
            $response->before = static fn() => throw new \RuntimeException('Provider failed.');
        } elseif ($phase === 'malformed') {
            $response = $client->enqueue("data: {\n\n");
        } elseif ($phase === 'unknown') {
            $call = cliCommand('exit 0');
            $call->name = 'unknown';
            $response = $client->enqueue(cliAnswer('', $call));
        } else {
            $response = $client->enqueue('data: ' . str_repeat('x', Json::MAX_BYTES + 1), 8192);
        }
        raises(ProviderException::class, static fn() => turn($previous, provider(false, $client), 'fail', cliIgnore(...), cliIgnore(...)));
        same(1, count($previous));
        same('earlier', $previous[0]->text);
        same(true, $response->closed);
    }
}

function cliConsumerFailures(): void
{
    foreach (['text', 'diagnostic', 'observation'] as $phase) {
        $client = new FixtureClient();
        $response = $client->enqueue($phase === 'text' ? sse('openai_text') : cliAnswer('', cliCommand('exit 0')));
        $failure = new \RuntimeException('consumer failed');
        $calls = 0;
        $broken = static function () use ($failure, &$calls, $phase): void {
            $calls++;
            if ($phase !== 'observation' || $calls === 2) {
                throw $failure;
            }
        };
        same($failure, raises(\RuntimeException::class, static fn() => turn([], provider(false, $client), 'fail',
            $phase === 'text' ? $broken : cliIgnore(...), $phase !== 'text' ? $broken : cliIgnore(...))));
        same(true, $response->closed);
    }
    foreach ([true, false] as $wrap) {
        $failure = new \RuntimeException('original');
        $provider = new ScriptedProvider(static function ($request, $emit, $token) use ($wrap): ProviderTurn {
            try {
                $emit('text', $token);
            } catch (\Throwable $error) {
                if ($wrap) {
                    throw new ProviderException('wrapped', previous: $error);
                }
            }
            return new ProviderTurn('done', [], [], []);
        });
        same($failure, raises(\RuntimeException::class, static fn() => turn([], $provider, 'fail',
            static fn() => throw $failure, cliIgnore(...))));
    }
}

function cliCancellation(): void
{
    withProcessDirectory(static function (string $directory): void {
        $path = $directory . '/pid';
        foreach (['request', 'request_throw', 'cleanup', 'bash', 'throw'] as $phase) {
            $previous = [new UserMessage('keep')];
            $client = new FixtureClient();
            $response = $client->enqueue(in_array($phase, ['bash', 'throw'], true) ? cliAnswer('', cliCommand(waitingCommand($path))) : cliAnswer());
            $entered = false;
            if (in_array($phase, ['request', 'request_throw'], true)) {
                $response->before = static function (CancellationToken $token) use (&$entered): void {
                    $entered = true;
                    while (true) {
                        $token->throwIfCancelled();
                        Cooperative::pause();
                    }
                };
            } elseif ($phase === 'cleanup') {
                $response->cleanup = static fn(CancellationToken $token) => $token->cancel();
            }
            $task = new CooperativeTask(static fn(CancellationToken $token): array => turn($previous, provider(false, $client),
                'cancel', cliIgnore(...), cliIgnore(...), $token));
            try {
                if (in_array($phase, ['bash', 'throw'], true)) {
                    $task->until(static fn(): bool => processPidReady($path));
                } elseif (in_array($phase, ['request', 'request_throw'], true)) {
                    $task->until(static function () use (&$entered): bool { return $entered; });
                }
                if (in_array($phase, ['throw', 'request_throw'], true)) {
                    $failure = new \RuntimeException('caller interrupted');
                    $task->fiber->throw($failure);
                    $task->finish();
                    same(true, $failure === $task->error);
                } else {
                    if ($phase !== 'cleanup') {
                        $task->token->cancel();
                    }
                    $task->finish();
                    same(Cancelled::class, get_class($task->error));
                }
                same(1, count($previous));
                same(true, $response->closed);
                if (in_array($phase, ['bash', 'throw'], true)) {
                    processGone((int) file_get_contents($path));
                    unlink($path);
                }
            } finally {
                $task->close();
            }
        }
    });
}

function cliTerminal(): void
{
    foreach ([false, true] as $cancel) {
        $client = new FixtureClient();
        $response = $client->enqueue(cliAnswer('terminal café'));
        $token = new CancellationToken();
        $text = '';
        $emit = static function (string $delta, CancellationToken $signal) use (&$text, $cancel): void {
            $text .= $delta;
            if ($cancel && $delta === "\n") {
                $signal->cancel();
            }
        };
        if ($cancel) {
            raises(Cancelled::class, static fn() => turn([], provider(false, $client), 'go', $emit, cliIgnore(...), $token));
        } else {
            same(2, count(turn([], provider(false, $client), 'go', $emit, cliIgnore(...), $token)));
        }
        same("terminal café\n", $text);
        same(true, $response->closed);
        same(false, $token->isCancelled());
    }
    $retained = null;
    $provider = new ScriptedProvider(static function ($request, $emit, $token) use (&$retained): ProviderTurn {
        $retained = static fn() => $emit('late', $token);
        return new ProviderTurn('done', [], [], []);
    });
    turn([], $provider, 'go', cliIgnore(...), cliIgnore(...));
    raises(Cancelled::class, $retained);
    $provider = new ScriptedProvider(static function ($request, $emit, $token): ProviderTurn {
        $token->cancel();
        return new ProviderTurn('late', [], [], []);
    });
    raises(Cancelled::class, static fn() => turn([], $provider, 'go', cliIgnore(...), cliIgnore(...)));
}

function cliBorrowedStreams(): void
{
    withProcessDirectory(static function (string $directory): void {
        $streams = array_map(static fn(string $name) => fopen($directory . '/' . $name, 'w+'), ['in', 'out', 'err']);
        [$input, $output, $error] = $streams;
        fwrite($input, "first\nfail\nlast");
        rewind($input);
        $counts = [];
        $provider = new ScriptedProvider(static function (ProviderRequest $request) use (&$counts): ProviderTurn {
            $counts[] = count($request->history);
            if ($request->history[array_key_last($request->history)]->text === 'fail') {
                throw new ProviderException('Expected failure');
            }
            return new ProviderTurn('done', [], [], []);
        });
        set_error_handler(static fn() => throw new \RuntimeException('caller handler'));
        try {
            same(0, run($provider, $input, $output, $error));
            same([1, 3, 3], $counts);
            same($provider->entered, $provider->closed);
            foreach ($streams as $stream) {
                same(true, is_resource($stream));
                same(true, stream_get_meta_data($stream)['blocked']);
            }
            rewind($output);
            same(2, substr_count(stream_get_contents($output), "done\n"));
            $token = new CancellationToken();
            $token->cancel();
            raises(Cancelled::class, static fn() => run($provider, $input, $output, $error, $token));
            rewind($input);
            $waiting = new ScriptedProvider(static function (): never {
                while (true) {
                    Cooperative::pause();
                }
            });
            $task = new CooperativeTask(static fn(CancellationToken $signal): int => run($waiting, $input, $output, $error, $signal));
            try {
                $task->until(static fn(): bool => $waiting->entered > 0);
                $interruption = new \RuntimeException('stop console');
                $task->fiber->throw($interruption);
                $task->finish();
                same(true, $task->error === $interruption);
                same($waiting->entered, $waiting->closed);
                foreach ($streams as $stream) {
                    same(true, stream_get_meta_data($stream)['blocked']);
                }
            } finally {
                $task->close();
            }
            raises(\RuntimeException::class, static fn() => trigger_error('probe'), 'caller handler');
        } finally {
            restore_error_handler();
            foreach ($streams as $stream) {
                fclose($stream);
            }
        }
    });
}

/** @param list<string> $modes */
function cliInterrupt(array $modes): void
{
    withProcessDirectory(static function (string $directory) use ($modes): void {
        foreach ($modes as $mode) {
            $path = $directory . '/' . $mode . '-pid';
            $statsPath = $directory . '/' . $mode . '-stats';
            $environment = getenv();
            unset($environment['OPENAI_API_KEY'], $environment['ANTHROPIC_API_KEY']);
            $process = proc_open([PHP_BINARY, __DIR__ . '/CliFixture.php', $mode, $path, $statsPath],
                [['pipe', 'r'], ['pipe', 'w'], ['pipe', 'w']], $pipes, env_vars: $environment);
            same(true, is_resource($process));
            foreach ($pipes as $pipe) {
                stream_set_blocking($pipe, false);
            }
            $child = null;
            $output = $errors = '';
            $stalled = in_array($mode, ['stdout', 'nonblocking', 'stderr', 'broken'], true);
            try {
                if (!in_array($mode, ['idle', 'conversation'], true)) {
                    fwrite($pipes[0], "wait\n");
                }
                $deadline = microtime(true) + 5;
                do {
                    if (!$stalled) {
                        $output .= stream_get_contents($pipes[1]);
                        $errors .= stream_get_contents($pipes[2]);
                    }
                    $ready = $mode === 'bash' ? processPidReady($path)
                        : (in_array($mode, ['idle', 'conversation'], true) ? str_contains($output, '> ') : processPidReady($path . '.ready'));
                    if (microtime(true) >= $deadline) {
                        throw new \RuntimeException('CLI did not reach its expected state: ' . $errors);
                    }
                    usleep(1000);
                } while (!$ready);
                if ($mode === 'bash') {
                    $child = (int) file_get_contents($path);
                }
                if ($stalled) {
                    usleep(100000);
                    same(false, is_file($path . '.delivered'));
                    same(false, is_file($path . '.executed'));
                    same(true, proc_get_status($process)['running']);
                }
                if ($mode === 'conversation') {
                    fwrite($pipes[0], "\nfirst\nfail\nlast");
                    fclose($pipes[0]);
                } elseif ($mode === 'broken') {
                    fclose($pipes[1]);
                } else {
                    same(true, proc_terminate($process, SIGINT));
                }
                $deadline = microtime(true) + 5;
                do {
                    if (!$stalled) {
                        $output .= stream_get_contents($pipes[1]);
                        $errors .= stream_get_contents($pipes[2]);
                    }
                    $status = proc_get_status($process);
                    if (microtime(true) >= $deadline) {
                        throw new \RuntimeException('CLI did not finish cleanup: ' . $errors);
                    }
                    usleep(1000);
                } while ($status['running']);
                same($mode === 'conversation' ? 0 : ($mode === 'broken' ? 1 : 130), $status['exitcode']);
                $stats = json_decode(file_get_contents($statsPath), flags: JSON_THROW_ON_ERROR);
                same(true, $stats->signalsRestored);
                same(true, $stats->streamsOpen);
                same(true, $stats->blockingRestored);
                same(true, $stats->responsesClosed);
                same(0, $stats->active);
                same($mode === 'broken' ? \TinyAgent\Cli\ConsoleException::class : null, $stats->failure);
                if ($mode === 'conversation') {
                    same([1, 3, 3], $stats->inputCounts);
                }
                same(false, str_contains($errors, 'Warning'));
                if ($child !== null) {
                    processGone($child);
                }
            } finally {
                if (proc_get_status($process)['running']) {
                    if ($child !== null) {
                        posix_kill($child, SIGKILL);
                    }
                    proc_terminate($process, SIGKILL);
                }
                foreach ($pipes as $pipe) {
                    if (is_resource($pipe)) {
                        fclose($pipe);
                    }
                }
                proc_close($process);
            }
        }
    });
}
