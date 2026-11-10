# TinyAgent PHP SDK

Native agent runs with validated tools, ordered events, immutable completed
history, and streaming OpenAI Responses or Anthropic Messages. The SDK requires
PHP 8.4 with JSON, cURL, and iconv. Install the pinned Composer dependencies
before running the examples. `RpcSession` adds parsed commands and committed session
history; `Rpc` adds native POSIX JSONL transport with PCNTL. `HttpServer` serves
authenticated HTTP sessions through ReactPHP and the same session driver.
The imported SDK, coding CLI, RPC, and HTTP server share one `Agent` loop.
`Tool` validates arguments through Opis JSON Schema 2.6.0 before invoking its
native PHP handler.

## Run an agent

```php
<?php
declare(strict_types=1);

require __DIR__ . '/vendor/autoload.php';

use TinyAgent\Agent;
use TinyAgent\AgentEvent;
use TinyAgent\CancellationToken;
use TinyAgent\OpenAIProvider;
use TinyAgent\TextDelta;

$provider = new OpenAIProvider();
$agent = new Agent($provider, 'gpt-5.6', 'Answer concisely.');
$result = $agent->run('Explain this project.', onEvent:
    static function (AgentEvent $event, CancellationToken $signal): void {
        $signal->throwIfCancelled();
        if ($event instanceof TextDelta) {
            echo $event->delta;
        }
    },
);
echo "\nCompleted: {$result->text}\n";
```

Set `OPENAI_API_KEY` before running the example. Construction and import require
no credentials. Pass `$result->history` as the `history` argument on the next
run to continue the conversation. `RunResult` contains readonly `text` and
`history` plus an `output()` accessor that returns detached provider JSON.
No result is returned for failed or cancelled runs.

`Agent` stores immutable configuration. Each `run` owns its transcript, event
sequence, and child cancellation token. The optional `runId` must be nonempty;
otherwise the core generates it. Events are `AgentStarted`, `TextDelta`,
`ToolExecutionStarted`, `ToolExecutionEnded`, `AgentCompleted`, `AgentFailed`,
and `AgentAborted`, all extending `AgentEvent`.
Each has a readonly `runId` and `sequence`; JSON serialization produces the
shared protocol object. The sequence starts at one. Failure events use
`provider_error`; the original exception is raised after delivery.

Callbacks run synchronously in the calling thread and fiber. Provider cleanup
finishes before final-event delivery, which must finish before `run` returns.
A failed consumer receives no later event, and its original exception survives
provider wrapping or suppression. Failure or cancellation during completed-event
delivery cannot return a result or emit another terminal event.

Pass a `CancellationToken` as `cancellation` and call its `cancel()` to stop a
run. Callback tokens can also cancel the run without cancelling the caller's
token. Provider deadlines raise `ProviderException`; explicit cancellation
raises `Cancelled`. Callbacks must return or cooperate with cancellation. If an
embedding application suspends a fiber, it must resume that fiber for cleanup.
Exceptions thrown into SDK-owned suspension points retain their identity through
provider wrapping and response cleanup before the core reports the failed run.
The core does not schedule background work. Concurrent calls using an external
scheduler retain independent history and cancellation.

Custom providers must deliver callbacks in order and finish their request
cleanup before returning. The caller owns the provider and must keep it alive
until its runs finish. Calls to unregistered tools fail the run before any
handler in that returned batch executes. Retained provider callbacks cannot
deliver text after their request ends, including between tool rounds.

## Register a tool

```php
<?php
declare(strict_types=1);

require __DIR__ . '/vendor/autoload.php';

use TinyAgent\Agent;
use TinyAgent\CancellationToken;
use TinyAgent\Json;
use TinyAgent\OpenAIProvider;
use TinyAgent\Tool;

$echo = new Tool('echo', 'Echo nonempty text.', Json::parseObject(
    '{"type":"object","properties":{"text":{"type":"string","minLength":1}},"required":["text"],"additionalProperties":false}',
), static function (stdClass $arguments, CancellationToken $token): string {
    $token->throwIfCancelled();
    return $arguments->text;
});
$agent = new Agent(new OpenAIProvider(), 'gpt-5.6', tools: [$echo]);
echo $echo->call((object) ['text' => 'hello']), "\n";
```

