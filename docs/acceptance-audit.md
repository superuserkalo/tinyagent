# TinyAgent acceptance audit

Status: historical baseline evidence, last verified 2026-09-10. The current
first-release evidence for Python and TypeScript is in the
[finish contract](../FINISH_CONTRACT.md).

The [first-release contract](../FINISH_CONTRACT.md) and [roadmap](../ROADMAP.md)
supersede the requirement results below. Python and TypeScript now require
durable sessions, resume, and compaction; other languages do not block release.
Retained test and package records describe the earlier in-memory scope.

The [finish contract](../FINISH_CONTRACT.md) remains open for Swift's native tool
implementation and shared coding CLI. The audit read all eleven Agent
core modules and cross-checked the shared runner, native check results, and
independent package origins. Passing text SDK tests does not substitute for
missing tool behavior.

## Requirement results

| Contract requirement | Result and evidence |
| --- | --- |
| 1. Native core and packages | Python, TypeScript, Go, Rust, Zig, C#, Ruby, PHP, Java, and Elixir expose executable tools and their native loop. All eleven packages import independently; package checks are recorded below. Swift has a text Agent that rejects every tool call and no native executable Tool registration. |
| 2. Providers | All eleven pass native OpenAI Responses and Anthropic Messages fixture/TCP checks, including request serialization, SSE framing, complete arguments, replay metadata, malformed responses, and cancellation. OpenAI requests include encrypted reasoning with `store: false`. No paid provider request was used. |
| 3. Tools | The ten tool-capable SDKs pass shared Draft 2020-12 cases, sequential execution, bounded recoverable errors, and cancellation/Bash cleanup. Swift has a guarded Bash executor and separate CLI observations but lacks validated Agent tools. Requirement remains incomplete. |
| 4. Lifecycle and history | Current suites cover run identity, ordered events, cancellation, terminal delivery, cleanup, independent sessions, and atomic history. Audit found and fixed Python/TypeScript terminal cancellation and duplicate terminal delivery described below. The Swift core still requires tool-event coverage after implementation. |
| 5. RPC | Native subprocess suites exercise the shared commands, IDs, response ordering, errors, UTF-8, bounds, busy state, reset, EOF, interrupts, and cleanup. The Swift coding CLI still needs to route through the shared Agent/RPC core with tools. |
| 6. HTTP | All eleven have passing real local socket checks for all five routes, loopback/authentication, explicit remote opt-in, absent CORS, limits, history, cancellation, disconnect, deletion, and shutdown. Swift serves text agents pending executable tools. |
| 7. Shared conformance | Python, TypeScript, Go, Rust, Zig, C#, Ruby, PHP, Java, and Elixir pass their implemented full runner dispatch. The runner deliberately reports Swift as incomplete for tools and CLI integration, even when their native tests pass. The all-language acceptance gate therefore remains nonzero. |
| 8. Quality and delivery | Ten coding CLIs share their native tool loop. Swift still has a separate coding loop, explicitly tracked above. Strict native checks pass for the implemented scope. No placeholder implementation was found in the inspected runtime paths; Ruby's explicit unsupported-platform Bash error is a documented boundary. Root `npm run check` remains blocked by existing Biome configuration roots. Changes are uncommitted. |

The remaining text-only boundary is visible in Swift's `Agent.swift`:
construction has no executable tools and a returned tool call fails as
unregistered. Dependency approval alone will not satisfy the contract; handlers,
events, CLI routing, and native conformance still need implementation.

## Final-event correction

Python and TypeScript previously returned completed history when the consumer
cancelled during the `agent_end` callback. Throwing a consumer exception directly
into a stream suspended at that yield also emitted a second terminal event.
Both failures reproduced through their public APIs before the fix.

Each loop now marks its terminal boundary, checks cancellation after delivery,
and lets later exceptions propagate without another event. No second loop,
adapter, or dependency was added. Regressions live in the existing lifecycle
test files and cover `run`, direct stream consumption, cancellation, original
exception identity, provider cleanup, ordered events, retained input history,
and successful reuse. Three of four scenarios failed before the fix in each
language; all four now pass.

Source dispatches pass 105 Python tests and 95 TypeScript tests. The independent
wheel/tarball consumers pass those complete suites again, including the final
test-file organization, native pipes/TCP, self-tests, and checked README examples.
TypeScript's strict declaration and example compilation also passes. Model-calling
snippets are compiled or syntax-checked without paid requests.

## Current package evidence

Scratch paths below are relative to `packages/radian_v2/.scratch/tmp/`.
Each directory contains a consumer helper; retained logs are named where available. The helper checks installed
or extracted SDK origins and package bytes, preventing workspace imports from
silently substituting for the package. These packages are local artifacts.

