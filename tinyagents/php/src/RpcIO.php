<?php
declare(strict_types=1);

namespace TinyAgent;

/** @internal Owns native POSIX streams, with bounded nonblocking operations. */
final class RpcIO
{
    /** @param resource $input
     *  @param resource $output
     */
    public function __construct(private mixed $input, private mixed $output)
    {
        if (PHP_OS_FAMILY === 'Windows') {
            throw new \InvalidArgumentException('Native RPC requires POSIX streams.');
        }
        if ($input === $output) {
            throw new \InvalidArgumentException('RPC input and output must be distinct streams.');
        }
        foreach ([$input, $output] as $stream) {
            if (!is_resource($stream) || get_resource_type($stream) !== 'stream'
                || !in_array(stream_get_meta_data($stream)['stream_type'], ['STDIO', 'generic_socket', 'tcp_socket', 'unix_socket'], true)) {
                throw new \InvalidArgumentException('RPC requires native file, pipe, or socket streams.');
            }
        }
        try {
            foreach ([$input, $output] as $stream) {
                if (!self::call(static fn() => stream_set_blocking($stream, false), 'io_error')) {
                    throw new RpcException('io_error', 'RPC stream does not support nonblocking I/O');
                }
            }
            // Native sockets write directly through send(); only STDIO has a user-space write buffer.
            if (stream_get_meta_data($output)['stream_type'] === 'STDIO'
                && self::call(static fn() => stream_set_write_buffer($output, 0), 'output_error') !== 0) {
                throw new RpcException('output_error', 'RPC output buffering could not be disabled');
            }
        } catch (\Throwable $error) {
            try {
                $this->close();
            } catch (RpcException) {
                // Preserve the configuration failure after attempting both closes.
            }
            throw $error;
        }
    }

    /** Null is EOF; an empty string means input is not ready. */
    public function read(): ?string
    {
        $bytes = self::call(fn() => fread($this->input, 8192), 'input_error');
        if ($bytes === false) {
            throw new RpcException('input_error', 'RPC input could not be read');
        }
        return $bytes === '' && feof($this->input) ? null : $bytes;
    }

    public function write(string $bytes, int $offset): int
    {
        $count = self::call(fn() => fwrite($this->output, substr($bytes, $offset, 65_536)), 'output_error');
        if ($count === false) {
            throw new RpcException('output_error', 'RPC output could not be written');
        }
        return $count;
    }

    public function close(): void
    {
        $error = null;
        foreach ([$this->input, $this->output] as $stream) {
            if (is_resource($stream)) {
                try {
                    self::call(static fn() => fclose($stream), 'io_error');
                } catch (RpcException $failure) {
                    $error ??= $failure;
                }
            }
        }
        if ($error !== null) {
            throw $error;
        }
    }

    private static function call(callable $operation, string $code): mixed
    {
        set_error_handler(static function (int $severity, string $message) use ($code): never {
            throw new RpcException($code, $message);
        });
        try {
            return $operation();
        } catch (RpcException $error) {
            throw $error;
        } catch (\Throwable $error) {
            throw new RpcException($code, $error->getMessage(), $error);
        } finally {
            restore_error_handler();
        }
    }
}
