<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\AnthropicProvider;
use TinyAgent\AssistantMessage;
use TinyAgent\Cancelled;
use TinyAgent\CancellationToken;
use TinyAgent\HttpRequest;
use TinyAgent\OpenAIProvider;
use TinyAgent\Provider;
use TinyAgent\ProviderException;
use TinyAgent\ProviderRequest;
use TinyAgent\ProviderTurn;
use TinyAgent\ToolCall;
use TinyAgent\ToolDefinition;
use TinyAgent\ToolResultMessage;
use TinyAgent\UserMessage;

function request(array $history = []): ProviderRequest
{
    return new ProviderRequest('fixture', 'Instructions', $history ?: [new UserMessage('go')], [
        new ToolDefinition('echo', 'Echo text', (object) [
            'type' => 'object', 'properties' => (object) ['text' => (object) ['type' => 'string']],
            'required' => ['text'],
        ]),
    ]);
}

function provider(bool $anthropic, FixtureClient $client, float $timeout = 2): Provider
{
    return $anthropic
        ? new AnthropicProvider('fixture-key', 'http://fixture.invalid/api?mode=test', $timeout, client: $client)
        : new OpenAIProvider('fixture-key', 'http://fixture.invalid/api?mode=test', $timeout, $client);
}

function sharedFixtures(bool $anthropic): void
{
    $prefix = $anthropic ? 'anthropic' : 'openai';
    $client = new FixtureClient();
    $first = $client->enqueue(sse($prefix . '_tool'));
    $second = $client->enqueue(sse($prefix . '_text'));
    $provider = provider($anthropic, $client);
    $turn = $provider->stream(request());
    $call = $turn->toolCalls()[0];
    same('café', $call->arguments()->text);
    same(true, $first->closed);
    $sent = json_decode($client->requests[0]->body, flags: JSON_THROW_ON_ERROR);
    same(true, $sent->stream);
    same('http://fixture.invalid/api?mode=test', $client->requests[0]->endpoint);
    same($anthropic ? 'fixture-key' : 'Bearer fixture-key',
        $client->requests[0]->headers[$anthropic ? 'x-api-key' : 'Authorization']);
    if (!$anthropic) {
        same(false, $sent->store);
        same(['reasoning.encrypted_content'], $sent->include ?? null);
        same(false, $sent->parallel_tool_calls);
        same('object', $sent->tools[0]->parameters->type);
    } else {
        same('2023-06-01', $client->requests[0]->headers['anthropic-version']);
        same('signed-fixture', $turn->replay()[0]->content[0]->signature);
        same('keep', $turn->replay()[0]->fixture_metadata);
        same(8, $turn->replay()[0]->usage->input_tokens);
        same(12, $turn->replay()[0]->usage->output_tokens);
    }
    $deltas = '';
    $history = [new UserMessage('go'), $turn->assistant, new ToolResultMessage($call->callId, $call->name, 'café')];
    if ($anthropic) {
        $history[] = new ToolResultMessage('second', 'echo', 'failed', true);
    }
    $answer = $provider->stream(request($history), static function (string $text) use (&$deltas): void {
        $deltas .= $text;
    });
    same("done\ncafé", $answer->text);
    same($answer->text, $deltas);
    same(true, $second->closed);
    $sent = json_decode($client->requests[1]->body, flags: JSON_THROW_ON_ERROR);
    if ($anthropic) {
        same('signed-fixture', $sent->messages[1]->content[0]->signature);
        same(2, count($sent->messages[2]->content));
        same(true, $sent->messages[2]->content[1]->is_error);
    } else {
        same('opaque-fixture', $sent->input[1]->encrypted_content);
        same(['reasoning.encrypted_content'], $sent->include ?? null);
        same('café', json_decode($sent->input[2]->arguments)->text);
        same('function_call_output', $sent->input[3]->type);
    }
}

function immutableValues(): void
{
    $json = (object) ['nested' => (object) ['value' => 'keep'], 'empty' => new \stdClass(), 'list' => []];
    $call = new ToolCall('call', 'echo', $json);
    $tool = new ToolDefinition('echo', 'Echo', $json);
    $assistant = new AssistantMessage('hello', [$call], [$json]);
    $turn = new ProviderTurn('hello', [$call], [$json], [$json]);
    $json->nested->value = 'changed';
    $call->arguments()->nested->value = 'changed again';
    $assistant->replay()[0]->nested->value = 'changed again';
    $turn->output()[0]->nested->value = 'changed again';
    same('keep', $call->arguments()->nested->value);
    same('keep', $tool->parameters()->nested->value);
    same('keep', $assistant->replay()[0]->nested->value);
    same('keep', $turn->output()[0]->nested->value);
    same('{}', json_encode($call->arguments()->empty));
    same('[]', json_encode($call->arguments()->list));

    $message = new UserMessage('first');
    $history = [&$message];
    $request = new ProviderRequest('fixture', '', $history);
    $message = new UserMessage('replacement');
    same('first', $request->history[0]->text);
    $calls = [&$call];
    $assistant = new AssistantMessage('', $calls);
    $call = new ToolCall('replacement', 'echo', new \stdClass());
    same('call', $assistant->toolCalls[0]->callId);
    $tools = [&$tool];
    $request = new ProviderRequest('fixture', '', [], $tools);
    $tool = new ToolDefinition('replacement', '', true);
    same('echo', $request->tools[0]->name);
    $header = 'original';
    $request = new HttpRequest('http://fixture.invalid', ['Authorization' => &$header], '{}');
    $header = 'replacement';
    same('original', $request->headers['Authorization']);
}