| Package | Scratch directory and current result |
| --- | --- |
| Python | `ta-python-package.s9XCed`: `env/bin/python check_consumer.py`; 105 tests, self-test and examples pass. `source-final.log` and `consumer-final.log`. Wheel rebuilt and reinstalled offline after the lifecycle fix. |
| TypeScript | `ta-typescript-package.fa5qhu`: `python3 check_consumer.py`; 95 tests, strict checks, self-test and examples pass. `source-final.log` and `consumer-final.log`. Tarball rebuilt and installed offline with scripts disabled after the lifecycle fix. |
| Go | `ta-go-package.PO1Leq`: full standalone race/vet/CLI suites pass in `consumer.log`; `consumer-resume.log` records the separate offline importer/tool loop. All 63 archive files match. |
| Rust | `ta-rust-package.fnHKaj`: 60 tests, Clippy, offline example and self-test pass; `check-0.log` through `check-3.log`. Cargo validates the independent crate. |
| Zig | `ta-zig-sdk.KYXMx0`: all 68 native, 9 CLI, 17 RPC and 13 HTTP groups plus eight README and complete examples pass. Consumer `consumer-c043e4206dd6`; all 54 files match. |
| Java | `ta-java-tools`: `check_consumer.py` passes all 77 SDK and 11 CLI groups against the installed Maven JAR. All runtime class bytes and origins match. Five README examples and the CLI compile strictly; Tool/Bash/RPC/HTTP examples and installed CLI RPC/HTTP modes execute. Maven resolves twelve exact runtime dependencies from the reviewed local repository. |
| Ruby | `ta-ruby-tools`: `check_consumer.py` and `check-*.log`; all 93 tests / 1170 assertions, self-test, six example syntax checks, and Tool/Bash/HTTP examples pass against the installed gem. The package guard verifies all sixteen runtime file origins and bytes plus exact validator dependencies. `check_cli_modes.py` verifies RPC state/EOF and authenticated HTTP startup/Ctrl-C cleanup. The source shared runner passes with Bundler selecting the lockfile. |
| Elixir | `ta-elixir-tools`: `check_consumer.py` passes all 136 tests with bounded schedulers, strict compilation, and eight example builds. All 32 package files, 40 runtime module origins, and fifteen exact dependency versions match. Offline examples and installed CLI RPC/HTTP modes execute; the authenticated HTTP example starts and closes cleanly. |
| C# | `ta-csharp-package.aiBGIZ`: `check_tools_consumer.py` passes all 95 groups, self-test and seven example builds against independent NuGet packages; per-command output is in `check-*.log`. RPC/Bash/HTTP/tool examples execute. `check_cli_modes.py` passes installed RPC state/EOF and authenticated HTTP startup/Ctrl-C cleanup. Source checks and scoped formatting pass. Package assets match every dependency version and content hash in the source lockfile. |
| PHP | `ta-php-tools`: `python3 check_consumer.py` passes all 97 native groups, self-test, seven example syntax checks, credential-free examples, and CLI RPC/HTTP modes against the installed ZIP. All 43 files and SDK class origins match. The 13 dependency versions and dist metadata match the source lock before installation; no lifecycle scripts or plugins run. |
| Swift | `ta-swift-http.WRbw62`: `python3 check_consumer.py`; `consumer-refresh.log` records a warnings-denied build, all 81 groups and HTTP README example passing. All 25 package files and loaded library hashes match; SwiftPM pins match the package lock. Tests and shared fixtures are copied into the consumer. |

| Archive | SHA-256 |
| --- | --- |
| Python wheel | `66b9cd4365b121a2129d2f5677405cad89bc84055486c21b902619b31c35bddc` |
| TypeScript tarball | `218bb584b32a717090ed6a6a947c56b40b5800e5d4bfab3a1f043575eaa49ba9` |
| Go archive | `00044bcd4738b833cdf0d460f9174ae84f97dbb8a43663b1d857bfda101dd90d` |
| Rust crate | `30618ba35d6997139a8d6f66bf5914464b74a6d158847fcd80839034a78ece46` |
| Zig archive | `c043e4206dd6a895c358b9506819fdb6c320838f938c08a6209cb6c3588cfdea` |
| Java JAR | `c3476c3eed93b5ea9631e681df782f902ec7a242204346cd16087e50f981dc21` |
| Ruby gem | `96fb545fef780b07dc4cf9a0907adfe07af2126fa553f2600d8c31621b87df87` |
| Elixir Hex package | `e1edf92ffb1941406ab6c2499bfae3561004e3c159cfbf575eb48648c3c1893b` |
| C# SDK NuGet | `d6e8028a844d9dcaa437f671af7216dc81bfdb996ead7ef50ebd1673fdcec265` |
| C# HTTP NuGet | `b813e4ba31a7867197e48cf749a4fca1bb25e9ed139692b24ead3ea13a5683d8` |
| PHP ZIP | `e363cf5f06e3ab8bf84667854bdb219526d6c10eefab99bbadfd4488b2e334b2` |
| Swift archive | `4cdec5e62530121a9e5a3db6694741911d636757891fcce9444280c5eb3ef129` |

## Verification limits

Execution evidence is macOS arm64 with the recorded installed runtimes. Swift's
earlier iOS 16 simulator compilation/linking is not an iOS execution result.
Other-platform and minimum-version claims remain unverified. Existing package
metadata warnings and earlier load-sensitive subprocess timeouts are preserved
in the [checkpoint archive](archive/2026-09-09/acceptance-checkpoints.md).

The final repository `npm run check` after the Elixir tool integration stops
before source checks at these existing nested Biome roots:
`tuiv2-bottom-up`, `rust-performance-candidate`, and `rust-performance-master`.
No unrelated configuration or concurrent work was changed to bypass them.
Final direct checks pass for whitespace, Python syntax, local documentation
links, and bundled Go/Rust/Zig fixture parity. `audit_finish.py` in the Python
scratch directory covers the final changed files; the existing Zig `audit.py`
also passes. Git's whitespace check alone does not cover the untracked SDK tree.
The final requirement audit must be refreshed after the remaining Swift tool/CLI
implementation. The goal is not complete.
