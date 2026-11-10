<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\Agent;
use TinyAgent\AgentAborted;
use TinyAgent\AgentCompleted;
use TinyAgent\AgentEvent;
use TinyAgent\AgentFailed;
use TinyAgent\Cancelled;
use TinyAgent\CancellationToken;
use TinyAgent\Cooperative;
use TinyAgent\Json;
use TinyAgent\ProviderException;
use TinyAgent\ProviderRequest;
use TinyAgent\ProviderTurn;
use TinyAgent\RpcSession;
use TinyAgent\TextDelta;
use TinyAgent\Tool;
use TinyAgent\ToolCall;
use TinyAgent\ToolExecutionEnded;
use TinyAgent\ToolExecutionStarted;

function toolSchemas(): void
{
    $fixture = Json::parseObject(file_get_contents(__DIR__ . '/../../../conformance/tool_arguments.json'));
    foreach ($fixture->cases as $case) {
        $calls = 0;
        $tool = new Tool('echo', '', $case->schema, static function () use (&$calls): string { $calls++; return 'ok'; });
        foreach ($case->valid as $arguments) {
            same('ok', $tool->call($arguments));
        }
        same(count($case->valid), $calls);
        foreach ($case->invalid as $arguments) {
            raises(\InvalidArgumentException::class, static fn() => $tool->call($arguments));
        }
        same(count($case->valid), $calls);
    }
    foreach ([
        '{"type":"bad"}', '{"properties":[]}', '{"required":"text"}', '{"required":["x","x"]}',
        '{"$defs":{"unused":{"type":"bad"}}}', '{"properties":{"unused":{"$ref":"https://invalid.example"}}}',
        '{"$ref":"#/$defs/missing"}', '{"$ref":"#"}', '{"allOf":[{"$ref":"#"}]}',
        '{"$defs":{"unused":{"$ref":"#/$defs/unused"}}}', '{"$dynamicAnchor":"node","$dynamicRef":"#node"}',
        '{"$schema":"http://json-schema.org/draft-07/schema#"}', '{"$schema":"https://invalid.example"}',
    ] as $schema) {
        raises(\Throwable::class, static fn() => new Tool('echo', '', Json::parseObject($schema), static fn() => 'ok'));
    }
    same('ok', (new Tool('allow', '', true, static fn() => 'ok'))->call(new \stdClass()));
    raises(\InvalidArgumentException::class,
        static fn() => (new Tool('deny', '', false, static fn() => 'bad'))->call(new \stdClass()));
}

function toolSnapshots(): void
{
    $schema = Json::parseObject('{"type":"object","properties":{"n":{"type":"integer","default":4},"text":{"format":"email"}},"examples":[{"$ref":"https://annotation.invalid"}]}');
    $received = null;
    $tool = new Tool('echo', 'Example', $schema, static function (\stdClass $args) use (&$received): string {
        $received = Json::copyObject($args);
        $args->changed = true;
        return 'ok';
    });
    $schema->properties->n->type = 'string';
    $tool->definition->parameters()->properties->n->type = 'boolean';
    $arguments = (object) ['text' => 'not an email'];
    same('ok', $tool->call($arguments));
    same('{"text":"not an email"}', json_encode($received));
    same('{"text":"not an email"}', json_encode($arguments));
    raises(\InvalidArgumentException::class, static fn() => $tool->call((object) ['n' => '4']));
    $provider = new ScriptedProvider(static fn() => new ProviderTurn('', [], [], []));
    raises(\InvalidArgumentException::class, static fn() => new Agent($provider, 'fixture', tools: [$tool, $tool]));
    raises(\InvalidArgumentException::class, static fn() => new Agent($provider, 'fixture', tools: [new \stdClass()]));
    $event = new ToolExecutionStarted('run', 1, 'call', 'echo', $arguments);
    $event->arguments()->text = 'changed';
    $event->jsonSerialize()->arguments->text = 'changed';
    same('not an email', $event->arguments()->text);
}

