<?php
declare(strict_types=1);

namespace TinyAgent;

/** @internal A run retains its frame until the complete line reaches the stream. */
final class RpcFrame
{
    public int $offset = 0;
    public bool $done = false;
    public ?float $deadline = null;

    public function __construct(
        public readonly string $bytes,
        public readonly ?CancellationToken $token,
        public ?RpcException $error = null,
    ) {}
}

/** @internal One bounded writer; command replies never block input processing. */
final class RpcOutput
{
    /** @var \SplQueue<RpcFrame> */
    private \SplQueue $queue;
    private ?RpcFrame $current = null;
    private int $bytes = 0;
    private ?RpcException $failure = null;
    private ?float $deadline = null;

    public function __construct(private readonly RpcIO $io, private readonly CancellationToken $stopping)
    {
        $this->queue = new \SplQueue();
    }

    public function emit(\stdClass $value, ?CancellationToken $token = null): void
    {
        if ($this->failure !== null) {
            throw $this->failure;
        }
        [$bytes, $error] = RpcProtocol::frame($value);
        if ($this->bytes + strlen($bytes) > RpcProtocol::QUEUE_BYTES) {
            $overflow = new RpcException('output_queue_full', 'RPC output queue exceeds ' . RpcProtocol::QUEUE_BYTES . ' bytes');
            // Preserve an already-started line before reporting overflow.
            $this->fail($overflow, retainCurrent: true);
            [$report] = RpcProtocol::frame(RpcProtocol::error($overflow));
            $this->enqueue(new RpcFrame($report, null));
            throw $overflow;
        }
        $frame = new RpcFrame($bytes, $token, $error);
        $this->enqueue($frame);
        if ($error !== null) {
            $token?->cancel();
        }
        if ($token !== null) {
            while (!$frame->done) {
                \Fiber::suspend();
            }
        }
        if ($frame->error !== null) {
            throw $frame->error;
        }
    }

    public function advance(): void
    {
        if ($this->current === null && !$this->queue->isEmpty()) {
            $this->current = $this->queue->dequeue();
        }
        $frame = $this->current;
        if ($frame === null) {
            return;
        }
        $now = hrtime(true) / 1e9;
        if ($frame->token?->isCancelled()) {
            $frame->deadline ??= $now + 1;
        }
        if (($this->deadline !== null && $now >= $this->deadline)
            || ($frame->deadline !== null && $now >= $frame->deadline)) {
            $this->fail(new RpcException('output_stalled', 'Cancelled RPC output did not drain within one second'));
            return;
        }
        try {
            $frame->offset += $this->io->write($frame->bytes, $frame->offset);
        } catch (RpcException $error) {
            $this->fail($error);
            return;
        }
        if ($frame->offset === strlen($frame->bytes)) {
            $this->bytes -= strlen($frame->bytes);
            $frame->done = true;
            $this->current = null;
        }
    }

    public function cancel(): void { $this->deadline ??= hrtime(true) / 1e9 + 1; }
    public function pending(): bool { return $this->current !== null || !$this->queue->isEmpty(); }
    public function failure(): ?RpcException { return $this->failure; }

    private function enqueue(RpcFrame $frame): void
    {
        $this->queue->enqueue($frame);
        $this->bytes += strlen($frame->bytes);
    }

    private function fail(RpcException $error, bool $retainCurrent = false): void
    {
        $this->failure ??= $error;
        $this->stopping->cancel();
        $this->cancel();
        while (!$this->queue->isEmpty()) {
            $frame = $this->queue->dequeue();
            $frame->error = $this->failure;
            $frame->done = true;
        }
        if ($this->current !== null) {
            $this->current->error = $this->failure;
            $this->current->done = true;
            if (!$retainCurrent || $this->current->offset === 0) {
                $this->current = null;
            }
        }
        $this->bytes = $this->current === null ? 0 : strlen($this->current->bytes);
    }
}
