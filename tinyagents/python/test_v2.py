"""Protocol version 2 conformance scenarios, Parts A to D. Each test names its scenario."""

import contextlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import tiny_agent
from crash_fixture import EXIT_CODE, SCRIPT, build_agent
from tinyagent import (Agent, CancellationToken, CancelledError, FileStorage, LimitExceededError,
                       MemoryStorage, OpenAIProvider, ProviderError, ProviderEvent, ProviderTurn,
                       RecoveryRequiredError, RepeatedToolErrorsError, ResumeRequiredError, Session,
                       SessionFencedError, Tool, ToolCall, bash_tool, check_storage, serve)
from tinyagent_session import INTERRUPTED
from tinyagent_testing import ScriptedProvider

HERE = Path(__file__).parent
POSIX = unittest.skipUnless(os.name == "posix", "Bash process groups are a POSIX contract")


def tool(name="effect", handler=lambda arguments: "ok", **options):
    return Tool(name, "A test tool.", {"type": "object"}, handler, **options)


def call(name="effect", identifier="call_1", **arguments):
    return [{"name": name, "id": identifier, "arguments": arguments}]


class TempCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ta-v2-")
        self.addCleanup(temp.cleanup)
        self.directory = temp.name


class StorageTests(TempCase):
    def test_d01_storage_suite_passes_for_file_and_memory_storage(self):
        check_storage(MemoryStorage())
        check_storage(FileStorage(Path(self.directory) / "not-created-yet"))

    def test_d01_storage_suite_rejects_a_store_that_does_not_fence(self):
        class Careless(MemoryStorage):
            def append(self, session_id, expected, record):
                self._sessions.setdefault(session_id, []).append(record)
                return len(self._sessions[session_id])

        with self.assertRaises(AssertionError):
            check_storage(Careless())

    def test_d01_file_storage_does_nothing_until_used_and_release_frees_the_lock(self):
        target = Path(self.directory) / "sessions"
        first, second = FileStorage(target), FileStorage(target)
        self.assertEqual(first.list(), [])
        self.assertFalse(target.exists())
        first.append("s_one", 0, {"type": "session"})
        with self.assertRaises(tiny_agent.SessionBusyError):
            second.load("s_one")
        first.release("s_one")
        self.assertEqual(len(second.load("s_one")), 1)
        second.release("s_one")


