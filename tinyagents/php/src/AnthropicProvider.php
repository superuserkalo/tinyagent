<?php
declare(strict_types=1);

namespace TinyAgent;

final readonly class AnthropicProvider implements Provider
{
    private ProviderStream $stream;

    public function __construct(
        private ?string $apiKey = null,
        private string $endpoint = 'https://api.anthropic.com/v1/messages',
        private float $timeout = 180,
        private int $maxTokens = 8192,
        ?HttpClient $client = null,
    ) {
        new HttpRequest($endpoint, [], '', $timeout);
        if ($maxTokens <= 0) {
            throw new \InvalidArgumentException('maxTokens must be positive.');
        }
        $this->stream = new ProviderStream($client ?? new CurlClient());
    }

    public function stream(ProviderRequest $request, ?callable $onText = null, ?CancellationToken $cancellation = null): ProviderTurn
    {
        $cancellation ??= new CancellationToken();
        $cancellation->throwIfCancelled();
        $key = $this->apiKey ?? getenv('ANTHROPIC_API_KEY');
        if ($key === false || trim($key) === '') {
            throw new ProviderException('ANTHROPIC_API_KEY is not set.');
        }
        $messages = [];
        foreach ($request->history as $message) {
            if ($message instanceof UserMessage) {
                $messages[] = ['role' => 'user', 'content' => $message->text];
            } elseif ($message instanceof AssistantMessage) {
                $replay = $message->replay();
                $content = [];
                if ($replay !== []) {
                    $content = Json::list($replay[0]->content ?? null, 'Anthropic replay content');
                } else {
                    if ($message->text !== '') {
                        $content[] = ['type' => 'text', 'text' => $message->text];
                    }
                    foreach ($message->toolCalls as $call) {
                        $content[] = [
                            'type' => 'tool_use', 'id' => $call->callId,
                            'name' => $call->name, 'input' => $call->arguments(),
                        ];
                    }
                }
                $messages[] = ['role' => 'assistant', 'content' => $content];
            } elseif ($message instanceof ToolResultMessage) {
                $block = [
                    'type' => 'tool_result', 'tool_use_id' => $message->callId,
                    'content' => $message->output, 'is_error' => $message->isError,
                ];
                $last = count($messages) - 1;
                $previous = $messages[$last] ?? null;
                if ($previous !== null && $previous['role'] === 'user' && is_array($previous['content'])
                    && array_all($previous['content'], static fn(array|\stdClass $item): bool =>
                        ($item instanceof \stdClass ? ($item->type ?? null) : ($item['type'] ?? null)) === 'tool_result')) {
                    $messages[$last]['content'][] = $block;
                } else {
                    $messages[] = ['role' => 'user', 'content' => [$block]];
                }
            }
        }
        $payload = [
            'model' => $request->model, 'messages' => $messages, 'max_tokens' => $this->maxTokens, 'stream' => true,
        ];
        if ($request->instructions !== '') {
            $payload['system'] = $request->instructions;
        }
        if ($request->tools !== []) {
            $payload['tools'] = array_map(static fn(ToolDefinition $tool): array => [
                'name' => $tool->name, 'description' => $tool->description, 'input_schema' => $tool->parameters(),
            ], $request->tools);
        }
        return $this->stream->run(
            new HttpRequest($this->endpoint, ['x-api-key' => $key, 'anthropic-version' => '2023-06-01'], Json::encode($payload), $this->timeout),
            new AnthropicResponse(), $onText, $cancellation,
        );
    }
}

/** @internal */
final class AnthropicBlock
{
    public bool $open = true;
    public readonly Arguments $arguments;

    public function __construct(public readonly \stdClass $value)
    {
        $this->arguments = new Arguments();
    }
}

/** @internal */
final class AnthropicResponse implements ResponseParser
{
    /** @var array<int, AnthropicBlock> */
    private array $blocks = [];
    private ?\stdClass $message = null;
    private \stdClass $usage;

    public function __construct()
    {
        $this->usage = new \stdClass();
    }

