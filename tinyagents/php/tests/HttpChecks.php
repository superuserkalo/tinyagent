<?php
declare(strict_types=1);

namespace TinyAgent\Checks;

use TinyAgent\Agent;
use TinyAgent\HttpServer;
use TinyAgent\ProviderTurn;

function withHttp(callable $check, ?string $token = null, string $host = '127.0.0.1', bool $remote = false): void
{
    withProcessDirectory(static function (string $directory) use ($check, $token, $host, $remote): void {
        $server = new HttpProcess($directory, $token, $host, $remote);
        try {
            $check($server);
        } finally {
            $server->close();
        }
    });
}

function httpExposure(): void
{
    $agent = new Agent(new ScriptedProvider(static fn() => new ProviderTurn('', [], [], [])), 'fixture');
    foreach ([['0.0.0.0', null, false], ['0.0.0.0', 'secret', false], ['0.0.0.0', null, true],
        ['127.0.0.1', '', false], ['127.0.0.1', "bad\ntoken", false], ['localhost', null, false]] as [$host, $token, $remote]) {
        raises(\InvalidArgumentException::class, static fn() => HttpServer::start($agent, host: $host,
            port: 0, token: $token, allowRemote: $remote));
    }
    withHttp(static function (HttpProcess $server): void {
        same(true, str_starts_with($server->url, 'http://127.0.0.1:'));
        same(['status' => 'ok', 'protocolVersion' => 1], (array) $server->request('GET', '/health'));
        $response = $server->open('OPTIONS', '/health');
        try {
            same(404, $response->status());
            same(false, isset($response->headers['access-control-allow-origin']));
            same(false, isset($response->headers['server']));
        } finally { $response->close(); }
        $server->finish();
    });
    withHttp(static function (HttpProcess $server): void {
        same('ok', $server->request('GET', '/health')->status);
        $server->finish();
    }, 'secret', '0.0.0.0', true);
    withHttp(static function (HttpProcess $server): void {
        $id = $server->session();
        same('history:1', $server->completed($id)->text);
        $server->finish();
    }, host: '::1');
}

function httpAuthentication(): void
{
    withHttp(static function (HttpProcess $server): void {
        foreach (['', "Authorization: Bearer wrong\r\n", "Authorization: Bearer secret\r\nAuthorization: Bearer secret\r\n"] as $auth) {
            $response = $server->connect();
            try {
                $response->write("GET /health HTTP/1.1\r\nHost: localhost\r\n{$auth}\r\n");
                same(401, $response->status());
                same('Bearer', $response->headers['www-authenticate']);
                same('unauthorized', json_decode($response->body())->error->code);
            } finally { $response->close(); }
        }
        same('ok', $server->request('GET', '/health')->status);
        $server->finish();
    }, 'secret');
}

function httpFraming(): void
{
    withHttp(static function (HttpProcess $server): void {
        $id = $server->session();
        foreach ([['[]', 400], ['{', 400], ['{"message":"\uD800"}', 400], ['{"n":1e999}', 400],
            ["{\"message\":\"\xff\"}", 400], ['{"message":""}', 400], ['{"message":1}', 400]] as [$body, $status]) {
            $server->request('POST', "/sessions/{$id}/prompts", $body, $status);
        }
        foreach (["Content-Type: text/plain\r\n", "Content-Type: application/json\r\nContent-Type: application/json\r\n"] as $type) {
            $response = $server->connect();
            try {
                $response->write("POST /sessions HTTP/1.1\r\nHost: localhost\r\n{$type}Content-Length: 2\r\n\r\n{}");
                same(415, $response->status());
            } finally { $response->close(); }
        }
        foreach (["Content-Length: nope\r\n", "Content-Length: 2\r\nContent-Length: 3\r\n",
            "Transfer-Encoding: chunked\r\nContent-Length: 2\r\n"] as $framing) {
            $response = $server->connect();
            try {
                $response->write("POST /sessions HTTP/1.1\r\nHost: localhost\r\n{$framing}\r\n");
                same(400, $response->status());
            } finally { $response->close(); }
        }
        $response = $server->connect();
        try {
            $response->write("POST /sessions/{$id}/prompts HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n");
            foreach (str_split('{"message":"café"}') as $byte) {
                $response->write("1\r\n{$byte}\r\n");
            }
            $response->write("0\r\n\r\n");
            $events = $response->events();
            same('history:1', end($events)->text);
        } finally { $response->close(); }
        $server->finish();
    });
}

function httpBodyLimits(): void
{
    withHttp(static function (HttpProcess $server): void {
        $body = '{"padding":"' . str_repeat('x', 1_048_562) . '"}';
        same(1_048_576, strlen($body));
        $server->request('POST', '/sessions', $body, 201);
        $response = $server->connect();
        try {
            $response->write("POST /sessions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 1048577\r\n\r\n");
            same(413, $response->status());
        } finally { $response->close(); }
        $response = $server->connect();
        try {
            $response->write("POST /sessions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n100001\r\n" . $body . ' ' . "\r\n0\r\n\r\n");
            same(413, $response->status());
        } finally { $response->close(); }
        $server->finish();
    });
}

function httpHistory(): void
{
    withHttp(static function (HttpProcess $server): void {
        $id = $server->session();
        same('history:1', $server->completed($id)->text);
        foreach (['fail_before', 'fail'] as $prompt) {
            $response = $server->prompt($id, $prompt);
            try {
                $events = $response->events();
                same('failed', end($events)->status);
                same('provider_error', end($events)->error->code);
            } finally { $response->close(); }
        }
        same('history:3', $server->completed($id)->text);
        $other = $server->session();
        same('history:1', $server->completed($other)->text);
        $server->request('DELETE', "/sessions/{$id}", status: 204);
        same('session_not_found', $server->request('POST', "/sessions/{$id}/prompts", '{"message":"ok"}', 404)->error->code);
        $server->finish();
    });
}