class ResumeTests(TempCase):
    """D02 to D08 and D11: a child process dies at a named point and this process resumes."""

    def crash(self, point, *flags, session_id="s_crash"):
        result = subprocess.run(
            [sys.executable, str(HERE / "crash_fixture.py"), self.directory, session_id, *flags],
            env={**os.environ, "TINYAGENT_TEST_CRASH_AT": point, "PYTHONPATH": os.pathsep.join(sys.path)},
            stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(result.returncode, EXIT_CODE, result.stderr.decode())
        return session_id

    def reopen(self, turns, idempotent=False, **options):
        self.provider = ScriptedProvider(turns)
        session = Session.open(build_agent(self.directory, idempotent, self.provider),
                               self.directory, "s_crash", **options)
        self.addCleanup(session.close)
        return session

    def effects(self):
        log = Path(self.directory) / "effects.log"
        return log.read_text().splitlines() if log.exists() else []

    def test_d02_crash_before_intent_resumes_and_the_tool_runs_once(self):
        self.crash("before_intent")
        self.assertEqual(self.effects(), [])
        session = self.reopen(SCRIPT[1:])
        self.assertTrue(session.resumable)
        events = []
        result = session.resume(on_event=events.append)
        self.assertEqual(result.text, "all done")
        self.assertEqual(len(self.effects()), 1)
        self.assertEqual(len(self.provider.requests), 1)  # the model is asked again, for the next step
        self.assertEqual(events[0].data["resumed"], True)
        self.assertNotIn("rerun", events[1].data)
        self.assertEqual([item["role"] for item in session.history],
                         ["user", "assistant", "tool_result", "assistant"])

    def test_d02_crash_before_anything_is_journaled_asks_the_model_for_the_same_turn(self):
        storage = MemoryStorage()
        provider = ScriptedProvider(SCRIPT)
        agent = build_agent(self.directory, False, provider)
        session = Session(agent, storage=storage)
        # What a process killed during its first model request leaves behind.
        session._append({"type": "run_start", "runId": "r_lost"})
        session._append({"type": "item", "item": {"role": "user", "content": "do it"}})
        reopened = Session.open(agent, storage, session.id)
        self.assertEqual(reopened.resume().text, "all done")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(len(self.effects()), 1)

    def test_d03_crash_mid_idempotent_tool_reruns_it_with_the_same_execution_id(self):
        self.crash("after_intent", "idempotent")
        session = self.reopen(SCRIPT[1:], idempotent=True)
        intent = session.records[-1]
        self.assertEqual((intent["type"], intent["idempotent"]), ("tool_intent", True))
        events = []
        session.resume(on_event=events.append)
        started = next(event for event in events if event.type == "tool_execution_start")
        execution_id = f"s_crash:{intent['sequence']}"
        self.assertEqual(started.data["executionId"], execution_id)
        self.assertIs(started.data["rerun"], True)
        self.assertEqual(self.effects(), [execution_id])
        self.assertEqual(session.records[-1]["type"], "run_commit")

    def test_d04_crash_mid_non_idempotent_tool_tells_the_model_and_completes(self):
        self.crash("after_intent")
        session = self.reopen(SCRIPT[1:])
        self.assertTrue(session.resumable)
        self.assertFalse(session.recovery_required)
        events = []
        result = session.resume(on_event=events.append)
        self.assertEqual(result.text, "all done")
        self.assertEqual(self.effects(), [])  # never run again
        self.assertEqual([event.type for event in events],
                         ["agent_start", "tool_execution_start", "tool_execution_end", "text_delta", "agent_end"])
        self.assertIs(events[1].data["interrupted"], True)
        observation = self.provider.requests[0]["history"][-1]
        self.assertEqual((observation["output"], observation["isError"]), (INTERRUPTED, True))
        self.assertEqual(INTERRUPTED, "This tool call was interrupted and may have partly run. "
                                      "Check its effects before continuing.")

    def test_d05_on_uncertain_reconcile_requires_reconciliation(self):
        self.crash("after_intent")
        session = self.reopen(["after the note"], on_uncertain="reconcile")
        self.assertTrue(session.recovery_required)
        self.assertFalse(session.resumable)
        for attempt in (lambda: session.run("again"), session.resume, session.compact):
            with self.assertRaises(RecoveryRequiredError):
                attempt()
        session.reconcile("Checked by hand: the effect did not happen.")
        self.assertFalse(session.recovery_required)
        self.assertFalse(session.resumable)
        self.assertEqual(session.run("again").text, "after the note")
        self.assertEqual(self.effects(), [])
        self.assertTrue(session.history[0]["content"].startswith("Interrupted run recovery:"))

    def test_d06_crash_after_result_does_not_rerun_the_tool(self):
        self.crash("after_result")
        ran_before = self.effects()
        self.assertEqual(len(ran_before), 1)
        session = self.reopen(SCRIPT[1:])
        events = []
        self.assertEqual(session.resume(on_event=events.append).text, "all done")
        self.assertEqual(self.effects(), ran_before)
        self.assertEqual([event.type for event in events], ["agent_start", "text_delta", "agent_end"])
        self.assertEqual(self.provider.requests[0]["history"][-1]["output"], "effect done")

    def test_d07_crash_after_final_reply_commits_without_a_provider_request(self):
        self.crash("after_reply")
        session = self.reopen([])
        self.assertEqual(session.history, [])
        result = session.resume()
        self.assertEqual(result.text, "all done")
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(session.records[-1]["type"], "run_commit")
        self.assertEqual(len(session.history), 4)
        self.assertFalse(session.resumable)

    def test_d08_idempotent_flag_that_differs_is_treated_as_uncertain(self):
        self.crash("after_intent", "idempotent")
        session = self.reopen(SCRIPT[1:], idempotent=False)
        session.resume()
        self.assertEqual(self.effects(), [])
        self.assertEqual(self.provider.requests[0]["history"][-1]["output"], INTERRUPTED)
        # And the other way round: recorded as not idempotent, declared idempotent now.
        self.crash("after_intent", session_id="s_other")
        provider = ScriptedProvider(SCRIPT[1:])
        with Session.open(build_agent(self.directory, True, provider), self.directory, "s_other") as other:
            other.resume()
        self.assertEqual(self.effects(), [])
        self.assertEqual(provider.requests[0]["history"][-1]["output"], INTERRUPTED)

    def test_d11_repeated_message_id_on_an_open_run_resumes_it(self):
        self.crash("after_result")
        session = self.reopen(SCRIPT[1:])
        with self.assertRaises(ResumeRequiredError):
            session.run("something else")
        with self.assertRaises(ResumeRequiredError):
            session.run("something else", message_id="m2")
        events = []
        result = session.run("do it", message_id="m1", on_event=events.append)
        self.assertEqual((result.text, result.repeated), ("all done", False))
        self.assertIs(events[0].data["resumed"], True)
        self.assertEqual(len(self.effects()), 1)
        self.assertEqual(sum(record["type"] == "run_start" for record in session.records), 1)

    def test_d06_a_turn_with_two_calls_continues_at_the_call_that_never_started(self):
        ran = []
        provider = ScriptedProvider(["finished"])
        agent = Agent(model="offline", provider=provider,
                      tools=[tool("effect", lambda arguments: ran.append(arguments["n"]) or "ok")])
        storage = MemoryStorage()
        session = Session(agent, storage=storage)
        assistant = {"role": "assistant", "content": [
            {"type": "tool_call", "callId": "a", "name": "effect", "arguments": {"n": 1}},
            {"type": "tool_call", "callId": "b", "name": "effect", "arguments": {"n": 2}}]}
        for record in ({"type": "run_start", "runId": "r_two"},
                       {"type": "item", "item": {"role": "user", "content": "both"}},
                       {"type": "item", "item": assistant},
                       {"type": "tool_intent", "callId": "a", "toolName": "effect", "arguments": {"n": 1},
                        "idempotent": False},
                       {"type": "tool_result", "item": {"role": "tool_result", "callId": "a", "toolName": "effect",
                                                        "output": "ok", "isError": False}}):
            session._append(record)
        reopened = Session.open(agent, storage, session.id)
        self.assertEqual(reopened.resume().text, "finished")
        self.assertEqual(ran, [2])
        self.assertEqual([item.get("callId") for item in provider.requests[0]["history"][2:]], ["a", "b"])


class SessionContractTests(unittest.TestCase):
    def test_d09_second_writer_is_fenced_before_its_tool_executes(self):
        storage = MemoryStorage()
        ran = []
        taken_over = []

        class Slow:
            """The first writer's model request, during which another writer takes the session."""

            def stream(self, model, instructions, history, tools, cancellation):
                with Session.open(other_agent, storage, session.id) as second:
                    taken_over.append(second.resume().text)
                yield from ()
                content = [{"type": "tool_call", "callId": "c", "name": "effect", "arguments": {}}]
                return ProviderTurn("", {"role": "assistant", "content": content}, [], [ToolCall("c", "effect", {})])

        effect = tool("effect", lambda arguments: ran.append(True))
        other_agent = Agent(model="offline", provider=ScriptedProvider(["second writer finished"]), tools=[effect])
        session = Session(Agent(model="offline", provider=Slow(), tools=[effect]), storage=storage)
        events = []
        with self.assertRaises(SessionFencedError):
            session.run("go", on_event=events.append)
        self.assertEqual(taken_over, ["second writer finished"])
        self.assertEqual(ran, [])
        self.assertEqual(events[-1].data["error"]["code"], "session_fenced")
        with self.assertRaisesRegex(RuntimeError, "reopen"):
            session.run("again")
        self.assertEqual(storage.load(session.id)[-1]["type"], "run_commit")

    def test_d10_repeated_message_id_after_commit_returns_the_stored_result(self):
        provider = ScriptedProvider([{"text": "first answer", "input_tokens": 5, "output_tokens": 2}, "second"])
        storage = MemoryStorage()
        agent = Agent(model="offline", provider=provider)
        session = Session(agent, storage=storage)
        first = session.run("question", message_id="m1")
        self.assertFalse(first.repeated)
        for current in (session, Session.open(agent, storage, session.id)):
            events = []
            again = current.run("question", message_id="m1", on_event=events.append)
            self.assertEqual((again.text, again.repeated, again.usage), ("first answer", True, first.usage))
            self.assertEqual([event.type for event in events], ["agent_start", "agent_end"])
            self.assertIs(events[-1].data["repeated"], True)
            self.assertEqual(events[-1].run_id, session.records[1]["runId"])
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(len(storage.load(session.id)), len(session.records))
        self.assertEqual(session.run("another", message_id="m2").text, "second")

    def test_d10_a_failed_message_id_runs_again(self):
        provider = ScriptedProvider([ProviderError("down"), "recovered"])
        session = Session(Agent(model="offline", provider=provider))
        with self.assertRaises(ProviderError):
            session.run("question", message_id="m1")
        result = session.run("question", message_id="m1")
        self.assertEqual((result.text, result.repeated), ("recovered", False))
        self.assertEqual(len(provider.requests), 2)

    def test_d12_events_after_a_cursor_are_exactly_the_later_events(self):
        provider = ScriptedProvider([{"text": "thinking", "calls": call()[:]}, "done", ProviderError("down"),
                                     "third"])
        session = Session(Agent(model="offline", provider=provider, tools=[tool()]))
        session.run("one")
        with self.assertRaises(ProviderError):
            session.run("fails")
        session.run("three")
        everything = session.events(limit=1000)["events"]
        self.assertEqual([event["type"] for event in everything],
                         ["agent_start", "text_delta", "tool_execution_start", "tool_execution_end",
                          "text_delta", "agent_end", "agent_start", "agent_end",
                          "agent_start", "text_delta", "agent_end"])
        cursors = [event["cursor"] for event in everything]
        self.assertEqual(cursors, sorted(set(cursors)))
        self.assertLess(cursors[-1], 2 ** 53)
        for event in everything:
            self.assertEqual(session.records[event["cursor"] - 1]["sequence"], event["cursor"])
        self.assertEqual([event["sequence"] for event in everything], [1, 2, 3, 4, 5, 6, 1, 2, 1, 2, 3])
        self.assertEqual(everything[2]["executionId"], f"{session.id}:{everything[2]['cursor']}")
        self.assertEqual((everything[5]["text"], everything[5]["usage"]["requests"]), ("done", 2))
        for index, cursor in enumerate(cursors):
            self.assertEqual(session.events(after=cursor, limit=1000)["events"], everything[index + 1:])
        self.assertEqual(session.events(after=cursors[-1]), {"events": [], "nextAfter": None})
        page = session.events(limit=4)
        self.assertEqual((page["events"], page["nextAfter"]), (everything[:4], cursors[3]))
        self.assertEqual(session.events(after=page["nextAfter"], limit=1000)["events"], everything[4:])

    def test_d12_live_events_carry_the_cursor_of_their_record(self):
        provider = ScriptedProvider([call(), "done"])
        session = Session(Agent(model="offline", provider=provider, tools=[tool()]))
        live = []
        session.run("one", on_event=live.append)
        replayed = {event["cursor"]: event["type"] for event in session.events()["events"]}
        with_cursor = {event.data["cursor"]: event.type for event in live if "cursor" in event.data}
        self.assertEqual(with_cursor, {cursor: kind for cursor, kind in replayed.items() if kind != "text_delta"})
        self.assertEqual([event.type for event in live if "cursor" not in event.data], ["text_delta"])

    def test_d13_import_and_construct_do_no_io(self):
        script = r'''
import os, sys
sys.dont_write_bytecode = True
sys.path[:0] = PATHS
roots = tuple(os.path.realpath(path) for path in {sys.prefix, sys.base_prefix, *sys.path} if path)
strict = False
seen = []

def audit(event, args):
    if event == "open":
        path, _, flags = args
        writes = isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC)
        library = isinstance(path, str) and os.path.realpath(path).startswith(roots)
        if writes or not library:
            seen.append((event, path))
    elif event.split(".")[0] in ("socket", "subprocess", "sqlite3", "shutil") or event in (
            "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.system", "os.exec", "os.fork",
            "os.posix_spawn", "os.putenv", "os.unsetenv", "_thread.start_new_thread"):
        seen.append((event, args))
    elif event in ("os.listdir", "os.scandir") and not os.path.realpath(args[0] or ".").startswith(roots):
        seen.append((event, args))

sys.addaudithook(audit)

def read(key):
    # The standard library reads a few variables of its own while it is imported.
    frame = sys._getframe(2)
    while os.path.basename(frame.f_code.co_filename) in ("os.py", "<frozen os>"):
        frame = frame.f_back
    if os.path.basename(frame.f_code.co_filename).startswith("tiny"):
        seen.append(("environ", key))

class Trap(dict):
    def __getitem__(self, key): read(key); raise KeyError(key)
    def get(self, key, default=None): read(key); return default
    def __contains__(self, key): read(key); return False

# The interpreter and macOS may add a variable or two of their own to an empty environment.
assert set(os.environ) <= {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}, os.environ
os.environ = Trap()
import threading
threading.Thread.start = lambda self: seen.append(("thread", self.name))

import tinyagent
import tinyagent_testing
assert seen == [], ("import", seen)

from tinyagent import *
tool = Tool("t", "A tool.", {"type": "object", "properties": {"a": {"$ref": "#/$defs/a"}}, "$defs": {"a": {"type": "string"}}},
            lambda arguments: "ok", idempotent=True)
agents = [Agent(model="m", tools=[tool, bash_tool(cwd="/nowhere", env={"A": "b"}, timeout_seconds=1, approve=lambda c: True)]),
          Agent(model="m", provider=AnthropicProvider()),
          Agent(model="m", provider=OpenAIProvider(), max_steps=3, max_seconds=1, max_tokens=10)]
storages = [MemoryStorage(), FileStorage("relative/sessions"), FileStorage("/nonexistent/sessions")]
session = Session(agents[0])
shared = Session(agents[1], storage=storages[0], on_uncertain="reconcile")
assert session.history == [] and shared.events()["events"] == [] and storages[1].list() == []
CancellationToken()
assert seen == [], ("construct", seen)
os.getenv("PROBE"); os.environ.get("PROBE")
assert seen == [], "the probe must ignore reads from outside the SDK"
exec(compile("import os; os.getenv('PROBE'); os.environ.get('PROBE')", "tinyagent_probe.py", "exec"))
assert len(seen) == 2, ("the probe must see reads from SDK files", seen)
del seen[:]
try:
    open("probe.txt", "w")
except OSError:
    pass
assert seen and seen[0][0] == "open", "the audit hook must see file access"
assert os.listdir(".") == []
print("clean")
'''.replace("PATHS", repr([path for path in sys.path if path and os.path.isabs(path)]))
        with tempfile.TemporaryDirectory(prefix="ta-readonly-") as cwd:
            os.chmod(cwd, 0o555)
            try:
                result = subprocess.run([sys.executable, "-I", "-c", script], cwd=cwd, env={},
                                        capture_output=True, text=True, timeout=60)
            finally:
                os.chmod(cwd, 0o755)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "clean"), result.stderr)


