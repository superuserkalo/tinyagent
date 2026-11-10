<?php
declare(strict_types=1);

namespace TinyAgent;

/** @internal Owns SSE parsing and cancellation through the client's cleanup boundary. */
final readonly class ProviderStream
{
    public function __construct(private HttpClient $client) {}

    /** @param null|callable(string, CancellationToken): void $onText */
    public function run(HttpRequest $request, ResponseParser $parser, ?callable $onText, CancellationToken $parent): ProviderTurn
    {
        $parent->throwIfCancelled();
        $scope = $parent->withTimeout($request->timeout);
        $sse = new Sse();
        $completed = null;
        try {
            $this->client->stream($request, $scope, static function (string $bytes) use (
                $sse, $parser, $scope, $onText, &$completed,
            ): bool {
                $scope->throwIfCancelled();
                foreach ($sse->feed($bytes) as $event) {
                    $completed = $parser->consume($event, $onText, $scope);
                    $scope->throwIfCancelled();
                    if ($completed !== null) {
                        return false;
                    }
                }
                return true;
            });
            $scope->throwIfCancelled();
            if ($completed === null) {
                foreach ($sse->finish() as $event) {
                    $completed = $parser->consume($event, $onText, $scope);
                    $scope->throwIfCancelled();
                    if ($completed !== null) {
                        break;
                    }
                }
            }
            return $completed ?? throw new ProviderException('Provider stream ended before a complete turn.');
        } catch (\Throwable $error) {
            $parent->throwIfCancelled();
            try {
                $scope->throwIfCancelled();
            } catch (Cancelled $cancelled) {
                $error = $cancelled;
            }
            if ($error instanceof Cancelled) {
                if ($error->timedOut) {
                    throw new ProviderException('Provider request timed out.', previous: $error);
                }
                throw $error;
            }
            if ($error instanceof ProviderException) {
                throw $error;
            }
            throw new ProviderException('Provider request failed.', previous: $error);
        }
    }
}
