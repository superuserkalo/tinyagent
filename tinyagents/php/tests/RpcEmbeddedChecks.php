<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\Agent;
use TinyAgent\Cancelled;
use TinyAgent\CancellationToken;
use TinyAgent\Cooperative;
use TinyAgent\Provider;
use TinyAgent\ProviderRequest;
use TinyAgent\ProviderTurn;
use TinyAgent\Rpc;
use TinyAgent\RpcException;
use TinyAgent\RpcIO;
use TinyAgent\RpcOutput;
use TinyAgent\RpcSignals;

/** Native Unix socket pairs exercise owned streams without binding a listener. */
final class RpcConnection
{
    public mixed $input;
    public mixed $output;
    public mixed $sender;
    public mixed $receiver;
    public readonly \Fiber $fiber;
    public readonly CancellationToken $token;
    public ?\Throwable $error = null;
    public string $received = '';

    public function __construct(Provider $provider)
    {
        [$this->input, $this->sender] = stream_socket_pair(STREAM_PF_UNIX, STREAM_SOCK_STREAM, STREAM_IPPROTO_IP);
        [$this->output, $this->receiver] = stream_socket_pair(STREAM_PF_UNIX, STREAM_SOCK_STREAM, STREAM_IPPROTO_IP);
        stream_set_blocking($this->receiver, false);
        $this->token = new CancellationToken();
        $this->fiber = new \Fiber(function () use ($provider): void {
            try {
                Rpc::run(new Agent($provider, 'fixture'), $this->input, $this->output, $this->token);
            } catch (\Throwable $error) {
                $this->error = $error;
            }
        });
    }

    public function send(\stdClass $value): void
    {
        $line = json_encode($value, JSON_THROW_ON_ERROR) . "\n";
        same(strlen($line), fwrite($this->sender, $line));
    }

    public function tick(): void
    {
        if (!$this->fiber->isStarted()) {
            $this->fiber->start();
        } elseif ($this->fiber->isSuspended()) {
            $this->fiber->resume();
        }
        $this->received .= stream_get_contents($this->receiver);
    }

    public function until(callable $done): void
    {
        $deadline = microtime(true) + 3;
        while (!$done()) {
            $this->tick();
            if (microtime(true) >= $deadline) {
                throw new \RuntimeException('Embedded RPC did not reach its expected state.');
            }
            usleep(1000);
        }
    }

    public function close(): void
    {
        $this->token->cancel();
        $this->until(fn(): bool => $this->fiber->isTerminated());
        foreach ([$this->input, $this->output, $this->sender, $this->receiver] as $stream) {
            if (is_resource($stream)) {
                fclose($stream);
            }
        }
    }
}

function rpcEmbeddedCleanup(): void
{
    foreach (['token', 'throw'] as $mode) {
        $cleanupEntered = false;
        $releaseCleanup = false;
        $provider = new ScriptedProvider(static function (ProviderRequest $request, callable $text, CancellationToken $token)
            use (&$cleanupEntered, &$releaseCleanup): never {
            try {
                while (true) {
                    $token->throwIfCancelled();
                    \Fiber::suspend();
                }
            } finally {
                $cleanupEntered = true;
                while (!$releaseCleanup) {
                    \Fiber::suspend();
                }
            }
        });
        $connection = new RpcConnection($provider);
        $failure = new \LogicException('Caller interrupted its RPC fiber.');
        try {
            $connection->send((object) ['type' => 'prompt', 'message' => 'go']);
            $connection->until(static fn(): bool => $provider->entered === 1);
            if ($mode === 'token') {
                $connection->token->cancel();
            } else {
                $connection->fiber->throw($failure);
            }
            $connection->until(static function () use (&$cleanupEntered): bool { return $cleanupEntered; });
            same(false, $connection->fiber->isTerminated());
            same(true, is_resource($connection->input));
            same(true, is_resource($connection->output));
            same(0, $provider->closed);
            $releaseCleanup = true;
            $connection->until(fn(): bool => $connection->fiber->isTerminated());
            same(1, $provider->closed);
            same(false, is_resource($connection->input));
            same(false, is_resource($connection->output));
            same(true, str_contains($connection->received, '"status":"aborted"'));
            if ($mode === 'token') {
                same(true, $connection->error instanceof Cancelled);
            } else {
                same($failure, $connection->error);
                same(false, $connection->token->isCancelled());
            }
        } finally {
            $releaseCleanup = true;
            $connection->close();
        }
    }
}

function rpcConcurrentConnections(): void
{
    $provider = new ScriptedProvider(static function (ProviderRequest $request): ProviderTurn {
        \Fiber::suspend();
        return new ProviderTurn($request->history[array_key_last($request->history)]->text, [], [], []);
    });
    $first = new RpcConnection($provider);
    $second = new RpcConnection($provider);
    $prior = pcntl_signal_get_handler(SIGPIPE);
    try {
        $first->send((object) ['type' => 'prompt', 'message' => 'first']);
        $second->send((object) ['type' => 'prompt', 'message' => 'second']);
        for ($i = 0; $i < 12; $i++) {
            $first->tick();
            $second->tick();
        }
        same(true, str_contains($first->received, '"text":"first"'));
        same(false, str_contains($first->received, 'second'));
        same(true, str_contains($second->received, '"text":"second"'));
        $first->close();
        same(SIG_IGN, pcntl_signal_get_handler(SIGPIPE));
        same(false, $second->fiber->isTerminated());
        $second->send((object) ['type' => 'get_state']);
        $second->tick();
        same(true, str_contains($second->received, '"messageCount":2'));
        $second->close();
        same($prior, pcntl_signal_get_handler(SIGPIPE));
    } finally {
        $first->close();
        $second->close();
    }
}

