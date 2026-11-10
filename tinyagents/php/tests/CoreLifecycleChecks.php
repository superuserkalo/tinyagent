<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\Agent;
use TinyAgent\AgentAborted;
use TinyAgent\AgentCompleted;
use TinyAgent\AgentEvent;
use TinyAgent\AgentFailed;
use TinyAgent\AgentStarted;
use TinyAgent\Cancelled;
use TinyAgent\CancellationToken;
use TinyAgent\Cooperative;
use TinyAgent\ProviderException;
use TinyAgent\ProviderRequest;
use TinyAgent\ProviderTurn;
use TinyAgent\TextDelta;
use TinyAgent\UserMessage;

function coreConsumerFailure(): void
{
    foreach ([false, true] as $anthropic) {
        foreach ([AgentStarted::class, TextDelta::class, AgentCompleted::class] as $phase) {
            $client = new FixtureClient();
            $response = $client->enqueue(sse($anthropic ? 'anthropic_text' : 'openai_text'));
            $failure = new \LogicException('event consumer failed');
            $events = [];
            $observe = static function (AgentEvent $event) use ($phase, $failure, &$events): void {
                $events[] = $event;
                if ($event instanceof $phase) {
                    throw $failure;
                }
            };
            $error = raises(\LogicException::class, static fn() => (new Agent(provider($anthropic, $client), 'fixture'))->run('go',
                onEvent: $observe));
            same($failure, $error);
            same(true, end($events) instanceof $phase);
            same($phase !== AgentStarted::class, $response->closed);
            same($phase === AgentStarted::class ? 0 : 1, count($client->requests));
            same($phase === AgentCompleted::class ? 1 : 0,
                count(array_filter($events, static fn(AgentEvent $event): bool => $event instanceof AgentCompleted)));
        }
    }
}

function coreSwallowedConsumerFailure(): void
{
    foreach ([false, true] as $cancel) {
        $failure = new \LogicException('original consumer failure');
        $provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $signal) use ($failure): ProviderTurn {
            same($failure, raises(\LogicException::class, static fn() => $text('first', $signal)));
            same($failure, raises(\LogicException::class, static fn() => $text('second', $signal)));
            return new ProviderTurn('ignored result', [], [], []);
        });
        $events = [];
        $parent = new CancellationToken();
        $observe = static function (AgentEvent $event, CancellationToken $signal) use (&$events, $failure, $cancel): void {
            $events[] = $event;
            if ($event instanceof TextDelta) {
                if ($cancel) {
                    $signal->cancel();
                }
                throw $failure;
            }
        };
        $error = raises(\LogicException::class, static fn() => (new Agent($provider, 'fixture'))->run('go',
            onEvent: $observe, cancellation: $parent));
        same($failure, $error);
        same(2, count($events));
        same(1, $provider->closed);
        same(false, $parent->isCancelled());
    }
}

function coreCancellation(): void
{
    $provider = new ScriptedProvider(static fn(): ProviderTurn => throw new \LogicException('Cancelled run entered provider.'));
    $parent = new CancellationToken();
    $parent->cancel();
    $events = [];
    $observe = static function (AgentEvent $event) use (&$events): void { $events[] = $event; };
    raises(Cancelled::class, static fn() => (new Agent($provider, 'fixture'))->run('go',
        onEvent: $observe, cancellation: $parent));
    same(0, $provider->entered);
    same([AgentStarted::class, AgentAborted::class], array_map(get_class(...), $events));

    foreach ([false, true] as $anthropic) {
        foreach ([AgentStarted::class, TextDelta::class, AgentCompleted::class] as $phase) {
            $client = new FixtureClient();
            $body = sse($anthropic ? 'anthropic_text' : 'openai_text');
            $response = $client->enqueue($body);
            $parent = new CancellationToken();
            $agent = new Agent(provider($anthropic, $client), 'fixture');
            $events = [];
            $observe = static function (AgentEvent $event, CancellationToken $signal) use ($phase, &$events): void {
                $events[] = $event;
                if ($event instanceof $phase) {
                    $signal->cancel();
                }
            };
            raises(Cancelled::class, static fn() => $agent->run('cancel', onEvent: $observe, cancellation: $parent));
            same(false, $parent->isCancelled());
            same($phase !== AgentStarted::class, $response->closed);
            same(true, $phase === AgentCompleted::class ? end($events) instanceof AgentCompleted : end($events) instanceof AgentAborted);
            same(1, count(array_filter($events, static fn(AgentEvent $event): bool =>
                $event instanceof AgentCompleted || $event instanceof AgentAborted)));
            if ($phase !== AgentStarted::class) {
                $client->enqueue($body);
            }
            same("done\ncafé", $agent->run('next', cancellation: $parent)->text);
        }
    }
}

