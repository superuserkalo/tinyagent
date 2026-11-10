<?php
declare(strict_types=1);

namespace TinyAgent;

use Evenement\EventEmitter;
use React\EventLoop\LoopInterface;
use React\Http\Message\Response;
use React\Socket\ConnectionInterface;
use React\Stream\DuplexResourceStream;
use React\Stream\ThroughStream;
use React\Stream\Util;
use React\Stream\WritableResourceStream;
use React\Stream\WritableStreamInterface;

/** @internal One native HTTP connection, with each event acknowledged before the next. */
final class HttpConnection extends EventEmitter implements ConnectionInterface
{
    private readonly DuplexResourceStream $stream;
    private readonly string $local;
    private readonly string $remote;
    private ?CancellationToken $cancellation = null;
    private ?Session $session = null;
    private ?string $runId = null;
    private bool $pending = false;
    private bool $ending = false;
    private bool $closed = false;
    private bool $failed = false;

    /** @param resource $resource */
    public function __construct($resource, private readonly LoopInterface $loop)
    {
        $this->local = 'tcp://' . stream_socket_get_name($resource, false);
        $this->remote = 'tcp://' . stream_socket_get_name($resource, true);
        // A one-byte soft limit makes drain mean empty, including the final HTTP chunk.
        $buffer = new WritableResourceStream($resource, $loop, 1, 65_536);
        $this->stream = new DuplexResourceStream($resource, $loop, 8192, $buffer);
        $this->stream->on('drain', function (): void { $this->pending = false; });
        $this->stream->on('close', function (): void {
            $this->closed = true;
            if (!$this->ending || $this->pending) {
                $this->failed = true;
                $this->cancellation?->cancel();
            }
            $this->emit('close');
            $this->removeAllListeners();
        });
        Util::forwardEvents($this->stream, $this, ['data', 'end', 'error', 'drain', 'pipe']);
    }

    public function getLocalAddress(): string { return $this->local; }
    public function getRemoteAddress(): string { return $this->remote; }
    public function isReadable(): bool { return $this->stream->isReadable(); }
    public function isWritable(): bool { return $this->stream->isWritable(); }
    public function pause(): void { $this->stream->pause(); }
    public function resume(): void { $this->stream->resume(); }
    public function close(): void { $this->stream->close(); }

    public function pipe(WritableStreamInterface $dest, array $options = []): WritableStreamInterface
    {
        return Util::pipe($this, $dest, $options);
    }

    public function write($data): bool
    {
        if (!$this->isWritable()) {
            return false;
        }
        $this->pending = $this->pending || $data !== '';
        return $this->stream->write($data);
    }

    public function end($data = null): void
    {
        if ($data !== null) {
            $this->write($data);
        }
        $this->ending = true;
        $this->stream->end();
    }

    public function stop(): void
    {
        if ($this->cancellation === null) {
            $this->close();
        } else {
            $this->cancellation->cancel();
        }
    }

    public function prompt(Session $session, string $message): Response
    {
        $body = new ThroughStream();
        $this->runId = $session->start($message, function (string $runId, CancellationToken $token): void {
            $this->cancellation = $token;
            $this->awaitDelivery($token);
        }, function (AgentEvent $event, CancellationToken $token) use ($body): void {
            $value = $event->jsonSerialize();
            $line = Json::encode($value);
            $oversized = strlen($line) > Json::MAX_BYTES;
            if ($oversized) {
                $value = (object) [
                    'type' => 'agent_end', 'runId' => $event->runId, 'sequence' => $event->sequence,
                    'status' => 'failed', 'error' => (object) [
                        'code' => 'output_too_large', 'message' => 'HTTP event exceeds its byte limit',
                        'retryable' => false,
                    ],
                ];
                $line = Json::encode($value);
            }
            if (!$body->isWritable() || !$this->isWritable()) {
                throw new \RuntimeException('HTTP connection closed');
            }
            $body->write($line . "\n");
            $this->awaitDelivery($token);
            if ($value->type === 'agent_end') {
                $body->end();
                $this->awaitDelivery($token, terminal: true);
            }
            if ($oversized) {
                throw new \RuntimeException('HTTP event exceeded its byte limit');
            }
        });
        $this->session = $session;
        // Resume after the native close callback finishes, before accepting the next request.
        $this->on('close', fn() => $this->loop->futureTick($session->advance(...)));
        return new Response(200, [
            'Content-Type' => 'application/jsonl; charset=utf-8', 'Connection' => 'close', 'Server' => '',
        ], $body);
    }

    public function finishIfIdle(): void
    {
        // Cancellation before the first event can finish without opening a response body.
        if ($this->session !== null && $this->session->state()['runId'] !== $this->runId) {
            $this->close();
        }
    }

    private function awaitDelivery(CancellationToken $token, bool $terminal = false): void
    {
        $deadline = null;
        while ($this->pending || ($terminal && !$this->closed)) {
            if ($this->failed) {
                break;
            }
            if ($token->isCancelled()) {
                $deadline ??= hrtime(true) + 1_000_000_000;
                if (hrtime(true) >= $deadline) {
                    $this->close();
                    break;
                }
            }
            Cooperative::pause();
        }
        if ($this->failed || (!$terminal && $this->closed)) {
            $this->cancellation?->cancel();
            throw new \RuntimeException('HTTP delivery failed');
        }
    }
}
