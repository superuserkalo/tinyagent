<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\Agent;
use TinyAgent\CancellationToken;
use TinyAgent\Cooperative;
use TinyAgent\HttpServer;
use TinyAgent\ProviderRequest;
use TinyAgent\ProviderTurn;
use TinyAgent\Tool;
use TinyAgent\ToolCall;
use TinyAgent\ToolResultMessage;

require getenv('TINYAGENT_PHP_AUTOLOAD') ?: __DIR__ . '/../vendor/autoload.php';
require __DIR__ . '/CoreChecks.php';

pcntl_alarm(20);
$directory = $argv[1];
$pipeHandler = pcntl_signal_get_handler(SIGPIPE);
$interruptHandler = pcntl_signal_get_handler(SIGINT);
$async = pcntl_async_signals();
$server = null;
$provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $token) use ($directory, &$server): ProviderTurn {
    $last = $request->history[array_key_last($request->history)];
    $prompt = $last instanceof ToolResultMessage ? 'tool_result' : $last->text;
    file_put_contents($directory . '/events', json_encode(['entered', $prompt]) . "\n", FILE_APPEND);
    try {
        if ($prompt === 'tool_result') {
            return new ProviderTurn($last->output, [], [], []);
        }
        if ($prompt === 'tool' || $prompt === 'tool_wait') {
            return new ProviderTurn('', [new ToolCall('call', 'echo', (object) ['text' => $prompt === 'tool' ? 'café' : 'wait'])], [], []);
        }
        if ($prompt === 'fail_before') {
            throw new \RuntimeException('fixture failure');
        }
        if ($prompt === 'large_final') {
            return new ProviderTurn(str_repeat('é', 600_000), [], [], []);
        }
        if ($prompt !== 'silent') {
            $text($prompt === 'large_delta' ? str_repeat('é', 600_000) : "partial\ncafé", $token);
        }
        if ($prompt === 'blocked') {
            for ($i = 0; $i < 200; $i++) {
                $text(str_repeat('x', 262_144), $token);
            }
        }
        if ($prompt === 'blocked_final') {
            return new ProviderTurn(str_repeat('x', 1_000_000), [], [], []);
        }
        if ($prompt === 'stop') {
            if ($server->close()) {
                throw new \LogicException('Callback close must request shutdown without waiting');
            }
        }
        if (in_array($prompt, ['wait', 'silent', 'cleanup'], true)) {
            while (true) {
                $token->throwIfCancelled();
                Cooperative::pause();
            }
        }
        if ($prompt === 'fail') {
            throw new \RuntimeException('fixture failure');
        }
        return new ProviderTurn('history:' . count($request->history), [], [], []);
    } finally {
        if ($prompt === 'cleanup') {
            file_put_contents($directory . '/cleanup', 'waiting');
            while (!is_file($directory . '/release')) {
                clearstatcache(true, $directory . '/release');
                Cooperative::pause();
            }
        }
        file_put_contents($directory . '/events', json_encode(['closed', $prompt]) . "\n", FILE_APPEND);
    }
});
try {
    $tool = new Tool('echo', '', (object) ['type' => 'object'], static function (\stdClass $args, CancellationToken $token): string {
        while ($args->text === 'wait') {
            $token->throwIfCancelled();
            Cooperative::pause();
        }
        return $args->text;
    });
    $server = HttpServer::start(new Agent($provider, 'fixture', tools: [$tool]), host: $argv[3] ?? '127.0.0.1', port: 0,
        token: ($argv[2] ?? '') === '' ? null : $argv[2], allowRemote: ($argv[4] ?? '') === 'remote');
    pcntl_signal(SIGINT, static fn() => $server->stop());
    echo $server->url, "\n";
    $server->run();
} finally {
    $server?->close();
    pcntl_signal(SIGINT, $interruptHandler);
    file_put_contents($directory . '/finished', json_encode([
        'entered' => $provider->entered, 'closed' => $provider->closed,
        'signalsRestored' => pcntl_signal_get_handler(SIGPIPE) === $pipeHandler
            && pcntl_signal_get_handler(SIGINT) === $interruptHandler && pcntl_async_signals() === $async,
    ], JSON_THROW_ON_ERROR));
}
