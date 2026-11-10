<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\Agent;
use TinyAgent\AgentAborted;
use TinyAgent\AgentCompleted;
use TinyAgent\AgentEvent;
use TinyAgent\AgentFailed;
use TinyAgent\AgentStarted;
use TinyAgent\AssistantMessage;
use TinyAgent\CancellationToken;
use TinyAgent\Provider;
use TinyAgent\ProviderException;
use TinyAgent\ProviderRequest;
use TinyAgent\ProviderTurn;
use TinyAgent\RunResult;
use TinyAgent\TextDelta;
use TinyAgent\UserMessage;

final class ScriptedProvider implements Provider
{
    public int $entered = 0;
    public int $closed = 0;

    public function __construct(private readonly \Closure $script) {}

    public function stream(ProviderRequest $request, ?callable $onText = null, ?CancellationToken $cancellation = null): ProviderTurn
    {
        $this->entered++;
        try {
            return ($this->script)($request, $onText, $cancellation);
        } finally {
            $this->closed++;
        }
    }
}

function coreProviders(): void
{
    foreach ([false, true] as $anthropic) {
        $client = new FixtureClient();
        $body = sse($anthropic ? 'anthropic_text' : 'openai_text');
        $first = $client->enqueue($body);
        $second = $client->enqueue($body, 8192);
        $agent = new Agent(provider($anthropic, $client), 'native-model', 'Native instructions');
        $events = [];
        $result = $agent->run('first', onEvent: static function (AgentEvent $event) use (&$events, $first): void {
            $events[] = $event;
            same($event instanceof AgentCompleted, $first->closed);
        }, runId: 'first-run');
        same("done\ncafé", $result->text);
        same('first', $result->history[0]->text);
        same(2, count($result->history));
        same('first-run', $events[0]->runId);
        same(true, $events[0] instanceof AgentStarted);
        same(true, end($events) instanceof AgentCompleted);
        same(range(1, count($events)), array_column($events, 'sequence'));
        same(['first-run'], array_values(array_unique(array_column($events, 'runId'))));
        $deltas = array_filter($events, static fn(AgentEvent $event): bool => $event instanceof TextDelta);
        same($result->text, implode('', array_map(static fn(TextDelta $event): string => $event->delta, $deltas)));
        $again = $agent->run('second', $result->history);
        same(4, count($again->history));
        same(2, count($result->history));
        same(true, $second->closed);
        $sent = json_decode($client->requests[1]->body, flags: JSON_THROW_ON_ERROR);
        same('native-model', $sent->model);
        same('Native instructions', $anthropic ? $sent->system : $sent->instructions);
        same(3, count($anthropic ? $sent->messages : $sent->input));
        same('second', $anthropic ? $sent->messages[2]->content : $sent->input[2]->content);
        same("done\ncafé", $anthropic ? $sent->messages[1]->content[0]->text : $sent->input[1]->content[0]->text);
        same([], $sent->tools ?? []);
    }
}

function coreEvents(): void
{
    $cases = [
        [new AgentStarted('run', 1), ['type' => 'agent_start']],
        [new TextDelta('run', 2, "hello\ncafé"), ['type' => 'text_delta', 'delta' => "hello\ncafé"]],
        [new AgentCompleted('run', 3, 'answer'), ['type' => 'agent_end', 'status' => 'completed', 'text' => 'answer']],
        [new AgentFailed('run', 4, 'failed'), ['type' => 'agent_end', 'status' => 'failed',
            'error' => ['code' => 'provider_error', 'message' => 'failed', 'retryable' => false]]],
        [new AgentAborted('run', 5), ['type' => 'agent_end', 'status' => 'aborted']],
    ];
    foreach ($cases as [$event, $fields]) {
        same(['runId' => 'run', 'sequence' => $event->sequence, ...$fields],
            json_decode(json_encode($event, JSON_THROW_ON_ERROR), true, flags: JSON_THROW_ON_ERROR));
        $copy = $event->jsonSerialize();
        $copy->runId = 'changed';
        if ($event instanceof AgentFailed) {
            $copy->error->message = 'changed';
            same('failed', $event->jsonSerialize()->error->message);
        }
        same('run', $event->runId);
        raises(\Error::class, static function () use ($event): void { $event->runId = 'changed'; });
    }
    raises(\InvalidArgumentException::class, static fn() => new AgentStarted('', 1));
    raises(\InvalidArgumentException::class, static fn() => new AgentStarted('run', 0));
    raises(\InvalidArgumentException::class, static fn() => new TextDelta('run', 1, "\xff"));
}

