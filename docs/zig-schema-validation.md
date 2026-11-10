# Zig schema validation

Status: written 2026-09-09 and last verified then, on macOS arm64. The
[Zig implementation](../tinyagents/zig/README.md) is experimental and not in
CI, so nothing here has been re-run since.

This note explains why the Zig SDK validates tool schemas through a small Rust
library, and which pure Zig and C++ validators were rejected.

Zig's agent loop, tool handlers, providers, sessions, and transports remain Zig.
`schema-native` statically links `jsonschema` 0.30.0 behind three C functions:
create a checked schema, validate an argument object, and destroy the schema.
The interface passes byte slices and opaque handles; Rust collections, errors,
and unwinding do not cross it. It links no other TinyAgent implementation.

Cargo is a build dependency. Runtime execution does not require a Rust
installation. In September 2026, on macOS arm64, Zig 0.16.0 and Rust 1.96.1
built and ran the tool example, and its `otool -L` output contained only
`/usr/lib/libSystem.B.dylib`. Builds on other platforms are unverified.

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

The probes and the downloaded candidates were kept outside the repository and
are not part of it.

## Regression coverage

The shared argument fixture,
[`conformance/tool_arguments.json`](../conformance/tool_arguments.json),
includes composed properties with `unevaluatedProperties`, local dynamic
references through a recursive tree, and large integral exponent values. Every
language's tests read the same fixture. The cases supplement the validator's declared dialect support; they
are not a complete independent JSON Schema certification.

In `tinyagents/zig/`, `test/tools.zig` covers the shared schema cases, invalid
construction, ownership, annotations, ordinary failures, output bounds, and
native cancellation. `test/tool_loop.zig` covers sequential recovery, callback
failure, duplicate names, and tool start/end pairing during cancellation.
