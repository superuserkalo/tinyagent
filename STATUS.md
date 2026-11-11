# TinyAgent status

As of 2026-11-11. Version 0.2.0, protocol version 2 in Python and TypeScript.

[PROTOCOL.md](PROTOCOL.md) is the contract. This page says how much of it is
verified, what is known not to be, and what comes next.

## What is verified, and how

| | Python | TypeScript |
|---|---|---|
| Protocol version | 2 | 2 |
| Unit tests | 279 pass on Python 3.10 and 3.14 | 214 pass on Node.js 22.18, 24 and 26; `npm run check` passes |
| Shared conformance (`conformance/run.py`) | passes | passes |
| Black-box harness, full run | 515 cases pass | 515 cases pass |
| Divergences between the two in the harness | none | none |

- **Unit suites** run offline against fake providers. They cover what cannot
  be seen through a CLI: the storage contract against both built-in stores, a
  fenced second writer, idempotent rerun, `onUncertain: "reconcile"`, hooks,
  pause and resume with a value, typed output, run context, provider options,
  trace events, tool timeouts, and import without I/O.
- **Shared conformance** feeds both languages the same RPC scenarios, byte
  limits, tool argument cases and provider SSE transcripts. See
  [conformance/README.md](conformance/README.md).
- **The black-box harness** starts each CLI as a process and drives it over
  RPC, HTTP and one-shot mode against a scripted model server. Its sweeps kill
  the process at every journal record, inject each provider fault at each
  request, cancel and disconnect after each event, and take seeded random
  walks, checking eight invariants after every step. See
  [conformance/blackbox/README.md](conformance/blackbox/README.md).
- **Adversarial review.** The version 2 code was reviewed with the aim of
  breaking it. Each reproduction is now a regression test in the language
  suites.
- **Examples.** Each guide's complete example runs under test as real
  processes against a local fake provider.
- **CI** runs the Python suite on Python 3.10, 3.12 and 3.14 and the TypeScript
  suite on Node.js 22 and 24, each on Linux and macOS, plus the shared
  conformance run and the harness in quick mode on Linux.

The numbers above were measured on macOS arm64. Linux results come from CI; the
badge in the [README](README.md) shows its current state.

## Known limits

- **Windows is untested.**
- **Real providers.** The examples and tests have run against local fake
  providers only, never against the real OpenAI or Anthropic APIs.
- **Nothing is published** to PyPI or npm yet. Install from a clone.
- **Limits the HTML builder demo ran into**, building on the TypeScript SDK as
  an outside consumer would:
  - `serve()` cannot share a port with your own routes; it returns only a URL
    and a close function. The demo runs the SDK's server on a second loopback
    port behind a forwarder.
  - `allowedOrigins` lets a listed origin past the Origin check, but the server
    sends no CORS headers and answers no preflight, so a browser page on
    another origin still cannot call it. Same-origin through a proxy works.
  - An HTTP prompt cannot set a per-run `model`, `instructions` or `extra`,
    and `context` cannot be supplied under `serve()`.
  - The tool context has no session ID; it can be read from the front of
    `executionId`.
  - Events do not carry the user's prompt. Rebuilding a transcript means
    paging `view=records`.
  - A follower reading from its last cursor does not see the `agent_start` of
    a resumed run, because it reuses the original run's cursor.
  - Opening a session that is already open is a `409`; there is no idempotent
    open.
  - `ScriptedProvider` plays its script once and keeps its position in memory,
    so a crash-and-resume test has to derive the next turn from history.
  - Tool arguments are not streamed, so a published page appears a version at
    a time.
- **Node.js 22** prints an `ExperimentalWarning` for `node:sqlite` on stderr
  the first time a process uses a durable session. Node.js 24 and later do not.
- **Python threads cannot be killed.** A handler, hook or provider that ignores
  cancellation is abandoned after one second so the run can be recorded and the
  session released, but its thread stays behind until it returns.
- **The seven other languages are at version 1.** Go, Rust, Zig, C#, Java, PHP
  and Elixir have the in-memory core only, are not in CI, and were last
  verified on macOS in September 2026. Their bundled copies of PROTOCOL.md
  describe version 2, which they do not implement yet.
- **Wording and record layouts are implementation-specific.** Error codes,
  statuses and event fields match between the two version 2 implementations;
  message texts and the extra fields in journal records do not, as
  [the contract says](PROTOCOL.md#implementation-specific-details).
- **One unreproduced observation.** A single full harness run, made while the
  machine was under heavy parallel load, showed four consecutive TypeScript
  RPC cases getting no reply to a prompt within 15 seconds. They have passed
  in every run since, including under deliberate load. The likeliest cause is
  the synchronous disk flushes before the first reply stalling together; this
  is unconfirmed.
- **Shared fixtures.** `conformance/rpc_v1.json` still describes version 1,
  because three version 1 packages bundle it. The Chat Completions SSE streams
  are defined inside each language's tests, not yet as shared fixtures.
- **Journals are not portable** between languages.

## What comes next

Port Go, Rust, Zig, C#, Java, PHP and Elixir to protocol version 2, one at a
time, against the black-box harness. Each already has an entry in
[`conformance/blackbox/targets.py`](conformance/blackbox/targets.py) that is
switched off; a port is done when that entry is enabled and the language passes
the full harness, the shared conformance run and its own suite for what the
harness cannot see. Settling the open points in the contract comes with that
work.

Publishing to PyPI or npm is a separate, explicitly authorised step.
