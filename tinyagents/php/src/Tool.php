<?php
declare(strict_types=1);

namespace TinyAgent;

final readonly class Tool
{
    public const MAX_OUTPUT_BYTES = 131_072;
    public ToolDefinition $definition;
    private ToolSchema $schema;
    private \Closure $handler;

    /** @param callable(\stdClass, CancellationToken): string $handler */
    public function __construct(string $name, string $description, \stdClass|bool $parameters, callable $handler)
    {
        $this->definition = new ToolDefinition($name, $description, $parameters);
        $this->schema = new ToolSchema($this->definition->parameters());
        $this->handler = \Closure::fromCallable($handler);
    }

    public function call(\stdClass $arguments, ?CancellationToken $cancellation = null): string
    {
        $cancellation ??= new CancellationToken();
        $cancellation->throwIfCancelled();
        $snapshot = Json::copyObject($arguments);
        if (!$this->schema->valid($snapshot)) {
            throw new \InvalidArgumentException("Invalid arguments for tool '{$this->definition->name}'.");
        }
        $cancellation->throwIfCancelled();
        return ($this->handler)($snapshot, $cancellation);
    }
}
