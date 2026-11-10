> Historical contract, superseded on 2026-09-11 by the [Python/TypeScript first-release contract](../../../FINISH_CONTRACT.md).

# TinyAgent finish contract

Status: implementation resumed; validator downloads approved 2026-09-09.

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

## Current status

Updated 2026-09-10. The requirement audit is **incomplete**: Swift still needs
validated executable tools in `Agent` and a coding CLI that uses that loop.
The approved native validator is being integrated. Its text SDK, providers,
sessions, RPC, HTTP, and coding CLI remain available.

Python, TypeScript, Go, Rust, Zig, C#, Ruby, PHP, Java, and Elixir have the complete native tool loop shared
with their CLIs and transports. Current source and independent-package checks
pass on macOS arm64. All eleven SDKs now have passing provider and HTTP TCP
checks from source and independent packages. This does not complete the
eleven-language contract.

| Language | Current source and package evidence | Remaining implementation |
| --- | --- | --- |
| Python | 105 tests, CLI self-test, and three checked examples; installed wheel passes | None identified by the current audit |
| TypeScript | 95 tests, strict source/declaration/test/example checks, CLI self-test; installed tarball passes | None identified by the current audit |
| Go | Full race suites, vet, both CLI self-tests, example and separate module import/tool loop pass | None identified by the current audit |
| Rust | 60 tests, Clippy, offline example, CLI self-test; independent crate passes | None identified by the current audit |
| Zig | 68 native tests, 9 CLI, 17 RPC, 13 HTTP groups and all examples; independent package passes | None identified by the current audit |
| Java | 77 SDK and 11 CLI groups, self-test, independent Maven consumer, five examples, and CLI RPC/HTTP modes pass | None identified by the current audit |
| Ruby | 93 source and installed-gem tests / 1170 assertions, self-test, six checked examples, and CLI RPC/HTTP modes pass | None identified by the current audit |
| Elixir | 136 source and independent-Mix tests, strict compilation, formatting, Hex build, self-test, eight examples and CLI RPC/HTTP modes pass with bounded schedulers | None identified by the current audit |
| C# | 95 source and independent-NuGet groups, self-test, seven examples and installed CLI RPC/HTTP modes pass | None identified by the current audit |
| PHP | 97 source and independent-Composer groups, self-test, seven checked examples and installed CLI RPC/HTTP modes pass | None identified by the current audit |
| Swift | 81 source and independent-SwiftPM groups, self-test and HTTP example pass; iOS simulator compile/link previously verified | Validated tools and shared CLI loop |

The [acceptance audit](../../acceptance-audit.md) maps all eight requirements to
evidence, records the final-event fixes found during review, and identifies
current packages and verification commands. Earlier
[acceptance checkpoints](../2026-09-09/acceptance-checkpoints.md) and
[implementation checkpoints](../2026-09-09/implementation-checkpoints.md)
remain available as history.

The repository check stops before source checks at three existing nested Biome
roots. Other OS and minimum-runtime execution is not established by these macOS
runs. Elixir's earlier subprocess timeouts remain recorded; its current bounded
scheduler pass does not prove their cause or eliminate them under competing load.
No commit, publication, push, or paid provider request was made.

## Remaining work

The user approved downloading the following candidates and their declared
dependencies on 2026-09-09:

| Language | Candidate |
| --- | --- |
| C# | JsonSchema.Net 9.4.0 |
| PHP | Opis JSON Schema 2.6.0 |
| Java | NetworkNT 3.0.7 and Jackson 3.2.1 |
| Ruby | JSONSchemer 2.5.0 |
| Swift | ajevans99/swift-json-schema 0.13.1 |
| Elixir | JSV 0.22.0 |

C# now uses JsonSchema.Net 8.0.5, the MIT-licensed release preceding the binary
maintenance-fee agreement. Ruby uses JSONSchemer 2.5.0, and PHP uses Opis JSON Schema 2.6.0.
Java uses NetworkNT 3.0.7 with Jackson 3.2.1.
Elixir uses JSV 0.22.0. All five locked graphs and shared schema cases pass.
Swift is the remaining validator integration. The earlier
download rejection is superseded by the user's explicit approval. Each SDK will
use its native dependency rather than another language's agent runtime.

Review and pin each dependency graph, validate construction
and arguments against the shared Draft 2020-12 cases, and add native handlers to
the existing `Agent`. Migrate the coding CLI to that loop and its existing RPC
and HTTP adapters. Work one language at a time, preserving readable native code.
Then refresh its source/package/example checks and this requirement audit.
Close the goal only when every acceptance requirement above is satisfied.