Construction validates a Draft 2020-12 schema and resolves references within the
schema document. Missing or external references and reference cycles that do not
descend into the input are rejected. Boolean schemas and recursive local dynamic
references are supported. Formats and defaults remain annotations: validation
does not coerce types, insert defaults, or mutate the caller's arguments.
Schemas, metadata accessors, event arguments, and handler inputs are detached
snapshots. Tool names must be unique within an agent.

Handlers return UTF-8 strings and run sequentially after provider cleanup. Invalid
arguments, ordinary exceptions, invalid UTF-8, and outputs above 131072 bytes
become bounded error observations for the next provider turn. Cancellation pairs
each delivered start with an end event, joins cooperative handler cleanup, and
aborts the run. Consumer failure stops further delivery. Handlers must check
their token and yield during waits when used inside session fibers.

## Bash execution

```php
<?php
declare(strict_types=1);

require __DIR__ . '/vendor/autoload.php';

use TinyAgent\Bash;
use TinyAgent\CancellationToken;

echo Bash::run("printf 'hello\\n'", new CancellationToken());
```

`Bash::run` requires Bash on Linux or macOS and closes command stdin. Stdout and
stderr share a 131072-byte limit, including the observation envelope. Nonzero
exits return observations; `Bash::deniedReason` exposes the shared blacklist,
whose denials return exit code 126. Overflow stops the command and raises a
`RuntimeException`. The shared Agent loop converts execution errors into bounded observations
so the model can recover.

The caller reads nonblocking native pipes. SDK-owned fibers yield between
iterations; main-stack calls and application-created fibers stay synchronous.
Cancellation stops the command's process group through a Bash supervisor, joins
that supervisor, and closes the pipes before raising `Cancelled`. An exception
thrown into the run fiber retains its identity after cleanup. Resume suspended
fibers through cleanup, including after cancellation. The executor installs no
signal handlers. Remaining background group members are removed after normal
completion; deliberately detached groups are outside this boundary. The
blacklist is not a sandbox.

## Own a session

```php
<?php
declare(strict_types=1);

require __DIR__ . '/vendor/autoload.php';

use TinyAgent\Agent;
use TinyAgent\CancellationToken;
use TinyAgent\OpenAIProvider;
use TinyAgent\RpcSession;

$rpc = new RpcSession(new Agent(new OpenAIProvider(), 'gpt-5.6'),
    static function (stdClass $value, ?CancellationToken $signal): void {
        echo json_encode($value, JSON_THROW_ON_ERROR), "\n";
    },
);
try {
    $rpc->handle((object) ['id' => 'state', 'type' => 'get_state']);
} finally {
    $rpc->close();
}
```

This state query needs no credentials. `handle()` accepts decoded JSON objects
for `prompt`, `abort`, `new_session`, and `get_state`; optional IDs must be
strings. Decode with `json_decode($line, flags: JSON_THROW_ON_ERROR)` so JSON
objects remain `stdClass` values. Invalid commands produce protocol errors.
Foreign objects, nonfinite numbers, malformed UTF-8, cycles, and excessive
nesting are rejected without invoking custom serializers.

A prompt reserves the session immediately and creates one pending fiber. Call
`advance()` to run it until its next suspension or completion. `awaitIdle()`
advances the observed run until it finishes; it does not join a later run.
From another caller fiber it suspends between advances, so that caller must
also be resumed. From the main stack it blocks. Parsed sessions have no background driver.
This follows PHP's explicit [fiber suspension and resumption](https://www.php.net/manual/en/language.fibers.php).

Providers and emitters execute inside that run fiber. `advance()` returns when
they return or call `Fiber::suspend()`. `CurlClient` yields during network waits
inside SDK-owned session fibers. Direct provider calls keep their synchronous
behavior, including calls inside an application-created fiber. Custom providers
and callbacks must also yield to let the caller process other work.

