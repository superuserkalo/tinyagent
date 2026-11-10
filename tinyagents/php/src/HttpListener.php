<?php
declare(strict_types=1);

namespace TinyAgent;

use Evenement\EventEmitter;
use React\EventLoop\LoopInterface;
use React\Socket\ServerInterface;

/** @internal Accepts owned native sockets with strict output-drain acknowledgements. */
final class HttpListener extends EventEmitter implements ServerInterface
{
    /** @var resource|null */
    private mixed $resource;

    public function __construct(string $address, private readonly LoopInterface $loop)
    {
        $resource = @stream_socket_server($address, $code, $message);
        if ($resource === false) {
            throw new \RuntimeException("Could not bind HTTP listener: {$message}", $code);
        }
        $this->resource = $resource;
        stream_set_blocking($resource, false);
        $this->resume();
    }

    public function getAddress(): ?string
    {
        return $this->resource === null ? null : 'tcp://' . stream_socket_get_name($this->resource, false);
    }

    public function pause(): void
    {
        if ($this->resource !== null) {
            $this->loop->removeReadStream($this->resource);
        }
    }

    public function resume(): void
    {
        if ($this->resource === null) {
            return;
        }
        $this->loop->addReadStream($this->resource, function (): void {
            $resource = @stream_socket_accept($this->resource, 0);
            if ($resource === false) {
                return;
            }
            try {
                $connection = new HttpConnection($resource, $this->loop);
            } catch (\Throwable $failure) {
                fclose($resource);
                throw $failure;
            }
            $this->emit('connection', [$connection]);
        });
    }

    public function close(): void
    {
        if ($this->resource !== null) {
            $this->pause();
            fclose($this->resource);
            $this->resource = null;
            $this->removeAllListeners();
        }
    }
}