class LoopTests(unittest.TestCase):
    def test_d14_max_steps_ends_the_run_with_limit_exceeded(self):
        provider = ScriptedProvider([call(), call(), call()])
        session = Session(Agent(model="offline", provider=provider, tools=[tool()], max_steps=2))
        events = []
        with self.assertRaises(LimitExceededError) as raised:
            session.run("loop", on_event=events.append)
        self.assertEqual(raised.exception.limit, "steps")
        self.assertEqual(events[-1].data["status"], "failed")
        self.assertEqual(events[-1].data["error"], {"code": "limit_exceeded", "limit": "steps", "retryable": False,
                                                    "message": "run limit exceeded: steps"})
        self.assertEqual(events[-1].data["usage"]["requests"], 2)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(session.history, [])  # recorded as a failed run, not part of the conversation
        self.assertEqual(session.records[-1]["type"], "run_failed")
        self.assertEqual(sum(record["type"] == "tool_result" for record in session.records), 2)
        self.assertFalse(session.recovery_required)

    def test_d14_max_seconds_ends_the_run_with_limit_exceeded(self):
        slow = tool(handler=lambda arguments: time.sleep(0.05) or "ok")
        agent = Agent(model="offline", provider=ScriptedProvider([call(), "never"]), tools=[slow], max_seconds=0.02)
        events = []
        with self.assertRaises(LimitExceededError) as raised:
            agent.run("slow", on_event=events.append)
        self.assertEqual((raised.exception.limit, events[-1].data["error"]["limit"]), ("seconds", "seconds"))

    def test_d14_max_tokens_ends_the_run_with_limit_exceeded(self):
        provider = ScriptedProvider([{"calls": call(), "input_tokens": 70, "output_tokens": 40}, "never"])
        agent = Agent(model="offline", provider=provider, tools=[tool()], max_tokens=100)
        events = []
        with self.assertRaises(LimitExceededError) as raised:
            agent.run("big", on_event=events.append)
        self.assertEqual((raised.exception.limit, events[-1].data["error"]["limit"]), ("tokens", "tokens"))
        self.assertEqual(events[-1].data["usage"], {"inputTokens": 70, "outputTokens": 40, "requests": 1})

    def test_d14_usage_is_summed_on_the_result_and_agent_end(self):
        provider = ScriptedProvider([{"calls": call(), "input_tokens": 10, "output_tokens": 3},
                                     {"text": "done", "input_tokens": 20, "output_tokens": 4}])
        session = Session(Agent(model="offline", provider=provider, tools=[tool()]))
        events = []
        result = session.run("go", on_event=events.append)
        expected = {"inputTokens": 30, "outputTokens": 7, "requests": 2}
        self.assertEqual((result.usage, events[-1].data["usage"], session.usage), (expected, expected, expected))

    def test_d15_unknown_tool_is_a_recoverable_observation(self):
        provider = ScriptedProvider([call("missing"), "recovered"])
        events = []
        result = Agent(model="offline", provider=provider, tools=[tool()]).run("go", on_event=events.append)
        self.assertEqual(result.text, "recovered")
        ended = next(event for event in events if event.type == "tool_execution_end")
        self.assertEqual((ended.data["output"], ended.data["isError"]), ("Unknown tool: missing", True))
        self.assertEqual(provider.requests[1]["history"][-1]["output"], "Unknown tool: missing")

    def test_d15_three_all_error_turns_end_with_repeated_tool_errors(self):
        def broken(arguments):
            raise RuntimeError("no")

        tools = [tool("broken", broken), tool("fine")]
        bad = call("broken")
        mixed = [bad[0], {"name": "fine", "id": "ok"}]
        provider = ScriptedProvider([bad, bad, mixed, bad, bad, bad, "never"])
        events = []
        with self.assertRaises(RepeatedToolErrorsError):
            Agent(model="offline", provider=provider, tools=tools).run("go", on_event=events.append)
        self.assertEqual(len(provider.requests), 6)  # the mixed turn reset the count
        self.assertEqual(events[-1].data["error"]["code"], "repeated_tool_errors")

    def test_d16_cancellation_during_backoff_stops_at_once(self):
        token = CancellationToken()
        provider = ScriptedProvider([ProviderError("busy", status=429, retryable=True), "never"])
        agent = Agent(model="offline", provider=provider)
        threading.Timer(0.05, token.cancel).start()
        began = time.monotonic()
        with self.assertRaises(CancelledError):
            agent.run("go", cancellation=token)  # the default sleep would wait a full second
        self.assertLess(time.monotonic() - began, 0.8)
        self.assertEqual(len(provider.requests), 1)

    def test_d16_backoff_doubles_honours_retry_after_and_gives_up_after_four_attempts(self):
        delays = []
        errors = [ProviderError("busy", status=503, retryable=True),
                  ProviderError("busy", status=429, retryable=True, retry_after=7),
                  ProviderError("busy", status=429, retryable=True, retry_after=300),
                  ProviderError("still busy", status=500, retryable=True)]
        provider = ScriptedProvider(errors + ["never"])
        agent = Agent(model="offline", provider=provider, sleep=lambda seconds, token: delays.append(seconds))
        events = []
        with self.assertRaisesRegex(ProviderError, "still busy"):
            agent.run("go", on_event=events.append)
        self.assertEqual(delays, [1, 7, 30])
        self.assertEqual(len(provider.requests), 4)
        self.assertEqual(events[-1].data["usage"]["requests"], 0)  # no request returned a response
        delays.clear()
        provider = ScriptedProvider([errors[0], errors[0], "third time"])
        agent = Agent(model="offline", provider=provider, sleep=lambda seconds, token: delays.append(seconds))
        self.assertEqual((agent.run("go").text, delays), ("third time", [1, 2]))