The emitter receives a cancellation token for run acknowledgements and events,
and `null` for ordinary command replies. It must finish acknowledgement delivery
before returning; the provider cannot start earlier. History commits only after
successful final delivery and fiber termination. Failed consumers receive no
retry, and failed or cancelled runs retain the last committed history. Separate
sessions can share one configured agent without sharing their run or history.

`state()` returns a detached protocol object. `cancel()` returns the active run
ID or `null` and requests cancellation. `stop()` also closes admission.
`new_session` clears history, cancels the old run, and waits for its cleanup;
admission stays closed while cleanup is pending. `close()` stops admission and
waits for cleanup. Calling reset from the run's own callback requests cleanup
without waiting; `close()` and `awaitIdle()` return `false` there to avoid a
self-wait. External close returns `true` and can be repeated safely. Keep the
session in a `try`/`finally` scope; dropping it is not a cleanup operation.

## Run native RPC

```php
<?php
declare(strict_types=1);

require __DIR__ . '/vendor/autoload.php';

use TinyAgent\Agent;
use TinyAgent\OpenAIProvider;
use TinyAgent\Rpc;

Rpc::runStdio(new Agent(new OpenAIProvider(), 'gpt-5.6'));
```

Pipe JSONL commands into this program. A `get_state` command needs no provider
credentials; a prompt does. `runStdio()` owns and closes `STDIN` and `STDOUT`,
handles SIGINT, and returns after EOF or interrupt cleanup. Diagnostics belong
on stderr. The coding CLI exposes the same adapter with
`php tiny_agent.php --mode rpc`.

For embedding, `Rpc::run($agent, $input, $output, $cancellation)` takes two
distinct, unfiltered native file, pipe, or connected stream-socket resources
(`STDIO`, `generic_socket`, `tcp_socket`, or `unix_socket`). It makes them
nonblocking, serializes output, and closes both on return or transport failure.
Basic validation and pre-cancellation leave the streams with the caller;
configuration takes ownership and closes both if setup fails. Do not use or
close the streams while RPC owns them. The caller continues to own the agent.

`run()` blocks on the main stack. In a caller fiber it yields between driver
iterations; the application must resume that fiber through cleanup. Cancelling
its token raises `Cancelled` after cleanup. Throwing into that caller fiber also
requests cleanup and preserves the original exception. Custom provider cleanup
must cooperate. Separate connections can use separate caller fibers.

Lines are limited to 1,048,576 UTF-8 bytes excluding LF. The output queue allows
4,194,304 bytes including complete frames and their LFs; a partial write still
counts as its whole frame. Run acknowledgement and event delivery wait for the
complete native write. Ordinary replies enqueue so input can still process
abort and EOF under output backpressure. Oversized output replaces the object
with a protocol error and cancels its run. Queue overflow terminates the
connection, preserving any partial line before the error when output can drain.
Cancelled writes and connection shutdown get one second to drain output; a
stalled or broken stream can therefore end with a partial line. Transport errors
raise `RpcException` with an `errorCode`.

PCNTL is required for native RPC and the coding CLI. Active RPC connections share scoped SIGPIPE
suppression. `runStdio()` also owns SIGINT; embedded `run()` can opt into it with
`handleInterrupt: true`. Only one connection may own SIGINT. Previous handlers
are restored unless the application replaced them during the connection, and
the application's asynchronous signal-dispatch setting is preserved. This
transport is verified on macOS; Linux acceptance remains open.

## HTTP sessions

Install the pinned HTTP dependencies from this directory:

```sh
composer install --no-scripts --no-plugins
```

```php
<?php
declare(strict_types=1);

require __DIR__ . '/vendor/autoload.php';

use TinyAgent\Agent;
use TinyAgent\HttpServer;
use TinyAgent\OpenAIProvider;

$server = HttpServer::start(new Agent(new OpenAIProvider(), 'gpt-5.6'),
    token: getenv('TINYAGENT_TOKEN') ?: null,
);
$previous = pcntl_signal_get_handler(SIGINT);
pcntl_signal(SIGINT, static fn() => $server->stop());
echo $server->url, "\n";
try {
    $server->run();
} finally {
    $server->close();
    pcntl_signal(SIGINT, $previous);
}
```

