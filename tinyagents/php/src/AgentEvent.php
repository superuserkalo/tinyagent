<?php
declare(strict_types=1);

namespace TinyAgent;

/** Immutable events; JSON serialization returns a detached protocol object. */
abstract readonly class AgentEvent implements \JsonSerializable
{
    public function __construct(public string $runId, public int $sequence)
    {
        if ($runId === '' || $sequence < 1) {
            throw new \InvalidArgumentException('Events require a run ID and positive sequence.');
        }
        Json::text($runId, 'Run ID');
    }

    /** @return array<string, string|bool|\stdClass> */
    abstract protected function payload(): array;

    final public function jsonSerialize(): \stdClass
    {
        return (object) ['runId' => $this->runId, 'sequence' => $this->sequence, ...$this->payload()];
    }
}

final readonly class AgentStarted extends AgentEvent
{
    protected function payload(): array
    {
        return ['type' => 'agent_start'];
    }
}

final readonly class TextDelta extends AgentEvent
{
    public function __construct(string $runId, int $sequence, public string $delta)
    {
        parent::__construct($runId, $sequence);
        Json::text($delta, 'Text delta');
    }

    protected function payload(): array
    {
        return ['type' => 'text_delta', 'delta' => $this->delta];
    }
}

final readonly class ToolExecutionStarted extends AgentEvent
{
    private \stdClass $arguments;

    public function __construct(string $runId, int $sequence, public string $toolCallId, public string $toolName, \stdClass $arguments)
    {
        parent::__construct($runId, $sequence);
        Json::text($toolCallId, 'Tool call ID');
        Json::text($toolName, 'Tool name');
        $this->arguments = Json::copyObject($arguments);
    }

    public function arguments(): \stdClass
    {
        return Json::copyObject($this->arguments);
    }

    protected function payload(): array
    {
        return ['type' => 'tool_execution_start', 'toolCallId' => $this->toolCallId,
            'toolName' => $this->toolName, 'arguments' => $this->arguments()];
    }
}

final readonly class ToolExecutionEnded extends AgentEvent
{
    public function __construct(string $runId, int $sequence, public string $toolCallId, public string $toolName,
        public string $output, public bool $isError)
    {
        parent::__construct($runId, $sequence);
        Json::text($toolCallId, 'Tool call ID');
        Json::text($toolName, 'Tool name');
        Json::text($output, 'Tool output');
    }

    protected function payload(): array
    {
        return ['type' => 'tool_execution_end', 'toolCallId' => $this->toolCallId,
            'toolName' => $this->toolName, 'output' => $this->output, 'isError' => $this->isError];
    }
}

final readonly class AgentCompleted extends AgentEvent
{
    public function __construct(string $runId, int $sequence, public string $text)
    {
        parent::__construct($runId, $sequence);
        Json::text($text, 'Final text');
    }

    protected function payload(): array
    {
        return ['type' => 'agent_end', 'status' => 'completed', 'text' => $this->text];
    }
}

final readonly class AgentFailed extends AgentEvent
{
    public function __construct(string $runId, int $sequence, public string $message)
    {
        parent::__construct($runId, $sequence);
        Json::text($message, 'Error message');
    }

    protected function payload(): array
    {
        return ['type' => 'agent_end', 'status' => 'failed', 'error' => (object) [
            'code' => 'provider_error', 'message' => $this->message, 'retryable' => false,
        ]];
    }
}

final readonly class AgentAborted extends AgentEvent
{
    protected function payload(): array
    {
        return ['type' => 'agent_end', 'status' => 'aborted'];
    }
}
