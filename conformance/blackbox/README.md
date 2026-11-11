# Black-box conformance and fault injection

One harness that drives any TinyAgent implementation from outside and checks
that all of them behave the same. It starts a language's CLI as a process and
talks to it only through its public surfaces:

- RPC mode (`--mode rpc`, JSONL on stdin and stdout),
- the HTTP server (`serve`),
- one-shot mode (`-p`) and the interactive prompt.

The model is a scripted fake server on `127.0.0.1` that speaks the OpenAI
Responses, Anthropic Messages and Chat Completions streaming APIs, records
every request, and misbehaves on demand. The CLI's only tool is bash, so every
tool call in a scenario is a shell command whose effect is a line appended to
a file in the case's temp directory.

Python 3.10 or newer, standard library only. Nothing is written into the
repository; every case has its own temp directory, fake provider and processes,
and removes them even when it fails.

## Running

From the repository root:

```sh
python conformance/blackbox/run.py python typescript            # everything
python conformance/blackbox/run.py python --quick               # CI subset
python conformance/blackbox/run.py typescript --sweeps crash,cancel --no-scenarios
python conformance/blackbox/run.py python --only 'crash[rpc,k=7,resume]' --seed 1
python conformance/blackbox/run.py python --sweeps none -v      # scenarios only, one line each
python conformance/blackbox/run.py --list                       # case names
```

| Option | Meaning |
| --- | --- |
| `languages...` | Names from `targets.py`. Default: all. A disabled or missing language prints `NOT RUN` and is never counted as passing; naming it explicitly makes the exit status non-zero. |
| `--only PATTERN` | Case name, substring or glob. Repeatable. Selects from the full set, ignoring `--quick`. |
| `--sweeps LIST` | Any of `crash,provider,cancel,client,walk`, or `none`. Default: all five. |
| `--no-scenarios` | Run only the sweeps. |
| `--seed N` | Seed for the random kill moments and the random walk. Default 1. Printed with every result. |
| `--quick` | Fast subset. |
| `--jobs N` | Cases run in parallel. Default 4. Cases share nothing, so this changes only the wall time. |
| `--json FILE` | Write every result, with what was observed, to a file. |

The output is a pass/fail/skip count per group and language, each failure with
what was expected, what happened and a command that reproduces that one case
(it carries the seed and the kill point), and then the list of cases where two
languages ended differently. The exit status is non-zero on any failure. A
`skip` means a timing window was missed (for example the process finished
before the journal reached the kill point); it is counted separately and is
not a pass.

Measured on an Apple M-series laptop with `--jobs 4`: `--quick` is about 270
cases and 50 to 60 seconds per language; the full run is about 520 cases and
two minutes per language.

## Adding a language

Add or enable one entry in [`targets.py`](targets.py): the command that starts
the CLI. The harness appends `--mode rpc`, `serve` or `-p ...` itself and
configures the process only through the documented environment:
`TINYAGENT_PROVIDER` (`openai`, `anthropic`, `chat`), `TINYAGENT_BASE_URL`,
`TINYAGENT_MODEL`, `TINYAGENT_SESSION_DIR`, `TINYAGENT_HOST`, `TINYAGENT_PORT`,
`TINYAGENT_TOKEN`, `TINYAGENT_ALLOW_REMOTE`, `TINYAGENT_CONTEXT_TOKENS`,
`TINYAGENT_RESPONSE_RESERVE`, `TINYAGENT_MAX_STEPS`, `TINYAGENT_MAX_SECONDS`,
`TINYAGENT_MAX_TOKENS`, `TINYAGENT_YOLO`, and the flags `--resume`, `--cwd`,
`--yolo`, `--allow`, `--max-steps`, `--max-seconds`, `--max-tokens`,
`--list-sessions`.

Entries for Go, Rust, Zig, C#, Java, PHP and Elixir are present with
`enabled=False`. Set `enabled=True` when the port to protocol version 2 lands.

What the harness assumes beyond the specification, because it must find the
journal from outside: a stored session is one file `<sessionId>.jsonl` in
`TINYAGENT_SESSION_DIR` with one newline-terminated record per line. It reads
nothing else from the journal except in `journal_damage`, which cuts the last
line short and changes one byte.

## Files

| File | What it is |
| --- | --- |
| `run.py` | Command line, parallel execution, report |
| `targets.py` | How to launch each language |
| `wire.py` | The three provider wire formats: decode a request, encode a scripted turn |
| `fake_provider.py` | The scripted model server and its faults |
| `driver.py` | Processes, the RPC client, the HTTP client, kill-at-record |
| `harness.py` | `Case` (one isolated test), assertions, the scenario registry |
| `invariants.py` | The eight invariants, implemented once |
| `scenarios/` | One function per specified behaviour |
| `sweeps/` | The five fault sweeps |

The fake provider is stateless about where a conversation is: its reply is a
function of the request. The last user message names a script (`[[s1]]`), and
the turn returned is the first one whose tool calls have no results yet. A
retried, resumed or repeated request therefore gets the answer a real model
would give, and a target can be killed and restarted without the fake losing
its place. A request with no tools is a compaction summary request.

