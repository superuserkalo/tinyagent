<?php
declare(strict_types=1);

namespace TinyAgent;

final class RpcException extends \RuntimeException
{
    public function __construct(public readonly string $errorCode, string $message, ?\Throwable $previous = null)
    {
        parent::__construct($message, previous: $previous);
    }
}

/** @internal Shared wire limits and bounded output encoding. */
final class RpcProtocol
{
    public const LINE_BYTES = 1_048_576;
    public const QUEUE_BYTES = 4_194_304;

    public static function error(RpcException $error): \stdClass
    {
        return (object) ['type' => 'protocol_error', 'error' => (object) [
            'code' => $error->errorCode, 'message' => $error->getMessage(),
        ]];
    }

    /** @return array{string, ?RpcException} */
    public static function frame(\stdClass $value): array
    {
        $bytes = Json::encode($value);
        $error = null;
        if (strlen($bytes) > self::LINE_BYTES) {
            $error = new RpcException('output_too_large', 'RPC output exceeds ' . self::LINE_BYTES . ' UTF-8 bytes');
            $bytes = Json::encode(self::error($error));
        }
        return [$bytes . "\n", $error];
    }
}
