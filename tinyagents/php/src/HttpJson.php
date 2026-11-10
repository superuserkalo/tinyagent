<?php
declare(strict_types=1);

namespace TinyAgent;

use Psr\Http\Message\ServerRequestInterface;
use React\Http\Message\Response;
use React\Promise\Deferred;
use React\Promise\PromiseInterface;
use React\Stream\ReadableStreamInterface;

/** @internal Bounded bodies and protocol errors; ReactPHP owns HTTP framing. */
final class HttpJson
{
    public static function response(int $status, ?\stdClass $value): Response
    {
        return new Response($status, [
            'Content-Type' => 'application/json; charset=utf-8', 'Connection' => 'close', 'Server' => '',
        ], $value === null ? '' : Json::encode($value));
    }

    public static function error(int $status, string $code, string $message): Response
    {
        $response = self::response($status, (object) ['error' => (object) [
            'code' => $code, 'message' => $message, 'retryable' => $status === 409,
        ]]);
        return $status === 401 ? $response->withHeader('WWW-Authenticate', 'Bearer') : $response;
    }

    /** @param callable(\stdClass): Response|PromiseInterface $consume */
    public static function read(ServerRequestInterface $request, callable $consume): Response|PromiseInterface
    {
        $body = $request->getBody();
        if ($body->getSize() === 0) {
            return $consume(new \stdClass());
        }
        if ($body->getSize() > Json::MAX_BYTES) {
            return self::error(413, 'request_too_large', 'HTTP body exceeds its byte limit');
        }
        $types = $request->getHeader('Content-Type');
        if (count($types) !== 1 || strtolower(trim(explode(';', $types[0], 2)[0])) !== 'application/json') {
            return self::error(415, 'unsupported_media_type', 'Content-Type must be application/json');
        }
        if (!$body instanceof ReadableStreamInterface) {
            throw new \LogicException('HTTP request body must be streaming');
        }
        $response = new Deferred();
        $bytes = '';
        $settled = false;
        $body->on('data', function (string $chunk) use ($body, $response, &$bytes, &$settled): void {
            if (strlen($chunk) > Json::MAX_BYTES - strlen($bytes)) {
                $settled = true;
                $bytes = '';
                $response->resolve(self::error(413, 'request_too_large', 'HTTP body exceeds its byte limit'));
                $body->close();
            } elseif (!$settled) {
                $bytes .= $chunk;
            }
        });
        $body->on('end', function () use ($response, $consume, &$bytes, &$settled): void {
            if ($settled) {
                return;
            }
            $settled = true;
            try {
                $value = Json::parseObject($bytes);
            } catch (\Exception) {
                $response->resolve(self::error(400, 'invalid_request', 'Body must be a UTF-8 JSON object'));
                return;
            } finally {
                $bytes = '';
            }
            $response->resolve($consume($value));
        });
        $body->on('close', function () use ($response, &$bytes, &$settled): void {
            if (!$settled) {
                $settled = true;
                $bytes = '';
                $response->resolve(self::error(400, 'invalid_request', 'Incomplete HTTP request body'));
            }
        });
        return $response->promise();
    }
}
