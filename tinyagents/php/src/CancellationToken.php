<?php
declare(strict_types=1);

namespace TinyAgent;

final class Cancelled extends \RuntimeException
{
    public function __construct(public readonly bool $timedOut = false)
    {
        parent::__construct($timedOut ? 'Provider request timed out.' : 'Request cancelled.');
    }
}

final class CancellationToken
{
    private bool $cancelled = false;
    private ?self $parent = null;
    private ?float $deadline = null;

    public function cancel(): void
    {
        $this->cancelled = true;
    }

    public function isCancelled(): bool
    {
        return $this->cancelled || ($this->parent?->isCancelled() ?? false)
            || ($this->deadline !== null && hrtime(true) / 1e9 >= $this->deadline);
    }

    public function throwIfCancelled(): void
    {
        $this->parent?->throwIfCancelled();
        if ($this->cancelled) {
            throw new Cancelled();
        }
        if ($this->deadline !== null && hrtime(true) / 1e9 >= $this->deadline) {
            throw new Cancelled(timedOut: true);
        }
    }

    /** @internal Cancelling a child never cancels its parent. */
    public function child(): self
    {
        $child = new self();
        $child->parent = $this;
        return $child;
    }

    /** @internal A request owns this child and its deadline. */
    public function withTimeout(float $seconds): self
    {
        if (!is_finite($seconds) || $seconds <= 0) {
            throw new \InvalidArgumentException('Timeout must be positive and finite.');
        }
        $child = $this->child();
        $child->deadline = hrtime(true) / 1e9 + $seconds;
        return $child;
    }
}
