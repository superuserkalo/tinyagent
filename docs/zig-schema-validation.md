# Zig schema validation

Status: current, 2026-09-09. Acceptance is tracked in
[the finish contract](../FINISH_CONTRACT.md).

Zig's agent loop, tool handlers, providers, sessions, and transports remain Zig.
`schema-native` statically links `jsonschema` 0.30.0 behind three C functions:
create a checked schema, validate an argument object, and destroy the schema.
The interface passes byte slices and opaque handles; Rust collections, errors,
and unwinding do not cross it. It links no other TinyAgent implementation.

Cargo is a build dependency. Runtime execution does not require a Rust
installation. The macOS arm64 tool example's `otool -L` output contains only
`/usr/lib/libSystem.B.dylib`. Zig 0.16.0 and Rust 1.96.1 built and ran the example.
Cross-platform builds remain unverified.

The selected validator already serves TinyAgent's Rust SDK. Its exact version
and `serde_json` 1.0.151 are pinned; `Cargo.lock` fixes transitive versions.
Default HTTP/file retrieval features are disabled. An explicit retriever rejects
external references, while the library retains its bundled metaschemas and local
reference support. Format assertion is disabled. Schema checks run at construction;
validation does not mutate the Zig argument object. The handler receives its own
copy, so handler mutations cannot rewrite replay history.

Validation and compilation are synchronous native calls. Cancellation is checked
around validation and interrupts native Zig handler I/O. The interface catches
Rust unwinding and reports a validator failure. It does not replace Rust's
process-level behavior for an exhausted native allocator.

## Candidates checked

- [devnw/zig/json](https://gitlab.com/devnw/zig/json), commit
  `1542437fd860fd85812fec80eda6b33598c7b51d`: source review found that schema
  compilation silently ignores incorrectly typed numeric keywords. Its dynamic
  reference resolver uses globally registered anchors rather than evaluation
  scope. Its conformance runner increments failure counters without failing the
  run. It was not added or built for the SDK.
- [jsoncons 1.9.0](https://github.com/danielaparker/jsoncons/tree/bcb44594c50c495ee1e690602cdd71455942ad0e):
  a Zig-compiled C++ probe used its bundled metaschema and conformance fixtures.
  The integer validator cast a value around `1e300` to `int64_t`, triggering a
  native undefined-behavior panic. The run aborted; it is not passing conformance
  evidence. No sanitizer was disabled and no jsoncons code was added to the SDK.
- [Blaze](https://github.com/sourcemeta/blaze/tree/e9ddd90e8c1b5b89e6f95660f6bbc5502c34bea6):
  the inspected revision offers AGPL-3.0-or-later or a commercial license. It was
  not selected for this MIT-licensed SDK. It was not built or incorporated.

The probes and downloaded candidates are isolated under
`../../../.scratch/tmp/ta-zig-sdk.KYXMx0`, relative to this document.
Previously declined validator downloads for the other six SDKs were not retried.

## Regression coverage

The shared argument fixture now adds composed properties with
`unevaluatedProperties`, local dynamic references through a recursive tree, and
large integral exponent values. Python, TypeScript, Go, Rust, and Zig pass those
same cases. These examples supplement the validator's declared dialect support;
they do not constitute a complete independent JSON Schema certification.

Six direct tool groups cover shared schemas, invalid construction, ownership,
annotations, ordinary failures, output bounds, and native cancellation. Four
agent groups cover sequential recovery, callback failure, duplicate names, and
tool start/end pairing during cancellation. The existing core, session, provider,
RPC, and HTTP suites also pass. Guarded Bash and coding CLI integration remain open.
