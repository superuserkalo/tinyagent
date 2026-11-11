"""Rulings H1 to H12: the wire details Python and TypeScript must share. One test per ruling."""

import contextlib
import io
import json
import os
import socket
import tempfile
import unittest

import tiny_agent
from test_g_rulings import FailingStorage, Http
from test_v2 import _ProviderServer, call, tool
from test_v2_surface import open_run
from tinyagent import (Agent, ContextBudget, MemoryStorage, OpenAIProvider, ProviderError, ResumeRequiredError,
                       ScriptedProvider, Session, Tool, bash_tool, pause)

ZERO = {"inputTokens": 0, "outputTokens": 0, "requests": 0}


def asking(provider, **options):
    return Agent(model="m", provider=provider, **options,
                 tools=[Tool("ask", "Ask.", {"type": "object"}, lambda arguments: pause("why?")), tool()])


class WireRulings(unittest.TestCase):
    def rpc(self, agent, **options):
        emitted = []
        session = tiny_agent.RpcSession(agent, emitted.append, lambda message: None, **options)
        self.addCleanup(session.close)

        def command(**request):
            del emitted[:]
            session.handle({"id": "1", **request})
            self.assertTrue(session.wait_idle(5))
            return emitted[0].get("data", emitted[0].get("error")), emitted[1:]

        return session, command

    def test_h01_retryable_is_true_after_exhausted_transient_failures_and_false_for_401(self):
        with socket.socket() as placeholder:
            placeholder.bind(("127.0.0.1", 0))
            closed_port = placeholder.getsockname()[1]
        cases = [([(429, {}, b"slow")] * 4, True), ([(503, {}, b"down")] * 4, True), ([(401, {}, b"no")], False),
                 ([(400, {}, b"bad")], False), (None, True)]
        for responses, expected in cases:
            if responses is None:
                endpoint = f"http://127.0.0.1:{closed_port}/v1/responses"   # nothing listens: a connection failure
            else:
                server = _ProviderServer(responses)
                self.addCleanup(server.close)
                endpoint = server.url + "/responses"
            events = []
            agent = Agent(model="m", provider=OpenAIProvider("k", endpoint=endpoint, timeout=5),
                          sleep=lambda seconds, token: None)
            with self.assertRaises(ProviderError):
                Session(agent).run("go", on_event=events.append)
            error = events[-1].data["error"]
            self.assertEqual((error["code"], error["retryable"]), ("provider_error", expected), responses)

    def test_h02_a_deduplicated_prompt_carries_the_stored_runs_cursors(self):
        session = Session(Agent(model="m", provider=ScriptedProvider(["one"])))
        first, again = [], []
        session.run("go", message_id="m1", on_event=first.append)
        session.run("go", message_id="m1", on_event=again.append)
        self.assertEqual([(event.type, event.data["cursor"], event.data["repeated"]) for event in again],
                         [("agent_start", first[0].data["cursor"], True), ("agent_end", first[-1].data["cursor"], True)])
        self.assertEqual([session.records[event.data["cursor"] - 1]["type"] for event in again],
                         ["run_start", "run_commit"])

    def test_h03_a_storage_failure_before_a_stream_starts_is_409_session_failed(self):
        agent = Agent(model="m", provider=ScriptedProvider(["hi"]))
        for kind in ("session", "run_start"):
            server = Http(self, agent, storage=FailingStorage(kind, OSError("disk full")))
            status, body = server.request("POST", "/sessions")
            if kind == "run_start":
                status, body = server.request("POST", f"/sessions/{body[0]['sessionId']}/prompts", {"message": "go"})
            self.assertEqual((status, body[0]["error"]["code"]), (409, "session_failed"), kind)

    def test_h04_refused_input_is_invalid_command_over_rpc_and_invalid_request_over_http(self):
        storage = MemoryStorage()
        provider = ScriptedProvider([call("ask"), call("ask")])
        agent = asking(provider)
        deep = "x"
        for _ in range(40):
            deep = [{"type": "text", "text": "t", "more": deep}]
        session, command = self.rpc(agent, storage=storage)
        self.assertEqual(command(type="prompt", message=deep)[0]["code"], "invalid_command")
        self.assertEqual(command(type="prompt", message="go", messageId=5)[0]["code"], "invalid_command")
        command(type="prompt", message="go")
        for bad in ({"error": 5}, {"result": "a", "error": "b"}, {}):
            self.assertEqual(command(type="resume_run", **bad)[0]["code"], "invalid_command", bad)
        identifier = session.session.id
        session.close()
        server = Http(self, agent, storage=storage)
        server.request("POST", "/sessions", {"sessionId": identifier})
        for path, body in ((f"/sessions/{identifier}/resume", {"error": 5}),
                           (f"/sessions/{identifier}/resume", {}),
                           (f"/sessions/{identifier}/prompts", {"message": deep}),
                           (f"/sessions/{identifier}/prompts", {"message": "x", "messageId": 5})):
            status, lines = server.request("POST", path, body)
            self.assertEqual((status, lines[0]["error"]["code"]), (400, "invalid_request"), body)

    def test_h05_a_detach_that_is_not_a_boolean_is_refused(self):
        server = Http(self, Agent(model="m", provider=ScriptedProvider(["hi"])))
        identifier = server.request("POST", "/sessions")[1][0]["sessionId"]
        for value in ("yes", 1, None, [], "true"):
            for action in ("prompts", "resume"):
                status, lines = server.request("POST", f"/sessions/{identifier}/{action}",
                                               {"message": "go", "detach": value})
                self.assertEqual((status, lines[0]["error"]["code"]), (400, "invalid_request"), (action, value))
        self.assertEqual(server.request("POST", f"/sessions/{identifier}/prompts",
                                        {"message": "go", "detach": False})[1][-1]["text"], "hi")

    def test_h06_an_oversized_resume_result_is_refused_and_the_pause_is_untouched(self):
        storage = MemoryStorage()
        agent = asking(ScriptedProvider([call("ask", identifier="q1"), "thanks"]))
        huge = "x" * (tiny_agent.MAX_TOOL_OUTPUT_BYTES + 1)
        session, command = self.rpc(agent, storage=storage)
        command(type="prompt", message="go")
        before = len(storage.load(session.session.id))
        error, events = command(type="resume_run", result=huge)
        self.assertEqual((error["code"], events), ("invalid_command", []))
        self.assertIn("exceeds", error["message"])
        self.assertEqual((len(storage.load(session.session.id)), session.session.paused["toolCallId"]), (before, "q1"))
        identifier = session.session.id
        session.close()
        server = Http(self, agent, storage=storage)
        server.request("POST", "/sessions", {"sessionId": identifier})
        status, lines = server.request("POST", f"/sessions/{identifier}/resume", {"result": {"blob": huge[:600_000]}})
        self.assertEqual((status, lines[0]["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(server.request("GET", f"/sessions/{identifier}")[1][0]["paused"]["toolCallId"], "q1")
        self.assertEqual(server.request("POST", f"/sessions/{identifier}/resume", {"result": "ok"})[1][-1]["text"],
                         "thanks")

    def test_h07_explicit_compact_while_a_run_is_open_or_paused_is_refused(self):
        budget = ContextBudget(10_000, 100, lambda request: len(json.dumps(request)))
        storage = MemoryStorage()
        agent = asking(ScriptedProvider([call("ask"), "x"]))
        opened = open_run(agent, storage)
        opened.budget = budget
        with self.assertRaises(ResumeRequiredError):
            opened.compact()
        paused = Session(agent, storage=storage, budget=budget)
        paused.run("go")
        with self.assertRaises(ResumeRequiredError):
            paused.compact()
        session, command = self.rpc(agent, storage=storage, session_id=opened.id, budget=budget)
        self.assertEqual(command(type="compact")[0]["code"], "resume_required")
        session.close()
        server = Http(self, agent, storage=storage, budget=budget)
        server.request("POST", "/sessions", {"sessionId": paused.id})
        status, lines = server.request("POST", f"/sessions/{paused.id}/compact")
        self.assertEqual((status, lines[0]["error"]["code"]), (409, "resume_required"))

    def test_h08_every_agent_end_carries_usage(self):
        storage = MemoryStorage()
        provider = ScriptedProvider([{"calls": call("ask"), "input_tokens": 4, "output_tokens": 1},
                                     ProviderError("down"), {"text": "ok", "input_tokens": 2}])
        agent = asking(provider)
        session = Session(agent, storage=storage)
        live = []
        collect = lambda event: live.append(event.as_dict()) if event.type == "agent_end" else None
        session.run("one", on_event=collect)                                    # paused
        session.abandon()                                                       # abandoned: replay only
        with self.assertRaises(ProviderError):
            session.run("two", on_event=collect)                                # failed
        session.run("three", message_id="m", on_event=collect)                  # completed
        session.run("three", message_id="m", on_event=collect)                  # repeated
        replayed = [event for event in session.events()["events"] if event["type"] == "agent_end"]
        self.assertEqual([event["status"] for event in replayed], ["paused", "failed", "failed", "completed"])
        for event in live + replayed:
            self.assertEqual(set(event["usage"]), set(ZERO), event)
        self.assertEqual((live[0]["usage"], replayed[0]["usage"]), ({"inputTokens": 4, "outputTokens": 1, "requests": 1},) * 2)
        # Adapter-synthesised ends: the commit is refused after the loop reported completion.
        failing = FailingStorage("run_commit", OSError("disk full"))
        session, command = self.rpc(Agent(model="m", provider=ScriptedProvider(["hi"])), storage=failing)
        _, events = command(type="prompt", message="go")
        self.assertEqual((events[-1]["status"], set(events[-1]["usage"])), ("failed", set(ZERO)))
        server = Http(self, Agent(model="m", provider=ScriptedProvider(["hi"])),
                      storage=FailingStorage("run_commit", OSError("disk full")))
        identifier = server.request("POST", "/sessions")[1][0]["sessionId"]
        lines = server.request("POST", f"/sessions/{identifier}/prompts", {"message": "go"})[1]
        self.assertEqual((lines[-1]["status"], set(lines[-1]["usage"])), ("failed", set(ZERO)))

    @unittest.skipUnless(os.name == "posix", "Bash is a POSIX contract here")
    def test_h09_bash_timeout_output_has_the_normal_shape_with_exit_code_124(self):
        provider = ScriptedProvider([call("bash", command="echo out; echo err >&2; sleep 30"), "done"])
        events = []
        Agent(model="m", provider=provider, tools=[bash_tool(timeout_seconds=0.5)]).run("go", on_event=events.append)
        ended = next(event for event in events if event.type == "tool_execution_end")
        self.assertEqual((ended.data["output"], ended.data["isError"]),
                         ("exit_code: 124\nstdout:\nout\nstderr:\nerr\n[timed out after 0.5 s]", True))

    def test_h10_the_chat_cli_requires_a_model_and_not_a_key(self):
        made = []
        original = tiny_agent.ChatCompletionsProvider

        class Recording:
            def __init__(self, key, **options):
                made.append((key, options))
                raise SystemExit("constructed")

        tiny_agent.ChatCompletionsProvider = Recording
        try:
            with self.assertRaisesRegex(SystemExit, "TINYAGENT_MODEL"):
                tiny_agent.main(["-p", "x"], environ={"TINYAGENT_PROVIDER": "chat"})
            self.assertEqual(made, [])
            with self.assertRaisesRegex(SystemExit, "constructed"):
                tiny_agent.main(["-p", "x"], environ={"TINYAGENT_PROVIDER": "chat", "TINYAGENT_MODEL": "llama3",
                                                      "TINYAGENT_BASE_URL": "http://localhost:11434/v1"})
            with self.assertRaisesRegex(SystemExit, "constructed"):
                tiny_agent.main(["-p", "x"], environ={"TINYAGENT_PROVIDER": "chat", "TINYAGENT_MODEL": "gpt",
                                                      "OPENAI_API_KEY": "sk-1"})
        finally:
            tiny_agent.ChatCompletionsProvider = original
        self.assertEqual(made, [("", {"base_url": "http://localhost:11434/v1"}), ("sk-1", {})])
        with self.assertRaisesRegex(SystemExit, "OPENAI_API_KEY"):
            tiny_agent.main(["-p", "x"], environ={"TINYAGENT_PROVIDER": "openai"})
        # Without a key the Chat provider sends no Authorization header.
        server = _ProviderServer([(200, {}, b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n'
                                           b'data: [DONE]\n\n')])
        self.addCleanup(server.close)
        Agent(model="m", provider=tiny_agent.ChatCompletionsProvider("", base_url=server.url, timeout=5)).run("go")
        self.assertNotIn("authorization", {name.lower() for name in server.requests[0]["headers"]})

    def test_h11_after_tool_runs_for_settled_calls_only(self):
        seen = []

        def broken(arguments):
            raise RuntimeError("boom")

        def before(tool_call, context):
            return {"denied": "no", "paused": pause("wait")}.get(tool_call.name)

        def after(tool_call, result, context):
            seen.append((tool_call.name, result["isError"]))

        tools = [tool("ok"), tool("broken", broken), tool("denied"), tool("paused")]
        provider = ScriptedProvider([call("ok"), call("broken"), call("missing"), call("ok"), call("denied"), call("paused"),
                                     "done"])
        agent = Agent(model="m", provider=provider, tools=tools, before_tool=before, after_tool=after)
        session = Session(agent)
        self.assertEqual(session.run("go").status, "paused")
        self.assertEqual(seen, [("ok", False), ("broken", True), ("missing", True), ("ok", False)])
        session.abandon()
        # A call settled as interrupted on resume is not passed to after_tool either.
        del seen[:]
        from test_v2_surface import ASSISTANT, INTENT
        agent.tools["effect"] = tool()
        interrupted = open_run(agent, MemoryStorage(), ASSISTANT, INTENT)
        provider.turns[:] = ["after"]
        interrupted.resume()
        self.assertEqual(seen, [])

    def test_h12_approval_answers_and_resume_text_in_the_interactive_cli(self):
        asked = []
        script = iter(["Y", "yes", "N", "No", "", "A"])
        approve = tiny_agent.command_approver([], lambda command: asked.append(command) or next(script))
        self.assertEqual([approve(f"c{index}") for index in range(6)], [True, True, False, False, False, True])
        self.assertIs(approve("later"), True)   # "a" allowed everything from then on
        self.assertEqual(len(asked), 6)
        for word in ("ALL", "all"):
            once = tiny_agent.command_approver([], lambda command, word=word: word)
            self.assertEqual((once("x"), once("y")), (True, True))
        self.assertEqual(tiny_agent.command_approver([], lambda command: "too risky")("x"), "too risky")
        provider = ScriptedProvider([call("ask", identifier="q1"), "Blue it is."])
        answers = iter(["start", "/resume blue", EOFError()])

        def read(prompt=""):
            answer = next(answers)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        original = tiny_agent.bash_tool
        tiny_agent.bash_tool = lambda **options: Tool("ask", "Ask.", {"type": "object"}, lambda arguments: pause("?"))
        tiny_agent.input = read
        output = io.StringIO()
        try:
            with tempfile.TemporaryDirectory(prefix="ta-h12-") as directory, \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                tiny_agent.main([], provider=provider, environ={"TINYAGENT_SESSION_DIR": directory})
        finally:
            tiny_agent.bash_tool = original
            del tiny_agent.input
        self.assertIn("Blue it is.", output.getvalue())
        self.assertEqual(provider.requests[1]["history"][-1]["output"], "blue")


if __name__ == "__main__":
    unittest.main()