function toolProviderReplay(): void
{
    foreach ([false, true] as $anthropic) {
        $client = new FixtureClient();
        $first = $client->enqueue(sse($anthropic ? 'anthropic_tool' : 'openai_tool'));
        $second = $client->enqueue(sse($anthropic ? 'anthropic_text' : 'openai_text'));
        $entered = 0;
        $tool = new Tool('echo', '', Json::parseObject('{"type":"object","required":["text"],"properties":{"text":{"type":"string"}}}'),
            static function (\stdClass $args) use (&$entered, $first): string {
                same(true, $first->closed);
                same('café', $args->text);
                $entered++;
                return 'native café';
            });
        $events = [];
        $result = (new Agent(provider($anthropic, $client), 'fixture', tools: [$tool]))->run('go',
            onEvent: static function (AgentEvent $event) use (&$events): void { $events[] = $event; });
        same(1, $entered);
        same(true, $second->closed);
        same(4, count($result->history));
        same('native café', $result->history[2]->output);
        same(range(1, count($events)), array_column($events, 'sequence'));
        same(1, count(array_unique(array_column($events, 'runId'))));
        same(1, count(array_filter($events, static fn($event) => $event instanceof ToolExecutionStarted)));
        same(1, count(array_filter($events, static fn($event) => $event instanceof ToolExecutionEnded)));
        same(true, end($events) instanceof AgentCompleted);
        $sent = Json::parseObject($client->requests[1]->body);
        same('echo', $sent->tools[0]->name);
        if ($anthropic) {
            same('tool_use', $sent->messages[1]->content[array_key_last($sent->messages[1]->content)]->type);
            same('native café', $sent->messages[2]->content[0]->content);
        } else {
            same('function_call', $sent->input[count($sent->input) - 2]->type);
            same('native café', $sent->input[array_key_last($sent->input)]->output);
        }
    }
}

/** @param list<ToolCall> $calls */
function toolScript(array $calls, ?\Closure $after = null): ScriptedProvider
{
    $round = 0;
    return new ScriptedProvider(static function (ProviderRequest $request) use (&$round, $calls, $after): ProviderTurn {
        if ($round++ === 0) {
            return new ProviderTurn('working', $calls, [], []);
        }
        if ($after !== null) {
            $after($request);
        }
        return new ProviderTurn('done', [], [], []);
    });
}

function toolRecovery(): void
{
    $handled = [];
    $schema = Json::parseObject('{"type":"object","required":["n"],"properties":{"n":{"type":"integer"}}}');
    $tool = new Tool('echo', '', $schema, static function (\stdClass $args) use (&$handled): string {
        $handled[] = $args->n;
        return match ($args->n) {
            1 => throw new \RuntimeException(str_repeat('é', 2000)),
            2 => str_repeat('é', Tool::MAX_OUTPUT_BYTES),
            3 => "\xff",
            default => 'ok',
        };
    });
    $calls = array_map(static fn($n) => new ToolCall((string) $n, 'echo', (object) ['n' => $n]), [0, 1, 2, 3, 'bad', 4]);
    $provider = toolScript($calls, static function (ProviderRequest $request): void {
        $results = array_slice($request->history, 2);
        same([false, true, true, true, true, false], array_column($results, 'isError'));
        foreach ($results as $result) {
            same(true, strlen($result->output) <= Tool::MAX_OUTPUT_BYTES);
            same(1, preg_match('//u', $result->output));
        }
    });
    $events = [];
    $result = (new Agent($provider, 'fixture', tools: [$tool]))->run('go',
        onEvent: static function (AgentEvent $event) use (&$events): void { $events[] = $event; });
    same([0, 1, 2, 3, 4], $handled);
    same(9, count($result->history));
    same(['working', 'done'], array_values(array_map(static fn(TextDelta $event) => $event->delta,
        array_filter($events, static fn($event) => $event instanceof TextDelta))));
    $toolEvents = array_values(array_filter($events,
        static fn($event) => $event instanceof ToolExecutionStarted || $event instanceof ToolExecutionEnded));
    foreach ($calls as $index => $call) {
        same($call->callId, $toolEvents[$index * 2]->toolCallId);
        same(true, $toolEvents[$index * 2] instanceof ToolExecutionStarted);
        same($call->callId, $toolEvents[$index * 2 + 1]->toolCallId);
        same(true, $toolEvents[$index * 2 + 1] instanceof ToolExecutionEnded);
    }
}