function coreValues(): void
{
    $json = (object) ['nested' => (object) ['value' => 'original'], 'empty' => new \stdClass(), 'list' => []];
    $message = new AssistantMessage('previous', replay: [$json]);
    $history = [&$message];
    $result = new RunResult('answer', $history, [$json]);
    $message = new UserMessage('replacement');
    $json->nested->value = 'changed';
    $result->output()[0]->nested->value = 'changed again';
    $result->history[0]->replay()[0]->nested->value = 'changed again';
    same('previous', $result->history[0]->text);
    same('original', $result->history[0]->replay()[0]->nested->value);
    same('original', $result->output()[0]->nested->value);
    same('{}', json_encode($result->output()[0]->empty));
    same('[]', json_encode($result->output()[0]->list));
    raises(\Error::class, static function () use ($result): void { $result->history[] = new UserMessage('changed'); });

    $prior = new UserMessage('original');
    $provider = new ScriptedProvider(static function (ProviderRequest $request): ProviderTurn {
        same('original', $request->history[0]->text);
        same('next', $request->history[1]->text);
        return new ProviderTurn('answer', [], [], []);
    });
    $result = (new Agent($provider, 'fixture'))->run('next', [&$prior], static function () use (&$prior): void {
        $prior = new UserMessage('changed');
    });
    same('original', $result->history[0]->text);
    same(3, count($result->history));
}

function coreProviderFailures(): void
{
    foreach ([false, true] as $partial) {
        $failure = new \LogicException('provider failed');
        $provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $signal) use ($partial, $failure): never {
            if ($partial) {
                $text('partial', $signal);
            }
            throw $failure;
        });
        $events = [];
        $original = [new UserMessage('seed')];
        $observe = static function (AgentEvent $event) use (&$events, $provider): void {
            if ($event instanceof AgentFailed) {
                same(1, $provider->closed);
            }
            $events[] = $event;
        };
        $thrown = raises(\LogicException::class,
            static fn() => (new Agent($provider, 'fixture'))->run('go', $original, $observe));
        same($failure, $thrown);
        same(1, count($original));
        same($partial ? 3 : 2, count($events));
        same(true, end($events) instanceof AgentFailed);
        same('provider failed', end($events)->message);
    }
    foreach ([false, true] as $anthropic) {
        $client = new FixtureClient();
        $response = $client->enqueue(sse($anthropic ? 'anthropic_tool' : 'openai_tool'));
        $events = [];
        $observe = static function (AgentEvent $event) use (&$events): void { $events[] = $event; };
        raises(ProviderException::class, static fn() => (new Agent(provider($anthropic, $client), 'fixture'))->run('go',
            onEvent: $observe), 'unregistered tool');
        same(true, $response->closed);
        same(true, end($events) instanceof AgentFailed);
        same(1, count($client->requests));
    }
}

function coreInputValidation(): void
{
    $provider = new ScriptedProvider(static fn(): ProviderTurn => new ProviderTurn('', [], [], []));
    raises(\InvalidArgumentException::class, static fn() => new Agent($provider, ''));
    raises(\InvalidArgumentException::class, static fn() => new Agent($provider, "\xff"));
    raises(\InvalidArgumentException::class, static fn() => new Agent($provider, 'fixture', "\xff"));
    $agent = new Agent($provider, 'fixture');
    foreach ([static fn() => $agent->run("\xff"), static fn() => $agent->run('go', runId: ''),
        static fn() => $agent->run('go', runId: "\xff"), static fn() => $agent->run('go', [new \stdClass()]),
        static fn() => new RunResult('', [new \stdClass()], [])] as $invalid) {
        raises(\InvalidArgumentException::class, $invalid);
    }
    raises(ProviderException::class, static fn() => $agent->run('go', [5 => new UserMessage('bad key')]));
    same(0, $provider->entered);
    $ids = [];
    for ($index = 0; $index < 2; $index++) {
        same('', $agent->run('', onEvent: static function (AgentEvent $event) use (&$ids): void {
            if ($event instanceof AgentStarted) {
                same(1, preg_match('/^r_[a-f0-9]{24}$/D', $event->runId));
                $ids[] = $event->runId;
            }
        })->text);
    }
    same(2, count(array_unique($ids)));
    foreach (['delta', 'final', 'error'] as $phase) {
        $failure = new \LogicException("\xff");
        $provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $signal) use ($phase, $failure): ProviderTurn {
            if ($phase === 'delta') {
                $text("\xff", $signal);
            }
            if ($phase === 'error') {
                throw $failure;
            }
            return new ProviderTurn("\xff", [], [], []);
        });
        $events = [];
        $observe = static function (AgentEvent $event) use (&$events): void {
            json_encode($event, JSON_THROW_ON_ERROR);
            $events[] = $event;
        };
        $error = raises($phase === 'error' ? \LogicException::class : \InvalidArgumentException::class,
            static fn() => (new Agent($provider, 'fixture'))->run('go', onEvent: $observe));
        same(2, count($events));
        same(true, end($events) instanceof AgentFailed);
        if ($phase === 'error') {
            same($failure, $error);
            same('Provider request failed.', end($events)->message);
        }
    }
}
