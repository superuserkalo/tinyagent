# TinyAgent conformance fixtures

`rpc_v1.json` is the shared language-neutral RPC v1 fixture. Language tests may add local provider mocks, but command framing, response fields, error codes, and limits must match this file exactly.

`jsonlLineBytes` counts UTF-8 bytes before the trailing LF. `queuedOutputBytes` counts complete framed bytes waiting to be written. `toolOutputBytes` counts the serialized UTF-8 observation. Synchronous writers have no in-memory output queue. Asynchronous writers either apply backpressure within the limit or reject output that would exceed it.

`tool_arguments.json` covers schema validation before native tool handlers run.
Python, TypeScript, Go, Rust, Zig, C#, Ruby, PHP, Java, and Elixir consume the same valid and invalid examples.
They include composed properties, local dynamic references, and large integral
exponent values in addition to required fields, nested arrays, bounds, and enums.

`providers/` contains synthetic SSE transcripts for both APIs, including split
tool arguments, UTF-8 text, multiline SSE data, reasoning metadata, and Anthropic
thinking signatures. All eleven languages exercise them through request
serialization and SSE parsers. Native TCP checks exercise real client connections.
Every OpenAI SDK requests encrypted reasoning with `store: false`. The native
request-and-replay checks assert that opt-in as well as retained response metadata.
Python and TypeScript also check their injected complete-response transports.

Their core lifecycle regressions cover cancellation during completed-event
delivery through both `run` and direct stream consumption. Such cancellation
cannot return history. A consumer exception at the terminal yield must propagate
unchanged, without a second terminal event. The checks also verify provider
cleanup, event sequence, unchanged input history, and successful agent reuse.

Python feeds the fixtures through its standard-library HTTP response parser,
including chunked framing and one-byte delivery, then runs native tool loops.
Its offline checks cover cancellation during headers and body reads, deadlines,
terminal cleanup, malformed responses, byte limits, and replay metadata.
Two separate mandatory provider TCP tests supplement its eight HTTP server tests.

Python HTTP fixtures connect the standard-library client and request handler with
bounded in-memory byte streams. They verify fixed and chunked request bodies,
UTF-8 boundaries, all five routes, duplicate authorization rejection, and body
limits. Lifecycle checks hold or fail the final HTTP chunk, cancel stalled
output, disconnect an active tool, and keep tool cleanup pending during deletion
and shutdown. Earlier completed history survives failed delivery. These eleven
groups, seven body-parser groups, and the exposure check also pass against the
installed wheel. The fixture does not bind a listener or replace TCP acceptance.

Python RPC checks hold prompt acknowledgements and terminal writes while closing,
resetting, or aborting a session. They verify busy state, cleanup, ordered tool
events, and retained history when delivery fails or cancellation wins. Binary
stream checks cover short writes, writes without progress, strict JSON numbers,
queue overflow, descriptor ownership, signal handlers, and command recovery.
Native subprocess checks cover EOF, Ctrl-C, stalled acknowledgements and replies,
fatal input, oversized replies, and failed stdout. Pipelined commands cannot hide
abort or EOF behind a stalled reply. A reader that resumes during the drain
period can reuse its connection. Bash checks verify paired tool events and the
termination of both the shell and its child. All thirty RPC groups also run
against the installed wheel, with copied subprocess fixtures and the installed
CLI. The native signal and process-group checks run on POSIX.

TypeScript delivers each fixture through native `Request`, `Response`, and
`ReadableStream` objects, splitting UTF-8 and delimiters across bytes. Its
provider checks cover the same tool loops and replay fields, malformed turns,
byte limits, cancellation, deadlines, consumer backpressure, and awaited body
cleanup. The deadline stays effective when a completion event is already
buffered behind a slow callback. Two mandatory provider TCP tests supplement
its eight HTTP server tests. Source and all tests receive strict type checks.

TypeScript RPC checks cover shared command fixtures, CLI subprocess framing,
history, EOF, output failure and closure, and recovery after an oversized reply.
Cancellation tests hold output writes across abort, reset, EOF, fatal input,
and oversized replies, then verify cleanup or recovery when the write drains.
Native subprocess checks exercise EOF and Ctrl-C during provider work and
Bash execution, verify child process-group termination, and hold a real stdout
pipe unread until the process exits. The same RPC checks run against
an isolated installed tarball. The CLI and fixture share the process adapter.

