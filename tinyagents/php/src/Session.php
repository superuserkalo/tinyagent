<?php
declare(strict_types=1);

namespace TinyAgent;

/** @internal Admission errors are separate from event-consumer failures. */
final class SessionException extends \LogicException
{
    public function __construct(public readonly string $reason, string $message)
    {
        parent::__construct($message);
    }
}

/** @internal A session retains this run through acknowledgement, delivery, and cleanup. */
final readonly class SessionRun
{
    public string $id;
    public CancellationToken $cancellation;
    public \Fiber $fiber;

    /** @param list<Message> $history
     *  @param callable(string, CancellationToken): void $before
     *  @param callable(AgentEvent, CancellationToken): void $events
     */
    public function __construct(Agent $agent, string $prompt, array $history, callable $before, callable $events)
    {
        $this->id = 'r_' . bin2hex(random_bytes(12));
        $this->cancellation = new CancellationToken();
        $id = $this->id;
        $token = $this->cancellation;
        $this->fiber = Cooperative::fiber(static function () use ($agent, $prompt, $history, $before, $events, $id, $token): ?RunResult {
            try {
                $before($id, $token);
                $token->throwIfCancelled();
                return $agent->run($prompt, $history, $events, $token, $id);
            } catch (\Throwable) {
                // The core reports provider errors. Failed emitters cannot receive another event.
                return null;
            }
        });
    }
}

/** @internal One cooperatively advanced fiber and its committed history. */
final class Session
{
    /** @var list<Message> */
    private array $history = [];
    private ?SessionRun $active = null;
    private bool $resetting = false;
    private bool $closed = false;

    public function __construct(private readonly Agent $agent) {}

    /** @param callable(string, CancellationToken): void $before
     *  @param callable(AgentEvent, CancellationToken): void $events
     */
    public function start(string $prompt, callable $before, callable $events): string
    {
        if ($this->closed) {
            throw new SessionException('closed', 'Session is closed');
        }
        if ($this->active !== null || $this->resetting) {
            throw new SessionException('busy', 'A run or reset is already active');
        }
        Json::text($prompt, 'Prompt');
        $this->active = new SessionRun($this->agent, $prompt, $this->history, $before, $events);
        return $this->active->id;
    }

    /** Advance to the next suspension or completion. Never resume an executing callback. */
    public function advance(): bool
    {
        $run = $this->active;
        if ($run === null || ($run->fiber->isStarted() && !$run->fiber->isSuspended())) {
            return false;
        }
        if ($run->fiber->isStarted()) {
            $run->fiber->resume();
        } else {
            $run->fiber->start();
        }
        if ($run->fiber->isTerminated()) {
            $result = $run->fiber->getReturn();
            if ($result !== null && !$run->cancellation->isCancelled() && !$this->closed && !$this->resetting) {
                $this->history = $result->history;
            }
            $run->cancellation->cancel();
            $this->active = null;
            $this->resetting = false;
        }
        return true;
    }

    /** @return array{runId: ?string, messageCount: int} */
    public function state(): array
    {
        return ['runId' => $this->active?->id, 'messageCount' => count($this->history)];
    }

    public function cancel(): ?string
    {
        $this->active?->cancellation->cancel();
        return $this->active?->id;
    }

    public function stop(): ?string
    {
        $this->closed = true;
        return $this->cancel();
    }

    public function reset(): bool
    {
        if ($this->closed) {
            throw new SessionException('closed', 'Session is closed');
        }
        if ($this->resetting) {
            throw new SessionException('busy', 'A run or reset is already active');
        }
        $this->history = [];
        $aborted = $this->cancel() !== null;
        $this->resetting = $aborted;
        $this->awaitIdle();
        return $aborted;
    }

    /** Wait cooperatively from a caller fiber; block from the main stack. */
    public function awaitIdle(): bool
    {
        $run = $this->active;
        while ($run !== null && $this->active === $run) {
            if (!$this->advance()) {
                return false;
            }
            if ($this->active === $run) {
                if (\Fiber::getCurrent() !== null) {
                    \Fiber::suspend();
                } else {
                    usleep(1000);
                }
            }
        }
        return true;
    }

    public function close(): bool
    {
        $this->stop();
        return $this->awaitIdle();
    }
}
