# TinyAgent Java SDK

Java 21 or later is required. The native SDK provides tool runs using streaming
OpenAI Responses and Anthropic Messages, ordered events, immutable completed
history, cancellation, native JSONL RPC, HTTP sessions, and guarded Bash execution.
RPC and HTTP share the same session owner. The SDK, coding CLI, RPC, and HTTP
server use one `Agent` loop with schema-validated native tools.

Build the SDK with the commands below, then run the original CLI from this directory:

```sh
java -cp 'target/tiny-agent-0.1.0.jar:target/dependency/*' TinyAgent.java
java -cp 'target/tiny-agent-0.1.0.jar:target/dependency/*' TinyAgent.java --self-test
java -cp 'target/tiny-agent-0.1.0.jar:target/dependency/*' TinyAgent.java --mode rpc
TINYAGENT_TOKEN=replace-this-token java -cp 'target/tiny-agent-0.1.0.jar:target/dependency/*' TinyAgent.java serve
```

The CLI registers Bash on the SDK's `Agent` with a streaming OpenAI provider.
It needs the SDK JAR and the declared dependencies copied into `target/dependency`.
All returned Bash calls execute sequentially, with recoverable observations for
invalid arguments and ordinary execution errors. Failed and cancelled turns
preserve prior history. A scoped shutdown hook cancels active HTTP or Bash work,
releases blocked output, and waits for the turn to exit. Idle console input
holds no hook. The CLI owns its standard descriptors; it installs no SDK-wide
signal handler.

RPC and HTTP startup need no provider credentials; requests read `OPENAI_API_KEY`.
The CLI's HTTP mode reads `TINYAGENT_HOST` (default `127.0.0.1`), `TINYAGENT_PORT`
(default `8080`), `TINYAGENT_TOKEN`, and `TINYAGENT_ALLOW_REMOTE=1`. Ctrl-C joins
server and session cleanup.

## Bash execution

Save this as `BashExample.java` and run it with the SDK JAR on the classpath:

```java
import dev.tinyagent.Bash;
import dev.tinyagent.CancellationToken;

public class BashExample {
    public static void main(String[] args) throws Exception {
        System.out.print(Bash.run("printf 'hello\\n'", new CancellationToken()));
    }
}
```

`Bash.run` requires Bash on Linux or macOS and closes the command's stdin.
Standard output and error share a 131072-byte limit, including the observation
envelope. Oversized output stops the command and raises `IOException`.
Nonzero exits return observations; denied commands return exit code 126.
The CLI converts execution errors into bounded observations so the model can retry.

Cancellation stops the command's process group and joins process and pipe-reader
cleanup before raising `CancellationException`. Caller interruption also stops
the command and retains the thread's interrupt flag. Normal completion removes
remaining background processes in the same group. Deliberately detached groups
are outside this cleanup boundary. The blacklist is not a sandbox.

## Run an agent

With the SDK and its declared dependencies on the classpath, save this as
`Example.java`. Replace `your-model` with the model you use and set
`OPENAI_API_KEY` before running it.

```java
import dev.tinyagent.*;
import java.util.List;

public class Example {
    public static void main(String[] args) throws Exception {
        try (var provider = new OpenAIProvider()) {
            var agent = new Agent(provider, "your-model", "Answer concisely.");
            var cancellation = new CancellationToken();
            var result = agent.run("Say hello.", List.of(), cancellation, event -> {
                if (event instanceof AgentEvent.TextDelta delta) {
                    System.out.print(delta.delta());
                }
            });
            System.out.println();
            System.out.println("Committed messages: " + result.history().size());
        }
    }
}
```

Pass `result.history()` to the next run to continue the conversation. `Agent`
holds immutable configuration; each run owns its transcript, cancellation
token, run ID, and event sequence. Concurrent runs on one agent remain separate.
The caller owns the provider and closes it after its runs finish.