function toolUnknownBatch(): void
{
    $entered = 0;
    $tool = new Tool('known', '', true, static function () use (&$entered): string { $entered++; return 'ok'; });
    $provider = toolScript([new ToolCall('1', 'known', new \stdClass()), new ToolCall('2', 'missing', new \stdClass())]);
    $events = [];
    $observe = static function (AgentEvent $event) use (&$events): void { $events[] = $event; };
    raises(ProviderException::class, static fn() => (new Agent($provider, 'fixture', tools: [$tool]))->run('go',
        onEvent: $observe));
    same(0, $entered);
    same(0, count(array_filter($events, static fn($event) => $event instanceof ToolExecutionStarted)));
    same(true, end($events) instanceof AgentFailed);
}

function toolCancellation(): void
{
    foreach (['start', 'handler', 'end'] as $phase) {
        $entered = 0;
        $tool = new Tool('echo', '', true, static function (\stdClass $args, CancellationToken $token) use (&$entered, $phase): string {
            $entered++;
            if ($phase === 'handler') {
                $token->cancel();
                throw new \RuntimeException('wrapped cancellation');
            }
            return 'ok';
        });
        $provider = toolScript([new ToolCall('1', 'echo', new \stdClass()), new ToolCall('2', 'echo', new \stdClass())]);
        $events = [];
        $parent = new CancellationToken();
        $observe = static function (AgentEvent $event, CancellationToken $token) use (&$events, $phase): void {
                $events[] = $event;
                if (($phase === 'start' && $event instanceof ToolExecutionStarted)
                    || ($phase === 'end' && $event instanceof ToolExecutionEnded)) {
                    $token->cancel();
                }
            };
        raises(Cancelled::class, static fn() => (new Agent($provider, 'fixture', tools: [$tool]))->run('go',
            onEvent: $observe, cancellation: $parent));
        same($phase === 'start' ? 0 : 1, $entered);
        same(1, $provider->entered);
        same(false, $parent->isCancelled());
        same(1, count(array_filter($events, static fn($event) => $event instanceof ToolExecutionStarted)));
        same(1, count(array_filter($events, static fn($event) => $event instanceof ToolExecutionEnded)));
        same(true, end($events) instanceof AgentAborted);
    }
}

function toolHeldCleanup(): void
{
    foreach ([false, true] as $interrupt) {
        $entered = false;
        $cleaning = false;
        $released = false;
        $closed = false;
        $failure = new \LogicException('caller interrupted');
        $tool = new Tool('wait', '', true, static function (\stdClass $args, CancellationToken $token) use (&$entered, &$cleaning, &$released, &$closed): string {
            try {
                $entered = true;
                while (true) {
                    $token->throwIfCancelled();
                    Cooperative::pause();
                }
            } catch (\Throwable $error) {
                throw new \RuntimeException('wrapped', previous: $error);
            } finally {
                $cleaning = true;
                while (!$released) {
                    Cooperative::pause();
                }
                $closed = true;
            }
        });
        $events = [];
        $provider = toolScript([new ToolCall('1', 'wait', new \stdClass())]);
        $observe = static function (AgentEvent $event) use (&$events, &$closed): void {
                if ($event instanceof ToolExecutionEnded) {
                    same(true, $closed);
                }
                $events[] = $event;
            };
        $task = new CooperativeTask(static fn(CancellationToken $token) => (new Agent($provider, 'fixture', tools: [$tool]))->run('go',
            onEvent: $observe, cancellation: $token));
        try {
            $task->until(static function () use (&$entered): bool { return $entered; });
            if ($interrupt) {
                $task->fiber->throw($failure);
            } else {
                $task->token->cancel();
            }
            $task->until(static function () use (&$cleaning): bool { return $cleaning; });
            same(false, $task->fiber->isTerminated());
            same(0, count(array_filter($events, static fn($event) => $event instanceof ToolExecutionEnded)));
            $released = true;
            $task->finish();
            same(true, $closed);
            same(1, count(array_filter($events, static fn($event) => $event instanceof ToolExecutionEnded)));
            same(true, $interrupt ? $task->error === $failure : $task->error instanceof Cancelled);
        } finally {
            $released = true;
            $task->close();
        }
    }
}