TypeScript HTTP checks connect Node's native server and client with bounded
paired byte streams. They exercise all five routes, authorization, fixed and
chunked body limits, ordered events, independent sessions, busy rejection,
rollback, disconnects, deletion, and cancellation under backpressure. Delaying
or failing the final framing write checks the history commit boundary. Tool
cleanup can remain pending while a healthy response stays open. The same
thirteen groups run against the installed package. These tests do not bind a
listener; the separate TCP suites still determine socket acceptance.

Go's runner executes race checks, vet, and the coding CLI self-test. Its RPC
tests compile a native subprocess. HTTP handler tests exercise lifecycle and
validation without a listener; native HTTP transport checks add actual request
parsing and response framing over bounded connections. Separate TCP suites
remain mandatory for socket acceptance.

Go's core lifecycle regressions cover cancellation during final delivery,
history snapshots before the start callback, closed provider-request scopes,
retained callbacks, and mutations of event payloads, handler arguments, and
provider-returned values. The offline race suite also passes against a copied
module with workspace resolution disabled. Runtime function locations confirm
that the core, tools, providers, RPC, and HTTP server use the copied module.

Four Go console groups add cancellation during native stdout/stderr writes,
restoration of blocking and descriptor flags for shared streams, and regular-file
ownership with conversation rollback. Seven native subprocess scenarios cover
idle input, requests, stdout, stderr, broken stdout, shared stdout/stderr, and
Bash. Cleanup records verify no active provider, retained original streams,
restored modes, and termination of the recorded Bash child. They exercise the
real interactive function with a fixture provider inside a native Go process.
The two actual Go CLI entrypoints also pass idle EOF and SIGINT checks.

Nine additional Go RPC groups cover bounded queuing, partial writes, invalid
write progress, oversized output recovery, pending acknowledgements, terminal
delivery and history, and slow tool cleanup. Ten native pipe cases cancel stalled
replies, acknowledgements, and events through EOF, context cancellation, abort,
or reset. Eight actual CLI subprocess cases cover stalled stdout with EOF,
SIGINT, abort, reset, overflow, and a broken reader. The full offline race suite
passes against the copied module. Both compiled CLI entrypoints independently
pass idle and stalled-output EOF/SIGINT checks. The earlier RPC hang is fixed;
The source and standalone module also pass the complete TCP race suite. Linux
execution and the final audit remain open.

Nine Go HTTP transport groups connect the real `net/http` server and client over
`net.Pipe`. They cover all five routes, fixed-length and fragmented chunked
requests, UTF-8 and byte limits, duplicate Authorization/Content-Type rejection,
independent sessions, busy handling, cancellation, deletion, output limits, and
disconnects. Holding or failing the final event verifies history commit and
rollback. Cancellation releases native blocked writes; deletion and shutdown
join cooperative provider cleanup. Slow tool cleanup retains paired end events.
Prompt responses use connection-close framing so completion has no unchecked
final chunk after the handler returns. These groups and the complete offline
race suite also pass against a copied module with workspace resolution disabled.

Rust runs core, provider, Bash, CLI, RPC subprocess, HTTP, and cleanup tests, plus
Clippy, the offline SDK example, and CLI self-test. Its provider fixtures split
every UTF-8 sequence and SSE delimiter across chunks. The RPC fixture is compiled
as a native executable and uses the same core and Bash tool as the CLI.
HTTP tests run native Hyper connections over bounded in-memory streams and
check terminal write failures, disconnects, deletion, and blocked readers.
Separate mandatory TCP checks cover the SDK server and CLI. The runner keeps
testing other Rust targets after a failed target.

The Go module and Rust crate bundle byte-identical copies of these JSON/SSE
fixtures under their own `conformance/`, plus the protocol reference, so extracted
packages can run the same suites without workspace files. Before invoking either
toolchain, the runner rejects missing, changed, or extra fixture copies and a
stale protocol reference. Update the shared sources first, then refresh the
copies using each package's README commands.