class _ProviderServer:
    """A loopback Responses endpoint that answers each request from a script."""

    OK = (b'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"message",'
          b'"content":[{"type":"output_text","text":"fine"}]}}\n\n'
          b'data: {"type":"response.completed","response":{"status":"completed",'
          b'"usage":{"input_tokens":11,"output_tokens":3}}}\n\n')

    def __init__(self, responses):
        owner = self
        self.responses = list(responses)
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                status, headers, payload = owner.responses.pop(0)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()


class ProviderRetryTests(unittest.TestCase):
    def run_against(self, responses):
        server = _ProviderServer(responses)
        self.addCleanup(server.close)
        self.delays = []
        agent = Agent(model="offline", provider=OpenAIProvider("key", endpoint=server.url + "/responses", timeout=5),
                      sleep=lambda seconds, token: self.delays.append(seconds))
        return server, agent

    def test_d16_provider_429_then_success_retries_once(self):
        server, agent = self.run_against([(429, {"Retry-After": "2"}, b"slow down"), (200, {}, _ProviderServer.OK)])
        result = agent.run("go")
        self.assertEqual((result.text, len(server.requests), self.delays), ("fine", 2, [2.0]))
        self.assertEqual(result.usage, {"inputTokens": 11, "outputTokens": 3, "requests": 1})

    def test_d16_provider_401_is_not_retried(self):
        server, agent = self.run_against([(401, {}, b"bad key"), (200, {}, _ProviderServer.OK)])
        events = []
        with self.assertRaisesRegex(ProviderError, "401"):
            agent.run("go", on_event=events.append)
        self.assertEqual((len(server.requests), self.delays), (1, []))
        self.assertIs(events[-1].data["error"]["retryable"], False)

    def test_d16_failure_after_the_first_body_byte_is_not_retried(self):
        partial = _ProviderServer.OK.split(b"\n\n")[0] + b"\n\n"
        server, agent = self.run_against([(200, {}, partial), (200, {}, _ProviderServer.OK)])
        with self.assertRaisesRegex(ProviderError, "ended before"):
            agent.run("go")
        self.assertEqual((len(server.requests), self.delays), (1, []))

    def test_d16_connection_error_is_retried(self):
        with socket.socket() as placeholder:
            placeholder.bind(("127.0.0.1", 0))
            port = placeholder.getsockname()[1]
        delays = []
        agent = Agent(model="offline", provider=OpenAIProvider("key", endpoint=f"http://127.0.0.1:{port}/", timeout=5),
                      sleep=lambda seconds, token: delays.append(seconds))
        with self.assertRaisesRegex(ProviderError, "connection failed"):
            agent.run("go")
        self.assertEqual(delays, [1, 2, 4])


