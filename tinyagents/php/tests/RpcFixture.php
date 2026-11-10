<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\Agent;
use TinyAgent\CancellationToken;
use TinyAgent\ProviderRequest;
use TinyAgent\ProviderTurn;
use TinyAgent\Rpc;
use TinyAgent\RpcException;
use TinyAgent\Tool;

require getenv('TINYAGENT_PHP_AUTOLOAD') ?: __DIR__ . '/../vendor/autoload.php';
require __DIR__ . '/FixtureClient.php';
require __DIR__ . '/ProviderChecks.php';
require __DIR__ . '/CoreChecks.php';

pcntl_alarm(12);
$pipeHandler = pcntl_signal_get_handler(SIGPIPE);
$interruptHandler = pcntl_signal_get_handler(SIGINT);
$async = pcntl_async_signals();
$script = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $token): ProviderTurn {
    $prompt = $request->history[array_key_last($request->history)]->text;
    try {
        if ($prompt === 'large_final') {
            return new ProviderTurn(str_repeat('é', 600_000), [], [], []);
        }
        $delta = match ($prompt) {
            'large_delta' => str_repeat('é', 600_000),
            'blocked' => str_repeat('x', 1_000_000),
            default => 'partial',
        };
        $text($delta, $token);
        if ($prompt === 'wait' || $prompt === 'cleanup') {
            while (true) {
                $token->throwIfCancelled();
                \Fiber::suspend();
            }
        }
        if ($prompt === 'fail') {
            throw new \LogicException('fixture failure');
        }
        return new ProviderTurn('history:' . count($request->history), [], [], []);
    } finally {
        if ($prompt === 'cleanup') {
            for ($i = 0; $i < 25; $i++) {
                \Fiber::suspend();
            }
        }
    }
});
$mode = $argv[1] ?? 'script';
$provider = $script;
$client = new FixtureClient();
$responses = [];
if ($mode !== 'script') {
    $anthropic = str_ends_with($mode, 'anthropic');
    foreach ([1, 8192] as $fragment) {
        $kind = str_starts_with($mode, 'tools_') && $fragment === 1 ? 'tool' : 'text';
        $responses[] = $client->enqueue(sse(($anthropic ? 'anthropic_' : 'openai_') . $kind), $fragment);
    }
    $provider = provider($anthropic, $client);
}
$error = null;
try {
    $tools = str_starts_with($mode, 'tools_')
        ? [new Tool('echo', '', (object) ['type' => 'object'], static fn(\stdClass $args): string => $args->text)] : [];
    Rpc::runStdio(new Agent($provider, 'fixture', tools: $tools));
} catch (\Throwable $failure) {
    $error = $failure instanceof RpcException ? $failure->errorCode : get_class($failure);
}
fwrite(STDERR, json_encode([
    'error' => $error,
    'entered' => $script->entered,
    'closed' => $script->closed,
    'responsesClosed' => array_column($responses, 'closed'),
    'signalsRestored' => pcntl_signal_get_handler(SIGPIPE) === $pipeHandler
        && pcntl_signal_get_handler(SIGINT) === $interruptHandler && pcntl_async_signals() === $async,
    'streamsClosed' => !is_resource(STDIN) && !is_resource(STDOUT),
], JSON_THROW_ON_ERROR) . "\n");
exit($error === null ? 0 : 1);
