# TinyAgent acceptance checkpoints

Status: archived. Superseded by the [current finish contract](../../../FINISH_CONTRACT.md).

The project is complete when the requirements below are implemented and verified
against the current source. Passing the Python and TypeScript tests alone does
not complete this contract.

## Scope

Finish the native runtime described in [PROTOCOL.md](../../../PROTOCOL.md) for Python,
TypeScript, Go, Rust, Zig, Java, Ruby, Elixir, C#, PHP, and Swift. Each language
owns one agent loop shared by its imported SDK, coding CLI, JSONL RPC adapter,
and HTTP server. No implementation launches another language's agent.

Keep the public interface small and the implementation readable. Use named
modules where they separate real responsibilities. Keep the documented CLI
entrypoints runnable and preserve the existing language examples. Do not add
frameworks, generated agent loops, or extension systems.

The existing deferrals in PROTOCOL.md remain outside this version: durable
sessions, replay subscriptions, provider switching within a session, ACP,
parallel tools, queues, steering, approval UI, orchestration, TLS termination,
and multi-user ownership. Registry publication and git commits are separate
user-directed actions, not completion prerequisites.

## Acceptance requirements

1. **Native core and packages.** Each language exposes idiomatic Tool, Provider,
   Agent, ordered events, completed results, replayable neutral history, and
   run-scoped cancellation. Package/import smoke tests run without starting a
   CLI or requiring credentials. Documented examples compile or execute.
2. **Providers.** OpenAI Responses and Anthropic Messages stream text, buffer
   complete tool arguments, preserve replay metadata, reject malformed or
   incomplete responses, and propagate cancellation. Local HTTP fixtures
   exercise request serialization and actual SSE parsing without paid calls.
3. **Tools.** Arguments are checked before handlers execute. Tools run
   sequentially. Ordinary errors and oversized outputs become bounded,
   recoverable observations. Cancellation pairs every started tool with an end
   event and stops Bash process groups where supported. CLI blacklist checks
   continue to pass.
4. **Lifecycle and history.** Run IDs and sequences are consistent, start is
   first, and exactly one terminal event is last. Successful sessions commit
   history atomically; failures, cancellation, and disconnects retain the prior
   history. Different sessions can run concurrently, with one run per session.
5. **RPC.** All commands, command IDs, response-before-event ordering, error
   codes, UTF-8 framing, limits, busy handling, reset, EOF, and interrupt behavior
   match the shared RPC fixture and PROTOCOL.md. Test actual subprocesses.
6. **HTTP.** Implement all five routes, streaming JSONL, status/error mappings,
   independent sessions, cancellation, deletion, disconnect cleanup, and server
   shutdown. Verify loopback defaults, bearer authentication, explicit remote
   opt-in, absent CORS, body limits, and bounded output over real local sockets.
7. **Shared conformance.** Every language passes all eleven scenarios listed in
   PROTOCOL.md, including transport failures and cancellation. A documented
   runner reports missing runtimes as incomplete rather than silently passing.
8. **Quality and delivery.** No duplicated agent loops, placeholders, explicit
   or implicit TypeScript any, unexplained compatibility branches, or stale
   implementation claims. Run relevant formatters, compilers/type checks,
   package smoke tests, and the repository check. Record unrelated baseline
   failures separately. Preserve concurrent work and leave changes uncommitted.

## Current evidence

Updated 2026-09-09. Loopback TCP now works in this environment. Python,
TypeScript, Go, Rust, Java, Ruby, C#, PHP, and Swift pass their implemented source
checks, including real provider and HTTP sockets. Elixir now passes all 122
source and independent-package tests under the bounded VM configuration; earlier
subprocess timeouts remain recorded below. Zig now has a native text core, both streaming providers, session
ownership, events, cancellation, JSONL RPC, native HTTP sessions, and validated
sequential tools, guarded Bash, and a shared coding CLI with RPC and HTTP modes.
Six other SDKs still need
validated executable tools and shared CLI loops.
Independent consumer checks below predate the latest OpenAI request changes
unless explicitly refreshed. Packaging acceptance and the final audit remain
open. Earlier evidence is preserved in the
[checkpoint archive](implementation-checkpoints.md).

| Language | Core and providers | RPC | HTTP | Full conformance and packaging |
| --- | --- | --- | --- | --- |
| Python | Implemented; lifecycle, schemas, providers, and cancellation verified | Implemented; subprocess, output, and history checks pass | All native framing, lifecycle, and TCP checks pass | All 104 source and installed-wheel tests and CLI self-test pass; examples checked; final audit open |
| TypeScript | Implemented; lifecycle, schemas, providers, and cancellation verified | Implemented; subprocess and output cleanup checks pass | All native framing, lifecycle, and TCP checks pass | All 91 installed-tarball tests, strict declaration/test/example checks, source build, and CLI self-test pass; final audit open |
| Go | Implemented; lifecycle, schemas, providers, and cancellable console verified | Implemented; native backpressure, delivery, and subprocess shutdown verified on macOS | All native framing, lifecycle, and TCP checks pass | Full source and independent-module race suites, vet, both CLI self-tests, and separate importer pass; examples compile; other-platform execution and final audit open |
| Rust | Implemented; lifecycle, schemas, providers, Bash, and cancellable shared CLI verified | Implemented; bounded output, native CLI pipes, and cleanup checks pass | All native framing, lifecycle, and TCP checks pass | All 60 source and independent-crate tests, Clippy, example, and self-test pass; other-platform execution and final audit open |
| Zig | Both streaming providers, checked sequential tools, guarded Bash, events, owned history, sessions, native cancellation, and shared CLI implemented | Implemented over borrowed native files; pipe, overflow, cleanup, and application-routed interrupt checks pass | Native HTTP implemented; all thirteen source and consumer socket groups pass | All 68 native tests, 9 CLI groups, 17 RPC groups, and 13 HTTP groups pass from source and an independent package; all examples pass. Verified on macOS arm64; the final cross-language audit remains open |
| Java | Text core, both providers, and Bash implemented; schema-validated tools and shared CLI loop open | Implemented for text agents; subprocess and pipe checks pass | Jetty implemented; framing, lifecycle, and TCP checks pass from source and installed Maven package | 66 SDK and 11 CLI groups pass; Maven build/install and separate consumer HTTP/provider TCP plus HTTP example pass; minimum-version execution and final acceptance open |
| Ruby | Text core, both providers, and Bash implemented; schema-validated tools and shared CLI loop open | Implemented for text agents; subprocess and pipe checks pass | WEBrick implemented; framing, lifecycle, and TCP checks pass from source and installed gem | All 81 source and installed-gem tests / 963 assertions pass; CLI self-test, five example syntax checks, and HTTP example pass; Linux and final acceptance open |
| Elixir | Text core, both providers, and Bash implemented; validated tools and shared CLI loop open | Implemented over POSIX descriptors; all 31 RPC subprocess checks pass within the complete source and package suites | Bandit implemented; all thirteen source and consumer HTTP groups pass after a final-write/EOF race fix | All 122 source and consumer tests, strict compilation, Hex packaging, self-test, and HTTP example pass with bounded VM schedulers; Linux, minimum-version execution, and final acceptance open |
| C# | Text core, both providers, and Bash implemented; schema-validated tools and shared CLI loop open | Implemented for streams and redirected Linux/macOS stdio; native checks pass | Kestrel implemented; native framing, cleanup, and TCP checks pass from source and independent NuGet packages | All 87 native groups pass against independent packages; CLI self-test and all six README example builds pass, including native RPC/Bash/HTTP execution; final acceptance open |
| PHP | Text core, both providers, and Bash implemented; schema-validated tools and shared CLI loop open | Implemented over owned POSIX streams; subprocess and Unix socket-pair checks pass | ReactPHP implemented; framing, lifecycle, and TCP checks pass from source and installed Composer package | All 86 source groups and ZIP packaging pass; separate Composer consumer HTTP/provider TCP and HTTP example pass; Linux and final acceptance open |
| Swift | Text core, both providers, and Bash implemented; validated tools and shared CLI loop open | Implemented for native handles and macOS stdio; subprocess checks pass | SwiftNIO implemented; framing, lifecycle, and TCP checks pass from source and independent SwiftPM consumer | All 81 source and consumer groups, HTTP README example, source packaging, and iOS 16 simulator compilation/linking pass; minimum-version execution and final acceptance open |