@POSIX
class BashTests(TempCase):
    def run_tool(self, command, **options):
        provider = ScriptedProvider([call("bash", command=command), "done"])
        events = []
        Agent(model="offline", provider=provider, tools=[bash_tool(**options)]).run("go", on_event=events.append)
        ended = next(event for event in events if event.type == "tool_execution_end")
        self.assertEqual(provider.requests[1]["history"][-1]["output"], ended.data["output"])
        return ended.data

    def test_d17_bash_timeout_returns_partial_output_and_kills_the_group(self):
        began = time.monotonic()
        data = self.run_tool("sleep 30 & echo child=$!; echo partial; wait", timeout_seconds=0.5)
        self.assertLess(time.monotonic() - began, 5)
        self.assertTrue(data["isError"])
        self.assertIn("partial\n", data["output"])
        self.assertTrue(data["output"].endswith("\n[timed out after 0.5 s]"), data["output"])
        child = int(data["output"].split("child=")[1].split()[0])
        deadline = time.monotonic() + 3
        while True:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            self.assertLess(time.monotonic(), deadline, "the child process outlived the timeout")
            time.sleep(0.02)

    def test_d18_bash_output_of_100000_bytes_is_truncated_not_killed(self):
        data = self.run_tool("head -c 50000 /dev/zero | tr '\\0' a; head -c 50000 /dev/zero | tr '\\0' z; "
                             "echo; echo finished >&2")
        self.assertFalse(data["isError"])
        expected = ("exit_code: 0\nstdout:\n" + "a" * 15000 + "\n[... 70001 bytes elided ...]\n"
                    + "z" * 14999 + "\nstderr:\nfinished\n")
        self.assertEqual(data["output"], expected)

    def test_d18_truncation_cuts_on_utf8_boundaries_and_keeps_30000_bytes_whole(self):
        capture = tiny_agent._Capture()
        capture.add(("a" + "é" * 20000 + "b").encode())  # both cuts fall inside a character
        text = capture.text()
        self.assertNotIn("�", text)
        head, tail = text.split("\n[... ")[0], text.split(" bytes elided ...]\n")[1]
        elided = int(text.split("[... ")[1].split(" ")[0])
        self.assertEqual(len(head.encode()) + len(tail.encode()) + elided, 40002)
        self.assertLessEqual(len(head.encode()), 15000)
        self.assertLessEqual(len(tail.encode()), 15000)
        whole = tiny_agent._Capture()
        for _ in range(3):
            whole.add(b"x" * 10000)
        self.assertEqual(whole.text(), "x" * 30000)

    def test_d19_bash_cwd_and_env_are_applied(self):
        os.environ["TA_V2_INHERITED"] = "inherited"
        self.addCleanup(os.environ.pop, "TA_V2_INHERITED")
        data = self.run_tool("pwd -P; echo $TA_V2_ADDED $TA_V2_INHERITED", cwd=self.directory,
                             env={"TA_V2_ADDED": "added"})
        self.assertEqual(data["output"],
                         f"exit_code: 0\nstdout:\n{os.path.realpath(self.directory)}\nadded inherited\nstderr:\n")

    def test_d20_denial_with_a_reason_is_a_tool_error_the_model_sees(self):
        marker = Path(self.directory) / "ran"
        asked = []

        def approve(command):
            asked.append(command)
            return "use ls instead"

        data = self.run_tool(f"touch {marker}", approve=approve)
        self.assertEqual(data, {**data, "output": "Rejected by user: use ls instead", "isError": True})
        self.assertEqual(asked, [f"touch {marker}"])
        self.assertFalse(marker.exists())
        self.assertEqual(self.run_tool("echo hi", approve=lambda command: False)["output"], "Rejected by user")
        self.assertIn("stdout:\nhi\n", self.run_tool("echo hi", approve=lambda command: True)["output"])

    def test_d20_the_blacklist_applies_before_and_despite_approval(self):
        asked = []
        data = self.run_tool("rm -rf /tmp/never", approve=lambda command: asked.append(command) or True)
        self.assertIn("blocked: 'rm' is denied", data["output"])
        self.assertTrue(data["isError"])
        self.assertEqual(asked, [])

    def test_d20_interactive_answers_allow_all_and_allow_patterns(self):
        answers = ["y", "n", "", "too risky", "a"]
        asked = []

        def ask(command):
            asked.append(command)
            return answers.pop(0)

        approve = tiny_agent.command_approver(["git status.*"], ask)
        self.assertIs(approve("git status --short"), True)  # --allow, never asked
        self.assertEqual(asked, [])
        self.assertEqual([approve(f"c{index}") for index in range(5)], [True, False, False, "too risky", True])
        self.assertIs(approve("anything afterwards"), True)  # allow all, for the rest of the session
        self.assertEqual(len(asked), 5)
        unattended = tiny_agent.command_approver(["echo .*"])
        self.assertIs(unattended("echo ok"), True)
        self.assertIn("--yolo", unattended("ls"))