    public function consume(\stdClass $event, ?callable $onText, CancellationToken $cancellation): ?ProviderTurn
    {
        $type = Json::string($event, 'type', true);
        switch ($type) {
            case 'message_start':
                if ($this->message !== null) {
                    throw new ProviderException('Repeated Anthropic message_start.');
                }
                $this->message = Json::copyObject(Json::object($event->message ?? null, 'message'));
                if (property_exists($this->message, 'usage')) {
                    $this->merge($this->usage, Json::object($this->message->usage, 'usage'));
                }
                break;
            case 'content_block_start':
                $index = Json::index($event->index ?? null);
                if ($this->message === null || isset($this->blocks[$index])) {
                    throw new ProviderException('Invalid Anthropic block order.');
                }
                $item = Json::copyObject(Json::object($event->content_block ?? null, 'content block'));
                Json::string($item, 'type', true);
                $this->blocks[$index] = new AnthropicBlock($item);
                break;
            case 'content_block_delta':
            case 'content_block_stop':
                $index = Json::index($event->index ?? null);
                $block = $this->blocks[$index] ?? null;
                if ($block === null || !$block->open) {
                    throw new ProviderException('Anthropic event referenced an unopened block.');
                }
                if ($type === 'content_block_stop') {
                    if (Json::string($block->value, 'type', true) === 'tool_use') {
                        $raw = $block->arguments->text();
                        if ($raw !== '') {
                            $block->value->input = Json::parseObject($raw);
                        }
                        Json::object($block->value->input ?? null, 'tool arguments');
                    }
                    $block->open = false;
                    break;
                }
                $delta = Json::object($event->delta ?? null, 'block delta');
                $kind = Json::string($delta, 'type', true);
                $fields = match ($kind) {
                    'text_delta' => ['text', 'text'],
                    'thinking_delta' => ['thinking', 'thinking'],
                    'signature_delta' => ['thinking', 'signature'],
                    'input_json_delta' => ['tool_use', 'partial_json'],
                    default => null,
                };
                if ($fields === null) {
                    break;
                }
                [$expected, $field] = $fields;
                if (Json::string($block->value, 'type', true) !== $expected) {
                    throw new ProviderException('Anthropic delta does not match its block.');
                }
                $addition = Json::string($delta, $field);
                if ($kind === 'input_json_delta') {
                    $block->arguments->append($addition);
                } else {
                    $previous = property_exists($block->value, $field) ? Json::string($block->value, $field) : '';
                    $block->value->{$field} = $previous . $addition;
                    if ($kind === 'text_delta' && $addition !== '' && $onText !== null) {
                        $onText($addition, $cancellation);
                    }
                }
                break;
            case 'message_delta':
                if ($this->message === null) {
                    throw new ProviderException('Anthropic delta preceded message_start.');
                }
                $this->merge($this->message, Json::object($event->delta ?? null, 'message delta'));
                if (property_exists($event, 'usage')) {
                    $this->merge($this->usage, Json::object($event->usage, 'usage delta'));
                }
                break;
            case 'message_stop':
                return $this->complete();
            case 'error':
                $error = Json::object($event->error ?? null, 'stream error');
                throw new ProviderException(Json::string($error, 'message'));
        }
        return null;
    }

    private function complete(): ProviderTurn
    {
        if ($this->message === null) {
            throw new ProviderException('Anthropic stream ended before a complete message.');
        }
        $reason = Json::string($this->message, 'stop_reason', true);
        if (!in_array($reason, ['end_turn', 'tool_use', 'stop_sequence', 'refusal'], true)) {
            throw new ProviderException("Anthropic response is incomplete: {$reason}");
        }
        ksort($this->blocks, SORT_NUMERIC);
        $text = '';
        $calls = [];
        $ids = [];
        $output = [];
        foreach ($this->blocks as $block) {
            if ($block->open) {
                throw new ProviderException('Anthropic stream ended with an unfinished content block.');
            }
            $item = $block->value;
            switch (Json::string($item, 'type', true)) {
                case 'tool_use':
                    $id = Json::string($item, 'id', true);
                    $name = Json::string($item, 'name', true);
                    if (isset($ids[$id])) {
                        throw new ProviderException('Duplicate Anthropic tool call.');
                    }
                    $ids[$id] = true;
                    $arguments = Json::object($item->input ?? null, 'tool arguments');
                    if (strlen(Json::encode($arguments)) > Json::MAX_BYTES) {
                        throw new ProviderException('Tool arguments exceed their byte limit.');
                    }
                    $calls[] = new ToolCall($id, $name, $arguments);
                    break;
                case 'text':
                    $text .= Json::string($item, 'text');
                    break;
            }
            $output[] = Json::copyObject($item);
        }
        $message = Json::copyObject($this->message);
        $message->content = $output;
        $message->usage = Json::copyObject($this->usage);
        return new ProviderTurn($text, $calls, [$message], $output);
    }

    private function merge(\stdClass $target, \stdClass $update): void
    {
        foreach (Json::copyObject($update) as $key => $value) {
            $target->{$key} = $value;
        }
    }
}