function httpCancellation(): void
{
    withHttp(static function (HttpProcess $server): void {
        $id = $server->session();
        $other = $server->session();
        $response = $server->prompt($id, 'wait');
        try {
            $start = $response->event();
            same('agent_start', $start->type);
            same('text_delta', $response->event()->type);
            $error = $server->request('POST', "/sessions/{$id}/prompts", '{"message":"busy"}', 409)->error;
            same('session_busy', $error->code);
            same(true, $error->retryable);
            same('history:1', $server->completed($other)->text);
            $cancel = $server->request('POST', "/sessions/{$id}/cancel");
            same(true, $cancel->accepted);
            same($start->runId, $cancel->runId);
            same('aborted', $response->event()->status);
            same('', $response->body());
        } finally { $response->close(); }
        same('history:1', $server->completed($id)->text);
        $cancel = $server->request('POST', "/sessions/{$id}/cancel");
        same(false, $cancel->accepted);
        same(null, $cancel->runId);
        $pending = $server->connect();
        try {
            $pending->write("POST /sessions/{$id}/cancel HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{");
            $server->request('GET', '/health');
            $server->request('DELETE', "/sessions/{$id}", status: 204);
            $pending->write('}');
            same(404, $pending->status());
            same('session_not_found', json_decode($pending->body())->error->code);
        } finally { $pending->close(); }
        $server->finish();
    });
}

function httpOversizedEvents(): void
{
    withHttp(static function (HttpProcess $server): void {
        $id = $server->session();
        foreach (['large_delta', 'large_final'] as $prompt) {
            $response = $server->prompt($id, $prompt);
            try {
                $events = $response->events();
                same(2, count($events));
                same('failed', end($events)->status);
                same('output_too_large', end($events)->error->code);
            } finally { $response->close(); }
        }
        same('history:1', $server->completed($id)->text);
        $server->finish();
    });
}

function httpDisconnects(): void
{
    withHttp(static function (HttpProcess $server): void {
        $id = $server->session();
        foreach (['wait', 'silent'] as $prompt) {
            $response = $server->prompt($id, $prompt);
            same('agent_start', $response->event()->type);
            $response->close();
            same('history:' . ($prompt === 'wait' ? 1 : 3), $server->completed($id)->text);
        }
        $server->finish();
    });
}

function httpBlockedOutput(): void
{
    withHttp(static function (HttpProcess $server): void {
        $id = $server->session();
        $response = $server->prompt($id, 'blocked');
        try {
            same('agent_start', $response->event()->type);
            same('text_delta', $response->event()->type);
            usleep(100_000);
            same(true, $server->request('POST', "/sessions/{$id}/cancel")->accepted);
            same('history:1', $server->completed($id)->text);
        } finally { $response->close(); }
        $server->finish();
    });
    foreach (['disconnect', 'cancel'] as $mode) {
        withHttp(static function (HttpProcess $server) use ($mode): void {
            $id = $server->session();
            $response = $server->open('POST', "/sessions/{$id}/prompts", '{"message":"blocked_final"}', smallWindow: true);
            try {
                same('agent_start', $response->event()->type);
                same('text_delta', $response->event()->type);
                $deadline = microtime(true) + 5;
                while (!str_contains(file_get_contents($server->directory . '/events'), '["closed","blocked_final"]')) {
                    if (microtime(true) >= $deadline) {
                        throw new \RuntimeException('Provider did not finish before held terminal delivery');
                    }
                    usleep(1000);
                }
                // Cleanup is over, but the terminal write is still holding the session.
                same('session_busy', $server->request('POST', "/sessions/{$id}/prompts", '{"message":"busy"}', 409)->error->code);
                if ($mode === 'disconnect') {
                    $response->close();
                } else {
                    same(true, $server->request('POST', "/sessions/{$id}/cancel")->accepted);
                }
                same('history:1', $server->completed($id)->text);
            } finally { $response->close(); }
            $server->finish();
        });
    }
}

function httpHeldCleanup(): void
{
    foreach (['delete', 'shutdown', 'delete_shutdown'] as $mode) {
        withHttp(static function (HttpProcess $server) use ($mode): void {
            $id = $server->session();
            $response = $server->prompt($id, 'cleanup');
            $deletion = null;
            try {
                same('agent_start', $response->event()->type);
                same('text_delta', $response->event()->type);
                if ($mode !== 'shutdown') {
                    $deletion = $server->open('DELETE', "/sessions/{$id}");
                    $server->waitFile('cleanup');
                    same('session_not_found', $server->request('POST', "/sessions/{$id}/prompts", '{"message":"no"}', 404)->error->code);
                }
                if ($mode !== 'delete') {
                    $server->signal();
                }
                $server->waitFile('cleanup');
                usleep(50_000);
                same(false, is_file($server->directory . '/finished'));
                if ($mode === 'delete') {
                    $read = [$deletion->socket];
                    $write = $except = [];
                    same(0, stream_select($read, $write, $except, 0, 0));
                }
                file_put_contents($server->directory . '/release', 'go');
                same('aborted', $response->event()->status);
                same('', $response->body());
                if ($mode === 'delete') {
                    same(204, $deletion->status());
                }
                $server->finish($mode === 'delete');
            } finally {
                $response->close();
                $deletion?->close();
            }
        });
    }
    withHttp(static function (HttpProcess $server): void {
        $response = $server->prompt($server->session(), 'stop');
        try {
            $events = $response->events();
            same('aborted', end($events)->status);
            $server->finish(false);
        } finally { $response->close(); }
    });
}