Update this evidence as requirements pass. Close the goal only after a final
requirement-by-requirement audit, with commands and results recorded here or
linked from this file.

## 2026-09-09 socket acceptance

These initial checks preceded the HTTP additions recorded in the later sections.

The former environment restriction on local TCP binding is gone. The complete
source commands below now include their mandatory provider and HTTP socket
tests; no credentials or paid provider requests are used.

| Command from this directory | Result |
| --- | --- |
| Isolated Python 3.14.2 environment: `python conformance/run.py python` | 103 tests and CLI self-test pass with declared jsonschema 4.26.0 and referencing 0.37.0. |
| `python3 conformance/run.py typescript` | 87 tests, strict source/test type checks, and CLI self-test pass. |
| Cached Go 1.26.4 on PATH, `GOTOOLCHAIN=local GOPROXY=off python3 conformance/run.py go` | Full race suite, vet, and CLI self-test pass. |
| `CARGO_NET_OFFLINE=true CARGO_INCREMENTAL=0 python3 conformance/run.py rust` | All 60 tests, Clippy with warnings denied, offline example, and CLI self-test pass. |
| `conformance/run.py` dispatches for C#, Java, Ruby, and Swift | Native checks pass: C# 87 groups, Java 57 SDK and 11 CLI groups, Ruby 71 tests / 797 assertions, Swift 71 groups. Includes native TCP, build checks, self-tests, and each configured package step. Runner status remains incomplete for missing runtime components. |
| From `tinyagents/php`: `php check.php` | Syntax, ZIP packaging, all 76 groups including both native cURL/TCP providers, and CLI self-test pass. |
| From `tinyagents/elixir`: `HEX_OFFLINE=1 mix deps.get` and `HEX_OFFLINE=1 mix compile --warnings-as-errors` | Restores the locked Mint 1.9.3, Jason 1.4.5, and HPAX 1.0.4 from the existing Hex cache; normal Mix compilation passes. No dependency version changed. |
| `HEX_OFFLINE=1 python3 conformance/run.py elixir` | All 108 tests including both TCP groups, formatting, compilation, Hex packaging, and self-test pass. Runner remains incomplete for tools, shared CLI integration, and HTTP. |
| From `tinyagents/swift`: `swift build` | SwiftPM compiles the native SDK successfully. |
| From the repository root: `npm run check` | Stops before source checks at three existing nested Biome roots: `tuiv2-bottom-up`, `rust-performance-candidate`, and `rust-performance-master`. |

Five test files needed corrections. Rust's provider fixture explicitly sets
accepted sockets to blocking mode and awaits its blocking server task, allowing
Tokio to finish connection cleanup. Python verifies early oversized-body
rejection using headers alone, and waits for session cleanup after final event
bytes arrive. Go separately bounds overflow-input delivery and process shutdown
so race-instrumented JSON parsing does not consume the shutdown deadline.
PHP uses an explicit reference capture for the TCP fixture accumulator; its
outer arrow closure previously captured a private copy. Elixir limits
concurrent test groups to two because multiple groups launch additional
BEAM runtimes. Cases and deadlines are unchanged. With the default sixteen
groups, the first full run had nine startup timeouts and one abort-terminal
assertion failure; the second had three startup timeouts. An isolated failing
Bash fixture passed, and the bounded full suite passes. This establishes the
bounded test configuration, not stability under the former competing load.
No runtime source changed in this block.

One Rust RPC interrupt check timed out during a concurrent run. It passed in
isolation and in the complete rerun without other language suites competing for
resources. Its source and timeout are unchanged; the passing rerun does not
establish that the intermittent failure is resolved.

The full eleven-language scope remains active. Previously declined validator
downloads for Java, Ruby, Elixir, C#, PHP, and Swift remain unauthorized. Package
TCP verification and the requirement-by-requirement audit are still open.
Changes are uncommitted. Historical checkpoints were moved byte for byte to
the linked archive; this file now holds the contract and current evidence.

## 2026-09-09 Ruby HTTP adapter

`TinyAgent.serve` now implements the five HTTP routes with bearer authentication,
loopback defaults, explicit remote opt-in, bounded JSON bodies and events, and
independent sessions. It uses the existing `Session` worker and history rules.
`http.rb` owns routing and server lifetime; `http_connection.rb` owns socket
delivery and cleanup. No second agent loop or event queue was added.

The gem pins WEBrick 1.9.2 for HTTP parsing and listening. The official gem has
no transitive runtime dependencies or native extensions. Each connection serves
one request; JSONL writes begin after headers and use an unchunked, closing
response. Cancelled pending writes have one second to drain. Disconnect,
deletion, and shutdown cancel the captured run and join cooperative cleanup.
The session stays owned while deletion waits, including during concurrent
server shutdown. A callback can request `stop`; an external owner joins with
`wait` or `close`.

Verification scratch is `../../.scratch/tmp/ta-ruby-http.nIAk6i` relative to this
directory. Commands run from `tinyagents/ruby` unless stated otherwise.

| Check | Result |
| --- | --- |
| With isolated `GEM_HOME`, `python3 conformance/run.py ruby` from this directory | 81 tests / 961 assertions and CLI self-test pass. Includes both provider TCP tests and ten HTTP socket groups. Runner correctly remains incomplete for tools and shared CLI integration. |
| `gem build tiny-agent-sdk.gemspec`, then local gem installation with WEBrick 1.9.2 into an isolated consumer | Build and installation pass. Build reports the existing missing-homepage warning. |
| Consumer `GEM_HOME`, `ruby -r <scratch>/package_guard.rb test/http_test.rb` | 10 tests / 164 assertions pass against the installed gem. Guard checks all sixteen packaged file hashes, all fourteen loaded SDK paths, and the pinned WEBrick installation before and after execution. |
| Same consumer and guard, `test/provider_tcp_test.rb` | Both native provider completion/cancellation tests pass against the installed gem. |
| `python3 <scratch>/check_example.py` | Extracted HTTP README example serves authenticated health and session requests and exits cleanly on Ctrl-C. Only its port changes to zero. No credentials or paid provider requests are used. |
| `ruby -c` for the nine changed Ruby source, test, build, and package files | All syntax checks pass. |
| Repository-root `npm run check` | Same three unrelated nested Biome roots prevent repository checks from starting. |

Validated executable tools, the shared coding CLI loop and its transport modes,
Linux execution, and the final Ruby acceptance audit remain open. The other ten
language requirements are unchanged. All changes remain uncommitted.

## 2026-09-09 Java HTTP adapter

`HttpServer.start` implements all five routes using the existing `Session`.
`HttpServer` owns routing and shutdown, `HttpJson` bounds JSON bodies and errors,
and `HttpConnection` owns delivery and disconnect cleanup. Each connection serves
one HTTP/1 request. Jetty parses requests and writes responses; the session
worker awaits each write, including terminal framing, before history can commit.
After reading the entire prompt body, its handler observes native endpoint EOF
while the worker runs. No event queue or second agent loop was added.

The POM pins Jetty server, HTTP, I/O, and utility modules at 12.1.13 and SLF4J API
2.0.17. Official POMs identify these five required HTTP libraries without native
extensions; optional JMX and test dependencies are excluded. Existing Jackson
versions are unchanged. No declined validator dependency was downloaded.
An embedding application supplies its logging implementation. Without one,
SLF4J emits its standard missing-provider warning when HTTP starts.

Verification scratch is `../../.scratch/tmp/ta-java-http.9xvlKR` relative to this
directory. The JDK is OpenJDK 25.0.1; compilation uses `--release 21`, all compiler
warnings enabled, and warnings treated as errors. This does not prove execution
on Java 21 itself.

| Check | Result |
| --- | --- |
| Isolated `MAVEN_REPO_LOCAL`, `python3 conformance/run.py java` | 66 SDK groups, 11 CLI groups, and the CLI self-test pass. The runner remains incomplete for tools and shared CLI integration. |
| Apache Maven 3.9.16, `mvn --batch-mode --no-transfer-progress -Dmaven.repo.local=<scratch>/repository package`, then `install` | Both succeed. Maven compiles 27 runtime files and packages the README, license, and POM metadata. Native checks are separate from Maven's test lifecycle. |
| `python3 <scratch>/check_consumer.py` | A separate project with one dependency on the installed SDK compiles. All nine HTTP groups and both provider TCP groups pass with all 27 SDK class origins checked against the installed JAR. Built/installed JAR and POM bytes match. |
| HTTP README example in that consumer | Compiles, serves authenticated health/session requests without credentials, and joins server shutdown when Enter is sent. Only its port changes to zero. |
| Repository-root `npm run check` | The same three unrelated nested Biome roots prevent repository checks from starting. |

