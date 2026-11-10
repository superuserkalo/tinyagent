<?php
declare(strict_types=1);

namespace TinyAgent;

interface Message {}

final readonly class UserMessage implements Message
{
    public function __construct(public string $text) {}
}

final readonly class AssistantMessage implements Message
{
    /** @var list<ToolCall> */
    public array $toolCalls;
    /** @var list<\stdClass> */
    private array $replay;

    /** @param list<ToolCall> $toolCalls
     *  @param list<\stdClass> $replay
     */
    public function __construct(public string $text, array $toolCalls = [], array $replay = [])
    {
        foreach (Json::list($toolCalls, 'tool calls') as $call) {
            if (!$call instanceof ToolCall) {
                throw new \InvalidArgumentException('Expected a ToolCall.');
            }
        }
        $this->toolCalls = array_map(static fn(ToolCall $call): ToolCall => $call, $toolCalls);
        $this->replay = Json::copyObjects($replay);
    }

    /** @return list<\stdClass> */
    public function replay(): array
    {
        return Json::copyObjects($this->replay);
    }
}

final readonly class ToolResultMessage implements Message
{
    public function __construct(
        public string $callId,
        public string $name,
        public string $output,
        public bool $isError = false,
    ) {
        if ($callId === '' || $name === '') {
            throw new \InvalidArgumentException('Tool result requires a call ID and name.');
        }
    }
}
