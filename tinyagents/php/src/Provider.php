<?php
declare(strict_types=1);

namespace TinyAgent;

final class ProviderException extends \RuntimeException {}

interface Provider
{
    /** @param null|callable(string, CancellationToken): void $onText */
    public function stream(
        ProviderRequest $request,
        ?callable $onText = null,
        ?CancellationToken $cancellation = null,
    ): ProviderTurn;
}

/** Provider metadata; executable tools expose this immutable definition. */
final readonly class ToolDefinition
{
    private \stdClass|bool $parameters;

    public function __construct(public string $name, public string $description, \stdClass|bool $parameters)
    {
        if ($name === '') {
            throw new \InvalidArgumentException('Tool name cannot be empty.');
        }
        Json::text($name, 'Tool name');
        Json::text($description, 'Tool description');
        $this->parameters = is_bool($parameters) ? $parameters : Json::copyObject($parameters);
    }

    public function parameters(): \stdClass|bool
    {
        return is_bool($this->parameters) ? $this->parameters : Json::copyObject($this->parameters);
    }
}

final readonly class ToolCall
{
    private \stdClass $arguments;

    public function __construct(public string $callId, public string $name, \stdClass $arguments)
    {
        if ($callId === '' || $name === '') {
            throw new \InvalidArgumentException('Tool call requires an ID and name.');
        }
        $this->arguments = Json::copyObject($arguments);
    }

    public function arguments(): \stdClass
    {
        return Json::copyObject($this->arguments);
    }
}

final readonly class ProviderRequest
{
    /** @var list<Message> */
    public array $history;
    /** @var list<ToolDefinition> */
    public array $tools;

    /** @param list<Message> $history
     *  @param list<ToolDefinition> $tools
     */
    public function __construct(public string $model, public string $instructions, array $history, array $tools = [])
    {
        if ($model === '') {
            throw new \InvalidArgumentException('Model cannot be empty.');
        }
        foreach (Json::list($history, 'history') as $message) {
            if (!$message instanceof UserMessage && !$message instanceof AssistantMessage && !$message instanceof ToolResultMessage) {
                throw new \InvalidArgumentException('Unsupported history message.');
            }
        }
        foreach (Json::list($tools, 'tools') as $tool) {
            if (!$tool instanceof ToolDefinition) {
                throw new \InvalidArgumentException('Expected a ToolDefinition.');
            }
        }
        $this->history = array_map(static fn(Message $message): Message => $message, $history);
        $this->tools = array_map(static fn(ToolDefinition $tool): ToolDefinition => $tool, $tools);
    }
}

final readonly class ProviderTurn
{
    public AssistantMessage $assistant;
    /** @var list<\stdClass> */
    private array $output;

    /** @param list<ToolCall> $toolCalls
     *  @param list<\stdClass> $replay
     *  @param list<\stdClass> $output
     */
    public function __construct(public string $text, array $toolCalls, array $replay, array $output)
    {
        $this->assistant = new AssistantMessage($text, $toolCalls, $replay);
        $this->output = Json::copyObjects($output);
    }

    /** @return list<ToolCall> */
    public function toolCalls(): array
    {
        return $this->assistant->toolCalls;
    }

    /** @return list<\stdClass> */
    public function replay(): array
    {
        return $this->assistant->replay();
    }

    /** @return list<\stdClass> */
    public function output(): array
    {
        return Json::copyObjects($this->output);
    }
}

/** @internal */
interface ResponseParser
{
    /** @param null|callable(string, CancellationToken): void $onText */
    public function consume(\stdClass $event, ?callable $onText, CancellationToken $cancellation): ?ProviderTurn;
}
