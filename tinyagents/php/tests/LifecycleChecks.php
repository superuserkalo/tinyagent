<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\AnthropicProvider;
use TinyAgent\Cancelled;
use TinyAgent\CancellationToken;
use TinyAgent\OpenAIProvider;
use TinyAgent\ProviderException;

function cancellation(): void
{
    foreach ([false, true] as $anthropic) {
        foreach (['client', 'callback', 'cleanup'] as $phase) {
            $client = new FixtureClient();
            $response = $client->enqueue(sse($anthropic ? 'anthropic_text' : 'openai_text'));
            $parent = new CancellationToken();
            if ($phase === 'client') {
                $response->before = static function (CancellationToken $scope) use ($parent): void {
                    $parent->cancel();
                    $scope->throwIfCancelled();
                };
            } elseif ($phase === 'cleanup') {
                $response->cleanup = static fn() => $parent->cancel();
            }
            $callback = static function () use ($phase, $parent): void {
                if ($phase === 'callback') {
                    $parent->cancel();
                }
            };
            raises(Cancelled::class, static fn() => provider($anthropic, $client)->stream(request(), $callback, $parent));
            same(true, $response->closed);
        }
    }
}

function deadlines(): void
{
    foreach ([false, true] as $anthropic) {
        foreach (['client', 'callback', 'callback_failure', 'cleanup'] as $phase) {
            $client = new FixtureClient();
            $response = $client->enqueue(sse($anthropic ? 'anthropic_text' : 'openai_text'));
            $wait = static function (CancellationToken $scope): void {
                $limit = hrtime(true) + 1_000_000_000;
                while (!$scope->isCancelled() && hrtime(true) < $limit) {
                    usleep(1000);
                }
                same(true, $scope->isCancelled());
            };
            if ($phase === 'client') {
                $response->before = $wait;
            } elseif ($phase === 'cleanup') {
                $response->cleanup = $wait;
            }
            $callback = static function (string $text, CancellationToken $scope) use ($phase, $wait): void {
                if (str_starts_with($phase, 'callback')) {
                    $wait($scope);
                    if ($phase === 'callback_failure') {
                        throw new \LogicException('Consumer failed after deadline.');
                    }
                }
            };
            $parent = new CancellationToken();
            raises(ProviderException::class, static fn() => provider($anthropic, $client, 0.04)->stream(request(), $callback, $parent), 'timed out');
            same(true, $response->closed);
            same(false, $parent->isCancelled());
        }
    }
}

function callbackFailure(): void
{
    foreach ([false, true] as $anthropic) {
        $client = new FixtureClient();
        $response = $client->enqueue(sse($anthropic ? 'anthropic_text' : 'openai_text'));
        $failure = new \LogicException('consumer stopped');
        $error = raises(ProviderException::class, static fn() => provider($anthropic, $client)->stream(
            request(), static fn() => throw $failure,
        ));
        same($failure, $error->getPrevious());
        same(true, $response->closed);
    }
}

function completedCleanup(): void
{
    foreach ([false, true] as $anthropic) {
        $client = new FixtureClient();
        $body = sse($anthropic ? 'anthropic_text' : 'openai_text');
        $response = $client->enqueue($body . "data: invalid trailing JSON\n\n", strlen($body) + 100);
        $cleaned = false;
        $response->cleanup = static function () use (&$cleaned): void {
            $cleaned = true;
        };
        $turn = provider($anthropic, $client)->stream(request());
        same("done\ncafé", $turn->text);
        same(true, $cleaned);
        same(true, $response->closed);
        same(1, $response->deliveries);
    }
}

function credentialsAndOptions(): void
{
    foreach ([false, true] as $anthropic) {
        $name = $anthropic ? 'ANTHROPIC_API_KEY' : 'OPENAI_API_KEY';
        $previous = getenv($name);
        putenv($name);
        try {
            $client = new FixtureClient();
            $provider = $anthropic ? new AnthropicProvider(client: $client) : new OpenAIProvider(client: $client);
            raises(ProviderException::class, static fn() => $provider->stream(request()), 'is not set');
            $cancelled = new CancellationToken();
            $cancelled->cancel();
            raises(Cancelled::class, static fn() => $provider->stream(request(), cancellation: $cancelled));
            same([], $client->requests);
            putenv($name . '=lazy-fixture-key');
            $client->enqueue(sse($anthropic ? 'anthropic_text' : 'openai_text'));
            $provider->stream(request());
            same($anthropic ? 'lazy-fixture-key' : 'Bearer lazy-fixture-key',
                $client->requests[0]->headers[$anthropic ? 'x-api-key' : 'Authorization']);
        } finally {
            putenv($previous === false ? $name : $name . '=' . $previous);
        }
    }
    foreach (['file:///fixture', 'https://user:secret@example.com', 'https://example.com/#fragment', "https://example.com/\npath"] as $endpoint) {
        raises(\InvalidArgumentException::class, static fn() => new OpenAIProvider(endpoint: $endpoint));
    }
    foreach ([0, -1, INF, NAN] as $timeout) {
        raises(\InvalidArgumentException::class, static fn() => new OpenAIProvider(timeout: $timeout));
    }
    $client = new FixtureClient();
    raises(\InvalidArgumentException::class, static fn() =>
        (new OpenAIProvider("key\r\nInjected: yes", client: $client))->stream(request()));
    same([], $client->requests);
    raises(\InvalidArgumentException::class, static fn() => new AnthropicProvider(maxTokens: 0));
}
