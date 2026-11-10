<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\CancellationToken;
use TinyAgent\ProviderRequest;
use TinyAgent\ProviderTurn;
use TinyAgent\SessionException;

function sessionPendingAcknowledgement(): void
{
    foreach ([false, true] as $advance) {
        $provider = new ScriptedProvider(static fn(): ProviderTurn => throw new \LogicException('Cancelled acknowledgement entered provider.'));
        [$rpc, $recorder] = rpcSession($provider);
        $recorder->before = static function (\stdClass $value): void {
            if (($value->command ?? null) === 'prompt') {
                \Fiber::suspend();
            }
        };
        try {
            $rpc->handle((object) ['type' => 'prompt', 'message' => 'go']);
            if ($advance) {
                $rpc->advance();
                same([], $recorder->values);
            }
            same($rpc->state()->runId, $rpc->cancel());
            $rpc->awaitIdle();
            same(['response'], array_column($recorder->values, 'type'));
            same(true, $recorder->values[0]->success);
            same(0, $rpc->state()->messageCount);
            same(0, $provider->entered);
            same(true, $recorder->tokens[0]->isCancelled());
        } finally {
            $rpc->close();
        }
    }
}

function sessionRunConsumerFailure(): void
{
    foreach (['response', 'agent_start', 'text_delta', 'agent_end'] as $phase) {
        foreach ([new \LogicException('delivery failed'), new SessionException('busy', 'delivery failed')] as $failure) {
            $provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $token): ProviderTurn {
                $text('done', $token);
                return new ProviderTurn('done', [], [], []);
            });
            [$rpc, $recorder] = rpcSession($provider);
            try {
                $rpc->handle((object) ['type' => 'prompt', 'message' => 'seed']);
                $rpc->awaitIdle();
                $calls = [];
                $recorder->before = static function (\stdClass $value) use ($phase, $failure, &$calls): void {
                    $calls[] = $value->type;
                    if ($value->type === $phase) {
                        throw $failure;
                    }
                };
                $rpc->handle((object) ['type' => 'prompt', 'message' => 'fail']);
                $rpc->awaitIdle();
                same($phase, end($calls));
                same(1, count(array_filter($calls, static fn(string $kind): bool => $kind === $phase)));
                same('idle', $rpc->state()->status);
                same(2, $rpc->state()->messageCount);
                same(in_array($phase, ['response', 'agent_start'], true) ? 1 : 2, $provider->closed);
            } finally {
                $rpc->close();
            }
        }
    }
}

function sessionHeldFinalDelivery(): void
{
    foreach ([false, true] as $cancel) {
        $client = new FixtureClient();
        $response = $client->enqueue(sse('openai_text'));
        [$rpc, $recorder] = rpcSession(provider(false, $client));
        $recorder->before = static function (\stdClass $value) use ($response): void {
            if ($value->type === 'agent_end') {
                same(true, $response->closed);
                \Fiber::suspend();
            }
        };
        try {
            $rpc->handle((object) ['type' => 'prompt', 'message' => 'go']);
            $rpc->advance();
            same('running', $rpc->state()->status);
            same(0, $rpc->state()->messageCount);
            same('text_delta', end($recorder->values)->type);
            $rpc->handle((object) ['type' => 'prompt', 'message' => 'busy']);
            same('busy', end($recorder->values)->error->code);
            if ($cancel) {
                $rpc->cancel();
            }
            $rpc->advance();
            same('idle', $rpc->state()->status);
            same($cancel ? 0 : 2, $rpc->state()->messageCount);
            same('completed', end($recorder->values)->status);
            same(1, count(array_filter($recorder->values, static fn(\stdClass $value): bool => $value->type === 'agent_end')));
        } finally {
            $rpc->close();
        }
    }
}

function sessionHeldCleanup(bool $close): void
{
    $release = false;
    $cleaning = false;
    $provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $token) use (&$release, &$cleaning): ProviderTurn {
        $text('waiting', $token);
        try {
            \Fiber::suspend();
            $token->throwIfCancelled();
            return new ProviderTurn('done', [], [], []);
        } finally {
            $cleaning = true;
            while (!$release) {
                \Fiber::suspend();
            }
        }
    });
    [$rpc, $recorder] = rpcSession($provider);
    $waiter = null;
    try {
        $rpc->handle((object) ['type' => 'prompt', 'message' => 'go']);
        $rpc->advance();
        $waiter = new \Fiber(static function () use ($rpc, $close): bool {
            if ($close) {
                return $rpc->close();
            }
            $rpc->handle((object) ['id' => 'reset', 'type' => 'new_session']);
            return true;
        });
        $waiter->start();
        same(true, $waiter->isSuspended());
        same(true, $cleaning);
        same(0, $provider->closed);
        same('running', $rpc->state()->status);
        same(0, $rpc->state()->messageCount);
        $rpc->handle((object) ['id' => 'busy', 'type' => 'prompt', 'message' => 'busy']);
        same($close ? 'session_closed' : 'busy', end($recorder->values)->error->code);
        $release = true;
        $waiter->resume();
        same(true, $waiter->isTerminated());
        same(true, $waiter->getReturn());
        same(1, $provider->closed);
        same('idle', $rpc->state()->status);
        $ends = array_values(array_filter($recorder->values, static fn(\stdClass $value): bool => $value->type === 'agent_end'));
        same(1, count($ends));
        same('aborted', $ends[0]->status);
        if (!$close) {
            same('reset', end($recorder->values)->id);
            same(true, end($recorder->values)->data->abortedRun);
            $rpc->handle((object) ['type' => 'prompt', 'message' => 'next']);
            $rpc->awaitIdle();
            same(2, $rpc->state()->messageCount);
        }
    } finally {
        $release = true;
        $rpc->close();
        if ($waiter?->isSuspended()) {
            $waiter->resume();
        }
    }
}

