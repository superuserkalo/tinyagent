<?php
declare(strict_types=1);

namespace TinyAgent\Cli;

require_once getenv('TINYAGENT_PHP_AUTOLOAD') ?: __DIR__ . '/vendor/autoload.php';

use TinyAgent\Agent;
use TinyAgent\AgentCompleted;
use TinyAgent\AgentEvent;
use TinyAgent\Bash;
use TinyAgent\Cancelled;
use TinyAgent\CancellationToken;
use TinyAgent\Cooperative;
use TinyAgent\HttpServer;
use TinyAgent\Json;
use TinyAgent\Message;
use TinyAgent\OpenAIProvider;
use TinyAgent\Provider;
use TinyAgent\Rpc;
use TinyAgent\RpcSignals;
use TinyAgent\TextDelta;
use TinyAgent\Tool;
use TinyAgent\ToolExecutionStarted;
use TinyAgent\ToolExecutionEnded;

const INSTRUCTIONS = "You are TinyAgent, a coding agent in the current directory.\n" .
    "Inspect before changing files. Use bash for shell and file work. Each call is a fresh process.";

function agent(Provider $provider): Agent
{
    $tool = new Tool('bash', 'Run a non-interactive bash command in the current directory.', Json::parseObject(
        '{"type":"object","properties":{"command":{"type":"string","minLength":1}},"required":["command"],"additionalProperties":false}',
    ), static fn(\stdClass $arguments, CancellationToken $token): string => Bash::run($arguments->command, $token));
    return new Agent($provider, getenv('TINYAGENT_MODEL') ?: 'gpt-5.6', INSTRUCTIONS, [$tool]);
}

/** @param list<Message> $history
 *  @param callable(string, CancellationToken): void $onText
 *  @param callable(string, CancellationToken): void $onDiagnostic
 *  @return list<Message>
 */
function turn(
    array $history,
    Provider $provider,
    string $prompt,
    callable $onText,
    callable $onDiagnostic,
    ?CancellationToken $cancellation = null,
): array {
    return agent($provider)->run($prompt, $history,
        static function (AgentEvent $event, CancellationToken $signal) use ($onText, $onDiagnostic): void {
            if ($event instanceof TextDelta) {
                $onText($event->delta, $signal);
            } elseif ($event instanceof ToolExecutionStarted) {
                $command = $event->arguments()->command ?? null;
                if (is_string($command) && $command !== '') {
                    $onDiagnostic("\n$ {$command}\n", $signal);
                }
            } elseif ($event instanceof ToolExecutionEnded && !$signal->isCancelled()) {
                $onDiagnostic($event->output . "\n", $signal);
            } elseif ($event instanceof AgentCompleted) {
                $onText("\n", $signal);
            }
        }, $cancellation)->history;
}

final class ConsoleException extends \RuntimeException {}

function console_io(callable $operation): mixed
{
    set_error_handler(static function (int $severity, string $message): never {
        throw new ConsoleException($message);
    });
    try {
        return $operation();
    } finally {
        restore_error_handler();
    }
}

/** @param resource $stream */
function write_text(mixed $stream, string $text, CancellationToken $cancellation): void
{
    $offset = 0;
    while ($offset < strlen($text)) {
        $cancellation->throwIfCancelled();
        $count = console_io(static fn() => fwrite($stream, substr($text, $offset, 65_536)));
        if ($count === false) {
            throw new ConsoleException('Console output could not be written.');
        }
        $offset += $count;
        if ($count === 0 && !Cooperative::pause()) {
            usleep(10_000);
        }
    }
    if (!console_io(static fn() => fflush($stream))) {
        throw new ConsoleException('Console output could not be flushed.');
    }
    $cancellation->throwIfCancelled();
}

/** @param resource $input */
function read_line(mixed $input, CancellationToken $cancellation): ?string
{
    $line = '';
    while (true) {
        $cancellation->throwIfCancelled();
        $part = console_io(static fn() => fgets($input));
        if ($part === false) {
            if (feof($input)) {
                return $line === '' ? null : $line;
            }
            if (!Cooperative::pause()) {
                usleep(10_000);
            }
            continue;
        }
        $line .= $part;
        if (str_ends_with($part, "\n")) {
            return $line;
        }
    }
}

/** Borrows native streams and restores their blocking modes and process signals.
 *  @param resource $input
 *  @param resource $output
 *  @param resource $error
 */
