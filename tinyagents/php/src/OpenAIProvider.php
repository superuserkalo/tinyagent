<?php
declare(strict_types=1);

namespace TinyAgent;

final readonly class OpenAIProvider implements Provider
{
    private ProviderStream $stream;

    public function __construct(
        private ?string $apiKey = null,
        private string $endpoint = 'https://api.openai.com/v1/responses',
        private float $timeout = 180,
        ?HttpClient $client = null,
    ) {
        new HttpRequest($endpoint, [], '', $timeout);
        $this->stream = new ProviderStream($client ?? new CurlClient());
    }

    public function stream(ProviderRequest $request, ?callable $onText = null, ?CancellationToken $cancellation = null): ProviderTurn
    {
        $cancellation ??= new CancellationToken();
        $cancellation->throwIfCancelled();
        $key = $this->apiKey ?? getenv('OPENAI_API_KEY');
        if ($key === false || trim($key) === '') {
            throw new ProviderException('OPENAI_API_KEY is not set.');
        }
        $input = [];
        foreach ($request->history as $message) {
            if ($message instanceof UserMessage) {
                $input[] = ['role' => 'user', 'content' => $message->text];
            } elseif ($message instanceof AssistantMessage) {
                $replay = $message->replay();
                if ($replay !== []) {
                    array_push($input, ...$replay);
                    continue;
                }
                if ($message->text !== '') {
                    $input[] = ['role' => 'assistant', 'content' => $message->text];
                }
                foreach ($message->toolCalls as $call) {
                    $input[] = [
                        'type' => 'function_call', 'call_id' => $call->callId, 'name' => $call->name,
                        'arguments' => Json::encode($call->arguments()),
                    ];
                }
            } elseif ($message instanceof ToolResultMessage) {
                $input[] = ['type' => 'function_call_output', 'call_id' => $message->callId, 'output' => $message->output];
            }
        }
        $tools = array_map(static fn(ToolDefinition $tool): array => [
            'type' => 'function', 'name' => $tool->name, 'description' => $tool->description,
            'parameters' => $tool->parameters(), 'strict' => true,
        ], $request->tools);
        $body = Json::encode([
            'model' => $request->model, 'instructions' => $request->instructions,
            'input' => $input, 'tools' => $tools,
            'parallel_tool_calls' => false, 'store' => false, 'stream' => true,
            'include' => ['reasoning.encrypted_content'],
        ]);
        return $this->stream->run(
            new HttpRequest($this->endpoint, ['Authorization' => "Bearer {$key}"], $body, $this->timeout),
            new OpenAIResponse(), $onText, $cancellation,
        );
    }
}

/** @internal */
final class OpenAIResponse implements ResponseParser
{
    /** @var array<int, \stdClass> */
    private array $output = [];
    /** @var array<int, Arguments> */
    private array $arguments = [];

    public function consume(\stdClass $event, ?callable $onText, CancellationToken $cancellation): ?ProviderTurn
    {
        $type = Json::string($event, 'type', true);
        switch ($type) {
            case 'response.output_item.added':
            case 'response.output_item.done':
                $index = Json::index($event->output_index ?? null);
                $item = Json::copyObject(Json::object($event->item ?? null, 'output item'));
                if (Json::string($item, 'type', true) === 'function_call') {
                    $text = Json::string($item, 'arguments');
                    if ($text !== '') {
                        $this->buffer($index)->set($text);
                    }
                }
                $this->output[$index] = $item;
                break;
            case 'response.output_text.delta':
                $text = Json::string($event, 'delta');
                if ($text !== '' && $onText !== null) {
                    $onText($text, $cancellation);
                }
                break;
            case 'response.function_call_arguments.delta':
                $this->buffer(Json::index($event->output_index ?? null))->append(Json::string($event, 'delta'));
                break;
            case 'response.function_call_arguments.done':
                $this->buffer(Json::index($event->output_index ?? null))->set(Json::string($event, 'arguments'));
                break;
            case 'response.completed':
                $response = Json::object($event->response ?? null, 'completed response');
                if (property_exists($response, 'status') && Json::string($response, 'status') !== 'completed') {
                    throw new ProviderException('OpenAI response is incomplete.');
                }
                if (property_exists($response, 'output')) {
                    $this->output = Json::copyObjects(Json::list($response->output, 'output'));
                }
                return $this->complete();
            case 'response.failed':
            case 'response.incomplete':
            case 'error':
                throw new ProviderException("OpenAI stream failed: {$type}");
        }
        return null;
    }

    private function buffer(int $index): Arguments
    {
        return $this->arguments[$index] ??= new Arguments();
    }

    private function complete(): ProviderTurn
    {
        ksort($this->output, SORT_NUMERIC);
        $calls = [];
        $ids = [];
        $text = '';
        foreach ($this->output as $index => $item) {
            switch (Json::string($item, 'type', true)) {
                case 'function_call':
                    $id = Json::string($item, 'call_id', true);
                    $name = Json::string($item, 'name', true);
                    if (isset($ids[$id])) {
                        throw new ProviderException('Duplicate OpenAI tool call.');
                    }
                    $ids[$id] = true;
                    $raw = Json::string($item, 'arguments');
                    if ($raw === '' && isset($this->arguments[$index])) {
                        $raw = $this->arguments[$index]->text();
                    }
                    if (strlen($raw) > Json::MAX_BYTES) {
                        throw new ProviderException('Tool arguments exceed their byte limit.');
                    }
                    $arguments = Json::parseObject($raw);
                    $item->arguments = $raw;
                    $calls[] = new ToolCall($id, $name, $arguments);
                    break;
                case 'message':
                    foreach (Json::list($item->content ?? null, 'message content') as $part) {
                        $part = Json::object($part, 'message content');
                        if (Json::string($part, 'type', true) === 'output_text') {
                            $text .= Json::string($part, 'text');
                        }
                    }
                    break;
            }
        }
        $output = array_values($this->output);
        return new ProviderTurn($text, $calls, $output, $output);
    }
}