function coreIgnoredCancellation(): void
{
    foreach ([false, true] as $throw) {
        $provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $signal) use ($throw): ProviderTurn {
            $text('partial', $signal);
            $signal->cancel();
            if ($throw) {
                throw new ProviderException('Provider wrapped cancellation.');
            }
            return new ProviderTurn('ignored result', [], [], []);
        });
        $events = [];
        $parent = new CancellationToken();
        $observe = static function (AgentEvent $event) use (&$events, $provider): void {
            if ($event instanceof AgentAborted) {
                same(1, $provider->closed);
            }
            $events[] = $event;
        };
        raises(Cancelled::class, static fn() => (new Agent($provider, 'fixture'))->run('go',
            onEvent: $observe, cancellation: $parent));
        same(false, $parent->isCancelled());
        same([AgentStarted::class, TextDelta::class, AgentAborted::class], array_map(get_class(...), $events));
    }
    // A provider must not turn cancellation of its callback token into success.
    $provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $signal): ProviderTurn {
        $child = $signal->child();
        raises(Cancelled::class, static fn() => $text('cancel this request', $child));
        same(true, $signal->isCancelled());
        return new ProviderTurn('ignored result', [], [], []);
    });
    $events = [];
    $observe = static function (AgentEvent $event, CancellationToken $signal) use (&$events): void {
        $events[] = $event;
        if ($event instanceof TextDelta) {
            $signal->cancel();
        }
    };
    $parent = new CancellationToken();
    raises(Cancelled::class, static fn() => (new Agent($provider, 'fixture'))->run('go', onEvent: $observe, cancellation: $parent));
    same(false, $parent->isCancelled());
    same([AgentStarted::class, TextDelta::class, AgentAborted::class], array_map(get_class(...), $events));
}

function coreDeadlines(): void
{
    foreach ([false, true] as $anthropic) {
        foreach (['client', 'callback', 'cleanup'] as $phase) {
            $client = new FixtureClient();
            $response = $client->enqueue(sse($anthropic ? 'anthropic_text' : 'openai_text'));
            $wait = static function (CancellationToken $signal): void {
                $limit = hrtime(true) + 1_000_000_000;
                while (!$signal->isCancelled() && hrtime(true) < $limit) {
                    usleep(1000);
                }
                same(true, $signal->isCancelled());
            };
            if ($phase === 'client') {
                $response->before = $wait;
            } elseif ($phase === 'cleanup') {
                $response->cleanup = $wait;
            }
            $events = [];
            $parent = new CancellationToken();
            $observe = static function (AgentEvent $event, CancellationToken $signal) use ($phase, $wait, &$events): void {
                $events[] = $event;
                if ($phase === 'callback' && $event instanceof TextDelta) {
                    $wait($signal);
                }
            };
            raises(ProviderException::class, static fn() => (new Agent(provider($anthropic, $client, 0.04), 'fixture'))->run('go',
                onEvent: $observe, cancellation: $parent), 'timed out');
            same(false, $parent->isCancelled());
            same(true, $response->closed);
            same(true, end($events) instanceof AgentFailed);
        }
    }
}

function coreHeldDelivery(): void
{
    foreach ([false, true] as $cancel) {
        $client = new FixtureClient();
        $response = $client->enqueue(sse('openai_text'));
        $parent = new CancellationToken();
        $events = [];
        $returned = false;
        $fiber = new \Fiber(static function () use ($client, $response, $parent, &$events, &$returned): void {
            (new Agent(provider(false, $client), 'fixture'))->run('go', onEvent:
                static function (AgentEvent $event) use ($response, &$events): void {
                    $events[] = $event;
                    if ($event instanceof AgentCompleted) {
                        same(true, $response->closed);
                        \Fiber::suspend('final delivery');
                    }
                }, cancellation: $parent);
            $returned = true;
        });
        same('final delivery', $fiber->start());
        same(false, $returned);
        if ($cancel) {
            $parent->cancel();
            raises(Cancelled::class, $fiber->resume(...));
        } else {
            $fiber->resume();
        }
        same(!$cancel, $returned);
        same(true, $fiber->isTerminated());
        same(1, count(array_filter($events, static fn(AgentEvent $event): bool => $event instanceof AgentCompleted)));
        same(true, end($events) instanceof AgentCompleted);
    }
}

function coreHeldCleanup(): void
{
    foreach ([false, true] as $anthropic) {
        $client = new FixtureClient();
        $response = $client->enqueue(sse($anthropic ? 'anthropic_text' : 'openai_text'));
        $response->cleanup = static fn() => \Fiber::suspend('provider cleanup');
        $parent = new CancellationToken();
        $events = [];
        $returned = false;
        $fiber = new \Fiber(static function () use ($client, $anthropic, $parent, &$events, &$returned): void {
            (new Agent(provider($anthropic, $client), 'fixture'))->run('go', onEvent:
                static function (AgentEvent $event) use (&$events): void { $events[] = $event; }, cancellation: $parent);
            $returned = true;
        });
        same('provider cleanup', $fiber->start());
        same(false, $returned);
        same(false, $response->closed);
        same(true, end($events) instanceof TextDelta);
        $parent->cancel();
        raises(Cancelled::class, $fiber->resume(...));
        same(false, $returned);
        same(true, $response->closed);
        same(true, $fiber->isTerminated());
        same(true, end($events) instanceof AgentAborted);
    }
}

