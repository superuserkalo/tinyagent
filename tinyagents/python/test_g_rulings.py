"""Rulings G11, G16, G17, G19 and G20 to G36, one test or more per ruling, named by ruling."""

import http.client
import json
import subprocess
import sys
import threading
import time
import unittest

import tiny_agent
from test_v2 import call, tool
from test_v2_surface import ASSISTANT, INTENT, open_run
from tinyagent import (Agent, CancellationToken, CancelledError, HookError, MemoryStorage, ProviderError,
                       ProviderEvent, ProviderTurn, ScriptedProvider, Session, SessionBusyError,
                       SessionFailedError, SessionFencedError, StorageError, Tool, pause, serve)

SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}


def types(events):
    return [event.type for event in events]


class Http:
    """A real loopback server and a one-request client."""

    def __init__(self, case, agent, **options):
        self.server = serve(agent, port=0, **options)
        case.addCleanup(self.server.close)
        self.port = int(self.server.url.rsplit(":", 1)[1])

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, None if body is None else json.dumps(body),
                               {**({"Content-Type": "application/json"} if body is not None else {}),
                                **(headers or {})})
            response = connection.getresponse()
            return response.status, [json.loads(line) for line in response.read().splitlines() if line]
        finally:
            connection.close()


class FailingStorage(MemoryStorage):
    """Fails the append of one record type, or returns a wrong sequence."""

    def __init__(self, kind=None, error=None, wrong=False):
        super().__init__()
        self.kind, self.error, self.wrong = kind, error, wrong

    def append(self, session_id, expected, record):
        if record["type"] == self.kind and self.error:
            raise self.error
        sequence = super().append(session_id, expected, record)
        return sequence + 5 if self.wrong and record["type"] == self.kind else sequence


