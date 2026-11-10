<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\AnthropicProvider;
use TinyAgent\Cancelled;
use TinyAgent\CancellationToken;
use TinyAgent\OpenAIProvider;
use TinyAgent\ProviderException;

function providerTcp(bool $anthropic): void
{
    foreach (['complete', 'cancel_body', 'cancel_headers', 'deadline_headers'] as $mode) {
        $listener = stream_socket_server('tcp://127.0.0.1:0', $code, $message);
        if ($listener === false) {
            throw new \RuntimeException("TCP bind failed: {$code} {$message}");
        }
        $address = stream_socket_get_name($listener, false);
        $child = pcntl_fork();
        if ($child < 0) {
            fclose($listener);
            throw new \RuntimeException('Could not fork the TCP fixture.');
        }
        if ($child === 0) {
            pcntl_alarm(6);
            try {
                serveFixture($listener, $anthropic, $mode);
                exit(0);
            } catch (\Throwable $error) {
                fwrite(STDERR, "TCP fixture failed: {$error}\n");
                exit(1);
            }
        }
        fclose($listener);
        $parent = new CancellationToken();
        $priorSignal = pcntl_signal_get_handler(SIGALRM);
        $priorAsync = pcntl_async_signals(true);
        $failure = null;
        try {
            if ($mode === 'cancel_headers') {
                pcntl_signal(SIGALRM, static fn() => $parent->cancel());
                pcntl_alarm(1);
            }
            $endpoint = 'http://' . $address . '/fixture';
            $timeout = $mode === 'deadline_headers' ? 0.1 : 3;
            $provider = $anthropic
                ? new AnthropicProvider('fixture-key', $endpoint, $timeout)
                : new OpenAIProvider('fixture-key', $endpoint, $timeout);
            $deltas = '';
            $call = static function () use ($provider, $mode, $parent, &$deltas) {
                return $provider->stream(request(), static function (string $text) use ($mode, $parent, &$deltas): void {
                    $deltas .= $text;
                    if ($mode === 'cancel_body') {
                        $parent->cancel();
                    }
                }, $parent);
            };
            if (str_starts_with($mode, 'cancel')) {
                raises(Cancelled::class, $call);
            } elseif ($mode === 'deadline_headers') {
                raises(ProviderException::class, $call, 'timed out');
            } else {
                same("done\ncafé", $call()->text);
                same("done\ncafé", $deltas);
            }
        } catch (\Throwable $error) {
            $failure = $error;
        } finally {
            pcntl_alarm(0);
            pcntl_signal(SIGALRM, $priorSignal);
            pcntl_async_signals($priorAsync);
            pcntl_waitpid($child, $status);
        }
        if ($failure !== null) {
            throw $failure;
        }
        same(true, pcntl_wifexited($status));
        same(0, pcntl_wexitstatus($status));
    }
}

/** @param resource $listener */
function serveFixture($listener, bool $anthropic, string $mode): void
{
    $stream = stream_socket_accept($listener, 3);
    if ($stream === false) {
        throw new \RuntimeException('No provider connection.');
    }
    try {
        stream_set_timeout($stream, 4);
        same("POST /fixture HTTP/1.1\r\n", fgets($stream, 16_384));
        $headers = [];
        $bytes = 0;
        while (($line = fgets($stream, 16_384)) !== "\r\n") {
            if ($line === false || ($bytes += strlen($line)) > 16_384) {
                throw new \RuntimeException('Invalid fixture request headers.');
            }
            [$name, $value] = explode(':', $line, 2);
            $headers[strtolower($name)] = trim($value);
        }
        same($anthropic ? 'fixture-key' : 'Bearer fixture-key', $headers[$anthropic ? 'x-api-key' : 'authorization']);
        $length = (int) ($headers['content-length'] ?? 0);
        if ($length <= 0 || $length > 1_048_576) {
            throw new \RuntimeException('Invalid fixture request size.');
        }
        $body = stream_get_contents($stream, $length);
        same($length, strlen($body));
        same(true, json_decode($body, flags: JSON_THROW_ON_ERROR)->stream);
        if (!str_ends_with($mode, 'headers')) {
            $body = sse($anthropic ? 'anthropic_text' : 'openai_text');
            if ($mode === 'cancel_body') {
                $terminal = $anthropic ? 'data: {"type":"message_stop"}' : 'data: {"type":"response.completed"';
                $body = substr($body, 0, strpos($body, $terminal));
            }
            $wire = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
                . dechex(strlen($body)) . "\r\n" . $body;
            same(strlen($wire), fwrite($stream, $wire));
        }
        // No HTTP terminator or EOF is sent; the provider owns closing this response.
        same('', fread($stream, 1));
        same(true, feof($stream));
    } finally {
        fclose($stream);
        fclose($listener);
    }
}
