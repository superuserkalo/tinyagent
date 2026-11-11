"""One isolated test case: a temp directory, a fake provider, and target processes.

A `Case` owns everything it starts and removes all of it in `cleanup()`, which
runs even when the case fails. Scenarios and sweeps are plain functions that
take a Case.
"""

from __future__ import annotations

import atexit
import collections
import os
import shutil
import signal
import socket
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import driver
import invariants
from driver import CommandError, Hang, HttpLink, HttpServer, Process, RpcLink, Run, TargetDied, wait_until
from fake_provider import Fault, FakeProvider
from targets import Target
from wire import bash, say

MODEL = "fake-model"
FINAL = "All done \u2713"
HTTP_STATUS = {"session_busy": 409, "resume_required": 409, "nothing_to_resume": 409, "recovery_required": 409,
               "session_closed": 409, "session_fenced": 409, "budget_required": 400, "session_not_found": 404,
               "unauthorized": 401, "invalid_request": 400}


class Failure(AssertionError):
    pass


class Skip(Exception):
    """The case could not be observed this time (for example a timing window was missed)."""


@dataclass
class Result:
    name: str
    status: str                      # pass | fail | skip
    detail: str = ""
    seconds: float = 0.0
    notes: dict[str, str] = field(default_factory=dict)
    group: str = "scenarios"


_live_cases: set["Case"] = set()


@atexit.register
def _cleanup_everything() -> None:
    for case in list(_live_cases):
        case.cleanup()
    for process in list(Process.live):
        process.destroy()


