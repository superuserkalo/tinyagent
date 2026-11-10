# TinyAgent first-release finish contract

Status: current, 2026-11-11. Requirements 1 to 4, 6 and 7 are met; 5 has an open item listed below.

The first public release supports Python and TypeScript. It provides a small
agent SDK with validated tools, streaming, cancellation, durable sessions,
resume, and basic compaction.

The other language implementations (Go, Rust, Zig, C#, Java, PHP and Elixir)
remain experimental reference code. Their completion does not block this
release.

## Acceptance requirements

1. **Small native SDK.** Python and TypeScript each own one agent loop and one
   Session implementation. Agent, Tool, and Session are the main public concepts.
   CLI, RPC, and HTTP reuse those implementations. Imports have no CLI side effects.
2. **Existing runtime reliability.** Preserve both streaming providers, complete
   validated tool arguments, sequential execution, replay metadata, bounded
   observations, ordered events, scoped cancellation, and awaited cleanup.
   Ordinary tool failures remain recoverable. Consumer failures retain their identity.
3. **Durable sessions and resume.** Create, list, inspect, close, and reopen stable
   sessions using local storage. Preserve an in-memory option. Version records,
   prevent concurrent writers, and define torn-write and corruption behavior.
   Record interrupted work without falsely committing a successful conversation.
   Restart never blindly repeats a tool whose effects are uncertain.
4. **Basic compaction.** Support explicit and automatic compaction with configured
   context accounting and response reserve. Preserve raw history, recent complete
   exchanges, instructions, and incremental summaries. Validate and persist a
   checkpoint before activating it. Failure or cancellation leaves the previous
   context intact. Resume restores the accepted checkpoint. Recovery is bounded.
5. **Shared adapters.** Session identity, ownership, persistence, resume, and
   compaction behave consistently through the SDK and optional CLI/RPC/HTTP.
   Keep protocol framing, authentication, limits, busy behavior, and cleanup checks.
   Update protocol documentation and shared fixtures for implemented changes.
6. **Adoption and packages.** Both guides contain a complete working integration
   that uses tools, streams, cancels, restarts, resumes, compacts, and continues.
   The actual independently installed artifacts pass that example and relevant
   failure tests. CI covers the platforms and runtime versions claimed publicly.
7. **Honest release evidence.** Record what was verified, on which platforms and
   runtime versions, against final source and built artifacts. Report unrelated
   failures separately. No unfinished feature is described as shipped.

## Current evidence and remaining work

Verified on 2026-11-10 on macOS arm64. Linux results come from CI; see the
badge in the [README](README.md) for the current state.

| # | Requirement | Python | TypeScript |
|---|---|---|---|
| 1 | Small native SDK | Met | Met |
| 2 | Existing runtime reliability | Met | Met |
| 3 | Durable sessions and resume | Met | Met |
| 4 | Basic compaction | Met | Met |
| 5 | Shared adapters | Code met; shared fixtures open | Code met; shared fixtures open |
| 6 | Adoption and packages | Met | Met |
| 7 | Honest release evidence | This section | Same |

Evidence:

- **Python:** 128 tests pass on Python 3.10 and 3.14, from source and from the
  built wheel installed into a clean environment with the tests copied outside
  the repository. The installed `tinyagent --self-test` passes.
- **TypeScript:** `npm run check` passes and 119 tests pass on Node.js 22.18,
  22.23, 24 and 26. The packed tarball, installed into a clean project, passes
  the suite against its compiled `dist` output.
- **Examples:** each guide's complete example (tool, stream, cancel, restart,
  resume, reconcile, compact, continue) runs unchanged as two real processes
  against a local fake provider, under test, from the installed package.
- **Shared conformance:** `conformance/run.py python typescript` passes.
- **Protocol:** [PROTOCOL.md](PROTOCOL.md) documents the session, compaction and
  recovery commands, routes, events and error codes.

Open items:

- **Shared session fixtures (requirement 5).** `conformance/rpc_v1.json` has no
  session or compaction scenarios. Each language tests them with its own suite.
  The Go, Rust and Zig packages bundle byte-identical copies of that file, so
  extending it means refreshing those copies too.
- **Real providers.** The examples have been run against local fake providers
  only, not against the OpenAI or Anthropic APIs.
- **Platforms.** Windows is untested.
- **Node.js 22.** A durable session prints Node's `ExperimentalWarning` for
  `node:sqlite` once on stderr. Two CLI tests ignore exactly that line.
- **Known defects.** In Python, an HTTP prompt on a session whose journal write
  has already failed returns `200` and then a truncated stream instead of a
  JSON error. In TypeScript, `Session.close()` waits indefinitely if a
  `stream()` was left suspended and never finished or closed.
- **Dependencies.** The Python package pins its two dependencies with `==`,
  which is strict for a library.
- **Publication.** Nothing is published to PyPI or npm, and the TypeScript
  package is still marked private.

## Scope limits

No branching, orchestration, plugin system, predictive background compaction,
queues, steering, approval UI, additional storage backends, TLS termination, or
multi-user ownership is required. Preserve other languages without expanding
them during this release. Keep changes native and readable.

Publishing packages to PyPI or npm is a separate, explicitly authorized step.
Completing this contract means a verified release candidate.
