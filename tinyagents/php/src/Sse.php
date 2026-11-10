<?php
declare(strict_types=1);

namespace TinyAgent;

/** @internal Incremental SSE framing. A yielded terminal event can stop parsing the current chunk. */
final class Sse
{
    private string $line = '';
    private string $data = '';
    private bool $hasData = false;

    /** @return \Generator<int, \stdClass> */
    public function feed(string $bytes): \Generator
    {
        $offset = 0;
        while ($offset < strlen($bytes)) {
            $newline = strpos($bytes, "\n", $offset);
            $length = ($newline === false ? strlen($bytes) : $newline) - $offset;
            if ($length > Json::MAX_BYTES - strlen($this->line)) {
                throw new ProviderException('SSE line exceeds its byte limit.');
            }
            $this->line .= substr($bytes, $offset, $length);
            $offset += $length;
            if ($newline === false) {
                break;
            }
            $offset++;
            $event = $this->readLine();
            if ($event !== null) {
                yield $event;
            }
        }
    }

    /** @return \Generator<int, \stdClass> */
    public function finish(): \Generator
    {
        if ($this->line !== '') {
            $event = $this->readLine();
            if ($event !== null) {
                yield $event;
            }
        }
        if ($this->hasData) {
            yield $this->dispatch();
        }
    }

    private function readLine(): ?\stdClass
    {
        $line = $this->line;
        $this->line = '';
        if (preg_match('//u', $line) !== 1) {
            throw new ProviderException('Provider stream is not valid UTF-8.');
        }
        if (str_ends_with($line, "\r")) {
            $line = substr($line, 0, -1);
        }
        if ($line === '') {
            return $this->hasData ? $this->dispatch() : null;
        }
        if ($line !== 'data' && !str_starts_with($line, 'data:')) {
            return null;
        }
        $text = $line === 'data' ? '' : substr($line, 5);
        if (str_starts_with($text, ' ')) {
            $text = substr($text, 1);
        }
        $added = strlen($text) + (int) $this->hasData;
        if ($added > Json::MAX_BYTES - strlen($this->data)) {
            throw new ProviderException('SSE event exceeds its byte limit.');
        }
        $this->data .= ($this->hasData ? "\n" : '') . $text;
        $this->hasData = true;
        return null;
    }

    private function dispatch(): \stdClass
    {
        $source = $this->data;
        $this->data = '';
        $this->hasData = false;
        if ($source === '[DONE]') {
            throw new ProviderException('Provider stream ended without a complete turn.');
        }
        return Json::parseObject($source);
    }
}