class RulingTests(unittest.TestCase):
    def test_g11_a_store_that_returns_the_wrong_sequence_is_a_storage_failure(self):
        session = Session(Agent(model="m", provider=ScriptedProvider(["hi"])),
                          storage=FailingStorage("run_start", wrong=True))
        with self.assertRaisesRegex(StorageError, "wrong sequence"):
            session.run("go")
        with self.assertRaises(SessionFailedError):
            session.run("again")

    def test_g16_host_and_origin_are_checked_on_every_route(self):
        agent = Agent(model="m", provider=ScriptedProvider(["hi"]))
        server = Http(self, agent, token="secret", allowed_hosts=["agent.internal"],
                      allowed_origins=["https://app.example"])
        auth = {"Authorization": "Bearer secret"}
        for host in (f"127.0.0.1:{server.port}", f"localhost:{server.port}", "LOCALHOST", "[::1]:9", "agent.internal"):
            self.assertEqual(server.request("GET", "/health", headers={"Host": host, **auth})[0], 200, host)
        for path, method in (("/health", "GET"), ("/sessions", "POST"), ("/sessions/s_x/prompts", "POST")):
            for host in ("evil.example", f"evil.example:{server.port}", "127.0.0.1.evil.example", "10.0.0.5"):
                status, body = server.request(method, path, headers={"Host": host, **auth})
                self.assertEqual((status, body[0]["error"]["code"]), (421, "misdirected_request"), (path, host))
            status, body = server.request(method, path, headers={"Origin": "https://evil.example", **auth})
            self.assertEqual((status, body[0]["error"]["code"]), (403, "origin_not_allowed"), path)
        self.assertEqual(server.request("GET", "/health", headers={"Origin": "https://app.example", **auth})[0], 200)
        # The checks do not depend on the token: they come first, with or without one.
        self.assertEqual(server.request("GET", "/health", headers={"Host": "evil.example"})[0], 421)
        open_server = Http(self, agent)
        self.assertEqual(open_server.request("POST", "/sessions", headers={"Origin": "null"})[0], 403)
        self.assertEqual(open_server.request("POST", "/sessions", headers={"Host": "rebound.example"})[0], 421)

    def test_g17_run_options_are_stored_in_run_start_and_reused_by_resume(self):
        storage = MemoryStorage()
        seen = []
        provider = ScriptedProvider([call("ask", identifier="q1"), call("final_output", n=3)])
        agent = Agent(model="default", instructions="Default.", provider=provider,
                      tools=[Tool("ask", "Ask.", {"type": "object"},
                                  lambda arguments, context: seen.append(context.context) or pause())])
        session = Session(agent, storage=storage)
        options = {"model": "special", "instructions": "For this run.", "extra": {"temperature": 0},
                   "output_schema": SCHEMA, "max_steps": 2}
        self.assertEqual(session.run("go", context={"secret": "host-only"}, **options).status, "paused")
        self.assertEqual(session.records[1]["options"], options)
        self.assertNotIn("host-only", json.dumps(storage.load(session.id)))
        reopened = Session.open(agent, storage, session.id)
        result = reopened.resume(result="yes")
        self.assertEqual(result.value, {"n": 3})
        request = provider.requests[1]
        self.assertEqual((request["model"], request["instructions"], request["extra"], request["tools"]),
                         ("special", "For this run.", {"temperature": 0}, ["ask", "final_output"]))
        # The stored limit applies to the resumed part too.
        provider.turns[:] = [call("ask", identifier="q2"), call("noop"), call("noop"), "never"]
        agent.tools["noop"] = tool("noop")
        limited = Session(agent, storage=storage)
        limited.run("again", max_steps=2)
        with self.assertRaises(tiny_agent.LimitExceededError):
            limited.resume(result="yes")

    def test_g19_replay_of_a_reconciled_run_pairs_every_start_with_an_end(self):
        agent = Agent(model="m", provider=ScriptedProvider(["after"]), tools=[tool()])
        session = open_run(agent, MemoryStorage(), ASSISTANT, INTENT)
        session.on_uncertain = "reconcile"
        session.reconcile("Checked: nothing happened.")
        events = session.events()["events"]
        self.assertEqual([(event["type"], event.get("status")) for event in events],
                         [("agent_start", None), ("tool_execution_start", None), ("tool_execution_end", None),
                          ("agent_end", "failed")])
        self.assertEqual(events[2]["toolCallId"], events[1]["toolCallId"])
        self.assertEqual(events[3]["error"]["code"], "abandoned")

    def test_g20_before_tool_is_not_run_again_for_an_answered_call_and_after_tool_sees_the_answer(self):
        before, after = [], []

        def before_tool(tool_call, context):
            before.append(tool_call.call_id)
            return pause("approve?")

        def after_tool(tool_call, result, context):
            after.append((tool_call.call_id, result["output"]))
            return result["output"].upper()

        provider = ScriptedProvider([call("effect", identifier="c1"), "done"])
        session = Session(Agent(model="m", provider=provider, tools=[tool()], before_tool=before_tool,
                                after_tool=after_tool))
        self.assertEqual(session.run("go").status, "paused")
        result = session.resume(result="approved")
        self.assertEqual((result.status, before, after), ("completed", ["c1"], [("c1", "approved")]))
        self.assertEqual(provider.requests[1]["history"][-1]["output"], "APPROVED")

    def test_g21_a_pause_keeps_live_and_replayed_streams_well_formed(self):
        provider = ScriptedProvider([call("ask", identifier="q1"), "done"])
        session = Session(Agent(model="m", provider=provider,
                                tools=[Tool("ask", "Ask.", {"type": "object"}, lambda arguments: pause("why?"))]))
        first, second = [], []
        session.run("go", on_event=first.append)
        self.assertEqual(types(first), ["agent_start", "tool_execution_start", "tool_execution_end", "agent_end"])
        self.assertEqual({key: first[2].data[key] for key in ("output", "isError", "paused")},
                         {"output": "", "isError": False, "paused": True})
        self.assertEqual(first[3].data["status"], "paused")
        session.resume(result="because", on_event=second.append)
        self.assertEqual(types(second), ["agent_start", "tool_execution_start", "tool_execution_end", "text_delta",
                                         "agent_end"])
        self.assertIs(second[1].data["resumed"], True)
        self.assertEqual((second[2].data["output"], second[2].data.get("paused")), ("because", None))
        replay = session.events()["events"]
        live = [event.as_dict() for event in first + second[1:]]
        self.assertEqual([(event["type"], event["cursor"], event.get("paused"), event.get("resumed"),
                           event.get("status")) for event in replay],
                         [(event["type"], event.get("cursor", replay[index]["cursor"]), event.get("paused"),
                           event.get("resumed"), event.get("status")) for index, event in enumerate(live)])

    def test_g22_only_the_runs_own_cancellation_aborts_it(self):
        def gives_up(arguments):
            raise CancelledError("my own deadline passed")

        provider = ScriptedProvider([call("gives_up"), "carried on"])
        events = []
        result = Agent(model="m", provider=provider, tools=[tool("gives_up", gives_up)]).run("go", on_event=events.append)
        self.assertEqual(result.text, "carried on")
        ended = next(event for event in events if event.type == "tool_execution_end")
        self.assertEqual((ended.data["isError"], ended.data["output"]), (True, "CancelledError: my own deadline passed"))

        class Aborting:
            def stream(self, *arguments):
                raise CancelledError("upstream aborted")
                yield

        events = []
        with self.assertRaises(ProviderError):
            Agent(model="m", provider=Aborting()).run("go", on_event=events.append)
        self.assertEqual((events[-1].data["status"], events[-1].data["error"]["code"]), ("failed", "provider_error"))
        hooked = Agent(model="m", provider=ScriptedProvider([call()]), tools=[tool()],
                       before_tool=lambda *arguments: (_ for _ in ()).throw(CancelledError("hook quit")))
        events = []
        with self.assertRaises(HookError):
            hooked.run("go", on_event=events.append)
        self.assertEqual(events[-1].data["error"]["code"], "hook_error")

    def test_g23_reentrant_calls_fail_fast_with_session_busy(self):
        seen = {}

        def attempt(name, action):
            began = time.monotonic()
            try:
                action()
                seen[name] = "no error"
            except SessionBusyError:
                seen[name] = "busy"
            except Exception as error:
                seen[name] = repr(error)
            self.assertLess(time.monotonic() - began, 0.5, name)

        def everything(where):
            for name, action in (("close", lambda: session.close()), ("abandon", lambda: session.abandon()),
                                 ("run", lambda: session.run("inner")), ("resume", lambda: session.resume()),
                                 ("compact", lambda: session.compact())):
                attempt(f"{where}:{name}", action)

        provider = ScriptedProvider([call("inner"), call("timed"), "done"])
        agent = Agent(model="m", provider=provider,
                      tools=[tool("inner", lambda arguments: everything("handler") or "ok"),
                             tool("timed", lambda arguments: everything("timed handler") or "ok", timeout_seconds=5)],
                      before_tool=lambda tool_call, context: everything("hook"))
        session = Session(agent)
        result = session.run("go", on_event=lambda event: everything("callback") if event.type == "agent_start" else None)
        self.assertEqual(result.text, "done")
        self.assertEqual(len(seen), 20)
        self.assertEqual(set(seen.values()), {"busy"}, seen)
        session.close()

    def test_g24_cancellation_is_bounded_against_host_code_that_never_returns(self):
        forever = threading.Event()
        self.addCleanup(forever.set)

        class Stuck:
            def stream(self, *arguments):
                forever.wait(30)
                yield

        cases = {
            "handler": Agent(model="m", provider=ScriptedProvider([call("stuck")]),
                             tools=[tool("stuck", lambda arguments: forever.wait(30))]),
            "hook": Agent(model="m", provider=ScriptedProvider([call()]), tools=[tool()],
                          before_tool=lambda *arguments: forever.wait(30)),
            "provider": Agent(model="m", provider=Stuck()),
        }
        for name, agent in cases.items():
            session = Session(agent)
            token = CancellationToken()
            events = []
            threading.Timer(0.1, token.cancel).start()
            began = time.monotonic()
            with self.assertRaises(CancelledError):
                session.run("go", cancellation=token, on_event=events.append)
            self.assertLess(time.monotonic() - began, 2.5, name)
            self.assertEqual((events[-1].type, events[-1].data["status"]), ("agent_end", "aborted"), name)
            self.assertEqual(session.records[-1]["status"], "aborted")
            self.assertFalse(session.resumable)
        # close() is bounded the same way, from another thread, while a handler is stuck.
        session = Session(cases["handler"].__class__(model="m", provider=ScriptedProvider([call("stuck")]),
                                                    tools=[tool("stuck", lambda arguments: forever.wait(30))]))
        started = threading.Event()
        worker = threading.Thread(target=lambda: self.assertRaises(CancelledError, session.run, "go",
                                                                   on_event=lambda event: started.set()), daemon=True)
        worker.start()
        self.assertTrue(started.wait(2))
        began = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - began, 3.5)
        worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_g25_a_provider_that_ends_without_a_turn_is_a_provider_error_not_a_loop(self):
        calls = []

        class NoTurn:
            def stream(self, *arguments):
                calls.append(1)
                yield ProviderEvent("text_delta", "partial")

        for provider in (NoTurn(), ScriptedProvider([]).__class__([{"text": "x"}])):
            pass
        events = []
        with self.assertRaisesRegex(ProviderError, "invalid turn"):
            Agent(model="m", provider=NoTurn()).run("go", on_event=events.append)
        self.assertEqual((len(calls), events[-1].data["error"]["code"]), (1, "provider_error"))

        class WrongShape:
            def stream(self, *arguments):
                yield from ()
                return ProviderTurn("x", {"role": "assistant", "content": []}, [], [{"name": "not a ToolCall"}])

        with self.assertRaisesRegex(ProviderError, "invalid turn"):
            Agent(model="m", provider=WrongShape()).run("go")

    def test_g26_replay_returns_content_part_tool_output_as_parts(self):
        parts = [{"type": "text", "text": "chart"}, {"type": "image", "url": "https://example.com/c.png"}]
        session = Session(Agent(model="m", provider=ScriptedProvider([call("chart"), "done"]),
                                tools=[tool("chart", lambda arguments: parts)]))
        session.run("go")
        ended = next(event for event in session.events()["events"] if event["type"] == "tool_execution_end")
        self.assertEqual(ended["output"], parts)

    def test_g27_every_http_refusal_has_a_json_error_body_including_detached(self):
        storage = MemoryStorage()
        agent = Agent(model="m", provider=ScriptedProvider(["hi"]), tools=[tool()])
        identifier = open_run(agent, storage).id
        server = Http(self, agent, storage=storage)
        server.request("POST", "/sessions", {"sessionId": identifier})
        refusals = [
            ("POST", f"/sessions/{identifier}/prompts", {"message": "x", "detach": True}, 409, "resume_required"),
            ("POST", f"/sessions/{identifier}/prompts", {"message": [{"type": "nope"}], "detach": True}, 400,
             "invalid_request"),
            ("POST", f"/sessions/{identifier}/resume", {"detach": True, "result": "not paused"}, 400, "invalid_request"),
            ("POST", f"/sessions/{identifier}/resume", {"error": 5}, 400, "invalid_request"),
            ("POST", "/sessions/s_missing/resume", {"detach": True}, 404, "session_not_found"),
            ("GET", f"/sessions/{identifier}/events?after=x", None, 400, "invalid_request"),
            ("POST", f"/sessions/{identifier}/abandon", {"reason": 5}, 400, "invalid_request"),
            ("GET", "/nowhere", None, 404, "not_found"),
        ]
        for method, path, body, status, code in refusals:
            got, lines = server.request(method, path, body)
            self.assertEqual((got, lines[0]["error"]["code"]), (status, code), path)
            self.assertIn("message", lines[0]["error"])
        # The refused requests left the session usable and not busy.
        self.assertEqual(server.request("POST", f"/sessions/{identifier}/resume", {"detach": True})[1][-1]["text"], "hi")

    def test_g28_storage_failures_are_named_and_the_session_then_answers_session_failed(self):
        for error, code in ((OSError("disk full"), "storage_error"),
                            (SessionFencedError("another writer"), "session_fenced")):
            storage = FailingStorage("tool_result", error)
            agent = Agent(model="m", provider=ScriptedProvider([call(), "never"]), tools=[tool()])
            session = Session(agent, storage=storage)
            events = []
            with self.assertRaises((StorageError, SessionFencedError)):
                session.run("go", on_event=events.append)
            self.assertEqual(events[-1].data["error"], {**events[-1].data["error"], "code": code, "retryable": False})
            for action in (lambda: session.run("again"), session.resume, session.inspect, session.events,
                           session.abandon, lambda: session.reconcile("note")):
                with self.assertRaises((SessionFailedError, ValueError)) as raised:
                    action()
                self.assertNotIsInstance(raised.exception, StorageError)
            with self.assertRaises(SessionFailedError):
                session.run("again")
        storage = FailingStorage("tool_result", OSError("disk full"))
        server = Http(self, Agent(model="m", provider=ScriptedProvider([call(), "never"]), tools=[tool()]),
                      storage=storage)
        identifier = server.request("POST", "/sessions")[1][0]["sessionId"]
        lines = server.request("POST", f"/sessions/{identifier}/prompts", {"message": "go"})[1]
        self.assertEqual((lines[-1]["status"], lines[-1]["error"]["code"]), ("failed", "storage_error"))
        for method, path, body in (("POST", f"/sessions/{identifier}/prompts", {"message": "again"}),
                                   ("POST", f"/sessions/{identifier}/resume", {}),
                                   ("GET", f"/sessions/{identifier}", None),
                                   ("GET", f"/sessions/{identifier}/events", None),
                                   ("POST", f"/sessions/{identifier}/abandon", {})):
            status, body = server.request(method, path, body)
            self.assertEqual((status, body[0]["error"]["code"]), (409, "session_failed"), path)
        emitted = []
        rpc = tiny_agent.RpcSession(Agent(model="m", provider=ScriptedProvider([call(), "never"]), tools=[tool()]),
                                    emitted.append, lambda message: None,
                                    storage=FailingStorage("tool_result", OSError("disk full")))
        rpc.handle({"id": "1", "type": "prompt", "message": "go"})
        self.assertTrue(rpc.wait_idle(5))
        self.assertEqual(emitted[-1]["error"]["code"], "storage_error")
        for command in ({"type": "prompt", "message": "again"}, {"type": "resume_run"}, {"type": "get_history"},
                        {"type": "get_events"}, {"type": "abandon_run"}):
            rpc.handle({"id": "2", **command})
            self.assertEqual(emitted[-1]["error"]["code"], "session_failed", command)

    def test_g29_options_are_validated_at_the_boundary_without_leaving_the_session_busy(self):
        provider = ScriptedProvider(["fine"])
        agent = Agent(model="m", provider=provider)
        session = Session(agent)
        deep = "x"
        for _ in range(40):
            deep = [{"type": "text", "text": "t", "more": deep}]
        bad = [dict(cancellation="not a token"), dict(max_steps=0), dict(max_steps=1.5), dict(max_steps=True),
               dict(max_seconds=float("inf")), dict(max_seconds=float("nan")), dict(max_seconds=86_401),
               dict(max_tokens=-1), dict(extra="no"), dict(output_schema={"type": "string"}), dict(model=5),
               dict(instructions=5), dict(trace="yes"), dict(unknown_option=1), dict(message_id="")]
        for options in bad:
            for runner in (session, agent):
                if runner is agent and "message_id" in options:
                    continue
                with self.assertRaises(ValueError, msg=options):
                    runner.run("go", **options)
        for prompt in (deep, [], [{"type": "text"}], 5, None, [{"type": "image", "url": 5}]):
            with self.assertRaises(ValueError):
                session.run(prompt)
        with self.assertRaises(ValueError):
            session.resume(cancellation=object())
        self.assertEqual(len(session.records), 1)  # nothing was started
        self.assertEqual(session.run("go").text, "fine")  # and the session is not busy
        for seconds in (0, -1, float("inf"), float("nan"), 86_401, "5", True):
            with self.assertRaises(ValueError, msg=seconds):
                tool(timeout_seconds=seconds)
            with self.assertRaises(ValueError, msg=seconds):
                Agent(model="m", provider=provider, max_seconds=seconds)
        self.assertEqual(tool(timeout_seconds=86_400).timeout_seconds, 86_400)

    def test_g30_compaction_events_inside_a_prompt_stream_are_not_terminal(self):
        from tinyagent import ContextBudget

        class Provider:
            def stream(self, model, instructions, history, tools, cancellation, extra=None):
                text = "summary" if instructions.startswith("Summarize") else "answer " * 100
                yield ProviderEvent("text_delta", text)
                return ProviderTurn(text, {"role": "assistant", "content": [{"type": "text", "text": text}]}, [], [])

        budget = ContextBudget(2500, 100, lambda request: len(json.dumps(request)), keep_turns=1)
        agent = Agent(model="m", provider=Provider())
        server = Http(self, agent, storage=MemoryStorage(), budget=budget)
        identifier = server.request("POST", "/sessions")[1][0]["sessionId"]
        emitted = []
        rpc = tiny_agent.RpcSession(agent, emitted.append, lambda message: None, budget=budget)
        seen = {"http": None, "rpc": None}
        for index in range(6):
            lines = server.request("POST", f"/sessions/{identifier}/prompts", {"message": f"prompt {index}"})[1]
            del emitted[:]
            rpc.handle({"id": "1", "type": "prompt", "message": f"prompt {index}"})
            self.assertTrue(rpc.wait_idle(5))
            for name, events in (("http", lines), ("rpc", emitted[1:])):
                kinds = [event["type"] for event in events]
                if "compaction_end" in kinds:
                    seen[name] = kinds
        for name, kinds in seen.items():
            self.assertIsNotNone(kinds, f"{name}: no run compacted")
            self.assertEqual(kinds, ["agent_start", "compaction_start", "compaction_end", "text_delta", "agent_end"], name)
        rpc.close()

    def test_g31_a_stored_session_answers_as_soon_as_it_is_open(self):
        storage = MemoryStorage()
        agent = Agent(model="m", provider=ScriptedProvider(["hi"]))
        session = Session(agent, storage=storage)
        session.run("go")
        emitted = []
        rpc = tiny_agent.RpcSession(agent, emitted.append, storage=storage, session_id=session.id)
        rpc.handle({"id": "1", "type": "get_state"})  # storage is synchronous: loaded before this returns
        self.assertEqual(emitted[0]["data"]["messageCount"], 2)
        rpc.close()

    def test_g32_a_cancel_that_lands_during_an_append_aborts_the_run(self):
        token = CancellationToken()

        class CancelsDuring(MemoryStorage):
            def append(self, session_id, expected, record):
                if record["type"] == self.kind:
                    token.cancel()
                return super().append(session_id, expected, record)

        for kind in ("item", "tool_intent", "tool_result"):
            token = CancellationToken()
            storage = CancelsDuring()
            storage.kind = kind
            ran = []
            session = Session(Agent(model="m", provider=ScriptedProvider([call(), "done"]),
                                    tools=[tool(handler=lambda arguments: ran.append(1) or "ok")]), storage=storage)
            events = []
            with self.assertRaises(CancelledError):
                session.run("go", cancellation=token, on_event=events.append)
            self.assertEqual((events[-1].data["status"], session.records[-1]["status"]), ("aborted", "aborted"), kind)
            starts = [event for event in events if event.type == "tool_execution_start"]
            ends = [event for event in events if event.type == "tool_execution_end"]
            self.assertEqual(len(starts), len(ends), kind)
            self.assertEqual(len(ran), 1 if kind == "tool_result" else 0, kind)

    def test_g33_the_typed_value_is_stored_with_the_result_and_survives_a_crash(self):
        storage = FailingStorage("run_commit", OSError("the process died before the commit"))
        provider = ScriptedProvider([[{"name": "final_output", "id": "f", "arguments": {"n": 7}},
                                      {"name": "effect", "id": "e"}]])
        ran = []
        agent = Agent(model="m", provider=provider, tools=[tool(handler=lambda arguments: ran.append(1) or "ok")])
        session = Session(agent, storage=storage)
        with self.assertRaises(StorageError):
            session.run("count", output_schema=SCHEMA)
        stored = [record for record in storage.load(session.id) if record["type"] == "tool_result"]
        self.assertEqual((stored[0]["value"], stored[1]["item"]["output"]), ({"n": 7}, "not run: the run finished"))
        storage.error = None
        reopened = Session.open(agent, storage, session.id)
        self.assertTrue(reopened.resumable)
        result = reopened.resume()
        self.assertEqual((result.value, result.status, len(provider.requests), ran), ({"n": 7}, "completed", 1, []))
        self.assertEqual(reopened.records[-1]["value"], {"n": 7})
        # Crash right after final_output's own result: the sibling call is still settled, not run.
        storage = FailingStorage("tool_intent", None)
        provider = ScriptedProvider([[{"name": "final_output", "id": "f", "arguments": {"n": 1}},
                                      {"name": "effect", "id": "e"}]])
        agent = Agent(model="m", provider=provider, tools=[tool(handler=lambda arguments: ran.append(1) or "ok")])
        session = Session(agent, storage=storage)
        seen = []

        def die_at_second_intent(record_type):
            seen.append(record_type)
            return OSError("died") if seen.count("tool_intent") == 2 else None

        original = storage.append
        storage.append = lambda session_id, expected, record: (
            (_ for _ in ()).throw(OSError("died")) if record["type"] == "tool_intent" and seen.append(1) is None
            and len(seen) == 2 else original(session_id, expected, record))
        with self.assertRaises(StorageError):
            session.run("count", output_schema=SCHEMA)
        storage.append = original
        result = Session.open(agent, storage, session.id).resume()
        self.assertEqual((result.value, len(provider.requests), ran), ({"n": 1}, 1, []))

    def test_g34_follow_replay_is_written_with_backpressure_page_by_page(self):
        big = "x" * 100_000
        turns = [call("big", identifier=f"c{index}") for index in range(30)] + ["done"]
        agent = Agent(model="m", provider=ScriptedProvider(turns), tools=[tool("big", lambda arguments: big)])
        server = Http(self, agent, storage=MemoryStorage())
        identifier = server.request("POST", "/sessions")[1][0]["sessionId"]
        self.assertEqual(server.request("POST", f"/sessions/{identifier}/prompts", {"message": "go"})[1][-1]["status"],
                         "completed")
        page = server.request("GET", f"/sessions/{identifier}/events")[1][0]  # G7: one page stays under the limit
        self.assertLess(len(json.dumps(page)), 1_048_576)
        self.assertIsNotNone(page["nextAfter"])
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", f"/sessions/{identifier}/events?after=0&follow=1")
        response = connection.getresponse()
        time.sleep(0.3)  # a slow reader: the server must wait for it, not buffer 3 MB or drop the stream
        lines = [json.loads(line) for line in response.read().splitlines()]
        self.assertEqual(len(lines), 63)
        self.assertEqual(lines[-1]["type"], "agent_end")
        self.assertEqual([event["cursor"] for event in lines], sorted({event["cursor"] for event in lines}))

    def test_g35_importing_the_library_never_starts_the_cli(self):
        script = ("import sys; sys.argv = ['host-program', '--self-test', '-p', 'x']\n"
                  f"sys.path[:0] = {[path for path in sys.path if path]!r}\n"
                  "import tinyagent, tiny_agent\nprint('imported')\n")
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "imported\n", ""))

    def test_g36_the_live_terminal_cursor_is_the_terminal_record_actually_written(self):
        token = CancellationToken()

        def cancel(arguments):
            token.cancel()
            token.raise_if_cancelled()

        def broken(arguments):
            raise RuntimeError("no")

        provider = ScriptedProvider([call("stop"), "fine", ProviderError("down"), call("ask")])
        session = Session(Agent(model="m", provider=provider, tools=[
            tool("stop", cancel), Tool("ask", "Ask.", {"type": "object"}, lambda arguments: pause())]))
        ends = []
        collect = lambda event: ends.append(event) if event.type == "agent_end" else None
        with self.assertRaises(CancelledError):
            session.run("one", cancellation=token, on_event=collect)   # aborted, with an unfinished tool
        session.run("two", on_event=collect)                            # completed
        with self.assertRaises(ProviderError):
            session.run("three", on_event=collect)                      # failed
        session.run("four", on_event=collect)                           # paused
        expected = {"aborted": "run_failed", "completed": "run_commit", "failed": "run_failed", "paused": "run_paused"}
        self.assertEqual([event.data["status"] for event in ends], list(expected))
        for event in ends:
            record = session.records[event.data["cursor"] - 1]
            self.assertEqual(record["type"], expected[event.data["status"]], event.data)
        # The interrupted result was written before the aborted run's terminal record.
        first = ends[0].data["cursor"]
        self.assertEqual((session.records[first - 2]["type"], session.records[first - 2]["item"]["isError"]),
                         ("tool_result", True))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(sys.platform != "win32", "process groups are a POSIX contract")