Events are typed `AgentEvent` values with `toJson()` for the shared wire format.
Callbacks run synchronously and provide backpressure. A failed callback stops
delivery; provider errors and cancellation emit a terminal event when the sink
remains usable. No result is returned until cleanup and terminal delivery finish.
Cancellation during terminal delivery also prevents returning a result.

Call `cancellation.cancel()` from another thread to stop a run. A custom provider
or callback must cooperate with cancellation; the agent does not abandon live
work. Failed or cancelled runs never modify the supplied history. Replay and
output JSON are copied defensively. An unknown tool rejects the entire returned
batch before any handler runs.

RPC uses private session ownership to admit one run per session, commit returned
history atomically, and retain worker ownership through reset and shutdown.

## Native tools

Save this offline example as `ToolExample.java`:

```java
import dev.tinyagent.*;
import java.util.List;
import tools.jackson.databind.json.JsonMapper;

public class ToolExample {
    public static void main(String[] args) throws Exception {
        var json = JsonMapper.builder().build();
        var tool = new Tool("echo", "Return text", json.readTree("""
                {"type":"object","properties":{"text":{"type":"string","minLength":1}},
                 "required":["text"],"additionalProperties":false}
                """), (arguments, cancellation) -> {
            cancellation.check();
            return arguments.path("text").stringValue();
        });
        Provider provider = (request, cancellation, onText) -> {
            if (request.history().getLast() instanceof Message.ToolResult result) {
                return new ProviderTurn(result.output(), List.of(), List.of(), List.of());
            }
            return new ProviderTurn("", List.of(new ToolCall("call", "echo",
                    json.createObjectNode().put("text", "hello"))), List.of(), List.of());
        };
        System.out.println(new Agent(provider, "fixture", "", List.of(tool)).run("go").text());
    }
}
```

`Tool` compiles a Draft 2020-12 schema at construction using NetworkNT. Invalid
schemas, unresolved or external references, and reference cycles that cannot
descend into the input are rejected before a run. Local references, anchors,
dynamic references, and recursive data schemas are supported. Formats are
annotations. Validation neither inserts defaults nor coerces handler arguments.
Schema bounds use arbitrary-precision numbers to avoid the validator's lossy
integer fast path for large inputs; the original schema and arguments stay intact.

Handlers receive a detached `ObjectNode` and the run's cancellation token. Calls
execute sequentially with `ToolStarted`/`ToolEnded` events. Invalid arguments,
ordinary exceptions, invalid Unicode, and output over 131072 UTF-8 bytes become
bounded error observations. Cancellation waits for cooperative handler cleanup,
pairs a delivered start with an end while the sink remains usable, and aborts
the run. Consumer failures stop further delivery. Provider callbacks are scoped
to one request and cannot emit during a later tool or request.

## JSONL RPC

Save this as `RpcExample.java` with the SDK and its dependencies on the classpath:

```java
import dev.tinyagent.*;

public class RpcExample {
    public static void main(String[] args) throws Exception {
        try (var provider = new OpenAIProvider()) {
            Rpc.runStdio(new Agent(provider, "your-model", "Answer concisely."));
        }
    }
}
```

`Rpc.runStdio` reads `prompt`, `abort`, `new_session`, and `get_state` commands
from stdin and writes responses and events to stdout. It uses the configured
Agent and owns the process's stdin/stdout channels. EOF cancels the active run;
the shutdown hook waits for cooperative cleanup on Ctrl-C. Only requests need
provider credentials, so an idle `get_state` command also works without them.

`Rpc.run(agent, input, output)` accepts blocking interruptible byte channels,
including native file and pipe channels. It takes ownership of both channels,
closes them on return, and propagates caller interruption after cleanup. The
driver joins its reader, writer, session worker, and deadline tasks. The caller
retains ownership of the Agent's provider. Custom provider code must cooperate
with cancellation.

