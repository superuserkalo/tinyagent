<?php
declare(strict_types=1);

namespace TinyAgent;

/** Configuration is shared; transcript, cancellation, and event order belong to each run. */
final readonly class Agent
{
    /** @var array<string, Tool> */
    private array $tools;
    /** @var list<ToolDefinition> */
    private array $definitions;

    /** @param list<Tool> $tools */
    public function __construct(private Provider $provider, public string $model, public string $instructions = '', array $tools = [])
    {
        if ($model === '') {
            throw new \InvalidArgumentException('Model cannot be empty.');
        }
        Json::text($model, 'Model');
        Json::text($instructions, 'Instructions');
        $registered = [];
        foreach (Json::list($tools, 'tools') as $tool) {
            if (!$tool instanceof Tool) {
                throw new \InvalidArgumentException('Expected a Tool.');
            }
            $name = $tool->definition->name;
            if (isset($registered[$name])) {
                throw new \InvalidArgumentException('Tool names must be unique.');
            }
            $registered[$name] = $tool;
        }
        $this->tools = $registered;
        $this->definitions = array_values(array_map(static fn(Tool $tool): ToolDefinition => $tool->definition, $registered));
    }

    /** @param list<Message> $history
     *  @param null|callable(AgentEvent, CancellationToken): void $onEvent
     */
    public function run(
        string $prompt,
        array $history = [],
        ?callable $onEvent = null,
        ?CancellationToken $cancellation = null,
        ?string $runId = null,
    ): RunResult {
        Json::text($prompt, 'Prompt');
        $runId ??= 'r_' . bin2hex(random_bytes(12));
        if ($runId === '') {
            throw new \InvalidArgumentException('Run ID cannot be empty.');
        }
        Json::text($runId, 'Run ID');
        Json::list($history, 'history');
        $request = new ProviderRequest($this->model, $this->instructions, [...$history, new UserMessage($prompt)], $this->definitions);
        $cancellation ??= new CancellationToken();
        $scope = $cancellation->child();
        $sequence = 0;
        $consumerFailure = null;
        $failure = null;
        $emit = static function (AgentEvent $event, CancellationToken $signal) use ($onEvent, &$consumerFailure): void {
            if ($consumerFailure !== null) {
                throw $consumerFailure;
            }
            try {
                if ($onEvent !== null) {
                    $onEvent($event, $signal);
                }
            } catch (\Throwable $error) {
                $consumerFailure = $error;
                throw $error;
            }
        };
        try {
            $emit(new AgentStarted($runId, ++$sequence), $scope);
            $scope->throwIfCancelled();
            while (true) {
                $streamed = false;
                $requestToken = $scope->child();
                try {
                    $turn = $this->provider->stream($request, static function (string $delta, CancellationToken $signal) use (
                        $emit, $scope, $requestToken, $runId, &$sequence, &$consumerFailure, &$streamed,
                    ): void {
                        if ($consumerFailure !== null) {
                            throw $consumerFailure;
                        }
                        $requestToken->throwIfCancelled();
                        try {
                            $signal->throwIfCancelled();
                            Json::text($delta, 'Provider text delta');
                            if ($delta !== '') {
                                $streamed = true;
                                $emit(new TextDelta($runId, ++$sequence, $delta), $signal);
                            }
                            $requestToken->throwIfCancelled();
                            $signal->throwIfCancelled();
                        } catch (Cancelled $error) {
                            if (!$error->timedOut) {
                                $scope->cancel();
                            }
                            throw $error;
                        }
                    }, $requestToken);
                    $requestToken->throwIfCancelled();
                } finally {
                    if ($requestToken->isCancelled()) {
                        $scope->cancel();
                    }
                    $requestToken->cancel();
                }
                if ($consumerFailure !== null) {
                    throw $consumerFailure;
                }
                $scope->throwIfCancelled();
                foreach ($turn->toolCalls() as $call) {
                    if (!isset($this->tools[$call->name])) {
                        throw new ProviderException('Provider requested an unregistered tool: ' . $call->name);
                    }
                }
                if (!$streamed && $turn->text !== '') {
                    $emit(new TextDelta($runId, ++$sequence, $turn->text), $scope);
                }
                $scope->throwIfCancelled();
                $transcript = [...$request->history, $turn->assistant];
                if ($turn->toolCalls() === []) {
                    $result = new RunResult($turn->text, $transcript, $turn->output());
                    break;
                }
                foreach ($turn->toolCalls() as $call) {
                    $transcript[] = $this->executeTool($call, $scope, $emit, $runId, $sequence);
                }
                $request = new ProviderRequest($this->model, $this->instructions, $transcript, $this->definitions);
            }
        } catch (\Throwable $error) {
            $failure = $consumerFailure ?? Cooperative::interruption($error) ?? $error;
        } finally {
            $cancelled = $scope->isCancelled();
            $scope->cancel();
        }
        if (($cancelled || $cancellation->isCancelled()) && $consumerFailure === null) {
            $failure = new Cancelled();
        }
        // Final delivery can cancel this run without cancelling the caller's token.
        $delivery = $cancellation->child();
        try {
            if ($failure !== null) {
                if ($consumerFailure === null) {
                    $message = $failure->getMessage();
                    if (preg_match('//u', $message) !== 1) {
                        $message = 'Provider request failed.';
                    }
                    $terminal = $failure instanceof Cancelled
                        ? new AgentAborted($runId, ++$sequence)
                        : new AgentFailed($runId, ++$sequence, $message);
                    $emit($terminal, $delivery);
                }
                throw $failure;
            }
            $emit(new AgentCompleted($runId, ++$sequence, $result->text), $delivery);
            $delivery->throwIfCancelled();
            return $result;
        } finally {
            $delivery->cancel();
        }
    }

    /** @param callable(AgentEvent, CancellationToken): void $emit */
    private function executeTool(ToolCall $call, CancellationToken $scope, callable $emit, string $runId, int &$sequence): ToolResultMessage
    {
        $scope->throwIfCancelled();
        $emit(new ToolExecutionStarted($runId, ++$sequence, $call->callId, $call->name, $call->arguments()), $scope);
        $output = 'Tool cancelled.';
        $failed = true;
        try {
            $output = $this->tools[$call->name]->call($call->arguments(), $scope);
            $scope->throwIfCancelled();
            Json::text($output, 'Tool output');
            if (strlen($output) > Tool::MAX_OUTPUT_BYTES) {
                throw new \RuntimeException('Tool output exceeds ' . Tool::MAX_OUTPUT_BYTES . ' UTF-8 bytes.');
            }
            $failed = false;
        } catch (Cancelled $error) {
            $output = 'Tool cancelled.';
            throw $error;
        } catch (\Throwable $error) {
            if (($interruption = Cooperative::interruption($error)) !== null) {
                throw $interruption;
            }
            $detail = json_decode(json_encode(substr($error->getMessage(), 0, 2000),
                JSON_THROW_ON_ERROR | JSON_INVALID_UTF8_SUBSTITUTE), flags: JSON_THROW_ON_ERROR);
            $output = $scope->isCancelled() ? 'Tool cancelled.' : 'Tool failed: ' . $detail;
        } finally {
            $emit(new ToolExecutionEnded($runId, ++$sequence, $call->callId, $call->name, $output, $failed), $scope);
        }
        $scope->throwIfCancelled();
        return new ToolResultMessage($call->callId, $call->name, $output, $failed);
    }
}
