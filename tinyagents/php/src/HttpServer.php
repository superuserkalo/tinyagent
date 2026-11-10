<?php
declare(strict_types=1);

namespace TinyAgent;

use Psr\Http\Message\ServerRequestInterface;
use React\EventLoop\StreamSelectLoop;
use React\Http\HttpServer as ReactHttpServer;
use React\Http\Message\Response;
use React\Http\Middleware\StreamingRequestMiddleware;
use React\Promise\Deferred;
use React\Promise\PromiseInterface;

/** In-memory HTTP sessions around one configured agent. The caller owns its provider. */
final class HttpServer
{
    private readonly StreamSelectLoop $loop;
    private readonly HttpListener $listener;
    private readonly RpcSignals $signals;
    public readonly string $url;
    /** @var array<string, Session> */
    private array $sessions = [];
    /** @var array<string, HttpConnection> */
    private array $connections = [];
    /** @var array<string, Deferred> */
    private array $deleting = [];
    private bool $stopping = false;
    private bool $stopped = false;
    private bool $running = false;

    public static function start(
        Agent $agent, string $host = '127.0.0.1', int $port = 8080,
        ?string $token = null, bool $allowRemote = false,
    ): self {
        return new self($agent, $host, $port, $token, $allowRemote);
    }

    private function __construct(
        private readonly Agent $agent, string $host, int $port,
        private readonly ?string $token, bool $allowRemote,
    ) {
        if (filter_var($host, FILTER_VALIDATE_IP) === false || $port < 0 || $port > 65535) {
            throw new \InvalidArgumentException('HTTP binding requires an IP address and a port from 0 to 65535');
        }
        if ($token !== null && preg_match('/^[\x21-\x7e]+$/D', $token) !== 1) {
            throw new \InvalidArgumentException('Token must contain printable non-space ASCII');
        }
        $packed = inet_pton($host);
        $loopback = (strlen($packed) === 4 && ord($packed[0]) === 127) || $packed === inet_pton('::1');
        if (!$loopback && !($allowRemote && $token !== null)) {
            throw new \InvalidArgumentException('Remote binding requires allowRemote and a token');
        }
        if (!class_exists(ReactHttpServer::class)) {
            throw new \RuntimeException('HTTP serving requires the Composer dependencies and vendor/autoload.php');
        }
        if (!function_exists('pcntl_signal_get_handler')) {
            throw new \RuntimeException('HTTP serving requires the PCNTL extension');
        }
        $async = pcntl_async_signals();
        try {
            $this->loop = new StreamSelectLoop();
        } finally {
            // Dispatch at driver steps; constructing a library loop must not change caller signals.
            pcntl_async_signals($async);
        }
        $this->signals = new RpcSignals(false);
        try {
            $address = str_contains($host, ':') ? "[{$host}]" : $host;
            $this->listener = new HttpListener("tcp://{$address}:{$port}", $this->loop);
            $this->url = str_replace('tcp://', 'http://', $this->listener->getAddress()) . '/';
            $this->listener->on('connection', function (HttpConnection $connection): void {
                $key = $connection->getLocalAddress() . '|' . $connection->getRemoteAddress();
                $this->connections[$key] = $connection;
                $connection->on('close', function () use ($key): void { unset($this->connections[$key]); });
            });
            $http = new ReactHttpServer($this->loop, new StreamingRequestMiddleware(), $this->handle(...));
            $http->listen($this->listener);
            $this->loop->addPeriodicTimer(0.001, $this->advance(...));
        } catch (\Throwable $failure) {
            if (isset($this->listener)) {
                $this->listener->close();
            }
            $this->signals->close();
            throw $failure;
        }
    }

    /** Drive native sockets and cooperative sessions until stop has joined all cleanup. */
    public function run(): void
    {
        if ($this->running) {
            throw new \LogicException('HTTP server is already running');
        }
        if ($this->stopped) {
            return;
        }
        $this->running = true;
        try {
            try {
                $this->loop->run();
            } catch (\Throwable $failure) {
                $this->stop();
                $this->loop->run();
                throw $failure;
            }
        } finally {
            $this->running = false;
        }
    }

    /** Call from a provider callback or the application's signal handler. */
    public function stop(): void
    {
        if ($this->stopping) {
            return;
        }
        $this->stopping = true;
        $this->listener->close();
        foreach ($this->sessions as $session) {
            $session->stop();
        }
        foreach ($this->connections as $connection) {
            $connection->stop();
        }
    }