Acknowledgements and events wait for their complete line to be written; other
responses enter a bounded queue so stalled stdout cannot hide abort or EOF.
Input and output objects have a 1 MiB limit, excluding LF. The output queue has
a 4 MiB limit, including framed lines. Invalid UTF-8 or oversized input ends
the connection. Oversized output cancels the run; queue overflow stops the
connection after preserving any line already being written. Cancelled output
gets one second to drain. A connection that drains after abort remains usable.

`RpcSession` exposes command mapping for an application that supplies its own
transport. Its emitter must serialize concurrent calls and await delivery when
the supplied cancellation token is non-null. `Rpc` supplies those guarantees
for native channels. `RpcSession.state()` returns a detached protocol object;
`close()` cancels and joins the session worker.

## HTTP sessions

Save this as `HttpExample.java` with the SDK and its dependencies on the classpath:

```java
import dev.tinyagent.Agent;
import dev.tinyagent.HttpServer;
import dev.tinyagent.OpenAIProvider;
import java.net.InetSocketAddress;

public class HttpExample {
    public static void main(String[] args) throws Exception {
        var options = new HttpServer.Options(
                new InetSocketAddress("127.0.0.1", 8080), "replace-this-token", false);
        try (var provider = new OpenAIProvider();
             var server = HttpServer.start(new Agent(provider, "your-model", "Answer concisely."), options)) {
            System.out.println(server.url());
            System.out.println("Press Enter to stop.");
            System.in.read();
        }
    }
}
```

`HttpServer.start(agent)` uses `127.0.0.1:8080` without a token. `Options` accepts
a resolved `InetSocketAddress`, an optional token, and `allowRemote`. Port zero
chooses an available port. Non-loopback binding requires explicit remote opt-in
and a nonempty token. Configured tokens protect every route, including health.
Duplicate Authorization headers are rejected. No CORS headers are added.

All five routes follow [PROTOCOL.md](../../PROTOCOL.md). Create a session, then
post a JSON `message` to `/sessions/:id/prompts` for ordered JSONL events. Each
session admits one run; separate sessions can run concurrently. Failed,
cancelled, disconnected, and undelivered runs retain the previous history.
The configured agent's tools run through the same loop as imported SDK calls.