Zig 0.16.0 runs seventeen core groups, seventeen provider groups, thirteen
session groups, ten tool groups, four Bash groups, two console/configuration groups,
five output groups, nine CLI subprocess groups, seventeen RPC subprocess groups, thirteen
HTTP socket groups, and an offline
SDK example. Core checks cover ordered events, owned history, allocation cleanup,
cancellation, terminal delivery, and independent runs. Native HTTP fixtures
exercise both providers, fragmented UTF-8 and chunk framing, replay, complete
tool arguments, malformed responses, byte limits, deadlines, callback failure,
and request cleanup. The package bundles byte-identical SSE fixtures; the runner
rejects missing, changed, or extra copies before checking formatting and execution.
Session checks cover acknowledgement ordering, owned history, atomic commit,
busy rejection, reset, callback stop, cancelled final delivery, allocation
failure, independent sessions, and shutdown that joins held cleanup. Explicit
cancellation events also stop a fast provider before its next core checkpoint.
Output checks cover exact framed byte counts, queue overflow, and every
allocation failure. Real RPC subprocess pipes exercise the shared command
responses, UTF-8 framing, byte limits, commit and rollback, stalled output,
partial-frame cancellation, EOF, application-routed SIGINT, broken pipes,
descriptor ownership, and held cleanup. The subprocess harness uses Python 3;
the compiled agent and fixture use Zig with a statically linked schema validator.
Cargo builds the checked native schema library; Rust is not needed to run the SDK.
HTTP checks cover all five routes, IPv4/IPv6 authentication, explicit remote
exposure, malformed framing, fixed and chunked body limits, ordered JSONL,
busy admission, independent sessions, rollback, deletion, and shutdown. A held
native final-framing result forces client EOF before delivery returns. Other
socket checks cancel stalled delta and terminal writes, keep cleanup pending,
and delete a session while a cancellation body is incomplete.
Tool checks cover construction, shared schema cases, argument/definition ownership,
annotation behavior, output bounds, sequential recovery, callback failures, and
paired cancellation events. A complete offline tool example also builds and runs.
Bash checks cover the blacklist, combined output bounds, exact observation size,
UTF-8, process-group cancellation, and reaping after open or closed pipe cleanup.
The console checks verify streamed/final text, shared native Bash, failure rollback,
borrowed files, input limits, stalled and broken output, provider cancellation,
and descendant termination. The actual CLI passes self-test, idle EOF/interrupt,
RPC protocol output, and authenticated HTTP startup/shutdown without model calls.
The runner uses `-j1` to serialize executables with short subprocess deadlines;
the tests still exercise concurrent tasks and sessions internally. Unrestricted
parallel suite execution has produced intermittent RPC shutdown timeouts.
The original CLI entrypoint now imports the Zig 0.16.0 SDK and participates in these checks.

Java compiles its native SDK module and creates a runtime-only JAR. Its
checks compile and execute against that JAR, including mandatory TCP fixtures.
The JDK build helper reads exact dependencies from the Maven POM; it needs those
JARs in the local Maven repository. Seventy-seven SDK groups cover providers,
validated tool runs, sessions, native RPC, bounded Bash execution, and Jetty HTTP sessions.
Eleven CLI groups
exercise the same packaged streaming provider, typed history, replay, sequential
Bash calls, argument errors, history rollback, overflow recovery, HTTP/Bash
cancellation, and native Ctrl-C during idle input, requests, Bash, and stalled
output. Broken output releases provider work. The helper copies the declared
dependency JARs for the documented CLI classpath and also runs the original
CLI source entrypoint's blacklist self-test. Nine tool groups cover shared schemas,
invalid construction, annotation behavior, both providers and replay, bounded
recovery, cancellation pairs, held cleanup, interruption, and callback ownership.
Native RPC and HTTP cases check tools, cancellation, and prior history rollback.
The CLI and transports share one Agent loop. The installed Maven consumer passes
the full suite, five example builds, and CLI RPC/HTTP startup and shutdown.