Socket cases cover native malformed/chunked framing, UTF-8 fragments, exact body
limits, authentication, media types, ordered events, history rollback, concurrent
sessions, cancellation, oversized output, stalled readers, silent disconnects,
deletion, and shutdown that retains cooperative cleanup. A callback can request
`stop`; an external owner uses `awaitStopped` or `close`. Tools, shared coding CLI
integration and its transport modes, and final Java acceptance remain open.
Changes are uncommitted; the full eleven-language goal remains active.

## 2026-09-09 PHP HTTP adapter

`HttpServer::start` implements all five HTTP routes with the existing `Session`
fibers. ReactPHP owns parsing and framing. `HttpServer` owns routing and shutdown,
`HttpJson` bounds request bodies, `HttpListener` accepts native sockets, and
`HttpConnection` awaits native delivery and handles disconnects. There is no
second agent loop or event queue. Each connection serves one request.

Events and final framing must drain before history commits. Cancelled pending
writes have one second to drain. A silent disconnect cancels its captured run;
deletion and shutdown retain the session until cooperative cleanup finishes.
The server preserves the application's asynchronous signal setting and shares
scoped SIGPIPE suppression with RPC. A callback requests `stop`; external
`close` joins. The caller still owns the provider.

The Composer manifest pins seven directly used HTTP libraries. The source
lockfile contains ten libraries total; dependency metadata was reviewed and
installation disabled scripts and plugins. Composer 2.10.3 was downloaded from
its official release URL and its published SHA-256 was verified. No declined
validator was downloaded. Core-only package imports remain usable without
Composer's autoloader. The ZIP contains twenty-eight runtime PHP files and
three metadata/documentation files.

Verification scratch is `../../.scratch/tmp/ta-php-http.9LTR0X`. PHP is 8.4.3 on
macOS. Commands run from `tinyagents/php` unless stated otherwise.
Consumer and artifact directories include the ZIP's content hash. The package
guard caught Composer reusing an older same-version archive during a documentation
refresh; the final installation uses a distinct artifact URL and verifies current bytes.

| Check | Result |
| --- | --- |
| Composer `update --lock --no-scripts --no-plugins --optimize-autoloader` | Dependency resolution and optimized autoload generation pass. |
| `python3 conformance/run.py php` from this directory | Syntax, ZIP packaging, 86 groups, and CLI self-test pass. Runner remains incomplete for executable tools and shared CLI integration. |
| `python3 <scratch>/check_consumer.py` | Separate project with one SDK dependency installs from the ZIP through Composer. Ten HTTP groups and both native provider TCP groups pass. All 31 package file hashes and loaded SDK class origins are checked. |
| HTTP README example in that consumer | Authenticated health and session requests and Ctrl-C shutdown pass without credentials. Only its port changes to zero. |
| Core-only package import | Constructs an agent without Composer's autoloader, loading ReactPHP, starting a CLI, or requiring credentials. |
| Composer `validate --strict --no-check-publish` | Manifest is valid; strict status is nonzero for advisory warnings about the explicit unpublished version and exact dependency pins. These are intentional package/repository requirements. |
| Repository-root `npm run check` | Same three unrelated nested Biome roots prevent repository checks from starting. |

Socket cases include IPv4/IPv6, explicit remote opt-in, bearer authentication,
duplicate headers, absent CORS, native malformed/chunked framing, UTF-8 fragments,
exact body bounds, event order, history rollback, independent sessions, busy
handling, cancellation, oversized output, stalled readers, silent disconnects,
deletion, and shutdown. A restricted client receive window holds final output
after provider cleanup and proves rollback on cancellation and disconnect.
Pending cancellation rechecks session ownership after its request body arrives.
Linux execution, tools, shared CLI integration and its transport modes, and the
final PHP acceptance audit remain open. All changes remain uncommitted.

## 2026-09-09 Swift HTTP adapter

`HttpServer.start` implements all five routes with the existing `Session` actor.
`HttpServer` owns routing and shutdown, `HttpRequest` bounds input and connects
native parser callbacks, and `HttpConnection` owns response delivery and run
cleanup. SwiftNIO parses and frames HTTP/1. Each connection serves one request;
later input is discarded while reads remain active to observe native EOF.
There is no second agent loop or event queue.

Every event awaits its native write promise. The terminal emitter also awaits
final HTTP framing before history may commit. Cancelled pending writes have one
second to drain. Disconnect cleanup cancels only its captured run ID. Session
waits also name the observed run, so an old connection cannot wait on a later
run. Deletion retains its session through cooperative cleanup, and concurrent
shutdown joins it. A detached shutdown owner avoids inheriting a callback's
run context. `stop` requests shutdown; external `close` joins session workers,
connection workers, and the native event loop. Caller-owned signal handlers and
provider lifetime remain intact.

The Swift 6.2 manifest pins SwiftNIO 2.102.0. Its lockfile resolves Atomics 1.3.1,
Collections 1.6.0, and System 1.8.1. Official release metadata, package manifests,
and the exact NIO write/parser APIs were inspected before integration. No
declined schema validator was downloaded. SwiftPM builds a dynamic SDK library;
the source archive contains twenty runtime files and five metadata/documentation
files. The check helper takes native module maps from SwiftPM's build description.

Verification scratch is `../../.scratch/tmp/ta-swift-http.WRbw62`. Swift is Apple
Swift 6.2.1 on macOS. The independent consumer directory is
`consumer-d270a51b752a`, named from the archive hash. Source and consumer checks
run without provider credentials or paid requests.

| Check | Result |
| --- | --- |
| From this directory, `python3 conformance/run.py swift` | Strict formatting, SwiftPM build, source archive, all 81 groups, and CLI self-test pass. Runner remains incomplete for executable tools and shared CLI integration. |
| `python3 <scratch>/check_consumer.py` | A separate SwiftPM project has one dependency on the extracted SDK. All 81 groups pass, including RPC/CLI subprocesses, both provider TCP groups, and ten HTTP socket groups. Checks verify the loaded SDK library path and SHA-256, all 25 package files, and the resolved dependency versions. |
| HTTP README example in that consumer | Builds, serves authenticated health/session requests, and joins shutdown after Enter. Only its port changes to zero. No credentials are required. |
| `python3 <scratch>/check_ios.py` | SDK and dependencies compile and link for the arm64 iOS simulator. Swift warnings and linker warnings are errors. `vtool` confirms IOSSIMULATOR, minimum iOS 16.0, SDK 26.1. Execution on iOS itself remains unverified. |
| `python3 <scratch>/audit.py` | Whitespace, Python syntax, Swift boundary types, runtime-file count, and exact dependency pins pass across the 17 changed files. |
| Repository-root `npm run check` | The same three unrelated nested Biome roots prevent repository checks from starting. |

Cross-compilation initially emitted a Clang SDK-path warning even though the
Swift compiler received the simulator SDK. The final helper explicitly supplies
that SDK to Clang's link driver and passes without the warning. Setting SDKROOT
globally was rejected by the host manifest compiler and is not part of the helper.

HTTP cases cover IPv4/IPv6, remote opt-in, bearer authentication, absent CORS,
malformed and chunked framing, duplicate headers, fragmented UTF-8, exact body
limits, ordered events, history rollback, independent sessions, busy handling,
cancellation, oversized events, stalled delta/final writes, silent disconnects,
pending cancellation after deletion, and cleanup retained during shutdown.
Validated tools, the shared coding CLI loop and its transport modes, minimum
platform execution, refreshed non-HTTP examples, and final Swift acceptance
remain open. The full eleven-language goal stays active. Changes are uncommitted.

## 2026-09-09 Elixir HTTP adapter

