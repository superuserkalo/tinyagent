# TinyAgent first-release roadmap

Status: agreed scope, 2026-09-11. Milestones 1 and 2 are implemented in Python and TypeScript; see the [finish contract](FINISH_CONTRACT.md) for current evidence and the open items of milestone 3.

TinyAgent is a small agent SDK that runs tools, remembers its work, and can
continue after a restart. The first public release supports Python and
TypeScript. Other language implementations remain in the repository as
experimental reference implementations, with their existing verification
records. They do not block this release or carry an equal maintenance promise.

## Where we stood on 2026-09-11

The rest of this document is the plan as it was agreed. It is kept as written;
the finish contract records what has been done since.

The existing Python and TypeScript implementations provide validated sequential
tools, OpenAI Responses and Anthropic Messages streaming, ordered events,
cancellation, bounded observations, in-memory history, coding CLIs, RPC, and HTTP.
The last recorded source and independent-package checks passed 105 Python tests
and 95 TypeScript tests, with checked examples. Those results cover the previous
in-memory scope; they do not establish readiness for this release.

Durable storage, restart/resume, and compaction now have an initial implementation
in both SDKs. The shared Session owns history and adapters use it. Initial Python
source checks pass 112 tests; seven new TypeScript session tests pass, including
process-crash recovery. Adapter lifecycle review, expanded failure coverage, and
packaged examples are ongoing. No public package has been published by this work.

## Milestone 1: durable sessions and resume

Expose a small Session interface shared by the imported SDK, CLI, RPC, and HTTP.
Support creation, stable IDs, listing, reopening, history inspection, and closing.
Provide local file storage and an in-memory option. Keep one active run and one
writer per session; different sessions may run independently.

Use versioned durable records for conversation data, tool calls/results, and run
outcomes. Preserve provider replay metadata. Record tool intent before execution
and the outcome afterwards. An interrupted run remains inspectable without being
presented as completed. On restart, uncertain tool effects require reconciliation;
resume must never blindly execute a pending tool again. Do not promise exactly-once
external effects.

Define committed conversation state separately from the durable record of failed
or interrupted work. Keep the existing transport cancellation and delivery tests
while adding persistence. Closing a session must not implicitly delete its data.

Done when a fresh process can reopen a session, inspect its prior work, and
continue; cancellation, a torn final write, conflicting writers, and a crash
around tool execution have explicit, tested outcomes. Begin with one small
end-to-end session example, then apply the same behavior in the second language.

## Milestone 2: basic compaction

Provide explicit compaction and automatic compaction before a request exceeds
its configured model budget, including instructions, tools, and output reserve.
Use an ordinary model completion with tool execution disabled to summarize older
history. Retain recent complete exchanges and preserve the original transcript.
Later compactions update the accepted summary with newly removed history.

Validate that the result is complete, nonempty, and leaves useful context room.
Persist the checkpoint and its covered boundary before making it active. Failed,
cancelled, or invalid summaries install nothing. If no valid request fits, return
an actionable error rather than silently dropping history or retrying forever.
Expose compaction progress and outcome through the same SDK event interface.

Done when a session can compact repeatedly, restart, restore its accepted summary
and recent history, and continue. Checks cover tool-pair boundaries, failed summary
requests, cancellation, checkpoint writes, and recovery from an invalid checkpoint.

## Milestone 3: adoption and release verification

Make Agent, Tool, and Session the main concepts in both getting-started guides.
Provide one complete example that registers tools, streams, cancels, restarts,
resumes, and continues through compaction. Keep the CLI, RPC, and HTTP optional
and route them through the same session implementation. Document any protocol
changes and update shared fixtures when those changes are implemented.

Run focused conformance with the Python environment containing the SDK's
dependencies: `python conformance/run.py python typescript`. The default
all-language runner remains available for experimental-language checks; it is
not the first-release gate. Passing the existing focused suite alone does not
cover the new session and compaction acceptance requirements.

Install the final Python and TypeScript artifacts in independent environments
and execute the examples and relevant suites against those artifacts. Add CI
for the runtime/platform combinations claimed by the release. Existing macOS
arm64 results do not establish Linux, Windows, or minimum-runtime support.
Record repository-wide baseline failures separately from SDK checks.

Done when the release acceptance example and failure cases pass from the actual
packages, documentation matches behavior, and package metadata and publication
steps have been reviewed. Publication and git operations require separate user
authorization; they are not implied by this roadmap.

## Later, driven by use

Promote another language when there is a concrete integration, passing CI, and
maintenance ownership. Consider additional capabilities only when real users
need them. Branching, orchestration, plugins, predictive compaction, queues,
steering, approval UI, and additional storage backends are outside this release.

## Reference and progress reporting

Radian V2 informs the durability and recovery rules: session-owned history,
preserved raw records, compacted model context, validated checkpoint installation,
and explicit uncertainty after interrupted tools. TinyAgent does not inherit
Radian's complete architecture or feature set.

The remaining work is two substantial implementation milestones followed by
release verification. Language counts and old test totals are not release
completion percentages. Report progress by working acceptance examples and
remaining failures. Calendar estimates should be revised after the first durable
session example establishes the actual integration work.

The [finish contract](FINISH_CONTRACT.md) defines the release gate. The
[earlier acceptance audit](docs/acceptance-audit.md) retains baseline evidence.