@POSIX
class CliTests(TempCase):
    def cli(self, arguments, turns, **environ):
        self.provider = ScriptedProvider(turns)
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = tiny_agent.main(arguments, provider=self.provider,
                                   environ={"TINYAGENT_SESSION_DIR": self.directory, **environ})
        return code, output.getvalue(), errors.getvalue()

    def observation(self):
        return self.provider.requests[1]["history"][-1]["output"]

    def test_d20_one_shot_without_yolo_rejects_every_command(self):
        code, _, _ = self.cli(["-p", "task"], [call("bash", command="echo hi"), "done"])
        self.assertEqual(code, 0)
        self.assertEqual(self.observation(), "Rejected by user: one-shot mode runs a command only with --yolo "
                                             "or a matching --allow")

    def test_d20_one_shot_allow_pattern_and_yolo(self):
        self.cli(["-p", "task", "--allow", "echo .*"], [call("bash", command="echo hi"), "done"])
        self.assertIn("stdout:\nhi\n", self.observation())
        self.cli(["--prompt", "task", "--allow", "echo .*", "--allow", "ls"], [call("bash", command="pwd"), "done"])
        self.assertIn("Rejected by user", self.observation())
        self.cli(["-p", "task", "--yolo", "--cwd", self.directory], [call("bash", command="pwd -P"), "done"])
        self.assertIn(os.path.realpath(self.directory), self.observation())
        self.cli(["-p", "task"], [call("bash", command="echo env"), "done"], TINYAGENT_YOLO="1")
        self.assertIn("stdout:\nenv\n", self.observation())

    def test_d20_allow_matches_the_whole_command_and_never_shell_syntax(self):
        marker = os.path.join(self.directory, "out")
        for command in ("echo one; printf no", "echo $(printf no)", "echo one | cat", f"echo one > {marker}",
                        "printf echo one", "echo one\nprintf no", "echo `printf no`", "echo one && printf no",
                        "echo one \\", "echo one\rprintf no", "echo <(printf no)"):
            self.cli(["-p", "task", "--allow", "echo .*"], [call("bash", command=command), "done"])
            self.assertTrue(self.observation().startswith("Rejected by user: one-shot mode"), command)
        self.assertFalse(os.path.exists(marker))
        self.cli(["-p", "task", "--allow", "echo .*"], [call("bash", command="echo one"), "done"])
        self.assertIn("stdout:\none\n", self.observation())
        # Interactively, such a command is not approved by the pattern: the person is asked.
        asked = []
        approve = tiny_agent.command_approver(["echo .*"], lambda command: asked.append(command) or "n")
        self.assertEqual((approve("echo one"), approve("echo one; printf no"), asked),
                         (True, False, ["echo one; printf no"]))

    def test_d21_one_shot_exit_codes_and_streamed_text(self):
        code, output, _ = self.cli(["-p", "task", "--yolo"], ["the answer"])
        self.assertEqual((code, output), (0, "the answer\n"))
        self.assertEqual(self.provider.requests[0]["history"][-1], {"role": "user", "content": "task"})
        code, _, errors = self.cli(["-p", "task", "--yolo"], [ProviderError("provider is down")])
        self.assertEqual(code, 1)
        self.assertIn("provider is down", errors)
        code, _, errors = self.cli(["-p", "task", "--yolo", "--max-steps", "1"],
                                   [call("bash", command="true"), "never"])
        self.assertEqual(code, 1)
        self.assertIn("steps", errors)
        code, _, errors = self.cli(["-p", "task", "--yolo"], [call("bash", command="true"), "never"],
                                   TINYAGENT_MAX_STEPS="1")
        self.assertEqual(code, 1)
        self.assertEqual(len(Session.list(self.directory)), 4)

    def test_d21_one_shot_exit_code_reaches_the_shell(self):
        script = ("import sys, tiny_agent\nfrom tinyagent_testing import ScriptedProvider\n"
                  "from tinyagent import ProviderError\n"
                  "turn = 'fine' if sys.argv[1] == 'ok' else ProviderError('down')\n"
                  "sys.exit(tiny_agent.main(['-p', 'task'], provider=ScriptedProvider([turn])))\n")
        for mode, expected in (("ok", 0), ("fail", 1)):
            result = subprocess.run([sys.executable, "-c", script, mode], cwd=self.directory, capture_output=True,
                                    env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path),
                                         "TINYAGENT_SESSION_DIR": self.directory}, timeout=30)
            self.assertEqual(result.returncode, expected, result.stderr)

    def test_d20_interactive_cli_asks_before_each_command(self):
        answers = iter(["run the task", "no thanks", EOFError()])

        def read(prompt=""):
            answer = next(answers)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        original = tiny_agent.input if hasattr(tiny_agent, "input") else None
        tiny_agent.input = read
        try:
            code, _, _ = self.cli([], [call("bash", command="echo hi"), "done"])
        finally:
            if original is None:
                del tiny_agent.input
        self.assertEqual(code, 0)
        self.assertEqual(self.observation(), "Rejected by user: no thanks")