`HttpServer.start_link` implements all five routes with the existing `Session`
controller. `HttpServer` owns routing, sessions, deletion, and shutdown;
`HttpJson` bounds request bodies and errors; `HttpConnection` owns synchronous
event delivery and disconnect cleanup. `HttpTransport` implements the public
Thousand Island transport behavior to register native sockets during handshake.
Bandit owns parsing and framing. No second agent loop or event queue was added.

Each connection serves one HTTP/1 request. Erlang's `:socket` backend makes
writes await native delivery without an intermediate driver queue. The terminal
callback also awaits final chunk framing before history can commit. A separate
reader detects silent EOF, monitors the current callback's cancellation token,
and gives cancelled pending writes one second to drain. Provider child-token
cancellation is covered separately from cancellation through the session.
Provider cleanup may stay held while the healthy response remains open.

Deletion keeps its session until cleanup completes, and simultaneous shutdown
joins it. Shutdown also closes idle connections and pending request bodies.
Callback `close` returns false; external `close` joins the workers and listener.
The creating process's exit also cancels and joins cleanup. Late connection waits
name their run ID, so they cannot accidentally wait for a later run. The provider
and application signal handlers remain caller-owned.

The manifest pins Bandit 1.12.5, Plug 1.20.3, and Thousand Island 1.5.0. Official
Hex metadata, archive checksums, build manifests, and native transport APIs were
reviewed before integration. The dependency check exposed two published Mint
1.9.3 parser vulnerabilities. Mint is now pinned to 1.10.0, the patched version
identified by the upstream [response-buffering advisory](https://github.com/elixir-mint/mint/security/advisories/GHSA-g83f-2j6r-q6m4)
and [chunk-size advisory](https://github.com/elixir-mint/mint/security/advisories/GHSA-7p8w-j234-7qc8).
A native-parser regression verifies rejection of excessive unfinished status
lines, chunk-extension lines, and chunk-size digits. Jason remains 1.4.5.
The lock contains ten libraries. No declined schema validator was downloaded.

Verification scratch is `../../.scratch/tmp/ta-elixir-http.cOr1F8`. Elixir is
1.20.4 on OTP 29 and macOS. The final consumer is `consumer-4fef24264aa2`.
Its source archive SHA-256 is
`4fef24264aa2277b156828f4e396db5ea63b1b44e4cbb38b8150ab65d1c350e9`.
No provider credentials or paid requests are used.

| Check | Result |
| --- | --- |
| From this directory, `HEX_OFFLINE=1 python3 conformance/run.py elixir` | All 121 checks and CLI self-tests pass, including both provider TCP groups and twelve HTTP groups. Formatting, strict compilation, and Hex packaging pass. The runner remains incomplete only for executable tools and shared CLI integration. |
| From `tinyagents/elixir`, `mix compile --warnings-as-errors` and `mix format --check-formatted` | Both pass. The direct check now uses Mix's lock reader and verifies every locked library, avoiding warnings from treating generated lock syntax as ordinary source. |
| `python3 <scratch>/check_consumer.py` | A separate Mix application depends only on the extracted SDK. Normal dependency restoration, strict compilation, and all 121 tests pass. It checks all thirty package files and all thirty-eight loaded SDK module origins before and after execution. |
| HTTP README example in that consumer | Starts an authenticated server, serves health and session requests, and joins shutdown after Enter. Only its port changes to zero. |
| `python3 <scratch>/audit.py` | Whitespace, Python syntax, runtime-file count, and dependency pins pass across twenty changed files. |
| Repository-root `npm run check` | The same three unrelated nested Biome roots prevent repository checks from starting. |

The first consumer run passed 120 of 121 tests. The existing RPC
`abort_terminal` fixture cancelled after provider cleanup, which can precede the
core's completion check. It now waits for actual terminal bytes before sending
abort, preserving its intended stalled-delivery assertion. The focused check
and full source and consumer reruns pass. No RPC production behavior changed.
The HTTP child-token regression also failed before the transport began monitoring
the current callback token; its focused and full reruns now pass.

HTTP cases cover IPv4/IPv6, remote opt-in, bearer authentication, absent CORS,
native malformed and chunked framing, fragmented UTF-8, exact body limits,
ordered events, history rollback, independent sessions, busy handling,
oversized output, stalled delta/final writes, silent disconnects, pending
cancellation after deletion, held cleanup, callback shutdown, and owner exit.
A client closing immediately after final chunk framing still commits completed
history. The package contains twenty-three runtime Elixir files and seven native
source/build/metadata/documentation files. Linux, minimum-version execution,
refreshed non-HTTP examples, validated tools, shared CLI integration, and final
acceptance remain open. The full eleven-language goal is active and changes
remain uncommitted.

## 2026-09-09 Zig SDK core

Zig now exposes a native text core through the `tinyagent` module. Four runtime
files separate public exports, provider/history/event types, history copying,
and run lifecycle. The SDK has no external dependencies. Agent configuration is
borrowed; each completed result owns its strings and nested JSON in a run arena.
Callbacks finish before results return. Native Zig task cancellation joins
provider cleanup, and failed terminal delivery cannot return completed history.

The SDK targets the official stable Zig 0.16.0 compiler. The macOS arm64 archive
was downloaded from [Zig's release site](https://ziglang.org/download/0.16.0/zig-aarch64-macos-0.16.0.tar.xz)
and matched published SHA-256
`b23d70deaa879b5c2d486ed3316f7eaa53e84acf6fc9cc747de152450d401489`.
Its standard-library source supplied the build, allocator, JSON, and cancellation
interfaces. The original Zig 0.13 coding CLI is unchanged and not part of these
SDK checks. No declined validator dependency was downloaded.

Verification scratch is `../../.scratch/tmp/ta-zig-sdk.KYXMx0`. Add its
`zig-aarch64-macos-0.16.0` directory to PATH and set `ZIG_GLOBAL_CACHE_DIR` to
its `cache` directory to reproduce the source command below.

| Check | Result |
| --- | --- |
| `python3 conformance/run.py zig` from this directory | Formatting, all 12 native core groups, and the offline example pass. The runner correctly exits nonzero for missing providers, tools, sessions, RPC, HTTP, and CLI integration. |
| `python3 <scratch>/check_consumer.py` | A separate Zig package depends on the extracted SDK through its documented path dependency. All 12 tests and both README examples pass. Compiler arguments identify the extracted module, and all ten package files match source before and after execution. |
| Repository-root `npm run check` | The same three unrelated nested Biome roots prevent repository checks from starting. |

The verified consumer is `consumer-f80b9bc54612`; the source archive SHA-256 is
`f80b9bc546123b48e0e6b1156feefbb1af48ac260830972741e748839752acab`.
Checks cover UTF-8 events, final-answer selection, copied replay metadata and
large JSON numbers, provider/consumer failures, allocation cleanup, held
provider cleanup, cancelled terminal delivery, and concurrent independent runs.
The initial held-cleanup fixture cancelled before provider entry and waited for
cleanup that never began. It now waits for provider entry before cancellation;
the full source and consumer suites pass. Runtime deadlines were not changed.

OpenAI and Anthropic adapters, validated executable tools, session ownership,
RPC, HTTP, shared CLI integration, and final Zig acceptance remain open.
Verification is macOS arm64 only. The full eleven-language goal remains active,
and all changes remain uncommitted.

## 2026-09-09 Zig streaming providers

`OpenAIProvider` and `AnthropicProvider` now use Zig's native HTTP client.
Eight runtime files separate the core, history, public types, provider request
and response formats, bounded JSON helpers, and HTTP/SSE lifetime. No external
dependency or second agent loop was added. Each request runs alongside its
deadline; cancellation joins both tasks before temporary memory is released.
Returned history and metadata belong to the caller's arena.

Native socket checks cover shared text and tool fixtures, fragmented UTF-8 and
chunked HTTP, complete arguments, exact large numbers, replay, malformed and
incomplete responses, byte limits, and callback failure. Deadlines remain
effective during a stalled callback even with completion already buffered.
Terminal events close the connection without waiting for the HTTP terminator.
Requests reject redirects, compressed responses, and wrong media types.

Wire assertions exposed Zig 0.16.0 storing but not emitting `privileged_headers`.
Credentials now use ordinary request headers with redirects disabled. The
separate consumer verifies the actual authorization headers received by its
server. OpenAI also explicitly requests encrypted reasoning with `store: false`,
as required for stateless replay in the official
[Responses reference](https://developers.openai.com/api/reference/cli/resources/responses/methods/create).
The request regression first failed with `MissingReasoningRequest`, then passed
after adding the opt-in. Shared response fixtures alone did not catch its absence.

Verification scratch remains `../../.scratch/tmp/ta-zig-sdk.KYXMx0`, using the
same verified Zig 0.16.0 compiler and cache as the core section. The final
consumer is `consumer-27242e0bc503`; its source archive SHA-256 is
`27242e0bc5034ba8954998d66d81cbd533dd1eed93931aceccc20ba33465d697`.

| Check | Result |
| --- | --- |
| `python3 conformance/run.py zig` with the compiler and cache configured above | Formatting, twelve core groups, seventeen provider groups, and the offline example pass. The runner checks bundled SSE fixture parity and still exits nonzero for missing tools, sessions, RPC, HTTP, and CLI integration. |
| `python3 <scratch>/check_consumer.py` | A separate Zig package depends only on the extracted SDK. All 29 groups pass; compiler arguments identify the extracted module. All nineteen packaged files match source before and after execution. |
| Four README examples in that consumer | Offline and cancellation examples run; both provider snippets compile and run against native local HTTP fixtures with synthetic credentials. Only their endpoint changes. |
| `python3 <scratch>/audit.py` | Whitespace, Python syntax, shared fixture parity, and runtime-file count pass across twenty-four files. |
| Repository-root `npm run check` | The same three unrelated nested Biome roots prevent repository checks from starting. |

Zig verification covers macOS arm64 and plain local HTTP. TLS code compiles but
has not been exercised against a TLS server. Validated tools, sessions, RPC,
HTTP, shared CLI integration, and final acceptance remain open. The original
Zig 0.13 coding CLI is unchanged. The later serializer audit below completes
the encrypted-reasoning request change in the other ten SDKs.
The eleven-language goal remains active, and changes are uncommitted.

## 2026-09-09 encrypted reasoning and Elixir final delivery

All eleven OpenAI SDKs now send `include: ["reasoning.encrypted_content"]`
with `store: false`. The ten serializers audited after Zig had omitted the
opt-in. Python and TypeScript apply it to both streaming and injected
complete-response transports. Existing tests now inspect the initial and replay
request bodies; each of those two SDKs also has a complete-transport regression.
The Python and TypeScript request assertions failed before the fix. Preserving
metadata from synthetic response fixtures alone did not verify its request.
No agent loop or public API changed.

The broad run exposed an Elixir HTTP race: a client could receive the final
chunk, close the connection, and lose completed history because the disconnect
watcher cancelled before the writer acknowledged its successful native write.
The watcher now distinguishes final writes. EOF closes the socket to release
an unfinished write and waits for its owner to report success or cancel on
failure. Successful final acknowledgement retires the watcher. Explicit
cancellation and failed native writes retain their rollback behavior.

A new test deliberately holds a successful final write's return until native
EOF has been observed. It fails against the retained development build with
the former `writing/3` implementation: completed bytes arrive but history is
empty. It passes against the corrected source. The fixture initially assumed
a closed socket's `sockname` error would be `:closed`; the installed TCP driver
returns `:einval`. It now checks for an error, which is the relevant closed-socket
condition. No test or production deadline was extended.

Verification uses the previously installed toolchains and dependency caches.
Scratch is `../../.scratch/tmp/ta-reasoning-replay.tOVvJt`. No dependency was
downloaded and no paid provider request was made.

| Check | Result |
| --- | --- |
| Isolated Python environment, `conformance/run.py python typescript go rust java ruby csharp php elixir swift`, with cached Go 1.26.4, offline Cargo/Hex, and isolated Maven/WEBrick paths | Python 104 tests, TypeScript 88 tests and strict type checks, Go race suite/vet, Rust 60 tests/Clippy/example, Java 66 SDK + 11 CLI groups, C# 87 groups, PHP 86 groups, and Swift 81 groups pass. Their CLI self-tests and configured package steps pass. Ruby and Elixir's initial failures are detailed below. |
| Corrected Ruby gem path, `python3 conformance/run.py ruby` | 81 tests / 963 assertions and CLI self-test pass. The first run hid the already installed minitest gem; the corrected path includes both the isolated WEBrick gem directory and Ruby 3.1.0's existing gem directory. No installation was needed. |
| From `tinyagents/elixir`, `HEX_OFFLINE=1 mix test --warnings-as-errors test/rpc_io_test.exs test/rpc_test.exs` | All 31 native subprocess checks pass in isolation with unchanged deadlines. |
| Same directory, `HEX_OFFLINE=1 mix test --warnings-as-errors test/http_connection_test.exs test/http_test.exs test/http_lifecycle_test.exs` | All 13 HTTP groups pass, including the forced final-write/EOF race, normal client close after terminal framing, stalled final writes, child-token cancellation, and held cleanup. |
| `HEX_OFFLINE=1 python3 conformance/run.py elixir` after the HTTP fix | 106/122 pass. Sixteen native Bash, CLI, and RPC checks time out, mostly before their fixture reports readiness. Formatting, strict compilation, Hex packaging, and self-tests pass. The earlier full run passed 107/121, with thirteen subprocess timeouts and the now-fixed HTTP race. |
| `ERL_FLAGS='+S 2:2 +SDcpu 1 +SDio 1' HEX_OFFLINE=1 python3 conformance/run.py elixir` | 118/122 pass. RPC `interrupt`, `abort_terminal`, and `errors`, plus native I/O `invalid`, still time out. The VM configuration was verified as two normal schedulers, one dirty CPU scheduler, and one dirty I/O scheduler. Cases and deadlines are unchanged; the cap is diagnostic and is not made a project default. |
| From `tinyagents/elixir`, `HEX_OFFLINE=1 mix compile --warnings-as-errors`, then `mix hex.build --output dist/tiny_agent_sdk-0.0.1.tar` | Normal Mix compilation and the final source package pass. Archive SHA-256 is `ad759b92c1d30467ad62284dbc4284c5e02d211199e6017460a18a4d90628b1c`. Independent consumer execution of this archive has not been refreshed. |
| `python3 <scratch>/audit.py` | Whitespace and Python syntax pass across all 27 changed source, test, and documentation files. All thirty files in the Elixir archive match current source byte for byte. |
| Repository-root `npm run check` | The same three unrelated nested Biome roots prevent repository checks from starting. Full output was inspected. |

During the second Elixir run the host load averages were 60.61, 33.89, and 22.74.
That is evidence of competing load, not proof that every timeout is environmental.
The scheduler cap did not remove all timeouts. The focused RPC pass does not
establish full-suite stability. Current Elixir
acceptance therefore remains open. The six SDKs awaiting validated executable
tools and shared CLI loops, Zig's remaining runtime components, refreshed package
acceptance, and the final contract audit also remain open. Changes are uncommitted.

## 2026-09-09 Zig sessions and cancellation signals

`Session` now owns committed history around the existing agent core. It permits
one run, reserves the session during prompt acknowledgement, and commits only
after successful terminal delivery and cleanup. Returned results and committed
history have independent ownership. A failed provider, acknowledgement, final
callback, or history allocation retains prior history. Idle sessions need no
background task, and callbacks execute outside the session mutex.

`cancel`, `reset`, and `stop` request cancellation without joining, so callbacks
can use them. Reset hides history immediately and retains the old storage until
the active worker stops borrowing it. Admission remains reserved through cleanup.
External `waitIdle` joins cleanup; an expected run ID avoids waiting on a later
run. The external owner uses `deinit` to stop admission, cancel, join, and free
the session. Its provider, configuration, allocator, and stable storage remain
caller-owned. Separate sessions may share a thread-safe provider.

`Agent.run` also accepts a caller-owned `std.Io.Event` cancellation signal.
Agents and sessions use one internal driver that owns a worker and cancellation
waiter, joins both, and frees an undelivered result. Cancelling a native future
does not set the caller's event. The core checks that event between callbacks
as well as at native I/O cancellation points. A callback-stop regression first
returned `Canceled` with a misleading completed terminal event; it now emits
aborted before another provider step. Cancellation during an already emitted
terminal event still cannot emit a second terminal event.

The SDK has nine runtime files and no external dependencies. The original Zig
0.13 coding CLI is unchanged. Checks use the previously verified Zig 0.16.0
compiler and cache in `../../.scratch/tmp/ta-zig-sdk.KYXMx0`. No dependency was
downloaded and no paid provider request was made.

| Check | Result |
| --- | --- |
| With that compiler on PATH and its cache selected, `python3 conformance/run.py zig` | Formatting, all 47 native groups, and the offline example pass: seventeen core, seventeen provider, and thirteen session groups. The runner now reports only tools, RPC, HTTP, and shared CLI integration as unimplemented. |
| `python3 <scratch>/check_consumer.py` | An independent Zig package depends only on the extracted SDK. All 47 groups pass. Compiler arguments identify the extracted module; all twenty-one packaged files match current source before and after execution. |
| Six README examples in that consumer | Offline, native-future cancellation, explicit-signal cancellation, and session examples run. Both provider examples compile and run against native local HTTP fixtures with synthetic credentials; only their endpoint changes. |
| Native session regressions | History survives returned-result mutation and release. Checks cover acknowledgement order, busy rejection, failures, held provider cleanup, cancelled final delivery, reset, parent cancellation, old-run waits, callback stop, concurrent independent sessions, external shutdown, and every history-allocation failure. |
| Native cancellation-signal regressions | Explicit signals interrupt blocked work and join cleanup; pre-cancelled runs avoid provider entry; callback cancellation stops the next core step; consumer errors retain precedence; future cancellation preserves the caller's event. Both normal and signalled runs pass allocation-failure checks. |
| `python3 <scratch>/audit.py` | Whitespace, Python syntax, bundled SSE fixture parity, and runtime-file count pass across twenty-six project files. |
| Repository-root `npm run check` | The same three unrelated nested Biome roots prevent repository checks from starting. Full output was inspected. |

The verified consumer is `consumer-9b1a388d1c19`; the archive SHA-256 is
`9b1a388d1c1949483da15bc7989c83c6a6d78ca9862760236770a604175ed0b6`.

Zig verification is on macOS arm64. Validated executable tools, RPC, HTTP,
shared CLI integration, and final Zig acceptance remain open. The other ten
SDKs' outstanding requirements are unchanged. The full finish contract remains
active, and all changes are uncommitted.

## 2026-09-09 Zig native RPC

`runRpc` now serves the shared JSONL command protocol around the existing
`Session` and agent core. `rpc.zig` owns commands and input; `rpc_output.zig`
owns one serialized output worker and delivery receipts. Session reservation
happens before reading the next command. Acknowledgements finish before agent
events, and successful final delivery precedes history commit. There is no
additional model or tool loop.

Command replies enqueue while native output is blocked; events await delivery.
The 4 MiB queue includes each complete pending frame and reserves its overflow
diagnostic. JSON serialization measures the object before allocating its frame.
The 1 MiB line limit excludes LF. Oversized objects become bounded protocol
errors and cancel the active run. Queue overflow, broken output, EOF, and
caller cancellation all join cooperative cleanup. Cancelled pending writes
have one second to drain. Input uses native cancellable reads and retains
partial UTF-8 lines across reads.

The application owns the supplied blocking files and process signals. RPC
accepts a cancellation event and also responds to native task cancellation.
The subprocess fixture routes SIGINT through that event. The SDK does not
install a signal handler or change file modes. The first descriptor check
incorrectly treated Darwin's kernel-managed `FWASWRITTEN` bit as a caller mode.
The check now excludes only that observation and separately checks descriptor
flags. Apple's [flag definition](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/fcntl.h)
and [write implementation](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/sys_generic.c)
explain the observed `1` to `65537` transition after a successful write.

Pipe tests observe native cancellation before resuming stalled output, so the
abort cannot race behind a successful terminal write. The same observation
keeps output stalled until queue overflow has stopped command processing.
The observation wraps native waiting in the fixture and preserves its result;
production code contains no test hooks.

The SDK has eleven runtime files and no external dependencies. Its Python 3
subprocess harness tests a compiled Zig executable. The original Zig 0.13
coding CLI is unchanged. Verification uses the previously verified Zig 0.16.0
compiler and cache in `../../.scratch/tmp/ta-zig-sdk.KYXMx0`.

| Check | Result |
| --- | --- |
| `python3 conformance/run.py zig` with that compiler on PATH and its cache selected | Formatting, all 52 native tests, all 17 RPC subprocess groups, and the offline example pass. The runner verifies bundled RPC/SSE fixture parity and still exits nonzero for tools, HTTP, and shared CLI integration. |
| `python3 <scratch>/check_consumer.py` | An independent Zig package depends on the extracted SDK. All 52 native tests and 17 RPC subprocess groups pass. Compiler arguments identify extracted modules; all twenty-eight package files match source before and after execution. |
| README and complete examples in that consumer | Seven README examples pass. Both provider examples use native local HTTP fixtures with synthetic credentials. The RPC call-site example and complete `examples/rpc.zig` run over native stdin/stdout. |
| Five native output groups | Exact line limits, UTF-8/escaping, queue accounting, overflow, and every allocation failure for ordinary, oversized, and full-queue output pass. |
| Seventeen RPC subprocess groups | Shared responses, command correlation, ordered events, history commit/rollback, reset, busy rejection, held cleanup, malformed input, byte limits, partial-write cancellation, overflow, EOF, application-routed SIGINT, broken pipes, and borrowed descriptor ownership pass. |
| `python3 <scratch>/audit.py` | Whitespace, Python syntax, fixture parity, and runtime-file count pass across thirty-three project files. |
| Repository-root `npm run check` | The same three existing nested Biome configurations stop the check. Full output was inspected. |

The verified consumer is `consumer-657c590298e8`; the archive SHA-256 is
`657c590298e8c346f1a42ae91eb4a38d8884101f4a89fec69f6a76059abe6c32`.

Verification remains on macOS arm64. Zig's validated executable tools, HTTP,
shared coding CLI, other-platform execution, and final acceptance remain open.
The other ten SDKs' outstanding requirements are unchanged. The full finish
contract remains active. No commit, publication, dependency download, or paid
provider request was made in this block.

## 2026-09-09 Zig native HTTP

`HttpServer.start` now owns a native listener, in-memory sessions, and connection
tasks around the same `Session` and agent core used by RPC. Five modules separate
server lifetime, session lookup, routing, request validation, and streamed
delivery. `jsonl.zig` and `delivery.zig` share bounded encoding and cancellation
draining between RPC and HTTP. The SDK has eighteen runtime files and no external
dependencies. The original Zig 0.13 coding CLI remains separate.

All five routes support IPv4/IPv6, optional bearer authentication, explicit
authenticated remote exposure, and absent CORS. The native HTTP parser handles
fixed-length and chunked requests. Boundary checks reject ambiguous framing and
malformed headers. Header storage is bounded to 16 KiB; decoded bodies and output
lines are bounded to 1 MiB. An exact-size body must leave room for the reader to
observe EOF. The initial boundary test caught and corrected that off-by-one.

Each connection serves one request. Streamed events own one pending native write
and no asynchronous queue. Final JSONL delivery includes connection-close
framing before history commits. Cancelled writes have one second to drain.
A separate reader observes silent disconnects and cancels only its owned run.
EOF during final delivery shuts down the socket and lets the write result
decide whether delivery succeeded. A fixture pauses after native final framing
until client EOF reaches that reader, then verifies the completed history.
Production code contains no test hooks.

Deletion removes a session from lookup before joining cooperative cleanup.
Requests retain references until they finish, and shutdown joins the deleting
request as well as active prompts. A delayed cancellation body rechecks the
session after reading; a regression caught the initial stale 200 response after
deletion. The test now observes admission before deleting, and its HTTP client
honors Content-Length rather than requiring EOF after an ordinary response.
Callback `stop` does not wait on itself. External `wait`/`deinit` joins work and
closes the listener, including idle connections and pending request bodies.

Verification uses the same Zig 0.16.0 compiler and cache in
`../../.scratch/tmp/ta-zig-sdk.KYXMx0`. All calls use offline providers or local
fixtures with synthetic credentials.

| Check | Result |
| --- | --- |
| `python3 conformance/run.py zig` with the compiler and cache configured above | Final isolated run passes formatting, all 52 native tests, all 17 RPC groups, all 13 HTTP groups, fixture parity, and the offline example. The runner exits nonzero only for executable tools and CLI integration. |
| `python3 <scratch>/check_consumer.py --resume` | A separate Zig package depends only on the extracted SDK. All 52 native tests, 17 RPC groups, and 13 HTTP groups pass. Compiler arguments identify extracted modules; all thirty-eight package files match source before and after execution. |
| README and complete examples in that consumer | All eight README examples pass. The two provider snippets use local HTTP fixtures with synthetic credentials; only their endpoints change. The HTTP snippet changes only its port to zero. Complete RPC/HTTP examples also run, including native requests and joined shutdown. |
| Thirteen HTTP socket groups | Routes, ordered Unicode JSONL, authentication, exposure, fixed/chunked bodies, exact byte limits, malformed framing, history, independent sessions, busy cancellation, silent disconnect, held cleanup, deletion, callback stop, stalled writes, and final-delivery/EOF ownership pass. |
| Eight isolated RPC shutdown probes | Broken stdout exits in 0.334 to 0.406 seconds; stalled stdout exits in 1.263 to 1.370 seconds. Diagnostics remain under 1 KiB. |
| `python3 <scratch>/audit.py` | Whitespace, Python syntax, fixture parity, and runtime-file count pass across forty-three project files. |
| Repository-root `npm run check` | The same three existing nested Biome configurations stop the check. Full output was inspected. |

The verified consumer is `consumer-cb282eb4564e`; its archive SHA-256 is
`cb282eb4564e73284c3d3438293b27938233e8ebfd8ad81fafe555459853daea`.
The first build exceeded the verification wrapper's three-minute compilation
limit. The unchanged archive was checked again and resumed in the same consumer
with a ten-minute build limit. This did not change SDK or test deadlines.

Some earlier full runs exceeded three-second RPC subprocess deadlines while
other builds or repository checks were running, including one serialized Zig
run. The final isolated source command passes. The runner now uses `-j1` to
limit competing executables within its own build; tests retain their internal
concurrent tasks, and no shutdown deadline was relaxed. Host contention is a
plausible cause of those timeouts, not a proven diagnosis.

Zig's validated executable tools, shared coding CLI, other-platform execution,
and final acceptance remain open. The other SDKs' outstanding requirements are
unchanged. The finish contract remains active. No commit, publication,
dependency download, or paid provider request was made in this block.

## 2026-09-09 Zig executable tools

`Tool` owns its checked schema and definition, validates arguments before entering
the handler, and gives handlers private argument storage. The existing Agent
loop now makes sequential tool calls and sends their observations to the next
provider request. Ordinary handler, argument, and output errors become bounded
observations. Cancellation emits the matching tool-end event and joins cleanup.
Unknown tool names retain the existing `UnregisteredTool` failure behavior.

Schema validation uses three C ABI functions in a pinned native Rust static
library: jsonschema 0.30.0 and serde_json 1.0.151, with locked dependencies.
External schema retrieval is disabled. Zig still owns the agent, providers,
handlers, sessions, and transports. The SDK has nineteen Zig runtime files at
this checkpoint. Cargo is a build dependency; the executable has no Rust dynamic
runtime dependency. The [schema decision](../../zig-schema-validation.md) records
candidate evidence, ownership, and validation limits.

| Check | Result |
| --- | --- |
| Offline Cargo, verified Zig 0.16.0 and its cache, `python3 conformance/run.py zig` | Cargo formatting, warnings-denied Clippy, Zig formatting, 62 native tests, 17 RPC groups, 13 HTTP groups, and offline examples pass. Runner correctly exits nonzero for guarded Bash and shared CLI integration. |
| Shared schema fixture, six cases | Zig passes all cases. Focused Python tool tests (4), TypeScript tool tests (8), Rust shared-argument test, and Go shared-argument race test pass. New cases exercise unevaluated properties, recursive dynamic references, and large integers. These are focused checks, not fresh full-suite results for the other four SDKs. |
| `python3 <scratch>/check_consumer.py` | All 46 package files match source and extraction before and after execution. The independent consumer passes 62 native tests, 17 RPC groups, 13 HTTP groups, eight README examples, and complete tool/RPC/HTTP examples. Native provider examples use local fixtures and synthetic credentials. |
| `otool -L` on the native tools example | Only `/usr/lib/libSystem.B.dylib` is dynamically linked. |
| Repository-root `npm run check` | The same three existing nested Biome roots prevent source checks from starting. Full output was inspected. |

Scratch is `../../.scratch/tmp/ta-zig-sdk.KYXMx0`. The verified tools consumer is
`consumer-86c41b43e543`, archive SHA-256
`86c41b43e543d48249d2657b9cef2d40f36cd37bc58e3ba7d3d5ed36df770e3a`.
This package precedes the guarded Bash work. Verification is on macOS arm64;
other platform builds were not executed. No declined validator download for
the other six SDKs was retried. The finish contract remains active. Changes
are uncommitted, and no paid provider request or publication was made.

## 2026-09-09 Zig guarded Bash and shared CLI

`bashTool` is an ordinary validated SDK tool. Native stdout/stderr capture uses
one combined 128 KiB limit, including the final observation framing. The original
blacklist is preserved. Cancellation and overflow terminate the POSIX process
group; Windows targets terminate the direct child. The runner retains ownership
through pipe cleanup, then joins an uncancelable native process wait. This avoids
Zig 0.16 clearing a child handle before a cancelled wait has reaped the process.

The original `tiny_agent.zig` now owns signals and dispatches to the SDK CLI.
`cli.zig` owns configuration and mode selection; `console.zig` owns input and
display around the existing Session. There is no second model/tool loop. Text
deltas print once, Bash diagnostics use stderr, and final text delivery precedes
history commit. Every output site uses bounded cancellation draining. RPC and
HTTP modes call their existing adapters. Self-test needs no credentials.

Source verification passes Cargo formatting/Clippy, Zig formatting, 68 native
tests, 9 CLI subprocess groups, 17 RPC groups, 13 HTTP groups, self-test, and both
offline examples. `conformance/run.py zig` now exits zero. Four Bash groups
include exact framing limits, invalid UTF-8, closed stdin, and cancellation with
open pipes, closed pipes, and a shell that exited before its descendant. They
verify descendant termination and shell reaping. Console checks cover streaming,
ordinary provider failure rollback, native Bash, borrowed files, exact input
limits, unterminated input, native interruption, broken pipes, and stalled
stdout/stderr. Actual CLI checks cover both provider configurations, RPC, and
authenticated HTTP startup/shutdown, with no model calls.

The first CLI harness run imported the adjacent `test/http.py` instead of Python's
standard `http` package. Running that harness with Python's `-I` fixes import
resolution; no SDK behavior or deadline changed. The SDK has twenty-two runtime
Zig files. Execution remains verified on macOS arm64; Linux/Windows execution
has not been checked.

The independent consumer `consumer-c043e4206dd6` passes all 68 native tests,
9 CLI, 17 RPC, and 13 HTTP subprocess groups. It also runs all eight README
examples and the complete tool/RPC/HTTP examples. All 54 package files match
source before and after execution; module paths and the native Cargo manifest
resolve only inside the extracted package. Archive SHA-256 is
`c043e4206dd6a895c358b9506819fdb6c320838f938c08a6209cb6c3588cfdea`.
The same scratch directory and `check_consumer.py` reproduce the check.

The documented `zig build --build-file tinyagents/zig/build.zig -j1` succeeds
from this directory, and the resulting `tinyagents/zig/zig-out/bin/tinyagent
--self-test` passes. `audit.py` checks whitespace, Python syntax, fixture parity,
and runtime-file count across 59 project files. Root `npm run check` still stops
at the same three unrelated nested Biome roots; full output was inspected.
Changes are uncommitted. No paid request, publication, or declined dependency
download was performed.

## 2026-09-09 Python, TypeScript, Go, and Rust package acceptance

These checks use the current encrypted-reasoning requests and expanded six-case
schema fixture. Every consumer resolves the SDK from its independent installation
or extracted archive. Provider calls use synthetic local fixtures. No model
credentials or paid requests are involved.

| Package | Independent check |
| --- | --- |
| Python wheel | All ten runtime modules match source, wheel, and installation before and after execution. A fresh Python 3.14.2 environment installs the wheel and its declared jsonschema/referencing dependencies from cache. All 104 copied tests pass, including native provider/HTTP sockets, RPC subprocesses, and Bash cleanup. CLI self-test passes. Three README snippets compile; provider construction and the HTTP example run, with only HTTP's port changed to zero. The model-calling snippet is syntax-checked, not sent to a provider. |
| TypeScript tarball | All seventeen files match the freshly compiled JavaScript/declarations and installed tarball. All 91 copied tests pass, including provider/HTTP TCP and native RPC/Bash subprocesses. Test imports and CLI paths point to the installed JavaScript; fixture and scratch paths point to independent copies. Strict checks cover tests and all three README snippets against the installed declarations. Self-test and provider construction pass; an equivalent port-zero HTTP startup/close smoke passes. The model-calling snippet is type-checked, not sent to a provider. |
| Rust crate | `cargo package --offline --allow-dirty` verifies the 45-file crate. Forty-three original files, including the original manifest, match source; Cargo generates the normalized manifest and VCS metadata. The extracted crate passes all 60 tests, including five TCP groups, Clippy with warnings denied, the offline example, and CLI self-test. Cargo metadata locates every target in the extraction, with no path dependencies. Build artifacts reuse the existing target cache. Cargo still reports unassigned publication/license metadata. |
| Go module archive | A standalone copy first reproduces the missing shared fixture. Go now bundles six JSON/SSE files and the protocol, and the shared runner checks Go and Rust copies for freshness. All 63 archive files match source and extraction before and after execution. Source and standalone full race suites, vet, and both CLI self-tests pass. The echo example compiles. A separate module, with workspace resolution disabled, imports only the extracted SDK and completes a native tool loop under the race detector. |

Scratch directories are under `../../.scratch/tmp/`:

| Package | Directory | Archive SHA-256 |
| --- | --- | --- |
| Python | `ta-python-package.s9XCed` | `f84bb000099d92868a1f05c0a37c5c7472b49eecb949b698ebf495ee70ef39d9` |
| TypeScript | `ta-typescript-package.fa5qhu` | `f4bd8cac0cb3cad88f3b225b2175b04f5c3e5e2337fbaa513e594d7d6e4af5d2` |
| Go | `ta-go-package.PO1Leq` | `00044bcd4738b833cdf0d460f9174ae84f97dbb8a43663b1d857bfda101dd90d` |
| Rust | `ta-rust-package.fnHKaj` | `30618ba35d6997139a8d6f66bf5914464b74a6d158847fcd80839034a78ece46` |

Each directory contains `check_consumer.py` and execution logs. Python's first
offline wheel build lacked setuptools; the declared build dependency was then
resolved in a repository-local cache and the wheel built successfully. TypeScript's
first offline install lacked fast-uri; a normal install of the declared
dependencies, with lifecycle scripts disabled, succeeded. Go's optional consumer
`go mod tidy` attempted to resolve upstream test-only dependencies missing from
cache; `go run -mod=mod -race .` resolves the application's required imports and
passes offline. The completed module suites were not repeated for that change.

No SDK runtime behavior changed in this block. Go test paths, bundled assets,
package documentation, and the runner's shared freshness gate changed.
Go formatting passes. Repository-root `npm run check` still stops before source
checks at the same three unrelated nested Biome configurations; full output
was inspected. Changes remain uncommitted. The six unfinished tool/CLI SDKs,
remaining package checks, and final contract audit remain open.

## 2026-09-09 Elixir acceptance refresh

With host load averages at 2.68, 3.55, and 4.22 before execution, the full source
command `ERL_FLAGS='+S 2:2 +SDcpu 1 +SDio 1' HEX_OFFLINE=1 python3
conformance/run.py elixir` passes all 122 tests in 23.7 seconds, formatting,
strict compilation, Hex packaging, and CLI self-tests. The runner exits nonzero
because validated executable tools and shared CLI integration are still missing.
The scheduler settings match the earlier 118/122 run. No SDK code, test case,
deadline, or default scheduler configuration changed. This establishes a current
passing run under lower host load, not a proven cause or fix for earlier timeouts.

The independent Mix consumer also passes all 122 tests in 24.7 seconds and runs
the HTTP README example with only its port changed to zero. Tests, CLI source,
and shared fixtures are copied into the consumer; all 38 SDK module origins are
checked before and after the suite. The thirty packaged files match current
source before and after execution. Dependencies come from the existing offline
Hex cache. Archive SHA-256 remains
`ad759b92c1d30467ad62284dbc4284c5e02d211199e6017460a18a4d90628b1c`.
Scratch is `../../.scratch/tmp/ta-elixir-acceptance.onOr5X`, with source and
consumer logs and `check_consumer.py`. No paid request, new dependency, or commit
was made. The unfinished tools/CLI requirements and final audit remain open.

## 2026-09-09 C# NuGet acceptance and TCP fixture corrections

The independent NuGet consumer passes all 87 native groups, including both
provider TCP groups and Kestrel's real socket check. The original CLI self-test
passes against the installed packages. All six README examples compile with
warnings treated as errors; both RPC examples, Bash, and HTTP run. HTTP changes
only port 8080 to zero and checks health/session creation before joined shutdown.
The two model-calling examples are compiled without sending paid requests.

Restore uses an empty external feed and the two local NuGet archives, with a
fresh package directory. Every project references packages, with no SDK project
references. A module initializer verifies the loaded SDK/HTTP assembly paths
and byte hashes in the main process, native child processes, and examples.
The packaged libraries match the current source build; README/license bytes
also match. No SDK runtime or package API changed in this block.

Two fixture assumptions were corrected after independent execution exposed them:

- The Kestrel TCP test submitted its next prompt when the provider entered
  `finally`, before session cleanup necessarily finished. An isolated probe
  holding that cleanup for 50 ms made the old test fail in all ten runs. The
  corrected test uses an explicit release signal, requires `session_busy` while
  cleanup is held, then waits within the existing four-second deadline for the
  next accepted prompt and checks retained history. The same bounded helper
  replaces duplicated polling after final-write failure. All ten corrected
  isolated runs pass.
- The provider fixture required a clean EOF when disposing an unfinished HTTP
  response can instead reset the peer. A reset-on-close client reproduced the
  old failure in all ten isolated runs after the provider's own result or
  cancellation assertion passed. The fixture now accepts only EOF or the specific
  peer-reset socket error after request validation; other I/O failures still fail.
  Both providers exercise completion and cancellation with default clients and
  reset-on-close clients. All ten corrected isolated runs pass.

Scratch is `../../.scratch/tmp/ta-csharp-package.aiBGIZ`; `check_consumer.py`,
`consumer-verified.log`, and the reproduction logs contain the evidence.
The SDK archive SHA-256 is
`3d3ad89913136d0615e72b066f4708316cbfd3ebf9caee84e9f97f547869d4b4`;
the HTTP archive is
`8489675a0f08e292e99b6965bcbede31de0803f6dd9c52b4157c3170b1175235`.
Verification uses .NET SDK 8.0.303 on macOS arm64. The final `python3 check.py`
passes all 87 source groups, both package builds, and CLI self-test with zero
compiler warnings or errors. Repository-root `npm run check` again stops at the
same three unrelated nested Biome roots; full output was inspected.
No new dependency, paid request,
publication, or commit was made. Validated tools, shared CLI integration, and
final acceptance remain open.

## 2026-09-09 Ruby package acceptance refresh

The current gem passes all 81 tests and 963 assertions after isolated local
installation with WEBrick 1.9.2. Tests, CLI, and shared fixtures are copied away
from source. The package guard checks all sixteen package files against source
and all fourteen runtime module origins, including in native subprocesses.
The CLI self-test and all five README syntax checks pass. The HTTP example
serves authenticated health/session requests and joins shutdown on Ctrl-C;
only its port changes to zero. No paid provider request is made.

Scratch is `../../.scratch/tmp/ta-ruby-package.nlGFTP`; `consumer.log` and
`check_consumer.py` record the checks. Gem SHA-256 is
`fd28c56adfa6d7d0ef0199251258e477f875c43e281da0e6baa2baf088f2c895`.
Gem build retains the existing unassigned-homepage warning. Installation first
required setting `GEM_HOME` to the isolated directory so the SDK could find the
locally installed WEBrick dependency. No new dependency was downloaded. Ruby
runtime source is unchanged. Tools, shared CLI integration, Linux execution,
and final acceptance remain open.
