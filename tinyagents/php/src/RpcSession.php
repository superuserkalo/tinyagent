<?php
declare(strict_types=1);

namespace TinyAgent;

/** Parsed RPC commands. The embedding application owns fiber advancement and byte transport. */
final class RpcSession
{
    private readonly Session $session;
    private readonly \Closure $emit;

    /** @param callable(\stdClass, ?CancellationToken): void $emit */
    public function __construct(Agent $agent, callable $emit)
    {
        $this->session = new Session($agent);
        $this->emit = \Closure::fromCallable($emit);
    }

    public function handle(mixed $command): void
    {
        if (!$command instanceof \stdClass || get_class($command) !== \stdClass::class) {
            $this->error(null, null, 'invalid_command', 'RPC command must be an object');
            return;
        }
        if (!self::validJson($command)) {
            $this->error(null, null, 'invalid_json', 'RPC input is not valid JSON');
            return;
        }
        if (property_exists($command, 'id') && !is_string($command->id)) {
            $this->error(null, null, 'invalid_command', 'command.id must be a string');
            return;
        }
        $id = $command->id ?? null;
        $type = is_string($command->type ?? null) ? $command->type : 'unknown';
        if (!in_array($type, ['prompt', 'abort', 'new_session', 'get_state'], true)) {
            $this->error($id, $type, 'invalid_command', 'Unknown command type');
            return;
        }
        if ($type === 'prompt' && (!is_string($command->message ?? null) || $command->message === '')) {
            $this->error($id, $type, 'invalid_command', 'prompt.message must be a non-empty string');
            return;
        }
        try {
            $data = $this->execute($command, $id, $type);
        } catch (SessionException $error) {
            $this->error($id, $type, $error->reason === 'busy' ? 'busy' : 'session_closed', $error->getMessage());
            return;
        }
        if ($data !== null) {
            ($this->emit)(self::response($id, $type, $data), null);
        }
    }

    public function state(): \stdClass
    {
        $state = $this->session->state();
        return (object) [
            'protocolVersion' => 1,
            'status' => $state['runId'] === null ? 'idle' : 'running',
            ...$state,
        ];
    }

    public function advance(): bool { return $this->session->advance(); }
    public function awaitIdle(): bool { return $this->session->awaitIdle(); }
    public function cancel(): ?string { return $this->session->cancel(); }
    public function stop(): ?string { return $this->session->stop(); }
    public function close(): bool { return $this->session->close(); }

    private function execute(\stdClass $command, ?string $id, string $type): ?\stdClass
    {
        switch ($type) {
            case 'prompt':
                $emit = $this->emit;
                $this->session->start($command->message,
                    static fn(string $runId, CancellationToken $signal) =>
                        $emit(self::response($id, $type, (object) ['runId' => $runId]), $signal),
                    static fn(AgentEvent $event, CancellationToken $signal) => $emit($event->jsonSerialize(), $signal),
                );
                return null;
            case 'abort':
                $runId = $this->cancel();
                return (object) ['accepted' => $runId !== null, 'runId' => $runId];
            case 'new_session':
                return (object) ['abortedRun' => $this->session->reset()];
            case 'get_state':
                return $this->state();
            default:
                throw new \LogicException('Command was not validated.');
        }
    }

    private static function response(?string $id, string $command, \stdClass $data): \stdClass
    {
        $value = (object) ['type' => 'response', 'command' => $command, 'success' => true, 'data' => $data];
        if ($id !== null) {
            $value->id = $id;
        }
        return $value;
    }

    private function error(?string $id, ?string $command, string $code, string $message): void
    {
        $value = (object) ['type' => 'protocol_error', 'error' => (object) ['code' => $code, 'message' => $message]];
        if ($id !== null) {
            $value->type = 'response';
            $value->id = $id;
            $value->command = $command;
            $value->success = false;
        }
        ($this->emit)($value, null);
    }

    private static function validJson(mixed $value, int $depth = 0): bool
    {
        if ($depth > 100) {
            return false;
        }
        if (is_string($value)) {
            return preg_match('//u', $value) === 1;
        }
        if ($value === null || is_bool($value) || is_int($value)) {
            return true;
        }
        if (is_float($value)) {
            return is_finite($value);
        }
        if (is_array($value)) {
            if (!array_is_list($value)) {
                return false;
            }
        } elseif (!$value instanceof \stdClass || get_class($value) !== \stdClass::class) {
            return false;
        }
        foreach ($value as $name => $item) {
            if (preg_match('//u', (string) $name) !== 1 || !self::validJson($item, $depth + 1)) {
                return false;
            }
        }
        return true;
    }
}