class _Gate:
    """A provider that calls a tool, then streams A and B, waits, and finishes with C."""

    def __init__(self):
        self.streaming, self.release, self.cancelled = threading.Event(), threading.Event(), threading.Event()

    def stream(self, model, instructions, history, tools, cancellation):
        if history[-1]["role"] == "user" and history[-1]["content"] == "tool first":
            yield from ()
            content = [{"type": "tool_call", "callId": "c1", "name": "effect", "arguments": {}}]
            return ProviderTurn("", {"role": "assistant", "content": content}, [], [ToolCall("c1", "effect", {})])
        yield ProviderEvent("text_delta", "A")
        yield ProviderEvent("text_delta", "B")
        self.streaming.set()
        while not self.release.wait(0.01):
            if cancellation.cancelled:
                self.cancelled.set()
                cancellation.raise_if_cancelled()
        yield ProviderEvent("text_delta", "C")
        return ProviderTurn("ABC", {"role": "assistant", "content": [{"type": "text", "text": "ABC"}]}, [], [])


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.gate = _Gate()
        self.server = serve(Agent(model="offline", provider=self.gate, tools=[tool()]), port=0)
        self.addCleanup(self.server.close)
        self.addCleanup(self.gate.release.set)
        self.port = int(self.server.url.rsplit(":", 1)[1])
        self.session = self.json("POST", "/sessions")["sessionId"]

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        payload = None if body is None else json.dumps(body)
        connection.request(method, path, payload, {"Content-Type": "application/json"} if payload else {})
        return connection, connection.getresponse()

    def json(self, method, path, body=None):
        return json.loads(self.request(method, path, body)[1].read())

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            self.assertLess(time.monotonic(), deadline, "condition did not become true")
            time.sleep(0.01)

    def start_and_disconnect(self, body):
        connection, response = self.request("POST", f"/sessions/{self.session}/prompts", body)
        self.assertEqual(json.loads(response.readline())["type"], "agent_start")
        self.assertTrue(self.gate.streaming.wait(5))
        response.close()
        connection.close()

    def test_d22_detached_run_survives_a_disconnect_and_commits(self):
        self.start_and_disconnect({"message": "go", "detach": True, "messageId": "m1"})
        time.sleep(0.2)  # long enough for a disconnect to have cancelled it
        self.assertFalse(self.gate.cancelled.is_set())
        self.gate.release.set()
        self.wait_for(lambda: len(self.json("GET", f"/sessions/{self.session}")["history"]) == 2)
        events = self.json("GET", f"/sessions/{self.session}/events")["events"]
        self.assertEqual([event["type"] for event in events], ["agent_start", "text_delta", "agent_end"])
        _, again = self.request("POST", f"/sessions/{self.session}/prompts", {"message": "go", "messageId": "m1"})
        self.assertIs(json.loads(again.read().splitlines()[-1])["repeated"], True)

    def test_d22_without_detach_the_same_disconnect_cancels_the_run(self):
        self.start_and_disconnect({"message": "go"})
        self.assertTrue(self.gate.cancelled.wait(5))
        self.wait_for(lambda: self.json("POST", f"/sessions/{self.session}/cancel")["accepted"] is False)
        page = self.json("GET", f"/sessions/{self.session}?view=records")
        self.assertEqual(page["records"][-1]["type"], "run_failed")
        self.assertEqual(self.json("GET", f"/sessions/{self.session}")["history"], [])

    def test_d22_a_detached_run_is_cancelled_by_cancel(self):
        self.start_and_disconnect({"message": "go", "detach": True})
        self.assertTrue(self.json("POST", f"/sessions/{self.session}/cancel")["accepted"])
        self.assertTrue(self.gate.cancelled.wait(5))

    def test_d23_follow_joined_mid_run_replays_buffers_and_follows_without_duplicates(self):
        _, earlier = self.request("POST", f"/sessions/{self.session}/prompts", {"message": "tool first"})
        self.gate.release.set()
        first_run = [json.loads(line) for line in earlier.read().splitlines()]
        self.assertEqual(first_run[-1]["status"], "completed")
        self.wait_for(lambda: self.json("POST", f"/sessions/{self.session}/cancel")["accepted"] is False)
        self.gate.release.clear()
        self.gate.streaming.clear()
        self.start_and_disconnect({"message": "go", "detach": True})
        _, following = self.request("GET", f"/sessions/{self.session}/events?after=0&follow=1")
        lines = []
        for _ in range(8):  # everything available before the provider is released
            lines.append(json.loads(following.readline()))
        self.gate.release.set()
        lines += [json.loads(line) for line in following.read().splitlines()]
        self.assertEqual([(event["type"], event.get("delta")) for event in lines], [
            ("agent_start", None), ("tool_execution_start", None), ("tool_execution_end", None),
            ("text_delta", "ABC"), ("agent_end", None),          # the earlier run, replayed
            ("agent_start", None),                               # the active run, replayed
            ("text_delta", "A"), ("text_delta", "B"),            # buffered: the turn in progress
            ("text_delta", "C"), ("agent_end", None)])           # live
        self.assertEqual(["cursor" in event for event in lines], [True] * 6 + [False] * 3 + [True])
        cursors = [event["cursor"] for event in lines[:6]]
        self.assertEqual(cursors, sorted(set(cursors)))
        self.assertEqual(lines[-1]["status"], "completed")
        self.assertEqual(len({event["runId"] for event in lines}), 2)
        # A later reader starting from a cursor gets only what follows it, and no run is active now.
        _, later = self.request("GET", f"/sessions/{self.session}/events?after={cursors[4]}&follow=1")
        self.assertEqual([json.loads(line)["type"] for line in later.read().splitlines()],
                         ["agent_start", "text_delta", "agent_end"])
        page = self.json("GET", f"/sessions/{self.session}/events?after={cursors[4]}&limit=2")
        self.assertEqual(([event["type"] for event in page["events"]], page["nextAfter"]),
                         (["agent_start", "text_delta"], page["events"][-1]["cursor"]))

    def test_d23_resume_route_and_its_errors(self):
        status = self.request("POST", f"/sessions/{self.session}/resume")[1]
        self.assertEqual((status.status, json.loads(status.read())["error"]["code"]), (409, "nothing_to_resume"))
        self.assertIs(self.json("GET", f"/sessions/{self.session}")["resumable"], False)