Ruby's fifteen provider groups use the native `Net::HTTP` request serializer,
response parser, and chunk reader over fragmented in-memory streams. They cover
both shared text and tool transcripts, replay metadata, immutable snapshots,
invalid events, byte limits, cancellation during headers and body reads,
deadlines, and consumer failure. Callback failure closes the request before the
HTTP parser can wait for more chunk framing. Synchronous fiber handoff also
preserves consumer `break` across native chunk cleanup. Seventeen core groups
cover text-run events, immutable replay history, provider and callback failures,
early exits, cancellation, cleanup, and concurrent runs. Seven session groups
verify shared commands, response/event order, history commit and rollback,
busy state, pending acknowledgements, reset, and reentrant closure. Fourteen
pipe groups cover real Ruby subprocesses, UTF-8 and JSONL limits, Ctrl-C/EOF,
stalled acknowledgements and output, reuse after cancellation, queue overflow,
and caller-owned IO modes. Six Bash groups verify native output, bounds,
cancellation, interruption, and process-group cleanup. Eleven CLI groups cover
streaming and terminal-only text, replay, sequential tool observations, argument
errors, overflow recovery, frozen history, callback failures/nonlocal exits,
HTTP/Bash cancellation, console rollback, and EOF. Native subprocesses verify
Ctrl-C during idle input, requests, Bash, and stalled output, plus broken output,
closed HTTP streams, joined threads, child termination, and restored blocking
modes and descriptor ownership. Ten tool groups check shared Draft 2020-12 cases,
construction errors, recursive local references, metadata snapshots, native
provider replay, bounded observations, paired cancellation, held cleanup, callback
exits, and RPC history. A native RPC subprocess checks valid, invalid, and cancelled
tool calls. The full suite has 93 tests and 1170 assertions, including two provider
TCP groups and eleven WEBrick HTTP groups. The same suite passes against an
independently installed gem. The coding CLI shares this Agent loop and passes
RPC and authenticated HTTP startup/shutdown checks. The runner uses Bundler to
select the locked dependencies. Six README examples pass syntax checks; the
Tool, Bash, and HTTP examples execute without provider requests.

C# builds its core and optional HTTP packages, native checks, and original CLI
with the .NET 8 SDK and ASP.NET Core reference pack. Its fifteen provider groups exercise native
`SocketsHttpHandler` request serialization and response parsing over fragmented
memory streams. They cover shared fixtures, immutable replay, malformed events,
SSE and cumulative argument limits, cancellation, deadlines, callback failure,
and asynchronous cleanup. Cancellation during response disposal prevents a
successful turn. Fourteen core groups, fourteen session groups, thirteen native
RPC groups, twelve HTTP groups, five Bash groups, and eleven CLI groups bring the
offline total to ninety-one with seven tool groups.
RPC uses real subprocesses and pipes. HTTP connects Kestrel and HttpClient over
bounded pipes, exercising routes, authentication, malformed framing, body/output
limits, concurrency, disconnects, final-chunk failure, and held shutdown cleanup.
The Bash checks execute real processes and verify process-group cleanup,
independent cancellation, exact combined output bounds, and background-child
cleanup. CLI checks compile the actual CLI source and use the streaming SDK
provider over an injected HttpClient. They cover sequential calls, replay,
argument failures, bounded observations, immutable history, callback failures,
terminal-only text, HTTP cancellation, console rollback, and EOF. Native processes
verify Ctrl-C during idle input, HTTP, Bash, and stalled stdout/stderr, plus
broken stdout and `/dev/null` redirection. Cleanup records confirm released
HTTP streams and disposed providers; Bash child PIDs disappear.
All ninety-five groups also pass against isolated NuGet packages, including
native TCP. All seven README examples compile; both RPC examples and Bash run
unchanged, and the HTTP example runs on a native listener with port zero.
The full runner includes four mandatory TCP groups and the original CLI self-test.
The C# TCP disconnect check holds provider cleanup open, verifies that the session
remains busy, then releases cleanup and waits within a bounded deadline for the
next prompt. A provider entering its cleanup block does not mean session cleanup
has finished. The next completed result must retain the earlier history.
Provider TCP checks cover both default clients and sockets configured to reset
on close. Completion and cancellation must close the connection; either EOF or
a peer reset satisfies that cleanup assertion after request validation.
C# now shares its validated Agent loop with the coding CLI, RPC, and HTTP.
Tool groups cover the shared schema cases, invalid schemas and unresolved
references, sequential recovery, bounded errors, cancellation pairs, and consumer
failures. The installed CLI also passes RPC EOF and authenticated HTTP startup
and Ctrl-C shutdown without provider requests.