class Case:
    def __init__(self, target: Target, wire: str = "openai", variant: str | None = None, think: float = 0.0) -> None:
        self.target, self.wire, self.variant = target, wire, variant
        self.root = Path(tempfile.mkdtemp(prefix="tinyagent-blackbox-")).resolve()
        self.sessions = self.root / "sessions"
        self.work = self.root / "work"
        self.work.mkdir()
        self.effects_file = self.root / "effects.log"
        self.pid_file = self.root / "pids.log"
        self.provider = FakeProvider(think)
        self.processes: list[Process] = []
        self.runs: list[Run] = []
        self.rpc_links: list[RpcLink] = []
        self.notes: dict[str, str] = {}
        self._server: HttpServer | None = None
        self._env: dict[str, str] = {}
        self._args: list[str] = []
        _live_cases.add(self)

    # -- configuration ------------------------------------------------------

    def configure(self, *args: str, **env: str) -> "Case":
        """Extra CLI arguments and environment for every process this case starts."""
        self._args += args
        self._env.update(env)
        return self

    def environment(self, **extra: str | None) -> dict[str, str]:
        env = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", "LANG", "SystemRoot")
               if name in os.environ}
        env.update({
            "TINYAGENT_PROVIDER": self.wire, "TINYAGENT_MODEL": MODEL, "TINYAGENT_BASE_URL": self.provider.url,
            "OPENAI_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key",
            "TINYAGENT_SESSION_DIR": str(self.sessions),
            "GIT_DIR": str(self.root / "no-such-repository"), "NO_COLOR": "1",
        })
        env.update(self.target.env)
        env.update(self._env)
        for name, value in extra.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def spawn(self, mode: str, args: tuple[str, ...] = (), env: dict | None = None,
              on_line: Callable | None = None) -> Process:
        argv = self.target.argv(mode) + self._args + list(args)
        process = Process(argv, self.environment(**(env or {})), self.target.cwd or str(self.work), on_line)
        self.processes.append(process)
        return process

    # -- links --------------------------------------------------------------

    def rpc(self, *args: str, resume: str | None = None, env: dict | None = None) -> RpcLink:
        extra = args + (("--resume", resume) if resume else ())
        link = RpcLink(lambda on_line: self.spawn("rpc", extra, env, on_line), self.runs.append)
        self.rpc_links.append(link)
        return link

    def serve(self, *args: str, token: str | None = None, env: dict | None = None) -> HttpServer:
        """Start the HTTP server on a free loopback port and wait for /health."""
        for _ in range(3):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            settings = {"TINYAGENT_PORT": str(port), "TINYAGENT_HOST": "127.0.0.1", **(env or {})}
            if token:
                settings["TINYAGENT_TOKEN"] = token
            server = HttpServer(self.spawn("serve", args, settings), port, token)

            def healthy() -> bool:
                try:
                    return server.request("GET", "/health", timeout=2)[0] in (200, 401)
                except (TargetDied, Hang):
                    return not server.process.alive
            wait_until(healthy, what="the HTTP server to answer /health")
            if server.process.alive:
                self._server = server
                return server
        raise Failure(f"HTTP server exited at startup: {bytes(server.process.stderr).decode(errors='replace')[-400:]}")

    def http(self, session_id: str | None = None) -> HttpLink:
        if self._server is None or not self._server.process.alive:
            self.serve()
        return HttpLink(self._server, self.runs.append, session_id)

    def link(self, transport: str | None = None) -> RpcLink | HttpLink:
        return self.http() if (transport or self.variant) == "http" else self.rpc()

    def reopen(self, link: RpcLink | HttpLink) -> RpcLink | HttpLink:
        """Open the same stored session again: after a kill, or after a clean close."""
        identifier = getattr(link, "_known_id", None) or self.session_ids()[0]
        if link.transport == "rpc":
            if link.process.alive:
                link.close()
            new: Any = self.rpc(resume=identifier)
        else:
            if link.process.alive:
                link.close()
            new = self.http(identifier)
        new._known_id = identifier
        return new

    def reopen_stored(self, transport: str | None = None) -> RpcLink | HttpLink:
        """Open the one stored session in a new process (after a kill)."""
        identifier = self.session_ids()[0]
        if (transport or self.variant) == "http":
            return self.http(identifier)
        return self.rpc(resume=identifier)

    def session_ids(self) -> list[str]:
        return sorted(path.stem for path in self.sessions.glob("*.jsonl"))

    def oneshot(self, *args: str, stdin: str = "", env: dict | None = None, mode: str = "oneshot",
                timeout: float = driver.DEADLINE) -> tuple[int, str, str]:
        process = self.spawn(mode, args, env)
        if stdin:
            process.write(stdin.encode())
        process.close_stdin()
        code = process.wait(timeout)
        return code, bytes(process.stdout).decode(errors="replace"), bytes(process.stderr).decode(errors="replace")

    # -- scripts and side effects -------------------------------------------

    def script(self, *turns: Any) -> str:
        return self.provider.script(*turns)

    def standard(self, slow: float = 0.3) -> tuple[str, dict[str, str]]:
        """The conversation the sweeps use: four model turns, four commands, one of them slow.

        Returns the prompt and a map from tool call ID to effect tag.
        """
        turns = [bash(self.command("a"), text="Working. "), bash(self.command("b", slow)),
                 bash(self.command("c"), self.command("d")), say(FINAL)]
        prompt = self.script(*turns)
        tags = dict(zip([call.id for turn in turns for call in turn.calls], "abcd"))
        return prompt, tags

    def command(self, tag: str, sleep: float = 0) -> str:
        """A bash command whose effect is one line `tag` appended to the effects file."""
        pause = f"sleep {sleep:g}; " if sleep else ""
        return f"echo $$ >> {self.pid_file}; {pause}echo {tag} >> {self.effects_file}"

    def blocker(self, tag: str, seconds: float = 30) -> str:
        """A command that announces it started, then sleeps; its effect appears only if it is left alone."""
        return (f"echo $$ >> {self.pid_file}; echo started > {self.root / ('started-' + tag)}; "
                f"sleep {seconds:g}; echo {tag} >> {self.effects_file}")

    def wait_started(self, tag: str) -> None:
        wait_until((self.root / f"started-{tag}").exists, what=f"command {tag} to start")

    def stall(self, at: int | None = None, mid: bool = False, seconds: float = 60) -> None:
        """Make the provider hang on a request (the next one by default) until `provider.release` is set."""
        fault = Fault("stall_mid" if mid else "stall_headers", seconds=seconds, after=3)
        self.provider.fault(at or len(self.provider.requests) + 1, fault)

    def wait_stalled(self) -> None:
        wait_until(self.provider.stalled.is_set, what="the provider to receive the request it stalls on")

    def effects(self) -> collections.Counter:
        if not self.effects_file.exists():
            return collections.Counter()
        return collections.Counter(self.effects_file.read_text().split())

    def child_pids(self) -> list[int]:
        if not self.pid_file.exists():
            return []
        return [int(word) for word in self.pid_file.read_text().split() if word.isdigit()]

    def wait_children(self, timeout: float = 5.0) -> None:
        """After a SIGKILL an orphaned command may still be finishing; let it."""
        wait_until(lambda: not any(driver.pid_alive(pid) for pid in self.child_pids()), timeout,
                   "orphaned bash commands to finish")

    def records(self) -> int:
        return driver.record_count(str(self.sessions))

    def kill_at(self, process: Process, at: int) -> driver.KillAt:
        return driver.KillAt(process, str(self.sessions), at)

    # -- assertions ---------------------------------------------------------

    def check(self, condition: Any, message: str) -> None:
        if not condition:
            raise Failure(message)

    def equal(self, actual: Any, expected: Any, what: str) -> None:
        if actual != expected:
            raise Failure(f"{what}: expected {expected!r}, got {actual!r}")

    def fails(self, code: str | tuple[str, ...], action: Callable[[], Any], what: str = "") -> CommandError:
        codes = (code,) if isinstance(code, str) else code
        try:
            value = action()
        except CommandError as error:
            if error.code not in codes:
                raise Failure(f"{what or 'command'}: expected error {'/'.join(codes)}, got {error.code} ({error.message})")
            if error.status is not None and error.status != HTTP_STATUS.get(error.code, error.status):
                raise Failure(f"{what or 'command'}: {error.code} came with HTTP {error.status}, "
                              f"expected {HTTP_STATUS[error.code]}")
            return error
        raise Failure(f"{what or 'command'}: expected error {'/'.join(codes)}, got success {str(value)[:120]}")

    def hold(self, found: list[str]) -> None:
        """Fail on any invariant violation."""
        if found:
            raise Failure("invariant: " + "; ".join(dict.fromkeys(found)))

    def note(self, key: str, value: Any) -> None:
        """Record what this language did where the specification allows more than one outcome."""
        self.notes[key] = str(value)

    def completed(self, run: Run, what: str = "run") -> Run:
        run.drain()
        if run.status != "completed":
            raise Failure(f"{what}: expected completed, got {run.status} "
                          f"{(run.end or {}).get('error', '')}{self.stderr_tail()}")
        return run

    def stderr_tail(self) -> str:
        text = b"".join(bytes(process.stderr) for process in self.processes).decode(errors="replace").strip()
        return f"\n    stderr: ...{text[-300:]}" if text else ""

    # -- end of case --------------------------------------------------------

    def finish(self) -> list[str]:
        """Shut everything down politely and return the universal invariant violations."""
        found: list[str] = []
        for run in self.runs:
            if not run.partial and not run.died:
                found += invariants.stream(run.events)
        found += invariants.requests(self.provider)
        found += [f"RPC stdout carried a line that is not one JSON object: {line[:80]!r}"
                  for link in self.rpc_links for line in link.garbage]
        self.provider.release.set()
        for process in self.processes:
            if process.alive:
                process.close_stdin()
                process.signal(signal.SIGTERM)
                try:
                    process.wait(10)
                except Hang:
                    found.append(f"process did not exit on EOF and SIGTERM: {' '.join(process.argv[-3:])}")
        try:
            wait_until(lambda: not self.leftovers(), 3, "child processes to exit")
        except Hang:
            found.append(f"processes left running after the target exited: {self.leftovers()}")
        return found

    def leftovers(self) -> list[int]:
        groups = [process.pid for process in self.processes if not process.alive and process.group_alive()]
        return groups + [pid for pid in self.child_pids() if driver.pid_alive(pid) or driver.group_alive(pid)]

    def cleanup(self) -> None:
        for pid in self.child_pids():
            for kill in (os.killpg, os.kill):
                try:
                    kill(pid, signal.SIGKILL)
                except (OSError, PermissionError):
                    pass
        for process in self.processes:
            process.destroy()
        self.provider.close()
        shutil.rmtree(self.root, ignore_errors=True)
        _live_cases.discard(self)


