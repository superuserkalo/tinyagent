<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\CancellationToken;
use TinyAgent\HttpClient;
use TinyAgent\HttpRequest;

// Delivers response bytes directly. This fixture does not exercise cURL or HTTP framing.
final class FixtureResponse
{
    public bool $closed = false;
    public int $deliveries = 0;
    public ?\Closure $before = null;
    public ?\Closure $cleanup = null;

    public function __construct(public string $bytes, public int $fragment = 1) {}
}

final class FixtureClient implements HttpClient
{
    /** @var list<HttpRequest> */
    public array $requests = [];
    /** @var list<FixtureResponse> */
    private array $responses = [];

    public function enqueue(string $bytes, int $fragment = 1): FixtureResponse
    {
        $response = new FixtureResponse($bytes, $fragment);
        $this->responses[] = $response;
        return $response;
    }

    public function stream(HttpRequest $request, CancellationToken $cancellation, callable $consume): void
    {
        $this->requests[] = $request;
        $response = array_shift($this->responses) ?? throw new \LogicException('No response fixture.');
        try {
            if ($response->before !== null) {
                ($response->before)($cancellation);
            }
            for ($offset = 0; $offset < strlen($response->bytes); $offset += $response->fragment) {
                $cancellation->throwIfCancelled();
                $response->deliveries++;
                if (!$consume(substr($response->bytes, $offset, $response->fragment))) {
                    break;
                }
            }
        } finally {
            if ($response->cleanup !== null) {
                ($response->cleanup)($cancellation);
            }
            $response->closed = true;
        }
    }
}

// Provider and session code may catch a callback's assertion. The runner must still fail.
final class AssertionFailure extends \RuntimeException
{
    public static ?self $first = null;

    public function __construct(string $message, ?\Throwable $previous = null)
    {
        parent::__construct($message, previous: $previous);
        self::$first ??= $this;
    }
}

function same(mixed $expected, mixed $actual): void
{
    if ($expected !== $actual) {
        throw new AssertionFailure('Expected ' . var_export($expected, true) . '; received ' . var_export($actual, true));
    }
}

/** @param class-string<\Throwable> $type */
function raises(string $type, callable $action, ?string $message = null): \Throwable
{
    try {
        $action();
    } catch (\Throwable $error) {
        if (!$error instanceof $type) {
            throw new AssertionFailure("Expected {$type}; received " . get_class($error), previous: $error);
        }
        if ($message !== null && !str_contains($error->getMessage(), $message)) {
            throw new AssertionFailure("Missing error text {$message}: {$error->getMessage()}", previous: $error);
        }
        return $error;
    }
    throw new AssertionFailure("Expected {$type}.");
}

function sse(string $name): string
{
    $directory = getenv('TINYAGENT_PHP_FIXTURES') ?: __DIR__ . '/../../../conformance/providers';
    return file_get_contents($directory . '/' . $name . '.sse');
}

function events(\stdClass ...$values): string
{
    return implode('', array_map(static fn(\stdClass $value): string =>
        'data: ' . json_encode($value, JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE) . "\n\n", $values));
}
