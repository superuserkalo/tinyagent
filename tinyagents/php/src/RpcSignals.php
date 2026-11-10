<?php
declare(strict_types=1);

namespace TinyAgent;

/** @internal Scoped process signals; concurrent connections share SIGPIPE suppression. */
final class RpcSignals
{
    private static int $users = 0;
    /** @var int|callable|null */
    private static mixed $previousPipe = null;
    private static bool $interruptOwned = false;
    /** @var int|callable|null */
    private mixed $previousInterrupt = null;
    private ?\Closure $handler = null;
    private bool $closed = false;
    public bool $interrupted = false;

    public function __construct(bool $handleInterrupt)
    {
        if (!function_exists('pcntl_signal_get_handler')) {
            throw new RpcException('signal_error', 'Native RPC requires the PCNTL extension');
        }
        if ($handleInterrupt && self::$interruptOwned) {
            throw new RpcException('signal_error', 'An RPC connection already owns SIGINT');
        }
        if (self::$users === 0) {
            self::$previousPipe = pcntl_signal_get_handler(SIGPIPE);
            if (!pcntl_signal(SIGPIPE, SIG_IGN)) {
                throw new RpcException('signal_error', 'Could not suppress SIGPIPE');
            }
        }
        self::$users++;
        if ($handleInterrupt) {
            $this->previousInterrupt = pcntl_signal_get_handler(SIGINT);
            $handler = function (): void { $this->interrupted = true; };
            if (!pcntl_signal(SIGINT, $handler)) {
                $this->close();
                throw new RpcException('signal_error', 'Could not handle SIGINT');
            }
            $this->handler = $handler;
            self::$interruptOwned = true;
        }
    }

    public function close(): void
    {
        if ($this->closed) {
            return;
        }
        $this->closed = true;
        if ($this->handler !== null) {
            if (pcntl_signal_get_handler(SIGINT) === $this->handler) {
                pcntl_signal(SIGINT, $this->previousInterrupt);
            }
            self::$interruptOwned = false;
            $this->handler = null;
        }
        if (--self::$users === 0) {
            if (pcntl_signal_get_handler(SIGPIPE) === SIG_IGN) {
                pcntl_signal(SIGPIPE, self::$previousPipe);
            }
            self::$previousPipe = null;
        }
    }
}