function coreConcurrentRuns(): void
{
    $provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $signal): ProviderTurn {
        $prompt = $request->history[array_key_last($request->history)]->text;
        $text($prompt, $signal);
        \Fiber::suspend($prompt);
        $signal->throwIfCancelled();
        return new ProviderTurn($prompt, [], [], []);
    });
    $agent = new Agent($provider, 'fixture');
    $firstToken = new CancellationToken();
    $secondToken = new CancellationToken();
    $events = ['first' => [], 'second' => []];
    $capture = static function (AgentEvent $event) use (&$events): void { $events[$event->runId][] = $event; };
    $first = new \Fiber(static fn() => $agent->run('one', [new UserMessage('first seed')], $capture, $firstToken, 'first'));
    $second = new \Fiber(static fn() => $agent->run('two', [], $capture, $secondToken, 'second'));
    same('one', $first->start());
    same('two', $second->start());
    same(0, $provider->closed);
    $firstToken->cancel();
    raises(Cancelled::class, $first->resume(...));
    same(false, $secondToken->isCancelled());
    $second->resume();
    same('two', $second->getReturn()->text);
    same(2, count($second->getReturn()->history));
    same('two', $second->getReturn()->history[0]->text);
    same(2, $provider->closed);
    same(true, end($events['first']) instanceof AgentAborted);
    same(true, end($events['second']) instanceof AgentCompleted);
    foreach ($events as $runEvents) {
        same([1, 2, 3], array_column($runEvents, 'sequence'));
    }
}

function coreRetainedCallbacks(): void
{
    $retained = null;
    $provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $signal) use (&$retained): ProviderTurn {
        $retained = static fn() => $text('late delta', $signal);
        return new ProviderTurn('done', [], [], []);
    });
    $events = [];
    $agent = new Agent($provider, 'fixture');
    same('done', $agent->run('go', onEvent: static function (AgentEvent $event) use (&$retained, &$events): void {
        $events[] = $event;
        if ($event instanceof AgentCompleted) {
            raises(Cancelled::class, $retained);
        }
    })->text);
    raises(Cancelled::class, $retained);
    same([AgentStarted::class, TextDelta::class, AgentCompleted::class], array_map(get_class(...), $events));
    same('done', $events[1]->delta);
}

function coreCallerInterruption(): void
{
    foreach ([false, true] as $anthropic) {
        foreach (['request', 'cleanup', 'wrapped'] as $phase) {
            $client = new FixtureClient();
            $response = $client->enqueue(sse($anthropic ? 'anthropic_text' : 'openai_text'));
            if ($phase === 'cleanup') {
                $response->cleanup = static function () use ($response): void {
                    try {
                        Cooperative::pause();
                    } finally {
                        $response->closed = true;
                    }
                };
            } else {
                $response->before = static fn() => Cooperative::pause();
            }
            $provider = provider($anthropic, $client);
            if ($phase === 'wrapped') {
                $provider = new ScriptedProvider(static function (
                    ProviderRequest $request, callable $onText, CancellationToken $signal,
                ) use ($provider): ProviderTurn {
                    try {
                        return $provider->stream($request, $onText, $signal);
                    } catch (\Throwable $error) {
                        throw new ProviderException('Custom provider wrapped failure.', previous: $error);
                    }
                });
            }
            $agent = new Agent($provider, 'fixture');
            $prior = [new UserMessage('seed')];
            $parent = new CancellationToken();
            $events = [];
            $observe = static function (AgentEvent $event) use (&$events, $response): void {
                if ($event instanceof AgentFailed) {
                    same(true, $response->closed);
                }
                $events[] = $event;
            };
            $fiber = Cooperative::fiber(static fn() => $agent->run('go', $prior, $observe, $parent, 'interrupted'));
            $fiber->start();
            same(true, $fiber->isSuspended());
            $failure = new \LogicException('Caller interrupted the run.');
            $error = raises(\LogicException::class, static fn() => $fiber->throw($failure));
            same(true, $error === $failure);
            same(true, $fiber->isTerminated());
            same(false, $parent->isCancelled());
            same('seed', $prior[0]->text);
            same(1, count($prior));
            same(true, end($events) instanceof AgentFailed);
            same($failure->getMessage(), end($events)->message);
            same(range(1, count($events)), array_column($events, 'sequence'));
            same(['interrupted'], array_values(array_unique(array_column($events, 'runId'))));
            $client->enqueue(sse($anthropic ? 'anthropic_text' : 'openai_text'));
            same("done\ncafé", $agent->run('next', $prior, cancellation: $parent)->text);
        }
    }
}