[Jetty's HTTP library](https://jetty.org/docs/jetty/12.1/programming-guide/server/http.html)
owns parsing, listening, and response framing. Each connection serves one HTTP/1
request. The shared session worker waits for every response write, including the
terminal response framing, before returning a result that can commit history.
There is no event queue. Bodies and individual events have a 1 MiB UTF-8 limit;
oversized events become one bounded failed terminal and cannot commit history.
After consuming a prompt body, the handler observes native endpoint EOF while
the provider runs, so a silent provider is also cancelled on disconnect.

Cancelled pending writes have one second to drain; cooperative provider cleanup
remains owned until it finishes. Deletion waits for that cleanup before releasing
the session, including during concurrent server shutdown. `stop()` requests
shutdown; `awaitStopped()` waits for it; `close()` does both. A provider callback
may call `stop()`, then an external owner awaits cleanup. The server installs no
process signal handlers. The application owns the provider; the CLI's `serve`
mode supplies process shutdown handling.

## Provider interface

`Provider.stream(request, cancellation, onText)` runs on the caller's thread.
It delivers text deltas in order, then returns a complete `ProviderTurn`.
Callbacks provide backpressure and may throw to stop consumption. Virtual
threads are suitable when an application needs concurrent requests.

`ProviderRequest` contains the model, instructions, neutral `Message` history,
and provider-facing `ToolDefinition` metadata. A definition has no executable
handler and does not validate a schema. Complete tool arguments are returned
as `ToolCall` values; no tool runs inside a provider.

Constructors require no credentials. Default providers read `OPENAI_API_KEY`
or `ANTHROPIC_API_KEY` when a request starts. They own their HTTP clients;
close them when finished. The injected constructors accept a caller-owned
`HttpClient`, endpoint, key, and timeout. Anthropic also accepts `maxTokens`.
Closing a provider does not close an injected client.

Request cancellation releases pending headers or body reads. Deadlines remain
active through body parsing and callback delivery; callbacks should return
promptly or honor the caller's cancellation token. Defaults use a 180-second
request deadline and a 30-second connection timeout. Malformed, oversized,
or incomplete streams throw `ProviderException`; a complete terminal event
returns without waiting for EOF.

History and result values defensively copy mutable JSON. Replay retains
OpenAI reasoning items and Anthropic thinking signatures. Use a turn's replay
when constructing the next assistant message.

## Build and checks

`pom.xml` pins Jackson core/databind/YAML 3.2.1, Jackson annotations 2.22,
NetworkNT JSON Schema Validator 3.0.7, itu 1.14.0, and SnakeYAML Engine 3.0.1.
HTTP additionally uses
Jetty server, HTTP, I/O, and utility modules at 12.1.13 and SLF4J API 2.0.17.
These are Java libraries without native extensions. No logging implementation is
forced on embedding applications; without an SLF4J provider, its standard warning
is printed when schema validation or HTTP first initializes. Maven builds the
library with Java 21 compatibility and compiler warnings treated as errors.
The direct check command also copies all twelve dependency JARs into
`target/dependency` for the CLI commands above:

```sh
cd tinyagents/java
mvn package
python3 check.py
```

The native checks run through `check.py`, not Maven's test lifecycle. The
script reads dependency coordinates from the POM and uses those JARs from
`~/.m2/repository` or `MAVEN_REPO_LOCAL`. It compiles the library, creates a
JAR containing runtime classes, this README, and the license, then compiles and
runs the SDK and original CLI checks against that packaged JAR with provider
credentials removed. CLI integration checks live in `src/test/cli` and are
compiled alongside `TinyAgent.java` by this helper.
Python only dispatches build commands; the SDK and checks execute natively in
Java. Use the complete declared dependency classpath for the SDK and CLI;
standalone Bash needs only the SDK JAR.

For a focused run without TCP binding:

```sh
python3 check.py --offline
```

Sixty-five offline SDK groups cover shared SSE fixtures split into individual bytes, request
serialization, replay metadata, strict UTF-8 and JSON framing, bounds,
incomplete turns, immutable values, cancellation, timeouts, interruption,
consumer failure, terminal cleanup, and HTTP client ownership. Core and session
checks also cover ordered events, concurrent runs, completed history, admission,
reset, cancellation during final delivery, and shutdown from callbacks. Fourteen
RPC groups add real subprocesses, native pipes, shared command fixtures, bounds,
busy handling, tool execution, rollback, EOF, Ctrl-C, blocked writes, and failed output cleanup.
Nine tool groups cover shared schemas, construction failures, native provider
replay, sequential recovery, output bounds, cancellation, held cleanup,
interruption, consumer failure, request-scoped callbacks, and session commit.
Six Bash groups cover shared guards, stdout/stderr, output bounds, cancellation,
interruption, independent commands, and native process cleanup. Eleven additional
CLI groups cover streaming, final text without deltas, replay, sequential tools,
argument errors, overflow recovery, committed history, provider and output
failure, HTTP/Bash cancellation, and real Ctrl-C during idle input, requests,
Bash, and stalled output. Native checks verify provider cleanup and child
termination before JVM exit. The CLI
self-test also runs through the documented source entrypoint.
The full run adds mandatory OpenAI and Anthropic TCP checks and ten HTTP socket
groups covering routes, authentication, request framing, exact body limits,
history rollback, independent sessions, cancellation, oversized events, stalled
output, tools, disconnects, deletion, and joined shutdown. `--offline` excludes these
twelve socket groups. The full run has 77 SDK groups and 11 CLI groups.
Checks execute against the packaged JAR.

The full direct JDK check passes with OpenJDK 25.0.1 using `--release 21`,
including provider and server sockets. Maven 3.9.16 also builds the package.
Maven compilation does not execute the native check runner. See the
[finish contract](../../FINISH_CONTRACT.md) for current evidence and remaining work.