function sessionSelfCleanup(): void
{
    foreach (['response', 'text_delta', 'agent_end'] as $phase) {
        foreach (['cancel', 'reset', 'close'] as $operation) {
            $provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $token): ProviderTurn {
                $text('done', $token);
                return new ProviderTurn('done', [], [], []);
            });
            [$rpc, $recorder] = rpcSession($provider);
            $entered = false;
            $recorder->before = static function (\stdClass $value) use ($rpc, $phase, $operation, &$entered): void {
                if ($value->type !== $phase || $entered) {
                    return;
                }
                $entered = true;
                same(false, $rpc->advance());
                same(false, $rpc->awaitIdle());
                if ($operation === 'close') {
                    same(false, $rpc->close());
                } elseif ($operation === 'reset') {
                    $rpc->handle((object) ['type' => 'new_session']);
                } else {
                    same($rpc->state()->runId, $rpc->cancel());
                }
            };
            try {
                $rpc->handle((object) ['type' => 'prompt', 'message' => 'go']);
                same(true, $rpc->awaitIdle());
                same(true, $entered);
                same('idle', $rpc->state()->status);
                same(0, $rpc->state()->messageCount);
                same($phase === 'response' ? 0 : 1, $provider->closed);
                if ($operation !== 'close') {
                    $rpc->handle((object) ['type' => 'prompt', 'message' => 'again']);
                    $rpc->awaitIdle();
                    same(2, $rpc->state()->messageCount);
                }
            } finally {
                same(true, $rpc->close());
                same(true, $rpc->close());
            }
        }
    }
}

function sessionIndependentRuns(): void
{
    $provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $token): ProviderTurn {
        same(1, count($request->history));
        $text($request->history[0]->text, $token);
        \Fiber::suspend();
        $token->throwIfCancelled();
        return new ProviderTurn($request->history[0]->text, [], [], []);
    });
    [$first, $one] = rpcSession($provider);
    [$second, $two] = rpcSession($provider);
    try {
        $first->handle((object) ['type' => 'prompt', 'message' => 'one']);
        $second->handle((object) ['type' => 'prompt', 'message' => 'two']);
        $first->advance();
        $second->advance();
        same(2, $provider->entered);
        same(false, $first->state()->runId === $second->state()->runId);
        $first->cancel();
        $first->awaitIdle();
        same('running', $second->state()->status);
        same('aborted', end($one->values)->status);
        same(false, $two->tokens[0]->isCancelled());
        $second->awaitIdle();
        same('completed', end($two->values)->status);
        same('two', end($two->values)->text);
        same(0, $first->state()->messageCount);
        same(2, $second->state()->messageCount);
        same(2, $provider->closed);
    } finally {
        $first->close();
        $second->close();
    }
}

function sessionWaitsForObservedRun(): void
{
    $provider = new ScriptedProvider(static function (): ProviderTurn {
        \Fiber::suspend();
        return new ProviderTurn('done', [], [], []);
    });
    [$rpc, $recorder] = rpcSession($provider);
    try {
        $rpc->handle((object) ['type' => 'prompt', 'message' => 'first']);
        $waiter = new \Fiber($rpc->awaitIdle(...));
        $waiter->start();
        same(true, $waiter->isSuspended());
        $rpc->advance();
        same('idle', $rpc->state()->status);
        $rpc->handle((object) ['type' => 'prompt', 'message' => 'second']);
        $waiter->resume();
        same(true, $waiter->isTerminated());
        same(true, $waiter->getReturn());
        same(1, $provider->entered);
        same('running', $rpc->state()->status);
        same(2, $rpc->state()->messageCount);
        $rpc->awaitIdle();
        same(4, $rpc->state()->messageCount);
        same(2, $provider->closed);
        same('completed', end($recorder->values)->status);
    } finally {
        $rpc->close();
    }
}
