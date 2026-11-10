<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\Agent;
use TinyAgent\CancellationToken;
use TinyAgent\Json;
use TinyAgent\Provider;
use TinyAgent\ProviderRequest;
use TinyAgent\ProviderTurn;
use TinyAgent\RpcSession;
use TinyAgent\SessionException;

final class SessionRecorder
{
    /** @var list<\stdClass> */
    public array $values = [];
    /** @var list<?CancellationToken> */
    public array $tokens = [];
    public ?\Closure $before = null;

    public function emit(\stdClass $value, ?CancellationToken $token): void
    {
        if ($this->before !== null) {
            ($this->before)($value, $token);
        }
        $this->values[] = Json::copyObject($value);
        $this->tokens[] = $token;
    }
}

/** @return array{RpcSession, SessionRecorder} */
function rpcSession(Provider $provider): array
{
    $recorder = new SessionRecorder();
    return [new RpcSession(new Agent($provider, 'fixture'), $recorder->emit(...)), $recorder];
}

function sessionSharedCommands(): void
{
    $provider = new ScriptedProvider(static fn(): ProviderTurn => throw new \LogicException('Unexpected provider entry.'));
    [$rpc, $recorder] = rpcSession($provider);
    try {
        $fixture = Json::parseObject(file_get_contents(__DIR__ . '/../../../conformance/rpc_v1.json'));
        foreach ($fixture->scenarios as $scenario) {
            if (!isset($scenario->expected)) {
                continue;
            }
            $recorder->values = [];
            foreach ($scenario->commands as $command) {
                $rpc->handle($command);
            }
            same(true, $scenario->expected == $recorder->values);
        }
        $rpc->handle((object) ['id' => '', 'type' => 'get_state']);
        same('', end($recorder->values)->id);
        $rpc->handle((object) ['type' => 'get_state', 'extra' => [null, false, 1, 1.5, (object) ['0' => []]]]);
        same('idle', end($recorder->values)->data->status);
        $snapshot = $rpc->state();
        $snapshot->messageCount = 50;
        same(0, $rpc->state()->messageCount);
        same(false, $rpc->advance());
        same(true, $rpc->awaitIdle());
        same(0, $provider->entered);
        same([null], array_values(array_unique($recorder->tokens)));
    } finally {
        $rpc->close();
    }
}

function sessionInvalidValues(): void
{
    [$rpc, $recorder] = rpcSession(new ScriptedProvider(static fn(): ProviderTurn => new ProviderTurn('', [], [], [])));
    $serializer = new class implements \JsonSerializable {
        public function jsonSerialize(): mixed { throw new \LogicException('Must not serialize foreign objects.'); }
    };
    $cyclic = new \stdClass();
    $cyclic->self = $cyclic;
    $array = [];
    $array[] = &$array;
    $deep = null;
    for ($i = 0; $i < 102; $i++) {
        $deep = [$deep];
    }
    $resource = fopen('php://memory', 'r+');
    try {
        foreach ([null, [], 1, false, 'get_state', $serializer, new class extends \stdClass {}] as $invalid) {
            $rpc->handle($invalid);
            same('invalid_command', end($recorder->values)->error->code);
        }
        foreach ([INF, NAN, "\xff", ['key' => 1], $cyclic, $array, $deep, $resource, $serializer] as $invalid) {
            $rpc->handle((object) ['id' => 'bad', 'type' => 'get_state', 'extra' => $invalid]);
            same('invalid_json', end($recorder->values)->error->code);
            same('protocol_error', end($recorder->values)->type);
        }
        foreach ([null, 1, false, []] as $invalid) {
            $rpc->handle((object) ['id' => $invalid, 'type' => 'get_state']);
            same('invalid_command', end($recorder->values)->error->code);
        }
        $rpc->handle((object) ['id' => 'unknown', 'type' => 4]);
        same('unknown', end($recorder->values)->command);
        $rpc->handle((object) ['type' => 'get_state', "\xff" => 'invalid key']);
        same('invalid_json', end($recorder->values)->error->code);
        same(false, $rpc->advance());
    } finally {
        fclose($resource);
        $rpc->close();
    }
}