function run(Provider $provider, mixed $input = STDIN, mixed $output = STDOUT, mixed $error = STDERR,
    ?CancellationToken $cancellation = null): int
{
    $cancellation ??= new CancellationToken();
    $scope = $cancellation->child();
    if (!function_exists('pcntl_signal_get_handler')) {
        throw new \RuntimeException('The coding CLI requires PCNTL for interrupt cleanup.');
    }
    $streams = [];
    foreach ([$input, $output, $error] as $stream) {
        if (!is_resource($stream) || get_resource_type($stream) !== 'stream'
            || !isset(stream_get_meta_data($stream)['blocked'])) {
            throw new \InvalidArgumentException('The coding CLI requires native streams with a blocking mode.');
        }
        $streams[] = [$stream, stream_get_meta_data($stream)['blocked']];
    }
    $signals = new RpcSignals(false);
    $previous = pcntl_signal_get_handler(SIGINT);
    $previousAsync = pcntl_async_signals();
    $interrupted = false;
    $handler = static function () use (&$interrupted, $scope): void {
        $interrupted = true;
        $scope->cancel();
    };
    try {
        if (!pcntl_signal(SIGINT, $handler)) {
            throw new ConsoleException('Could not handle Ctrl-C.');
        }
        pcntl_async_signals(true);
        foreach ($streams as [$stream]) {
            if (!console_io(static fn() => stream_set_blocking($stream, false))) {
                throw new ConsoleException('Could not configure console I/O.');
            }
        }
        $history = [];
        write_text($output, "TinyAgent. Type a task, or Ctrl-C or Ctrl-D to exit.\n", $scope);
        while (true) {
            write_text($output, "\n> ", $scope);
            $line = read_line($input, $scope);
            if ($line === null) {
                break;
            }
            $prompt = trim($line);
            if ($prompt === '') {
                continue;
            }
            try {
                $history = turn($history, $provider, $prompt,
                    static fn(string $text, CancellationToken $token) => write_text($output, $text, $token),
                    static fn(string $text, CancellationToken $token) => write_text($error, $text, $token), $scope);
            } catch (Cancelled|ConsoleException $failure) {
                throw $failure;
            } catch (\Throwable $failure) {
                if (($interruption = Cooperative::interruption($failure)) !== null) {
                    throw $interruption;
                }
                write_text($error, 'error: ' . $failure->getMessage() . "\n", $scope);
            }
        }
        write_text($output, "\n", $scope);
        return 0;
    } catch (Cancelled $failure) {
        if (!$interrupted || $cancellation->isCancelled()) {
            throw $failure;
        }
        return 130;
    } finally {
        $scope->cancel();
        try {
            foreach ($streams as [$stream, $blocking]) {
                if (is_resource($stream)) {
                    console_io(static fn() => stream_set_blocking($stream, $blocking));
                }
            }
        } finally {
            if (pcntl_signal_get_handler(SIGINT) === $handler) {
                pcntl_signal(SIGINT, $previous);
            }
            pcntl_async_signals($previousAsync);
            $signals->close();
        }
    }
}

function self_test(): void
{
    if (Bash::deniedReason("printf 'hello\\n'") !== null) {
        throw new \RuntimeException('safe command was denied');
    }
    foreach (['rm -rf /', 'r""m -rf /', 'sudo echo hi', 'git reset --hard HEAD', 'find . -delete', 'mkfs.ext4 /dev/sda'] as $command) {
        if (Bash::deniedReason($command) === null) {
            throw new \RuntimeException("unsafe command was allowed: {$command}");
        }
    }
    echo 'self-test passed', PHP_EOL;
}

function serve(Agent $agent): int
{
    $port = filter_var(getenv('TINYAGENT_PORT') === false ? '8080' : getenv('TINYAGENT_PORT'), FILTER_VALIDATE_INT);
    if ($port === false) {
        throw new \InvalidArgumentException('TINYAGENT_PORT must be an integer.');
    }
    $server = HttpServer::start($agent, host: getenv('TINYAGENT_HOST') ?: '127.0.0.1', port: $port,
        token: getenv('TINYAGENT_TOKEN') === false ? null : getenv('TINYAGENT_TOKEN'),
        allowRemote: getenv('TINYAGENT_ALLOW_REMOTE') === '1');
    $previous = pcntl_signal_get_handler(SIGINT);
    $interrupted = false;
    $handler = static function () use ($server, &$interrupted): void {
        $interrupted = true;
        $server->stop();
    };
    try {
        if (!pcntl_signal(SIGINT, $handler)) {
            throw new ConsoleException('Could not handle Ctrl-C.');
        }
        fwrite(STDERR, 'TinyAgent listening on ' . $server->url . PHP_EOL);
        $server->run();
        return $interrupted ? 130 : 0;
    } finally {
        try {
            $server->close();
        } finally {
            if (pcntl_signal_get_handler(SIGINT) === $handler) {
                pcntl_signal(SIGINT, $previous);
            }
        }
    }
}

/** @param list<string> $arguments */
function main(array $arguments): int
{
    if (in_array('--self-test', $arguments, true)) {
        self_test();
        return 0;
    }
    $args = array_slice($arguments, 1);
    $provider = new OpenAIProvider();
    if ($args === ['--mode', 'rpc']) {
        Rpc::runStdio(agent($provider));
        return 0;
    }
    if ($args === ['serve']) {
        return serve(agent($provider));
    }
    if ($args !== []) {
        throw new \InvalidArgumentException('Usage: tinyagent [--self-test | --mode rpc | serve]');
    }
    return run($provider);
}

if (realpath($_SERVER['SCRIPT_FILENAME'] ?? '') === __FILE__) {
    try {
        exit(main($argv));
    } catch (ConsoleException) {
        exit(1);
    } catch (\Throwable $error) {
        fwrite(STDERR, $error->getMessage() . PHP_EOL);
        exit(1);
    }
}