Faults (`fake_provider.FAULTS`): `http429`, `http429_retry_after`, `http500`,
`http503`, `http503_retry_after`, `http401`, `reset` (connection reset before
headers), `drop` (connection closed mid-stream), `truncated` (closed inside an
event), `error_event` (the API's in-stream error), `stall_headers`,
`stall_mid`, `split_utf8` (every multi-byte character split across writes),
`fragments` (tool arguments one character per event), `unknown_tool`,
`bad_args` (invalid JSON arguments), `malformed`, `duplicated`, `reordered`,
`oversized` (one event over 1 MiB).

## Invariants

Checked after every step of every sweep, and at the end of every scenario
where they apply (`invariants.py`, `Case.finish`).

1. **Streams.** Every `tool_execution_start` has exactly one matching
   `tool_execution_end`; no end without a start; one tool at a time; one
   terminal event, last; nothing after it.
2. **Requests.** Every request the provider received is well-formed for its
   API: no tool result without its call, no call without a result, no call or
   result twice, ending in a user message or a tool result.
3. **Effects.** A command ran at most once per tool call. A command whose
   `tool_execution_end` reported success ran exactly once, including across a
   crash and resume.
4. **Agreement.** A run reaches one terminal state, and `get_state`,
   `get_history` and `get_events` agree with it. Where the client cancelled or
   disconnected, the journal is the truth (F17).
5. **Deadlines.** Every wait has a deadline (15 s by default) and reports
   `HANG` when it passes. Nothing waits by sleeping where a condition can be
   awaited; the two exceptions are the random-kill delay and the provider's
   scripted latency.
6. **Replay.** `sequence` and `cursor` strictly increase; paging through
   `get_events` in small pages gives the same events as one large page;
   `follow` never delivers an event twice.
7. **Usable.** After any failure the session accepts a new prompt, or reports
   `resumable`, `paused` or `recoveryRequired` and the matching way out
   (`resume`, `abandon`, `reconcile`) works. The request for that next prompt
   carries no trace of a run that did not commit (F20).
8. **Processes.** After the target exits, neither it nor any command it
   started is still running.

## Sweeps

All sweeps use one standard conversation: four model turns and four commands,
one of which sleeps 0.3 s and one turn of which has two tool calls.

| Sweep | Cases | What it does |
| --- | --- | --- |
| `crash` | `crash[rpc\|http,k=K,resume\|abandon]`, `crash[..,random=I,..]` | Runs the conversation once to count journal records, then for each K kills the process (SIGKILL) as soon as the journal holds K records, restarts on the same session directory, and resumes or abandons. Also kills at seeded random moments. |
| `provider` | `provider[WIRE,FAULT,request=N]` | Injects each fault at each of the four model requests, for each provider API. The run must complete (after exactly one retry where A4 says so) or fail with a named error, and the session must then take another prompt. |
| `cancel` | `cancel[rpc\|http,after=I]` | Sends `abort` / `POST cancel` after each event index. |
| `client` | `client[attached\|detach,after=I]` | Disconnects the HTTP client after each event index. Attached: the run must be cancelled. Detached: the run must complete and `events?after=N&follow=1` must deliver the rest exactly once. |
| `walk` | `walk[rpc\|http,seed=S]` | A seeded random walk over one session mixing prompt, repeated `messageId`, abort, kill, clean restart, resume, abandon, compact, `get_history`, `get_events`. |

`--quick` runs every kill point with resume and every third with abandon over
RPC, six random kills, every fault at every request for the Responses API and
at request 2 for the other two, the whole cancel sweep, every other disconnect
point, and six short walks. The full run adds HTTP kill points, thirty random
kills per transport, every fault at every request for all three APIs, and
twenty-four longer walks.

## Scenarios

Names in brackets are variants: transport (`rpc`, `http`) and provider API
(`openai`, `anthropic`, `chat`). Numbers are the scenarios of the
specification (D 1–24, E 25–35, F 36–39).

| Scenario | Function |
| --- | --- |
| 2 crash before intent (F1) | sweep `crash` at the record after each assistant turn |
| 4 crash mid non-idempotent tool | `durability.crash_mid_tool_then_resume`, sweep `crash` |
| 6 crash after result | `durability.crash_after_result_then_resume`, sweep `crash` |
| 7 crash after final reply, before commit | sweep `crash` at the last records (the zero-request part is SDK-only, see below) |
| 9 two writers | `durability.two_writers` |
| 10 repeated messageId after commit | `durability.repeated_message_id`, sweep `walk` |
| 11 repeated messageId on an open run | `durability.message_id_on_open_and_failed_runs` |
| 12 events(after=n), cursors | `durability.replay_events`, `invariants.all_events` everywhere |
| 14 limits | `core.limits` |
| 15 unknown tool, repeated tool errors | `core.unknown_tool_then_repeated_errors`, sweep `provider` (`unknown_tool`) |
| 16 retry, no retry, cancel in backoff | `core.provider_retry`, `core.cancel_during_backoff`, sweep `provider` |
| 18 bash output truncation | `core.bash_output_truncation` |
| 19 bash cwd | `core.bash_cwd` |
| 20 approval | `cli.oneshot_approval`, `cli.interactive_approval` |
| 21 one-shot exit codes | `cli.oneshot_exit_codes` |
| 22 detached run | sweep `client`, `http.http_detached_cancel` |
| 23 follow joined mid-run | `http.http_follow_mid_turn`, sweep `client` |
| 24 version 1: text, tools, tool errors, busy, rollback | `core.text_only`, `core.tool_roundtrip`, `core.tool_errors_are_recoverable`, `core.busy_and_rollback`, `core.cancel_mid_stream_and_mid_bash` |
| 24 version 1: RPC framing, limits, EOF | `rpc.rpc_framing_and_errors`, `rpc.rpc_fatal_input`, `rpc.rpc_eof_and_sigint` |
| 24 version 1: sessions, history, compaction, storage | `rpc.rpc_sessions_and_history`, `rpc.compaction`, `durability.journal_damage` |
| 24 version 1: HTTP routes, status codes, auth, shutdown | `http.http_routes_and_status_codes`, `http.http_auth_and_exposure`, `http.http_delete_and_shutdown_cancel`, `http.http_prompt_right_after_stream_end`, `http.http_no_resume_at_startup` |
| 25 baseUrl and key header reach the request | `core.text_only` (every case runs through `TINYAGENT_BASE_URL`) |
| 26 Chat Completions: text, fragments, missing usage, errors | every `[chat]` variant, `core.missing_usage`, sweep `provider[chat,...]` |
| 32 image input, base64 and URL | `core.image_input` |
| 36 abandon | `durability.abandon_open_run`, `durability.open_run_refuses_prompts`, sweep `crash[...,abandon]` |
| 37 replay includes failed and aborted runs | `durability.replay_events`, `invariants.agreement` |
| 38 a resumed stream is well-formed | `durability.crash_mid_tool_then_resume`, invariant 1 on every resumed stream |
| 39 trace off leaves the version 1 event sequence | `core.tool_roundtrip`, `core.text_only` |
| F15 `--resume` continues an open run, no stderr echo | `cli.oneshot_resume`, `core.tool_roundtrip` |

### Covered by each language's own suite

These cannot be observed through the CLI, whose only tool is a non-idempotent
bash and which exposes no hooks. They are not skipped silently: each language's
own tests must cover them.

| Scenario | Why it is not black-box |
| --- | --- |
| 1 storage suite, MemoryStorage, custom Storage | Storage is an SDK interface. Only FileStorage's visible behaviour is tested here. |
| 3 crash mid idempotent tool, `rerun:true` | Needs a tool marked idempotent; bash is not. |
| 5 `onUncertain: "reconcile"` | A session option the CLI does not expose. The harness checks the opposite: the CLI never reports `recoveryRequired`. |
| 7 zero provider requests when resuming after the final reply | The crash sweep reaches that point and checks every invariant, but cannot tell from outside which record the kill followed, so it cannot assert the request count. |
| 8 idempotent flag differs between record and agent | Needs two agent configurations. |
| 9 `session_fenced` | The file lock refuses the second writer at open. A fenced append needs a shared custom store. |
| 13 import and construct without I/O | SDK property. |
| 17 bash timeout | The CLI fixes the timeout at 120 s. |
| 19 bash `env` | Factory option the CLI does not expose. |
| 25 `headers`, `extra`, per-run `extra` and `model` | Provider and run options with no CLI setting. |
| 27 `beforeTool`, `afterTool` | Hooks. |
| 28 pause and resume with a value; F18 | Needs a handler or hook that pauses. |
| 29 `outputSchema` | Run option with no wire field. |
| 30 `context` | Run option. |
| 31 reopen after changing instructions or tools | The CLI's configuration is fixed. |
| 32 a tool returning an image | bash returns text. |
| 33 `model_request_start` / `model_request_end`, `reasoning_delta`; 39 trace on | `trace` has no CLI setting. |
| 34 `ScriptedProvider` | SDK testing utility. |
| 35 general tool timeout | `Tool.timeoutSeconds` is an SDK option. |
| A5 process group gone after a timeout | Same as 17. Cancellation of the process group is tested (`cancel_mid_stream_and_mid_bash`, invariant 8). |

## Writing a scenario

```python
@scenario("D6", variants=BOTH)                      # runs once over RPC and once over HTTP
def crash_after_result_then_resume(case: Case) -> None:
    link = case.link()                              # RpcLink or HttpLink, same methods
    case.stall(at=2)                                # the provider hangs on its second request
    run = link.prompt(case.script(bash(case.command("x")), say("done")), detach=True)
    case.wait_stalled()
    link.kill()                                     # SIGKILL
    link = case.reopen_stored()
    resumed = case.completed(link.resume())
    case.equal(case.effects().get("x"), 1, "effect of the completed command")
```

`case.note(key, value)` records what a language did where the specification
allows more than one outcome. Notes are compared across languages in the
divergence report; a key starting with `~` is timing-dependent and is not.