`start()` binds immediately and returns a server with a readonly `url`.
`run()` drives its private event loop until shutdown and cooperative cleanup
finish. It blocks the calling stack. Keep the server in a `try`/`finally` scope.
`stop()` requests shutdown from a provider callback or application signal handler.
`close()` joins from an external owner; inside a running callback it requests
shutdown and returns `false` to avoid waiting for itself. Repeated external close
returns `true`. The caller owns the agent and provider. Custom providers must
yield through `Fiber::suspend()` during waits and honor their cancellation token.
Built-in cURL and Bash already cooperate inside session fibers.

The defaults are `host: '127.0.0.1'` and `port: 8080`. Use `port: 0` for an
available port. Hosts must be IPv4 or IPv6 addresses. Non-loopback binding
requires `allowRemote: true` and a nonempty token. Tokens use printable ASCII
without spaces and protect every route, including `/health`. CORS is disabled.
Remote deployments terminate HTTPS at a trusted reverse proxy.

`POST /sessions` creates a session. Send a nonempty string in
`{"message":"hello"}` to `POST /sessions/:id/prompts` as `application/json`.
The response contains ordered JSONL events. `POST /sessions/:id/cancel` requests
cancellation; `DELETE /sessions/:id` waits for cleanup before releasing the session.
`GET /health` reports process health and protocol version without calling a provider.
Each session allows one active run; different sessions progress independently.

ReactPHP owns HTTP/1 parsing and response framing. Each connection serves one
request. Bodies and event objects are limited to 1,048,576 UTF-8 bytes. An
oversized event becomes a bounded terminal `output_too_large` error. Event
delivery waits for the native socket buffer to empty, including the final HTTP
chunk, before history can commit. There is no event queue. Cancelled pending
writes have one second to drain before the connection closes. Failed,
cancelled, or disconnected runs retain prior history. Deletion and shutdown
keep ownership until cooperative cleanup finishes.

HTTP serving requires PCNTL. It shares scoped SIGPIPE suppression with RPC,
preserves the application's asynchronous signal setting, and installs no SIGINT
handler of its own. The example above owns and restores its SIGINT handler.
Run the coding agent as a server with `php tiny_agent.php serve`. Configure it
with `TINYAGENT_HOST`, `TINYAGENT_PORT`, `TINYAGENT_TOKEN`, and
`TINYAGENT_ALLOW_REMOTE=1`; the same exposure rules apply. The CLI owns SIGINT
and returns 130 after interrupt cleanup.

Tool validation pins Opis JSON Schema 2.6.0, string 2.1.0, and URI 1.1.0. The
package includes the official Draft 2020-12 meta-schemas and their license for
offline schema construction checks. HTTP pins ReactPHP HTTP 1.11.1, stream 1.4.0, socket 1.17.0, event-loop 1.6.0,
promise 3.3.0, Evenement 3.0.2, and PSR HTTP message 1.1. Its source lockfile also
records the three transitive libraries. No additional native extension is needed
by these HTTP libraries. Core-only imports can still require `autoload.php` without Composer; constructing
tools or serving HTTP loads dependencies through `vendor/autoload.php`.
Malformed HTTP syntax is rejected by ReactPHP before route handling.

## Check and package

From this directory:

```sh
php check.php
php check.php --offline
```

The helper checks PHP syntax, creates `dist/tinyagent-sdk-0.0.1.zip`, runs core
and provider checks, and checks the CLI blacklist. Packaging uses the zip extension.
The full suite includes two provider and eleven HTTP TCP groups and needs the PCNTL
extension for its local fixture processes. HTTP backpressure checks also use
the sockets extension to restrict the client's receive window. The offline RPC groups also require
PCNTL. Missing extensions or denied sockets
cause failures. `--offline` explicitly excludes provider and HTTP TCP groups.
Neither command calls a paid provider.

The eighty-four offline groups exercise the core and deliver fragmented response
bytes through an injected `HttpClient`. They cover serialization, SSE parsing,
replay, typed events, value snapshots, cancellation, deadlines, consumer failures,
held cleanup and final delivery, and independent fiber runs. The provider byte
fixtures do not exercise native cURL, HTTP framing, or TCP. Thirteen session groups add shared parsed
commands, strict native-value checks, acknowledgement order, committed history,
busy/abort/reset, failed emitters, held cleanup and final delivery, callback-
initiated shutdown, concurrent sessions, and waiters scoped to their observed run.

