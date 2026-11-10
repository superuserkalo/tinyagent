<?php
declare(strict_types=1);

namespace TinyAgent;

/** @internal Incremental UTF-8 JSONL parsing with no command queue. */
final class RpcInput
{
    public static function read(RpcIO $io, RpcSession $session, RpcOutput $output, CancellationToken $stopping): void
    {
        $line = '';
        while (!$stopping->isCancelled()) {
            $chunk = $io->read();
            if ($chunk === null) {
                if ($line !== '') {
                    self::command($line, $session, $output);
                }
                return;
            }
            $offset = 0;
            while ($offset < strlen($chunk) && !$stopping->isCancelled()) {
                $newline = strpos($chunk, "\n", $offset);
                $end = $newline === false ? strlen($chunk) : $newline;
                if (strlen($line) + $end - $offset > RpcProtocol::LINE_BYTES) {
                    $output->emit(RpcProtocol::error(new RpcException('line_too_large', 'RPC input exceeds ' . RpcProtocol::LINE_BYTES . ' UTF-8 bytes')));
                    return;
                }
                $line .= substr($chunk, $offset, $end - $offset);
                $offset = $end + 1;
                if ($newline !== false) {
                    if (!self::command($line, $session, $output)) {
                        return;
                    }
                    $line = '';
                }
            }
            \Fiber::suspend();
        }
    }

    private static function command(string $line, RpcSession $session, RpcOutput $output): bool
    {
        if (preg_match('//u', $line) !== 1) {
            $output->emit(RpcProtocol::error(new RpcException('invalid_utf8', 'RPC input is not valid UTF-8')));
            return false;
        }
        try {
            $command = json_decode($line, depth: 100, flags: JSON_THROW_ON_ERROR);
            Json::encode($command); // Reject non-finite numbers accepted by json_decode.
        } catch (\JsonException | ProviderException) {
            $output->emit(RpcProtocol::error(new RpcException('invalid_json', 'RPC input is not valid JSON')));
            return true;
        }
        try {
            $session->handle($command);
        } catch (RpcException $error) {
            if ($error->errorCode !== 'output_too_large') {
                throw $error;
            }
            // Cancel the current run before a later command can reserve a new one.
            $session->cancel();
        }
        return true;
    }
}
