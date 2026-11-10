"""Durability tests exercise the public Session with offline providers."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tinyagent import (Agent, Session, ContextBudget, SessionBusyError,
                       SessionCorruptionError, RecoveryRequiredError,
                       CancellationToken, CancelledError, ProviderError,
                       ProviderEvent, ProviderTurn, Tool, ToolCall)
from tinyagent_rpc import RpcSession
from tinyagent_storage import Journal
from http_fixture import HttpFixture


class Provider:
    def __init__(self):
        self.requests = []
        self.summary = "The user needs a report. Continue the report."

    def stream(self, model, instructions, history, tools, cancellation):
        self.requests.append((instructions, history, list(tools)))
        text = self.summary if instructions.startswith("Summarize") else "answer " * 100
        yield ProviderEvent("text_delta", text)
        return ProviderTurn(text, {"role": "assistant", "content": text}, [], [])


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ta-session-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.provider = Provider()
        self.agent = Agent(model="offline", instructions="Keep facts.", provider=self.provider)
        self.budget = ContextBudget(10000, 100, lambda request: len(json.dumps(request)), keep_turns=1)

    def test_reopen_in_fresh_process_and_exclusive_writer(self):
        session = Session(self.agent, directory=self.directory)
        session.run("first")
        self.assertEqual(Session.list(self.directory), [session.id])
        with self.assertRaises(SessionBusyError):
            Session.open(self.agent, self.directory, session.id)
        session.close()
        script = self.directory / "resume.py"
        script.write_text("from tinyagent import Agent, Session\n"
                          "import sys\n"
                          "agent = Agent(model='offline', instructions='Keep facts.', transport=lambda p: [])\n"
                          "with Session.open(agent, sys.argv[1], sys.argv[2]) as s:\n"
                          "    assert len(s.history) == 2\n"
                          "    assert s.history[0]['content'] == 'first'\n")
        subprocess.run([sys.executable, str(script), str(self.directory), session.id], check=True,
                       env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
        with Session.open(self.agent, self.directory, session.id) as resumed:
            resumed.run("second")
            self.assertEqual(len(resumed.history), 4)

    def test_torn_tail_is_removed_but_complete_corruption_is_preserved(self):
        with Session(self.agent, directory=self.directory) as session:
            session.run("first")
        path = self.directory / f"{session.id}.jsonl"
        original = path.read_bytes()
        path.write_bytes(original + b'{"incomplete":')
        with Session.open(self.agent, self.directory, session.id) as resumed:
            self.assertEqual(len(resumed.history), 2)
        self.assertEqual(path.read_bytes(), original)
        path.write_bytes(original + b'{}\n')
        with self.assertRaises(SessionCorruptionError):
            Session.open(self.agent, self.directory, session.id)
        self.assertEqual(path.read_bytes(), original + b'{}\n')

    def test_tool_intent_precedes_execution_and_result_precedes_delivery(self):
        calls = []
        class ToolProvider:
            def stream(self, *args):
                yield from ()
                return ProviderTurn("", {"role": "assistant", "content": []}, [],
                                    [ToolCall("c", "effect", {})])
        def effect(arguments):
            calls.append(True)
            self.assertEqual(session.records[-1]["type"], "tool_intent")
            return "effect happened"
        agent = Agent(model="offline", provider=ToolProvider(),
                      tools=[Tool("effect", "Effect", {"type": "object"}, effect)])
        session = Session(agent, directory=self.directory)
        failure = RuntimeError("consumer failed")
        def consume(event):
            if event.type == "tool_execution_end":
                self.assertEqual(session.records[-1]["item"]["output"], "effect happened")
                raise failure
        with self.assertRaises(RuntimeError) as raised:
            session.run("go", on_event=consume)
        self.assertIs(raised.exception, failure)
        self.assertEqual(session.history, [])
        session.close()
        with Session.open(agent, self.directory, session.id) as resumed:
            self.assertTrue(resumed.recovery_required)
            with self.assertRaises(RecoveryRequiredError):
                resumed.run("retry")
            self.assertEqual(calls, [True])
            resumed.reconcile("Inspected the effect. It completed; do not repeat it.")
            self.assertFalse(resumed.recovery_required)

    def test_terminal_cancellation_does_not_commit(self):
        token = CancellationToken()
        with Session(self.agent, directory=self.directory) as session:
            with self.assertRaises(CancelledError):
                session.run("go", cancellation=token,
                            on_event=lambda event: token.cancel() if event.type == "agent_end" else None)
            self.assertEqual(session.history, [])
            self.assertEqual(session.records[-1]["type"], "run_failed")

    def test_compaction_preserves_raw_history_and_resumes_incrementally(self):
        with Session(self.agent, directory=self.directory, budget=self.budget) as session:
            for prompt in ("first", "second", "third"):
                session.run(prompt)
            original = session.history
            self.assertTrue(session.compact())
            self.assertEqual(session.history, original)
            checkpoint = session.records[-1]
            self.assertEqual(checkpoint["covered"], 4)
        with Session.open(self.agent, self.directory, session.id, budget=self.budget) as resumed:
            resumed.run("fourth")
            self.assertIn(self.provider.summary, self.provider.requests[-1][1][0]["content"])
            self.assertTrue(resumed.compact())
            material = json.loads(self.provider.requests[-1][1][0]["content"])
            self.assertEqual(material["previousSummary"], self.provider.summary)
            self.assertEqual(material["transcript"], original[4:])
            self.assertEqual(self.provider.requests[-1][2], [])

    def test_invalid_or_cancelled_summary_installs_nothing(self):
        with Session(self.agent, directory=self.directory, budget=self.budget) as session:
            session.run("one")
            session.run("two")
            before = session.records
            self.provider.summary = " "
            with self.assertRaisesRegex(ValueError, "nonempty"):
                session.compact()
            self.assertEqual(session.records, before)
            token = CancellationToken()
            token.cancel()
            with self.assertRaises(CancelledError):
                session.compact(cancellation=token)
            self.assertEqual(session.records, before)

    def test_failed_journal_write_prevents_tool_execution(self):
        calls = []
        class ToolProvider:
            def stream(self, *args):
                yield from ()
                return ProviderTurn("", {"role": "assistant", "content": []}, [],
                                    [ToolCall("c", "effect", {})])
        agent = Agent(model="offline", provider=ToolProvider(),
                      tools=[Tool("effect", "Effect", {"type": "object"}, lambda args: calls.append(True))])
        with Session(agent, directory=self.directory) as session:
            def consume(event):
                if event.type == "agent_start":
                    self.patcher = patch("tinyagent_storage.os.fsync", side_effect=OSError("disk full"))
                    self.patcher.start()
            try:
                with self.assertRaisesRegex(OSError, "disk full"):
                    session.run("go", on_event=consume)
            finally:
                self.patcher.stop()
            self.assertEqual(calls, [])
            with self.assertRaisesRegex(RuntimeError, "failed"):
                session.run("again")

    def test_automatic_compaction_has_ordered_events_and_keeps_prompt(self):
        budget = ContextBudget(2500, 100, lambda request: len(json.dumps(request)), keep_turns=1)
        with Session(self.agent, budget=budget) as session:
            events = []
            for index in range(5):
                session.run(f"prompt {index}", on_event=events.append)
            self.assertTrue(any(event.type == "compaction_start" for event in events))
            self.assertEqual(len(session.history), 10)
            self.assertEqual(self.provider.requests[-1][1][-2]["content"], "prompt 4")
            for run_id in {event.run_id for event in events}:
                run = [event for event in events if event.run_id == run_id]
                self.assertEqual([event.sequence for event in run], list(range(1, len(run) + 1)))

    def test_oversized_pending_prompt_does_not_activate_a_checkpoint(self):
        with Session(self.agent, budget=self.budget) as session:
            session.run("one")
            session.run("two")
            with self.assertRaisesRegex(ValueError, "still exceeds"):
                session.run("x" * 10000)
            self.assertFalse(any(record["type"] == "checkpoint" for record in session.records))
            self.assertEqual(len(session.history), 4)

    def test_checkpoint_write_failure_retains_previous_context(self):
        with Session(self.agent, directory=self.directory, budget=self.budget) as session:
            session.run("one")
            session.run("two")
            before = session.records
            with patch("tinyagent_storage.os.fsync", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    session.compact()
            self.assertEqual(session.records, before)

    def test_process_crash_releases_lock_and_requires_reconciliation(self):
        script = self.directory / "crash.py"
        script.write_text("from tinyagent import Agent, Session, Tool\n"
                          "import os, sys\n"
                          "tool = Tool('effect', 'Effect', {'type': 'object'}, lambda args: os._exit(29))\n"
                          "agent = Agent(model='offline', tools=[tool], transport=lambda p: "
                          "[{'type':'function_call','call_id':'c','name':'effect','arguments':'{}'}])\n"
                          "Session(agent, directory=sys.argv[1], session_id='s_crashed').run('go')\n")
        result = subprocess.run([sys.executable, str(script), str(self.directory)],
                                env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
        self.assertEqual(result.returncode, 29)
        agent = Agent(model="offline", provider=self.provider,
                      tools=[Tool("effect", "Effect", {"type": "object"}, lambda args: self.fail("replayed"))])
        with Session.open(agent, self.directory, "s_crashed") as session:
            self.assertTrue(session.recovery_required)
            self.assertEqual(session.records[-1]["type"], "tool_intent")
            with self.assertRaises(RecoveryRequiredError):
                session.run("retry")

    def test_second_process_cannot_write_a_session_another_process_holds(self):
        script = self.directory / "hold.py"
        script.write_text("from tinyagent import Agent, Session\n"
                          "import sys\n"
                          "agent = Agent(model='offline', instructions='Keep facts.', transport=lambda p: [])\n"
                          "with Session(agent, directory=sys.argv[1], session_id='s_held'):\n"
                          "    print('ready', flush=True)\n"
                          "    sys.stdin.read()\n")
        holder = subprocess.Popen([sys.executable, str(script), str(self.directory)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
        try:
            self.assertEqual(holder.stdout.readline(), b"ready\n")
            with self.assertRaises(SessionBusyError):
                Session.open(self.agent, self.directory, "s_held")
        finally:
            holder.communicate(b"", timeout=10)
        self.assertEqual(holder.returncode, 0)
        with Session.open(self.agent, self.directory, "s_held") as session:
            self.assertEqual(session.history, [])

    def test_missing_session_is_reported_without_creating_files(self):
        with self.assertRaises(FileNotFoundError):
            Session.open(self.agent, self.directory, "s_missing")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_run_interrupted_without_a_tool_is_not_committed_and_may_continue(self):
        script = self.directory / "killed.py"
        script.write_text("from tinyagent import Agent, Session\n"
                          "import os, sys\n"
                          "agent = Agent(model='offline', instructions='Keep facts.', "
                          "transport=lambda p: os._exit(31))\n"
                          "Session(agent, directory=sys.argv[1], session_id='s_killed').run('lost')\n")
        result = subprocess.run([sys.executable, str(script), str(self.directory)],
                                env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
        self.assertEqual(result.returncode, 31)
        with Session.open(self.agent, self.directory, "s_killed") as session:
            self.assertEqual(session.records[-1]["item"]["content"], "lost")
            self.assertEqual(session.history, [])
            self.assertFalse(session.recovery_required)
            session.run("retry")
            self.assertEqual([item["role"] for item in session.history], ["user", "assistant"])
            self.assertEqual(session.history[0]["content"], "retry")

    def test_invalid_checkpoint_is_ignored_on_resume_and_earlier_context_is_kept(self):
        with Session(self.agent, directory=self.directory, budget=self.budget) as session:
            for prompt in ("first", "second", "third"):
                session.run(prompt)
            self.assertTrue(session.compact())
        journal = Journal(self.directory / f"{session.id}.jsonl", create=False)
        try:
            journal.append({"type": "checkpoint", "covered": 6, "summary": "forged",
                            "historyHash": "does not match"})
        finally:
            journal.close()
        with Session.open(self.agent, self.directory, session.id, budget=self.budget) as resumed:
            self.assertEqual(len(resumed.recovery_notes), 1)
            self.assertEqual(resumed.inspect()["recoveryNotes"], resumed.recovery_notes)
            self.assertEqual(len(resumed.history), 6)
            resumed.run("fourth")
            context = self.provider.requests[-1][1]
            self.assertEqual(context[0]["content"], "Earlier conversation summary:\n" + self.provider.summary)
            self.assertEqual([item["content"] for item in context if item["role"] == "user"][1:],
                             ["third", "fourth"])

    def test_summary_cancelled_or_failed_midway_installs_nothing_and_can_be_retried(self):
        token = CancellationToken()

        class Summaries(Provider):
            failure = None

            def stream(self, model, instructions, history, tools, cancellation):
                if instructions.startswith("Summarize"):
                    if self.failure:
                        raise self.failure
                    yield ProviderEvent("text_delta", "partial")
                    token.cancel()
                return (yield from Provider.stream(self, model, instructions, history, tools, cancellation))

        provider = Summaries()
        agent = Agent(model="offline", instructions="Keep facts.", provider=provider)
        with Session(agent, directory=self.directory, budget=self.budget) as session:
            session.run("one")
            session.run("two")
            before = session.records
            events = []
            with self.assertRaises(CancelledError):
                session.compact(cancellation=token, on_event=events.append)
            self.assertEqual([(event.type, event.data.get("status")) for event in events],
                             [("compaction_start", None), ("compaction_end", "aborted")])
            self.assertEqual(session.records, before)
        budget = ContextBudget(2500, 100, lambda request: len(json.dumps(request)), keep_turns=1)
        provider.failure = ProviderError("summary unavailable")
        with Session(agent, budget=budget) as session:
            events = []
            with self.assertRaisesRegex(ProviderError, "summary unavailable"):
                for index in range(5):
                    session.run(f"prompt {index}", on_event=events.append)
            self.assertEqual([(event.type, event.data.get("status")) for event in events[-2:]],
                             [("compaction_end", "failed"), ("agent_end", "failed")])
            self.assertEqual(session.records[-1]["type"], "run_failed")
            self.assertFalse(any(record["type"] == "checkpoint" for record in session.records))
            completed = sum(record["type"] == "run_commit" for record in session.records)
            self.assertEqual(len(session.history), 2 * completed)
            provider.failure = None
            session.run("again")
            self.assertEqual(session.records[-1]["type"], "run_commit")
            self.assertTrue(any(record["type"] == "checkpoint" for record in session.records))

    def test_busy_session_and_changed_configuration_are_rejected(self):
        with Session(self.agent) as session:
            stream = session.stream("go")
            next(stream)
            with self.assertRaises(SessionBusyError):
                session.run("concurrent")
            stream.close()
            self.agent.instructions = "changed"
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                session.run("go")

    def test_rpc_resume_compaction_inspection_and_close_release_writer(self):
        emitted = []
        rpc = RpcSession(self.agent, emitted.append, directory=str(self.directory), budget=self.budget)
        identifier = rpc.session.id
        for index in range(3):
            rpc.handle({"id": str(index), "type": "prompt", "message": f"turn {index}"})
            self.assertTrue(rpc.wait_idle(2))
        rpc.handle({"id": "compact", "type": "compact"})
        self.assertTrue(rpc.wait_idle(2))
        self.assertEqual(emitted[-1]["type"], "compaction_end")
        self.assertEqual(emitted[-1]["status"], "completed")
        rpc.handle({"id": "history", "type": "get_history", "limit": 1})
        self.assertEqual(emitted[-1]["data"]["nextOffset"], 1)
        rpc.close()
        resumed = RpcSession(self.agent, emitted.append, directory=str(self.directory),
                             session_id=identifier, budget=self.budget)
        try:
            resumed.handle({"id": "go", "type": "prompt", "message": "continue"})
            self.assertTrue(resumed.wait_idle(2))
            self.assertIn("Earlier conversation summary", self.provider.requests[-1][1][0]["content"])
        finally:
            resumed.close()

    def test_http_close_preserves_sessions_and_resume_restores_compaction(self):
        fixture = HttpFixture(self.agent, directory=str(self.directory), budget=self.budget)
        identifier = fixture.session()
        try:
            for prompt in ("first", "second", "third"):
                fixture.prompt(identifier, prompt).read()
                fixture.wait_idle(identifier)
            response = fixture.call("POST", f"/sessions/{identifier}/compact")
            self.assertEqual(response.status, 200)
            events = [json.loads(line) for line in response.read().splitlines()]
            self.assertEqual(events[-1]["type"], "compaction_end")
            self.assertEqual(events[-1]["status"], "completed")
            fixture.wait_idle(identifier)
        finally:
            fixture.close()
        resumed = HttpFixture(self.agent, directory=str(self.directory), budget=self.budget)
        try:
            listed = json.loads(resumed.call("GET", "/sessions").read())
            self.assertEqual(listed["sessionIds"], [identifier])
            opened = resumed.call("POST", "/sessions", {"sessionId": identifier})
            self.assertEqual(opened.status, 201)
            opened.read()
            page = json.loads(resumed.call("GET", f"/sessions/{identifier}?limit=1").read())
            self.assertEqual(page["nextOffset"], 1)
            resumed.prompt(identifier, "continue").read()
            self.assertIn("Earlier conversation summary", self.provider.requests[-1][1][0]["content"])
            resumed.wait_idle(identifier)
            removed = resumed.call("DELETE", f"/sessions/{identifier}")
            self.assertEqual(removed.status, 204)
            removed.read()
            self.assertEqual(Session.list(self.directory), [identifier])
            with Session.open(self.agent, self.directory, identifier) as direct:
                self.assertEqual(len(direct.history), 8)
        finally:
            resumed.close()


if __name__ == "__main__":
    unittest.main()
