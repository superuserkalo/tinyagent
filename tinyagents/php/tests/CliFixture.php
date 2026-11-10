<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\CancellationToken;
use TinyAgent\Cooperative;
use TinyAgent\ProviderRequest;
use TinyAgent\ProviderTurn;

function cliCommand(string $command, string $id = 'call-1'): \stdClass
{
    return (object) ['type' => 'function_call', 'name' => 'bash', 'call_id' => $id,
        'arguments' => json_encode(['command' => $command], JSON_THROW_ON_ERROR)];
}

function cliAnswer(string $text = 'done', \stdClass ...$items): string
{
    if ($text !== '') {
        $items[] = (object) ['type' => 'message', 'role' => 'assistant',
            'content' => [(object) ['type' => 'output_text', 'text' => $text]]];
    }
    return events((object) ['type' => 'response.completed', 'response' => (object) ['output' => $items]]);
}

if (realpath($_SERVER['SCRIPT_FILENAME'] ?? '') !== __FILE__) {
    return;
}

require_once getenv('TINYAGENT_PHP_CLI') ?: __DIR__ . '/../tiny_agent.php';
require __DIR__ . '/FixtureClient.php';
require __DIR__ . '/ProcessFixtures.php';
require __DIR__ . '/ProviderChecks.php';
require __DIR__ . '/CoreChecks.php';

$mode = $argv[1];
$path = $argv[2];
$statsPath = $argv[3];
$client = new FixtureClient();
$responses = [];
if ($mode === 'bash') {
    $responses[] = $client->enqueue(cliAnswer('', cliCommand(waitingCommand($path))));
} elseif (in_array($mode, ['stdout', 'nonblocking', 'broken'], true)) {
    $responses[] = $client->enqueue(events((object) ['type' => 'response.output_text.delta', 'delta' => str_repeat('x', 524288)])
        . cliAnswer(), 8192);
} elseif ($mode === 'stderr') {
    $responses[] = $client->enqueue(cliAnswer('', cliCommand('printf started > ' . escapeshellarg($path . '.executed')
        . "; printf '" . str_repeat('x', 200000) . "'")), 8192);
} elseif ($mode === 'request') {
    $responses[] = $client->enqueue('');
    $responses[0]->before = static function (CancellationToken $token) use ($path): void {
        file_put_contents($path . '.ready', 'ready');
        while (true) {
            $token->throwIfCancelled();
            if (!Cooperative::pause()) {
                usleep(1000);
            }
        }
    };
} elseif ($mode === 'conversation') {
    $responses[] = $client->enqueue(sse('openai_text'));
    $responses[] = $client->enqueue('');
    $responses[1]->before = static fn() => throw new \RuntimeException('Expected provider failure.');
    $responses[] = $client->enqueue(sse('openai_text'));
}
if ($mode === 'stderr') {
    $responses[0]->before = static fn() => file_put_contents($path . '.ready', 'ready');
}
$native = provider(false, $client, timeout: 10);
$provider = new ScriptedProvider(static function (ProviderRequest $request, ?callable $emit, CancellationToken $token) use (
    $native, $mode, $path,
): ProviderTurn {
    return $native->stream($request, static function (string $text, CancellationToken $signal) use ($emit, $mode, $path): void {
        if (in_array($mode, ['stdout', 'nonblocking', 'broken'], true)) {
            file_put_contents($path . '.ready', 'ready');
        }
        $emit($text, $signal);
        if (in_array($mode, ['stdout', 'nonblocking', 'broken'], true)) {
            file_put_contents($path . '.delivered', 'done');
        }
    }, $token);
});
$previous = static function (): void {};
pcntl_signal(SIGINT, $previous);
pcntl_signal(SIGPIPE, $previous);
pcntl_async_signals(false);
if ($mode === 'nonblocking') {
    stream_set_blocking(STDOUT, false);
}
$blocking = array_map(static fn($stream): bool => stream_get_meta_data($stream)['blocked'], [STDIN, STDOUT, STDERR]);
$status = 1;
$failure = null;
try {
    $status = \TinyAgent\Cli\run($provider);
} catch (\Throwable $error) {
    $failure = get_class($error);
} finally {
    file_put_contents($statsPath, json_encode([
        'signalsRestored' => pcntl_signal_get_handler(SIGINT) === $previous && pcntl_signal_get_handler(SIGPIPE) === $previous && !pcntl_async_signals(),
        'streamsOpen' => is_resource(STDIN) && is_resource(STDOUT) && is_resource(STDERR),
        'blockingRestored' => array_map(static fn($stream): bool => stream_get_meta_data($stream)['blocked'], [STDIN, STDOUT, STDERR]) === $blocking,
        'inputCounts' => array_map(static fn(\TinyAgent\HttpRequest $request): int => count(json_decode($request->body)->input), $client->requests),
        'responsesClosed' => array_all($responses, static fn(FixtureResponse $response): bool => $response->closed),
        'active' => $provider->entered - $provider->closed,
        'failure' => $failure,
    ], JSON_THROW_ON_ERROR));
}
exit($status);
