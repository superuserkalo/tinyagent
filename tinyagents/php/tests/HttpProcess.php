<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

/** Native subprocess and TCP client. All waits are bounded and teardown reaps the child. */
final class HttpProcess
{
    private mixed $process;
    /** @var array<int, resource> */
    private array $pipes;
    public readonly string $url;

    public function __construct(public readonly string $directory, private readonly ?string $token = null,
        string $host = '127.0.0.1', bool $remote = false)
    {
        $environment = getenv();
        foreach (['OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'TINYAGENT_PROVIDER', 'TINYAGENT_MODEL'] as $name) {
            unset($environment[$name]);
        }
        $this->process = proc_open([PHP_BINARY, __DIR__ . '/HttpFixture.php', $directory, $token ?? '', $host,
            $remote ? 'remote' : ''], [['pipe', 'r'], ['pipe', 'w'], ['pipe', 'w']], $pipes, env_vars: $environment);
        if (!is_resource($this->process)) {
            throw new \RuntimeException('Could not start HTTP fixture');
        }
        $this->pipes = $pipes;
        stream_set_timeout($pipes[1], 5);
        $url = fgets($pipes[1]);
        if ($url === false || !str_starts_with($url, 'http://')) {
            $this->close();
            throw new \RuntimeException('HTTP fixture did not publish its URL');
        }
        $this->url = trim($url);
        stream_set_blocking($pipes[2], false);
    }

    public function connect(bool $smallWindow = false): HttpResponse
    {
        $address = str_replace('http://', 'tcp://', rtrim($this->url, '/'));
        if ($smallWindow) {
            $parts = parse_url($address);
            $native = socket_create(AF_INET, SOCK_STREAM, SOL_TCP);
            socket_set_option($native, SOL_SOCKET, SO_RCVBUF, 4096);
            socket_connect($native, $parts['host'], $parts['port']);
            $socket = socket_export_stream($native);
        } else {
            $socket = stream_socket_client($address, $code, $message, 3);
        }
        if ($socket === false) {
            throw new \RuntimeException("HTTP fixture connection failed: {$message}", $code);
        }
        return new HttpResponse($socket);
    }

    /** @param list<string> $headers */
    public function open(string $method, string $path, string $body = '', array $headers = [], bool $smallWindow = false): HttpResponse
    {
        $response = $this->connect($smallWindow);
        $auth = $this->token === null ? [] : ['Authorization: Bearer ' . $this->token];
        $fields = ['Host: localhost', 'Content-Type: application/json', 'Content-Length: ' . strlen($body),
            ...$auth, ...$headers];
        $response->write("{$method} {$path} HTTP/1.1\r\n" . implode("\r\n", $fields) . "\r\n\r\n" . $body);
        return $response;
    }

    public function request(string $method, string $path, string $body = '', int $status = 200): \stdClass
    {
        $response = $this->open($method, $path, $body);
        try {
            same($status, $response->status());
            $bytes = $response->body();
            return $bytes === '' ? new \stdClass() : json_decode($bytes, flags: JSON_THROW_ON_ERROR);
        } finally {
            $response->close();
        }
    }

    public function session(): string { return $this->request('POST', '/sessions', status: 201)->sessionId; }

    public function prompt(string $id, string $message): HttpResponse
    {
        $deadline = microtime(true) + 5;
        do {
            $response = $this->open('POST', "/sessions/{$id}/prompts", json_encode(['message' => $message], JSON_THROW_ON_ERROR));
            if ($response->status() !== 409) {
                return $response;
            }
            $response->close();
            usleep(2000);
        } while (microtime(true) < $deadline);
        throw new \RuntimeException('HTTP session did not become idle');
    }

    public function completed(string $id, string $message = 'ok'): \stdClass
    {
        $response = $this->prompt($id, $message);
        try {
            $events = $response->events();
            same('completed', end($events)->status);
            return end($events);
        } finally {
            $response->close();
        }
    }

    public function waitFile(string $name): void
    {
        $deadline = microtime(true) + 5;
        do {
            clearstatcache(true, $this->directory . '/' . $name);
            if (is_file($this->directory . '/' . $name)) {
                return;
            }
            if (microtime(true) >= $deadline) {
                throw new \RuntimeException("HTTP fixture did not reach {$name}: " . stream_get_contents($this->pipes[2]));
            }
            usleep(1000);
        } while (true);
    }

    public function signal(): void { proc_terminate($this->process, SIGINT); }

    public function finish(bool $signal = true): void
    {
        if ($signal) {
            $this->signal();
        }
        $this->waitFile('finished');
        $deadline = microtime(true) + 5;
        do {
            $status = proc_get_status($this->process);
            if (!$status['running']) {
                break;
            }
            if (microtime(true) >= $deadline) {
                throw new \RuntimeException('HTTP fixture did not exit');
            }
            usleep(1000);
        } while (true);
        same('', stream_get_contents($this->pipes[2]));
        same(0, $status['exitcode']);
        $stats = json_decode(file_get_contents($this->directory . '/finished'), flags: JSON_THROW_ON_ERROR);
        same($stats->entered, $stats->closed);
        same(true, $stats->signalsRestored);
    }

    public function close(): void
    {
        if (proc_get_status($this->process)['running']) {
            proc_terminate($this->process, SIGKILL);
        }
        foreach ($this->pipes as $pipe) {
            fclose($pipe);
        }
        proc_close($this->process);
    }
}

/** Parses fixture responses independently of the server's HTTP library. */
final class HttpResponse
{
    private ?int $code = null;
    /** @var array<string, string> */
    public array $headers = [];
    private int $remaining = 0;
    private bool $chunked = false;
    private bool $ended = false;
    private string $buffer = '';