function rpcStreamOwnership(): void
{
    $provider = new ScriptedProvider(static fn(): ProviderTurn => new ProviderTurn('', [], [], []));
    $connection = new RpcConnection($provider);
    $memory = fopen('php://memory', 'r+');
    try {
        raises(\InvalidArgumentException::class, static fn() => new RpcIO($connection->input, $memory));
        same(true, is_resource($connection->input));
        same(true, stream_get_meta_data($connection->input)['blocked']);
        raises(\InvalidArgumentException::class, static fn() => new RpcIO($connection->input, $connection->input));
        $cancelled = new CancellationToken();
        $cancelled->cancel();
        raises(Cancelled::class, static fn() => Rpc::run(new Agent($provider, 'fixture'), $connection->input, $connection->output, $cancelled));
        same(true, is_resource($connection->input));
        $connection->tick();
        fclose($connection->output);
        $connection->send((object) ['type' => 'get_state']);
        $connection->until(fn(): bool => $connection->fiber->isTerminated());
        same(true, $connection->error instanceof RpcException);
        same('output_error', $connection->error->errorCode);
        same(false, is_resource($connection->input));
    } finally {
        fclose($memory);
        $connection->close();
    }
}

function rpcSignalOwnership(): void
{
    $pipe = pcntl_signal_get_handler(SIGPIPE);
    $interrupt = pcntl_signal_get_handler(SIGINT);
    $async = pcntl_async_signals();
    $one = $two = null;
    try {
        $original = static function (): void {};
        pcntl_signal(SIGPIPE, $original);
        pcntl_signal(SIGINT, $original);
        $one = new RpcSignals(false);
        $two = new RpcSignals(true);
        raises(RpcException::class, static fn() => new RpcSignals(true), 'already owns');
        $one->close();
        same(SIG_IGN, pcntl_signal_get_handler(SIGPIPE));
        $two->close();
        same($original, pcntl_signal_get_handler(SIGPIPE));
        same($original, pcntl_signal_get_handler(SIGINT));
        $one = new RpcSignals(true);
        $replacement = static function (): void {};
        pcntl_signal(SIGPIPE, $replacement);
        pcntl_signal(SIGINT, $replacement);
        $one->close();
        same($replacement, pcntl_signal_get_handler(SIGPIPE));
        same($replacement, pcntl_signal_get_handler(SIGINT));
        same($async, pcntl_async_signals());
    } finally {
        $one?->close();
        $two?->close();
        pcntl_signal(SIGPIPE, $pipe);
        pcntl_signal(SIGINT, $interrupt);
    }
}

function rpcOverflowFraming(): void
{
    $connection = new RpcConnection(new ScriptedProvider(static fn(): ProviderTurn => new ProviderTurn('', [], [], [])));
    $io = new RpcIO($connection->input, $connection->output);
    $stopping = new CancellationToken();
    $writer = new RpcOutput($io, $stopping);
    try {
        // Four complete frames fill exactly 4 MiB, including their LFs.
        $value = (object) ['data' => str_repeat('x', 1_048_564)];
        $writer->emit($value);
        $writer->advance(); // Begin a partial native write; retain its whole frame against the queue limit.
        for ($i = 0; $i < 3; $i++) {
            $writer->emit($value);
        }
        same('output_queue_full', raises(RpcException::class, static fn() => $writer->emit(new \stdClass()))->errorCode);
        same(true, $stopping->isCancelled());
        $received = '';
        while ($writer->pending()) {
            $received .= stream_get_contents($connection->receiver);
            $writer->advance();
        }
        $received .= stream_get_contents($connection->receiver);
        $lines = explode("\n", $received);
        same(3, count($lines));
        same(true, $value == json_decode($lines[0], flags: JSON_THROW_ON_ERROR));
        same('output_queue_full', json_decode($lines[1], flags: JSON_THROW_ON_ERROR)->error->code);
        same('', $lines[2]);
    } finally {
        $io->close();
        fclose($connection->sender);
        fclose($connection->receiver);
    }
}

function rpcCooperativeScope(): void
{
    same(false, Cooperative::pause());
    $unmanaged = new \Fiber(static fn(): bool => Cooperative::pause());
    $unmanaged->start();
    same(true, $unmanaged->isTerminated());
    same(false, $unmanaged->getReturn());
    $managed = Cooperative::fiber(static fn(): bool => Cooperative::pause());
    $managed->start();
    same(true, $managed->isSuspended());
    $managed->resume();
    same(true, $managed->getReturn());
    $weak = \WeakReference::create($managed);
    unset($managed);
    same(null, $weak->get());
}

function rpcNativeCurlCallback(): void
{
    $bytes = '';
    $cleaned = false;
    $fiber = Cooperative::fiber(static function () use (&$bytes, &$cleaned): void {
        $curl = curl_init('file://' . __FILE__);
        $multi = curl_multi_init();
        try {
            curl_setopt($curl, CURLOPT_WRITEFUNCTION, static function (\CurlHandle $handle, string $chunk) use (&$bytes): int {
                Cooperative::pause();
                $bytes .= $chunk;
                return strlen($chunk);
            });
            same(CURLM_OK, curl_multi_add_handle($multi, $curl));
            do {
                same(CURLM_OK, curl_multi_exec($multi, $running));
            } while ($running > 0);
            same(CURLE_OK, curl_multi_info_read($multi)['result']);
        } finally {
            curl_multi_remove_handle($multi, $curl);
            curl_reset($curl);
            $cleaned = true;
        }
    });
    $fiber->start();
    same(true, $fiber->isSuspended());
    same(false, $cleaned);
    same('', $bytes);
    while ($fiber->isSuspended()) {
        $fiber->resume();
    }
    same(file_get_contents(__FILE__), $bytes);
    same(true, $cleaned);
}