class WireTests(unittest.TestCase):
    """Part C over RPC and HTTP, on one stored session with an open run."""

    def setUp(self):
        self.storage = MemoryStorage()
        self.provider = ScriptedProvider(["resumed answer", "next answer"])
        self.agent = Agent(model="offline", provider=self.provider)
        session = Session(self.agent, storage=self.storage)
        session._append({"type": "run_start", "runId": "r_open", "messageId": "m1"})
        session._append({"type": "item", "item": {"role": "user", "content": "left open"}})
        self.id = session.id

    def test_d24_rpc_version_2_commands(self):
        emitted = []
        rpc = tiny_agent.RpcSession(self.agent, emitted.append, storage=self.storage, session_id=self.id)
        self.addCleanup(rpc.close)

        def command(**request):
            del emitted[:]
            rpc.handle({"id": "1", **request})
            self.assertTrue(rpc.wait_idle(5))
            return emitted[0].get("data", emitted[0].get("error")), emitted[1:]

        state, _ = command(type="get_state")
        self.assertEqual((state["protocolVersion"], state["resumable"], state["usage"]), (2, True, None))
        error, _ = command(type="prompt", message="new work")
        self.assertEqual(error["code"], "resume_required")
        data, events = command(type="resume_run")
        self.assertEqual(data, {"runId": "r_open"})
        self.assertEqual([event["type"] for event in events], ["agent_start", "text_delta", "agent_end"])
        self.assertEqual((events[0]["resumed"], events[0]["runId"]), (True, "r_open"))
        error, _ = command(type="resume_run")
        self.assertEqual(error["code"], "nothing_to_resume")
        data, events = command(type="prompt", message="left open", messageId="m1")
        self.assertEqual(data, {"runId": "r_open", "repeated": True})
        self.assertEqual((events[-1]["repeated"], events[-1]["text"]), (True, "resumed answer"))
        data, events = command(type="prompt", message="next", messageId="m2")
        self.assertIs(data["repeated"], False)
        self.assertEqual(events[-1]["usage"], {"inputTokens": 0, "outputTokens": 0, "requests": 1})
        state, _ = command(type="get_state")
        self.assertEqual((state["resumable"], state["usage"]["requests"], state["messageCount"]), (False, 1, 4))
        page, _ = command(type="get_events", after=0, limit=2)
        self.assertEqual(([event["type"] for event in page["events"]], page["nextAfter"]),
                         (["agent_start", "text_delta"], page["events"][-1]["cursor"]))
        rest, _ = command(type="get_events", after=page["nextAfter"])
        self.assertEqual(([event["type"] for event in rest["events"]], rest["nextAfter"]),
                         (["agent_end", "agent_start", "text_delta", "agent_end"], None))
        self.assertEqual(len(self.provider.requests), 2)

    def test_d24_http_version_2_routes(self):
        server = serve(self.agent, port=0, storage=self.storage)
        self.addCleanup(server.close)

        def request(method, path, body=None):
            connection = http.client.HTTPConnection("127.0.0.1", int(server.url.rsplit(":", 1)[1]), timeout=10)
            self.addCleanup(connection.close)
            connection.request(method, path, None if body is None else json.dumps(body),
                               {"Content-Type": "application/json"} if body is not None else {})
            response = connection.getresponse()
            lines = [json.loads(line) for line in response.read().splitlines()]
            return response.status, lines

        self.assertEqual(request("POST", "/sessions", {"sessionId": self.id})[0], 201)
        self.assertIs(request("GET", f"/sessions/{self.id}")[1][0]["resumable"], True)
        status, body = request("POST", f"/sessions/{self.id}/prompts", {"message": "new work"})
        self.assertEqual((status, body[0]["error"]["code"]), (409, "resume_required"))
        status, events = request("POST", f"/sessions/{self.id}/resume", {})
        self.assertEqual((status, [event["type"] for event in events]),
                         (200, ["agent_start", "text_delta", "agent_end"]))
        self.assertEqual((events[0]["resumed"], events[0]["runId"]), (True, "r_open"))
        status, body = request("POST", f"/sessions/{self.id}/resume", {"detach": True})
        self.assertEqual((status, body[0]["error"]["code"]), (409, "nothing_to_resume"))
        status, events = request("POST", f"/sessions/{self.id}/prompts", {"message": "x", "messageId": "m1"})
        self.assertEqual((events[-1]["repeated"], events[-1]["runId"]), (True, "r_open"))
        status, body = request("GET", f"/sessions/{self.id}/events?after=0&limit=100")
        self.assertEqual(([event["type"] for event in body[0]["events"]], body[0]["nextAfter"]),
                         (["agent_start", "text_delta", "agent_end"], None))
        self.assertEqual(request("GET", f"/sessions/{self.id}/events?after=-1")[0], 400)
        # Another writer appends behind the server's back: the next prompt is fenced before it streams.
        self.storage.append(self.id, len(self.storage.load(self.id)), {"type": "checkpoint"})
        status, body = request("POST", f"/sessions/{self.id}/prompts", {"message": "late"})
        self.assertEqual((status, body[0]["error"]["code"]), (409, "session_fenced"))


if __name__ == "__main__":
    unittest.main()