def run_case(name: str, function: Callable[[Case], None], make: Callable[[], Case], group: str = "scenarios") -> Result:
    """Run one case function with full cleanup, turning every outcome into a Result."""
    began = time.monotonic()
    case = None
    status, detail = "pass", ""
    try:
        case = make()
        function(case)
        found = case.finish()
        if found:
            status, detail = "fail", "invariant: " + "; ".join(dict.fromkeys(found))
    except Skip as skip:
        status, detail = "skip", str(skip)
    except (Failure, Hang) as error:
        status, detail = "fail", ("HANG: " if isinstance(error, Hang) else "") + str(error)
    except (CommandError, TargetDied) as error:
        status, detail = "fail", f"unexpected {type(error).__name__}: {error}" + (case.stderr_tail() if case else "")
    except Exception:
        status, detail = "fail", "harness error: " + traceback.format_exc(limit=6)
    finally:
        notes = dict(case.notes) if case else {}
        if case:
            case.cleanup()
    return Result(name, status, detail, time.monotonic() - began, notes, group)


@dataclass
class Spec:
    """One runnable case: a scenario, or one point of a sweep."""
    name: str
    group: str
    function: Callable[[Case], None]
    wire: str = "openai"
    variant: str | None = None
    think: float = 0.0
    refs: str = ""
    quick: bool = True
    module: str = ""


# -- scenario registry ---------------------------------------------------------

@dataclass
class Scenario:
    name: str
    refs: str
    function: Callable[[Case], None]
    variant: str | None
    wire: str | None
    quick: bool
    module: str


SCENARIOS: list[Scenario] = []


def scenario(refs: str, variants: tuple = (None,), wires: tuple = (None,), quick: bool = True) -> Callable:
    """Register a scenario. `refs` are the specification items it covers."""
    def register(function: Callable[[Case], None]) -> Callable[[Case], None]:
        for variant in variants:
            for wire in wires:
                label = ",".join(part for part in (variant, wire) if part)
                SCENARIOS.append(Scenario(function.__name__ + (f"[{label}]" if label else ""), refs, function,
                                          variant, wire, quick, function.__module__.split(".")[-1]))
        return function
    return register


BOTH = ("rpc", "http")