    /** An external owner joins. A callback only requests shutdown and returns false. */
    public function close(): bool
    {
        $this->stop();
        if ($this->running) {
            return false;
        }
        $this->run();
        return true;
    }

    private function advance(): void
    {
        pcntl_signal_dispatch();
        $busy = false;
        foreach ($this->sessions as $id => $session) {
            $session->advance();
            if ($session->state()['runId'] !== null) {
                $busy = true;
            } elseif (isset($this->deleting[$id])) {
                $this->deleting[$id]->resolve(HttpJson::response(204, null));
                unset($this->deleting[$id], $this->sessions[$id]);
            }
        }
        foreach ($this->connections as $connection) {
            $connection->finishIfIdle();
        }
        if ($this->stopping && !$busy) {
            foreach ($this->connections as $connection) {
                $connection->close();
            }
            $this->sessions = [];
            $this->stopped = true;
            $this->signals->close();
            $this->loop->stop();
        }
    }

    private function handle(ServerRequestInterface $request): Response|PromiseInterface
    {
        if ($this->token !== null) {
            $values = $request->getHeader('Authorization');
            if (count($values) !== 1 || !hash_equals('Bearer ' . $this->token, $values[0])) {
                return HttpJson::error(401, 'unauthorized', 'Bearer authentication required');
            }
        }
        $method = $request->getMethod();
        $path = $request->getUri()->getPath();
        if ($method === 'GET' && $path === '/health') {
            return HttpJson::response(200, (object) ['status' => 'ok', 'protocolVersion' => 1]);
        }
        if ($method === 'POST' && $path === '/sessions') {
            return HttpJson::read($request, function (): Response {
                $id = 's_' . bin2hex(random_bytes(12));
                $this->sessions[$id] = new Session($this->agent);
                return HttpJson::response(201, (object) ['sessionId' => $id]);
            });
        }
        $parts = explode('/', $path);
        if (count($parts) < 3 || count($parts) > 4 || $parts[0] !== '' || $parts[1] !== 'sessions') {
            return HttpJson::error(404, 'not_found', 'Unknown route');
        }
        $id = $parts[2];
        $session = $this->sessions[$id] ?? null;
        if ($session === null || isset($this->deleting[$id])) {
            return HttpJson::error(404, 'session_not_found', 'Unknown session');
        }
        if ($method === 'DELETE' && count($parts) === 3) {
            $session->stop();
            $response = new Deferred();
            $this->deleting[$id] = $response;
            return $response->promise();
        }
        if ($method === 'POST' && count($parts) === 4 && $parts[3] === 'cancel') {
            return HttpJson::read($request, function () use ($id, $session): Response {
                if (($this->sessions[$id] ?? null) !== $session || isset($this->deleting[$id])) {
                    return HttpJson::error(404, 'session_not_found', 'Unknown session');
                }
                $runId = $session->cancel();
                return HttpJson::response(200, (object) ['accepted' => $runId !== null, 'runId' => $runId]);
            });
        }
        if ($method === 'POST' && count($parts) === 4 && $parts[3] === 'prompts') {
            return HttpJson::read($request, function (\stdClass $body) use ($request, $session): Response {
                if (!isset($body->message) || !is_string($body->message) || $body->message === '') {
                    return HttpJson::error(400, 'invalid_request', 'message must be a nonempty string');
                }
                $params = $request->getServerParams();
                $key = "tcp://{$params['SERVER_ADDR']}:{$params['SERVER_PORT']}|tcp://{$params['REMOTE_ADDR']}:{$params['REMOTE_PORT']}";
                $connection = $this->connections[$key] ?? null;
                if ($connection === null || !$connection->isWritable()) {
                    return HttpJson::error(500, 'server_error', 'HTTP connection closed');
                }
                try {
                    return $connection->prompt($session, $body->message);
                } catch (SessionException $error) {
                    return $error->reason === 'busy'
                        ? HttpJson::error(409, 'session_busy', 'The session already has an active run')
                        : HttpJson::error(404, 'session_not_found', 'Unknown session');
                }
            });
        }
        return HttpJson::error(404, 'not_found', 'Unknown route');
    }
}
