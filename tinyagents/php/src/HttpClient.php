<?php
declare(strict_types=1);

namespace TinyAgent;

final readonly class HttpRequest
{
    /** @var array<string, string> */
    public array $headers;

    /** @param array<string, string> $headers */
    public function __construct(
        public string $endpoint,
        array $headers,
        public string $body,
        public float $timeout = 180,
    ) {
        self::validateEndpoint($endpoint);
        if (!is_finite($timeout) || $timeout <= 0 || $timeout > PHP_INT_MAX / 1000) {
            throw new \InvalidArgumentException('Timeout must be positive and representable in milliseconds.');
        }
        foreach ($headers as $name => $value) {
            if (!is_string($name) || !is_string($value) || preg_match('/^[A-Za-z0-9-]+$/D', $name) !== 1
                || strpbrk($value, "\r\n\0") !== false) {
                throw new \InvalidArgumentException('Invalid HTTP header.');
            }
        }
        $this->headers = array_map(static fn(string $value): string => $value, $headers);
    }

    private static function validateEndpoint(string $endpoint): void
    {
        $parts = parse_url($endpoint);
        if ($parts === false || !in_array(strtolower($parts['scheme'] ?? ''), ['http', 'https'], true)
            || ($parts['host'] ?? '') === '' || isset($parts['user']) || isset($parts['pass']) || isset($parts['fragment'])
            || preg_match('/[\x00-\x20\x7f]/', $endpoint) === 1) {
            throw new \InvalidArgumentException('Provider endpoint must be an HTTP or HTTPS URL.');
        }
    }
}

interface HttpClient
{
    /** Deliver successful response body bytes until EOF or consume returns false.
     *  Implementations must release the response before returning or throwing.
     *  @param callable(string): bool $consume
     */
    public function stream(HttpRequest $request, CancellationToken $cancellation, callable $consume): void;
}