Seventeen transport groups add native subprocess pipes, Unix socket pairs, JSONL
limits, command correlation, committed history, EOF/SIGINT, broken or stalled
output, queue overflow, held cleanup, and stream and signal ownership. A native
cURL file transfer proves that its C callback can suspend and resume a PHP
fiber; it does not prove the HTTP client or network scheduling over TCP.

Both provider TCP groups now pass with native cURL, including streamed text,
cancellation during headers and body delivery, deadlines, and connection cleanup.
The HTTP groups cover framing, limits, authentication, ordered events, history,
busy sessions, cancellation, and silent disconnects. A stalled terminal response
after provider cleanup verifies rollback on cancellation and disconnect. Deletion
and shutdown retain a provider whose cleanup is explicitly held by the fixture.
Ten tool groups add shared schemas, construction failures, immutable inputs,
sequential errors, bounded outputs, both providers and replay, cancellation, held
cleanup, callback ownership, and RPC commit and rollback. HTTP also checks tool
events and cancellation over TCP. The full suite contains 97 groups. See the
[finish contract](../../FINISH_CONTRACT.md) for package and final acceptance.

Seven Bash groups cover guards, native output and status, exact combined bounds,
infinite output, cancellation, exceptions thrown into fibers, independent commands,
background cleanup, and scheduling scope. Eleven CLI groups add streamed and
terminal-only text, opaque replay, sequential observations, invalid arguments,
overflow recovery, provider and callback failures, history rollback, request
and Bash cancellation, and retained-callback rejection. Exceptions thrown into
SDK-owned fibers survive provider and Bash cleanup. Native subprocesses verify
EOF, idle/request/Bash SIGINT, stalled stdout/stderr, broken output, and restored
stream ownership, blocking modes, and process signals. Native process checks
also use the POSIX extension to verify that recorded child PIDs disappear.

The ZIP contains thirty runtime PHP files, this README, the MIT license,
`composer.json`, and ten schema resource files. Tests and the CLI are excluded. Extract it into a
library directory and require its `autoload.php`. Composer can also load that
entrypoint through the manifest's `autoload.files` declaration and load the two
HTTP socket classes through its classmap. Version 0.0.1 is unpublished.

Independent package verification installs the ZIP with Composer 2.10.3 and the
same locked dependency graph. It runs the complete native suite against installed
classes, checks package bytes and class origins, and checks README examples and
the copied CLI's RPC and HTTP modes without paid requests. Core-only package
import also works without Composer's autoloader or credentials. Composer
validates the manifest; strict mode reports advisory warnings for the unpublished
version field and the exact dependency pins required by this repository.

## Stream a provider turn

```php
<?php
declare(strict_types=1);

require __DIR__ . '/vendor/autoload.php';

use TinyAgent\CancellationToken;
use TinyAgent\OpenAIProvider;
use TinyAgent\ProviderRequest;
use TinyAgent\UserMessage;

$model = getenv('TINYAGENT_MODEL') ?: throw new RuntimeException('Set TINYAGENT_MODEL.');
$provider = new OpenAIProvider();
$cancellation = new CancellationToken();
$history = [new UserMessage('Explain this project.')];
$turn = $provider->stream(
    new ProviderRequest($model, 'Answer concisely.', $history),
    static function (string $delta, CancellationToken $signal): void {
        $signal->throwIfCancelled();
        echo $delta;
    },
    $cancellation,
);
$history[] = $turn->assistant;
```

Set `OPENAI_API_KEY` before running this example. For Anthropic, use
`AnthropicProvider`, `ANTHROPIC_API_KEY`, and an Anthropic model. Credentials
resolve when a request starts; imports and constructors need no key. Running
this example sends a real provider request.

