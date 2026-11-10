<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

/** Native child and pipes. Every operation has a deadline and teardown reaps the child. */
final class RpcProcess
{
    private mixed $process;
    /** @var array<int, resource> */
    private array $pipes = [];
    private string $buffer = '';

    public function __construct(string $mode = 'script')
    {
        $environment = getenv();
        foreach (['OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'TINYAGENT_PROVIDER', 'TINYAGENT_MODEL'] as $name) {
            unset($environment[$name]);
        }
        $this->process = proc_open([PHP_BINARY, __DIR__ . '/RpcFixture.php', $mode],
            [['pipe', 'r'], ['pipe', 'w'], ['pipe', 'w']], $this->pipes, env_vars: $environment);
        if (!is_resource($this->process)) {
            throw new \RuntimeException('Could not start RPC fixture.');
        }
        foreach ($this->pipes as $stream) {
            stream_set_blocking($stream, false);
        }
    }

    public function send(\stdClass $command): void
    {
        $this->write(json_encode($command, JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE) . "\n");
    }

    public function write(string $bytes): void
    {
        $deadline = microtime(true) + 5;
        for ($offset = 0; $offset < strlen($bytes);) {
            $count = fwrite($this->pipes[0], substr($bytes, $offset, 8192));
            if ($count === false || microtime(true) >= $deadline) {
                throw new \RuntimeException('RPC input pipe did not accept bytes.');
            }
            $offset += $count;
            if ($count === 0) {
                usleep(1000);
            }
        }
    }

    public function read(): \stdClass
    {
        $deadline = microtime(true) + 5;
        while (($end = strpos($this->buffer, "\n")) === false) {
            $chunk = fread($this->pipes[1], 65_536);
            if ($chunk === false || ($chunk === '' && feof($this->pipes[1])) || microtime(true) >= $deadline) {
                throw new \RuntimeException('Missing RPC output: ' . stream_get_contents($this->pipes[2]));
            }
            $this->buffer .= $chunk;
            if ($chunk === '') {
                usleep(1000);
            }
        }
        $line = substr($this->buffer, 0, $end);
        $this->buffer = substr($this->buffer, $end + 1);
        same(true, strlen($line) <= 1_048_576);
        $value = json_decode($line, flags: JSON_THROW_ON_ERROR);
        same(true, $value instanceof \stdClass);
        return $value;
    }

    /** @return list<\stdClass> */
    public function through(string $type): array
    {
        $values = [];
        do {
            $value = $this->read();
            $values[] = $value;
        } while ($value->type !== $type);
        return $values;
    }

    public function state(): \stdClass
    {
        // Terminal delivery precedes the fiber's final commit.
        for ($i = 0; $i < 100; $i++) {
            $this->send((object) ['id' => 'state', 'type' => 'get_state']);
            $state = $this->read()->data;
            if ($state->status === 'idle') {
                return $state;
            }
            usleep(1000);
        }
        throw new \RuntimeException('RPC session did not become idle.');
    }

    public function signal(): void { proc_terminate($this->process, SIGINT); }

    public function waitForPartialOutput(): void
    {
        $deadline = microtime(true) + 5;
        while ($this->buffer === '') {
            $this->buffer .= fread($this->pipes[1], 1);
            if (microtime(true) >= $deadline || feof($this->pipes[1])) {
                throw new \RuntimeException('RPC frame did not begin.');
            }
            usleep(1000);
        }
    }

    public function closeInput(): void
    {
        if (is_resource($this->pipes[0])) {
            fclose($this->pipes[0]);
        }
    }

    public function closeOutput(): void { fclose($this->pipes[1]); }

    public function finish(int $expectedStatus = 0, bool $drain = true): \stdClass
    {
        $this->closeInput();
        $deadline = microtime(true) + 5;
        $stderr = '';
        do {
            if ($drain && is_resource($this->pipes[1])) {
                $this->buffer .= stream_get_contents($this->pipes[1]);
            }
            $stderr .= stream_get_contents($this->pipes[2]);
            $status = proc_get_status($this->process);
            if (!$status['running']) {
                break;
            }
            if (microtime(true) >= $deadline) {
                throw new \RuntimeException('RPC child did not stop.');
            }
            usleep(1000);
        } while (true);
        $stderr .= stream_get_contents($this->pipes[2]);
        same($expectedStatus, $status['exitcode']);
        $stats = json_decode(trim($stderr), flags: JSON_THROW_ON_ERROR);
        same(true, $stats->signalsRestored);
        same(true, $stats->streamsClosed);
        return $stats;
    }

    public function close(): void
    {
        if (is_resource($this->process)) {
            if (proc_get_status($this->process)['running']) {
                proc_terminate($this->process, SIGKILL);
            }
            foreach ($this->pipes as $stream) {
                if (is_resource($stream)) {
                    fclose($stream);
                }
            }
            proc_close($this->process);
        }
    }
}