PHP's 97 groups cover both providers, text runs, session ownership,
native RPC, guarded Bash, the streaming CLI, and ReactPHP HTTP sessions. Provider fixtures deliver
fragmented bytes through an injected client; they do not exercise cURL or HTTP
framing. Native subprocesses and Unix socket pairs verify framing, bounds,
history, cancellation, cleanup, and signal/stream ownership. Seven Bash groups
add combined output limits, process-group cleanup, and cooperative fiber
scheduling. Eleven CLI groups verify streamed and terminal-only text, replay,
sequential observations, argument errors, overflow recovery, history rollback,
bounded responses, callback failures, cancellation, and retained-callback rejection.
Caller exceptions thrown into SDK-owned fibers survive provider wrapping in
the core and CLI, response cleanup, and Bash cleanup. Native checks cover EOF, idle/request/Bash SIGINT, stalled
stdout/stderr, broken stdout, and restored signals, blocking modes, and stream
ownership. Ten tool groups cover the shared schema cases, construction errors,
immutable inputs, annotation behavior, both native providers and replay,
sequential bounded errors, paired cancellation, held cleanup, callback ownership,
and RPC history. Two mandatory cURL/TCP groups and eleven HTTP socket groups
exercise native framing, tool events, cancellation, and history rollback.
All 97 groups pass from source and an independent Composer installation. Seven
README examples pass syntax checks; credential-free examples and installed CLI
RPC/HTTP modes execute. The runner checks syntax, creates the local package, and
runs the CLI self-test. Agent owns the only tool loop.

Elixir's 136 source tests cover both Mint streaming providers, validated tool runs,
sessions, native RPC, guarded Bash, the streaming CLI, and Bandit HTTP sessions.
Native subprocess checks verify pipe framing, backpressure, process cleanup,
descriptor flags, and restored signal handling. Fourteen HTTP socket groups cover
authentication, IPv4/IPv6, native framing, body/output limits, history rollback,
silent disconnects, cancellation of stalled output through parent and provider
child tokens, deletion, and shutdown that joins held cleanup. Both provider TCP
groups also pass. A Mint regression rejects excessive unfinished response lines
and chunk-size digits. The RPC terminal-write cancellation check waits for actual
terminal bytes before sending abort, so provider cleanup cannot race its trigger.
A forced HTTP final-write interleaving verifies completed history when client
EOF reaches the watcher before the successful write returns.
The direct helper accepts compiled dependencies through `TINYAGENT_ELIXIR_DEPS`
and verifies every locked version. `--offline` excludes sixteen socket groups.
The original CLI self-test and Hex packaging pass. The finish contract records
independent Mix consumer acceptance. Eleven tool tests cover shared schemas,
construction, metadata, both providers, replay, bounded errors, paired cancellation,
held cleanup, nonlocal exits, and retained callbacks. Native RPC, parsed sessions,
and HTTP check tools and rollback. Agent owns the CLI and transport tool loop.
Eight examples compile; offline examples and CLI RPC/HTTP modes execute.
Linux and Elixir 1.15 execution remain unverified.

Swift compiles its library through SwiftPM and its checks in strict Swift 6 mode.
Its 81 groups cover typed JSON, both providers, the text-run core, sessions,
RPC, Bash, the streaming CLI, and SwiftNIO HTTP sessions. Native pipes and
subprocesses verify framing, backpressure, EOF, interrupts, rollback, and cleanup.
Provider fixtures use `URLSession.AsyncBytes` with an injected `URLProtocol`;
they supply parsed responses and do not verify HTTP framing or TLS. Tests
observe custom protocol cleanup separately from Foundation invalidation.
Two provider TCP groups and ten HTTP socket groups exercise native framing and
connections. All 81 groups also pass against the current independent SwiftPM
consumer, along with its HTTP README example. Validated tools and their loop,
Agent CLI integration, minimum platform execution, and final acceptance remain open.

After installing each SDK's dependencies, run from the TinyAgent root:

```sh
python3 conformance/run.py
python3 conformance/run.py python typescript
```

The runner uses its own Python interpreter for Python tests. Use your virtual
environment's interpreter if that is where you installed the SDK. It removes
provider credentials from child environments and includes the real HTTP socket
tests. A missing runtime, absent dependency, unfinished SDK, or failing check
returns a nonzero exit status. No language is silently skipped. Successful
checks still require the packaging and final audit in `FINISH_CONTRACT.md`.