function framingAndBounds(): void
{
    $complete = '{"type":"response.completed","response":{"output":[]}}';
    foreach ([
        ": comment\r\nevent: ignored\r\ndata: {\"type\":\"response.completed\",\r\ndata: \"response\":{\"output\":[]}}\r\n\r\n",
        'data: ' . $complete,
        "data: {$complete}\n\ndata: malformed trailing bytes\n\n",
    ] as $valid) {
        $client = new FixtureClient();
        $response = $client->enqueue($valid, 8192);
        same('', provider(false, $client)->stream(request())->text);
        same(true, $response->closed);
    }
    foreach ([
        "data: []\n\n", "data: {\"x\":NaN}\n\n", "data: {\"x\":1e999}\n\n",
        "data: [DONE]\n\n", "data: \xff\n\n",
        "data: {\"type\":\"response.output_text.delta\",\"delta\":42}\n\n",
        str_repeat('x', 1_048_577), 'data: ' . str_repeat(' ', 600_000) . "\ndata: " . str_repeat(' ', 600_000) . "\n",
    ] as $invalid) {
        reject(false, $invalid);
    }
}

function argumentBounds(): void
{
    $fragment = str_repeat(' ', 600_000);
    foreach ([false, true] as $anthropic) {
        $delta = $anthropic
            ? (object) ['type' => 'content_block_delta', 'index' => 0, 'delta' => (object) ['type' => 'input_json_delta', 'partial_json' => $fragment]]
            : (object) ['type' => 'response.function_call_arguments.delta', 'output_index' => 0, 'delta' => $fragment];
        $prefix = $anthropic ? events(
            (object) ['type' => 'message_start', 'message' => new \stdClass()],
            (object) ['type' => 'content_block_start', 'index' => 0, 'content_block' => (object) ['type' => 'tool_use', 'input' => new \stdClass()]],
        ) : '';
        reject($anthropic, $prefix . events($delta, $delta), 'Tool arguments');
    }
}

function malformedOpenAI(): void
{
    foreach ([
        '{"type":"response.function_call_arguments.delta","output_index":-1,"delta":"{}"}',
        '{"type":"response.function_call_arguments.delta","output_index":true,"delta":"{}"}',
        '{"type":"response.completed","response":{"status":"incomplete","output":[]}}',
        '{"type":"response.completed","response":{"output":[null]}}',
        '{"type":"response.failed"}', '{"type":"response.output_text.delta","delta":"partial"}',
    ] as $invalid) {
        reject(false, "data: {$invalid}\n\n");
    }
    $call = (object) ['type' => 'function_call', 'call_id' => 'c', 'name' => 'echo', 'arguments' => '{}'];
    reject(false, events((object) ['type' => 'response.completed', 'response' => (object) ['output' => [$call, $call]]]));
    $call->arguments = '[]';
    reject(false, events((object) ['type' => 'response.completed', 'response' => (object) ['output' => [$call]]]));
    $call->arguments = '{}';
    $client = new FixtureClient();
    $client->enqueue(events(
        (object) ['type' => 'response.output_item.added', 'output_index' => 100, 'item' => $call],
        (object) ['type' => 'response.completed', 'response' => new \stdClass()],
    ));
    same('c', provider(false, $client)->stream(request())->toolCalls()[0]->callId);
}

function malformedAnthropic(): void
{
    $body = sse('anthropic_tool');
    foreach ([
        'data: {"type":"message_stop"}' . "\n\n",
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}' . "\n\n",
        str_replace('"stop_reason":"tool_use"', '"stop_reason":"max_tokens"', $body),
        str_replace('"type":"signature_delta"', '"type":"text_delta"', $body),
        str_replace('data: {"type":"content_block_stop","index":1}', ': missing stop', $body),
        str_replace('"type":"input_json_delta"', '"type":"thinking_delta"', $body),
    ] as $invalid) {
        reject(true, $invalid);
    }
}

function reject(bool $anthropic, string $bytes, ?string $message = null): void
{
    $client = new FixtureClient();
    $response = $client->enqueue($bytes, 8192);
    raises(ProviderException::class, static fn() => provider($anthropic, $client)->stream(request()), $message);
    same(true, $response->closed);
}