`ProviderRequest` contains a model, instructions, neutral message history, and
optional `ToolDefinition` metadata. `ProviderTurn` contains text, an assistant
message, and `toolCalls()`, `replay()`, and `output()` accessors. A `ToolCall`
contains the call ID, tool name, and a complete argument object through
`arguments()`. `ToolResultMessage` carries the matching ID and output for the
next provider request. Tool definitions do not validate schemas or run handlers.

Message collections detach PHP reference aliases. JSON accessors return fresh
copies, retaining the difference between JSON objects and arrays. Changing one
returned object cannot mutate the stored replay. Keep replay with its original
provider; provider switching within a conversation is outside this version.

Callbacks run synchronously on the caller's thread. Each request owns a child
cancellation token and a default 180-second deadline. The deadline remains
effective through callbacks and response cleanup. Callbacks must return or
cooperate with their token. Deadline expiry or cancellation of a callback's child
token leaves the parent token unchanged. Parent cancellation raises `Cancelled`.
Deadlines and request failures raise `ProviderException`, with consumer errors
retained as the previous exception.

The default `CurlClient` uses cURL's multi interface to check cancellation while
waiting for the network. Every request owns its handles, disables redirects,
and releases its response before returning. Provider completion stops the body
reader without waiting for HTTP EOF. The separate TCP checks determine whether
that native transport behavior is accepted on a runner.

Providers accept an endpoint, timeout in seconds, and optional `HttpClient`.
An injected client receives an immutable `HttpRequest` and a byte consumer. It
must stop when the consumer returns false, honor cancellation, reject non-success
HTTP statuses, and release its response before returning or throwing.

## Structure

`autoload.php` loads the core and protocol files in `src/`. `Agent.php` owns run lifetime,
`AgentEvent.php` holds typed protocol events, and `RunResult.php` holds completed
values. Public provider values and cancellation
are separate from SSE framing, the HTTP client interface, native cURL I/O,
request lifetime, and the two provider parsers.
Each request has its own parsing state. `Bash.php` owns guarded process execution.
The coding CLI in `tiny_agent.php` uses the SDK's streaming provider, typed
history, replay, and Bash executor. It registers a validated Bash tool and delegates each turn to `Agent`.
`Tool.php` owns the native handler and argument boundary; `ToolSchema.php` fixes
the validator dialect and local-reference policy. Text arrives incrementally, with a
fallback for terminal-only text. Every returned Bash call executes sequentially;
invalid arguments and ordinary execution failures become recoverable observations.
Only successful turns return completed history. Caller interruptions and failed
output propagate after cleanup.

Nonblocking console reads and writes let Ctrl-C cancel idle input, provider work,
Bash, or stalled output. The CLI returns exit code 130 after interrupt cleanup
and does not write a final newline into a stalled pipe. It borrows native streams,
restores all three blocking modes, and restores SIGINT, shared SIGPIPE suppression,
and the prior asynchronous signal setting. Importing the CLI starts no input loop
or request. Run it with `php tiny_agent.php`; its blacklist check remains
`php tiny_agent.php --self-test`.

`Session.php` owns one fiber, admission, cleanup, and committed history.
`RpcSession.php` maps parsed commands and protocol replies onto that ownership.
`Rpc.php` drives input, the session, and one output writer. `RpcIO.php` owns
native streams; `RpcInput.php` parses bounded JSONL; `RpcOutput.php` owns queued
frames and delivery; `RpcProtocol.php` holds framing limits and transport errors.
`RpcSignals.php` scopes process handlers, and `Cooperative.php` identifies the
session fibers that may yield from cURL or Bash. `Cooperative` also identifies
exceptions thrown into those suspension points, so the core and CLI preserve caller
interruptions through provider wrapping and tool cleanup. It uses weak references
and does not retain completed exceptions. No transport adapter contains another agent loop.

`HttpServer.php` owns routes, sessions, and server lifetime. `HttpJson.php` bounds
request bodies and builds protocol responses. `HttpListener.php` accepts native
sockets; `HttpConnection.php` owns output acknowledgement and disconnect cleanup.
Both RPC and HTTP advance `Session.php`; neither changes the agent loop.

The native client follows PHP's documented
[cURL multi interface](https://www.php.net/manual/en/function.curl-multi-exec.php).