class BashGroupTests(unittest.TestCase):
    def test_g12_cancelling_right_as_a_child_starts_leaves_nothing_in_the_group(self):
        """Found by the black-box harness: a kill that lands while the shell is exec'ing a
        child can miss that child. The group is signalled until it is gone."""
        import os
        import tempfile
        for attempt in range(15):
            with tempfile.TemporaryDirectory(prefix="ta-group-") as directory:
                started = os.path.join(directory, "started")
                token = CancellationToken()
                done = threading.Event()

                def work():
                    try:
                        tiny_agent.run_bash(f"echo $$ > {started}.pid; echo started > {started}; sleep 30", token)
                    except CancelledError:
                        pass
                    done.set()

                threading.Thread(target=work, daemon=True).start()
                deadline = time.monotonic() + 5
                while not os.path.exists(started) and time.monotonic() < deadline:
                    pass  # spin: the cancel must land as close to the exec of sleep as possible
                token.cancel()
                self.assertTrue(done.wait(5))
                group = int(open(started + ".pid").read())
                deadline = time.monotonic() + 3
                while True:
                    try:
                        os.killpg(group, 0)
                    except ProcessLookupError:
                        break
                    except PermissionError:
                        pass
                    self.assertLess(time.monotonic(), deadline, f"attempt {attempt}: the group outlived the cancel")
                    time.sleep(0.02)