    /** @param resource $socket */
    public function __construct(public readonly mixed $socket)
    {
        stream_set_timeout($socket, 5);
    }

    public function write(string $bytes): void
    {
        for ($offset = 0; $offset < strlen($bytes);) {
            $count = fwrite($this->socket, substr($bytes, $offset, 65_536));
            if ($count === false || $count === 0) {
                throw new \RuntimeException('HTTP request write failed');
            }
            $offset += $count;
        }
    }

    public function status(): int
    {
        if ($this->code !== null) {
            return $this->code;
        }
        $status = fgets($this->socket);
        if ($status === false || preg_match('/^HTTP\/1\.[01] ([0-9]{3}) /', $status, $match) !== 1) {
            throw new \RuntimeException('Missing HTTP response status: ' . var_export($status, true));
        }
        $this->code = (int) $match[1];
        while (($line = fgets($this->socket)) !== "\r\n") {
            if ($line === false) {
                throw new \RuntimeException('Incomplete HTTP response headers');
            }
            [$name, $value] = explode(':', $line, 2);
            $this->headers[strtolower($name)] = trim($value);
        }
        $this->chunked = ($this->headers['transfer-encoding'] ?? '') === 'chunked';
        $this->remaining = (int) ($this->headers['content-length'] ?? 0);
        $this->ended = !$this->chunked && $this->remaining === 0;
        return $this->code;
    }

    private function exact(int $length): string
    {
        $bytes = '';
        while (strlen($bytes) < $length) {
            $chunk = fread($this->socket, $length - strlen($bytes));
            if ($chunk === false || $chunk === '') {
                throw new \RuntimeException('Incomplete HTTP response body');
            }
            $bytes .= $chunk;
        }
        return $bytes;
    }

    private function chunk(): string
    {
        $this->status();
        if ($this->ended) {
            return '';
        }
        if (!$this->chunked) {
            $this->ended = true;
            return $this->exact($this->remaining);
        }
        $line = fgets($this->socket);
        if ($line === false || preg_match('/^[a-fA-F0-9]+\r\n$/D', $line) !== 1) {
            throw new \RuntimeException('Missing HTTP response chunk');
        }
        $length = hexdec(trim($line));
        same(true, $length <= 1_048_577);
        $bytes = $this->exact($length);
        same("\r\n", $this->exact(2));
        $this->ended = $length === 0;
        return $bytes;
    }

    public function body(): string
    {
        $bytes = $this->buffer;
        $this->buffer = '';
        do {
            $bytes .= $this->chunk();
        } while (!$this->ended);
        return $bytes;
    }

    public function event(): \stdClass
    {
        while (($end = strpos($this->buffer, "\n")) === false) {
            $this->buffer .= $this->chunk();
            if ($this->ended) {
                throw new \RuntimeException('Missing HTTP event');
            }
        }
        $line = substr($this->buffer, 0, $end);
        $this->buffer = substr($this->buffer, $end + 1);
        same(true, strlen($line) <= 1_048_576);
        return json_decode($line, flags: JSON_THROW_ON_ERROR);
    }

    /** @return list<\stdClass> */
    public function events(): array
    {
        same(200, $this->status());
        same('application/jsonl; charset=utf-8', $this->headers['content-type']);
        $events = [];
        do {
            $event = $this->event();
            $events[] = $event;
        } while ($event->type !== 'agent_end');
        same('agent_start', $events[0]->type);
        same(range(1, count($events)), array_column($events, 'sequence'));
        same(1, count(array_unique(array_column($events, 'runId'))));
        same('', $this->body());
        return $events;
    }

    public function close(): void
    {
        if (is_resource($this->socket)) {
            fclose($this->socket);
        }
    }
}