function sessionNativeProviders(): void
{
    foreach ([false, true] as $anthropic) {
        $client = new FixtureClient();
        $body = sse($anthropic ? 'anthropic_text' : 'openai_text');
        $response = $client->enqueue($body);
        $client->enqueue($body);
        [$rpc, $recorder] = rpcSession(provider($anthropic, $client));
        try {
            $recorder->before = static function (\stdClass $value) use ($rpc, $response): void {
                if ($value->type === 'agent_end') {
                    same(true, $response->closed);
                    same(0, $rpc->state()->messageCount);
                }
            };
            $rpc->handle((object) ['id' => 'first', 'type' => 'prompt', 'message' => 'one']);
            same([], $recorder->values);
            $id = $rpc->state()->runId;
            same('running', $rpc->state()->status);
            same(true, $rpc->awaitIdle());
            same('first', $recorder->values[0]->id);
            same($id, $recorder->values[0]->data->runId);
            same(['response', 'agent_start', 'text_delta', 'text_delta', 'agent_end'], array_column($recorder->values, 'type'));
            same([1, 2, 3, 4], array_column($recorder->values, 'sequence'));
            same([$id], array_values(array_unique(array_column($recorder->values, 'runId'))));
            same('completed', end($recorder->values)->status);
            same("done\ncafé", end($recorder->values)->text);
            same(2, $rpc->state()->messageCount);
            foreach ($recorder->tokens as $token) {
                same(true, $token->isCancelled());
            }
            $recorder->before = null;
            $rpc->handle((object) ['type' => 'prompt', 'message' => 'two']);
            $rpc->awaitIdle();
            same(4, $rpc->state()->messageCount);
            $request = Json::parseObject($client->requests[1]->body);
            same(3, count($anthropic ? $request->messages : $request->input));
        } finally {
            $rpc->close();
        }
    }
}

function sessionBusyAndRollback(): void
{
    $provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $token): ProviderTurn {
        $prompt = $request->history[array_key_last($request->history)]->text;
        $text('partial', $token);
        if ($prompt === 'wait') {
            \Fiber::suspend();
            $token->throwIfCancelled();
        }
        if ($prompt === 'error') {
            throw new \LogicException('failed');
        }
        return new ProviderTurn('done', [], [], []);
    });
    [$rpc, $recorder] = rpcSession($provider);
    try {
        $rpc->handle((object) ['type' => 'prompt', 'message' => 'go']);
        $rpc->awaitIdle();
        same(2, $rpc->state()->messageCount);
        $rpc->handle((object) ['type' => 'prompt', 'message' => 'wait']);
        $rpc->handle((object) ['id' => 'busy', 'type' => 'prompt', 'message' => 'busy']);
        same('busy', end($recorder->values)->error->code);
        $rpc->advance();
        $id = $rpc->state()->runId;
        $rpc->handle((object) ['id' => 'abort', 'type' => 'abort']);
        same(true, end($recorder->values)->data->accepted);
        same($id, end($recorder->values)->data->runId);
        $rpc->awaitIdle();
        same('aborted', end($recorder->values)->status);
        same(2, $rpc->state()->messageCount);
        $rpc->handle((object) ['type' => 'prompt', 'message' => 'error']);
        $rpc->awaitIdle();
        same('failed', end($recorder->values)->status);
        same(2, $rpc->state()->messageCount);
        $rpc->handle((object) ['id' => 'reset', 'type' => 'new_session']);
        same(false, end($recorder->values)->data->abortedRun);
        same(0, $rpc->state()->messageCount);
        same(3, $provider->closed);
    } finally {
        $rpc->close();
    }
}

function sessionCommandConsumerFailure(): void
{
    foreach (['get_state', 'unknown', 'prompt'] as $type) {
        foreach ([new \LogicException('consumer failed'), new SessionException('busy', 'consumer failed')] as $failure) {
            $calls = 0;
            $rpc = new RpcSession(new Agent(new ScriptedProvider(static fn(): ProviderTurn => new ProviderTurn('', [], [], [])), 'fixture'),
                static function () use (&$calls, $failure): never { $calls++; throw $failure; });
            try {
                $error = raises(get_class($failure), static fn() => $rpc->handle((object) ['id' => 'id', 'type' => $type]));
                same($failure, $error);
                same(1, $calls);
            } finally {
                $rpc->close();
            }
        }
    }
}