function toolCallbackOwnership(): void
{
    foreach ([ToolExecutionStarted::class, ToolExecutionEnded::class] as $stage) {
        $failure = new \LogicException('consumer failed');
        $entered = 0;
        $events = [];
        $tool = new Tool('echo', '', true, static function () use (&$entered): string { $entered++; return 'ok'; });
        $provider = toolScript([new ToolCall('1', 'echo', new \stdClass())]);
        $observe = static function (AgentEvent $event) use (&$events, $stage, $failure): void {
                $events[] = $event;
                if ($event instanceof $stage) { throw $failure; }
            };
        same($failure, raises(\LogicException::class, static fn() => (new Agent($provider, 'fixture', tools: [$tool]))->run('go',
            onEvent: $observe)));
        same($stage === ToolExecutionStarted::class ? 0 : 1, $entered);
        same(true, end($events) instanceof $stage);
        same(1, $provider->entered);
    }
    $retained = null;
    $provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $token) use (&$retained): ProviderTurn {
        if ($retained === null) {
            $retained = static fn() => $text('late', $token);
            return new ProviderTurn('', [new ToolCall('1', 'echo', new \stdClass())], [], []);
        }
        raises(Cancelled::class, $retained);
        return new ProviderTurn('done', [], [], []);
    });
    $tool = new Tool('echo', '', true, static function () use (&$retained): string {
        raises(Cancelled::class, $retained);
        return 'ok';
    });
    same('done', (new Agent($provider, 'fixture', tools: [$tool]))->run('go')->text);
    raises(Cancelled::class, $retained);
}

function toolSessionCommit(): void
{
    $recorder = new SessionRecorder();
    $tool = new Tool('echo', '', true, static fn() => 'ok');
    $provider = toolScript([new ToolCall('1', 'echo', new \stdClass())]);
    $rpc = new RpcSession(new Agent($provider, 'fixture', tools: [$tool]), $recorder->emit(...));
    try {
        $rpc->handle((object) ['id' => 'go', 'type' => 'prompt', 'message' => 'go']);
        $rpc->awaitIdle();
        same('response', $recorder->values[0]->type);
        same('completed', end($recorder->values)->status);
        same(4, $rpc->state()->messageCount);
        $recorder->before = static function (\stdClass $event): void {
            if ($event->type === 'agent_end') { throw new \LogicException('disconnect'); }
        };
        $rpc->handle((object) ['type' => 'prompt', 'message' => 'again']);
        $rpc->awaitIdle();
        same(4, $rpc->state()->messageCount);
    } finally {
        $rpc->close();
    }
}

function toolRpcProcess(): void
{
    foreach (['tools_openai', 'tools_anthropic'] as $mode) {
        $rpc = new RpcProcess($mode);
        try {
            $rpc->send((object) ['id' => 'go', 'type' => 'prompt', 'message' => 'go']);
            $events = $rpc->through('agent_end');
            same('response', $events[0]->type);
            same('completed', end($events)->status);
            $starts = array_values(array_filter($events, static fn($event) => $event->type === 'tool_execution_start'));
            $ends = array_values(array_filter($events, static fn($event) => $event->type === 'tool_execution_end'));
            same(1, count($starts));
            same(1, count($ends));
            same('café', $starts[0]->arguments->text);
            same('café', $ends[0]->output);
            same(false, $ends[0]->isError);
            same(4, $rpc->state()->messageCount);
            same([true, true], $rpc->finish()->responsesClosed);
        } finally { $rpc->close(); }
    }
}

function toolHttpProcess(): void
{
    withHttp(static function (HttpProcess $server): void {
        $id = $server->session();
        $response = $server->prompt($id, 'tool');
        try {
            $events = $response->events();
            same(['agent_start', 'tool_execution_start', 'tool_execution_end', 'text_delta', 'agent_end'], array_column($events, 'type'));
            same('café', $events[1]->arguments->text);
            same('café', $events[2]->output);
            same(false, $events[2]->isError);
            same('café', end($events)->text);
        } finally { $response->close(); }
        $response = $server->prompt($id, 'tool_wait');
        try {
            same('agent_start', $response->event()->type);
            same('tool_execution_start', $response->event()->type);
            same(true, $server->request('POST', "/sessions/{$id}/cancel")->accepted);
            $end = $response->event();
            same('tool_execution_end', $end->type);
            same(true, $end->isError);
            same('aborted', $response->event()->status);
            same('', $response->body());
        } finally { $response->close(); }
        same('history:5', $server->completed($id)->text);
        $server->finish();
    });
}
