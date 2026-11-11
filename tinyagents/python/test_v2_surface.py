"""Protocol version 2 conformance scenarios, Part E. Each test names its scenario."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import tiny_agent
from test_v2 import _ProviderServer, call, tool
from tinyagent import (Agent, AnthropicProvider, HookError, MemoryStorage, OpenAIProvider, OutputMissingError,
                       ResumeRequiredError, Session, Tool, ToolContext, pause, serve)
from tinyagent_testing import ScriptedProvider

PAUSING = '''
import sys
from tinyagent import Agent, Session, Tool, pause
from tinyagent_testing import ScriptedProvider

def ask(arguments, context):
    return pause({"question": arguments["question"]})

agent = Agent(model="offline", provider=ScriptedProvider([[{"name": "ask", "id": "q1", "arguments": {"question": "Which colour?"}}]]),
              tools=[Tool("ask", "Ask the user.", {"type": "object"}, ask)])
with Session(agent, directory=sys.argv[1], session_id="s_paused") as session:
    result = session.run("start")
    assert result.status == "paused" and session.paused["payload"] == {"question": "Which colour?"}, result
'''


def asking_agent(provider, **options):
    return Agent(model="offline", provider=provider, **options,
                 tools=[Tool("ask", "Ask the user.", {"type": "object"}, lambda arguments: pause("unused"))])


class ProviderOptionTests(unittest.TestCase):
    def test_d25_base_url_headers_and_extra_reach_the_request(self):
        server = _ProviderServer([(200, {}, _ProviderServer.OK)] * 2)
        self.addCleanup(server.close)
        provider = OpenAIProvider("key", base_url=server.url + "/", timeout=5,
                                  headers={"authorization": "Bearer other", "X-Team": "blue"},
                                  extra={"temperature": 0.2, "reasoning": {"effort": "low"}, "store": True})
        agent = Agent(model="configured", instructions="Configured.", provider=provider)
        agent.run("go")
        first = server.requests[0]
        self.assertEqual(first["path"], "/v1/responses")
        self.assertEqual((first["headers"]["authorization"], first["headers"]["X-Team"]), ("Bearer other", "blue"))
        self.assertEqual([first["body"][key] for key in ("temperature", "reasoning", "store", "model")],
                         [0.2, {"effort": "low"}, True, "configured"])  # extra wins over TinyAgent's own field
        # Per-run extra is merged over the provider's; model and instructions can be overridden too.
        Session(agent).run("go", extra={"temperature": 0.9, "max_output_tokens": 50},
                           model="other-model", instructions="For this run.")
        body = server.requests[1]["body"]
        self.assertEqual([body[key] for key in ("temperature", "max_output_tokens", "reasoning", "model",
                                                "instructions")],
                         [0.9, 50, {"effort": "low"}, "other-model", "For this run."])

    def test_d25_anthropic_takes_the_same_options(self):
        seen = []

        def transport(payload, cancellation):
            seen.append(payload)
            yield {"type": "message_start", "message": {"role": "assistant", "usage": {"input_tokens": 4}}}
            yield {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}
            yield {"type": "message_stop"}

        provider = AnthropicProvider("key", transport, extra={"max_tokens": 100, "temperature": 0})
        result = Agent(model="m", provider=provider).run("go", extra={"temperature": 1})
        self.assertEqual((seen[0]["max_tokens"], seen[0]["temperature"]), (100, 1))
        self.assertEqual(result.usage, {"inputTokens": 4, "outputTokens": 2, "requests": 1})
        remote = AnthropicProvider(base_url="https://gateway.example/anthropic/v1/", headers={"X-Api-Key": "k"})
        self.assertEqual(remote.endpoint, "https://gateway.example/anthropic/v1/messages")


class HookTests(unittest.TestCase):
    def test_d27_before_tool_deny_replace_arguments_and_after_tool_replace_result(self):
        received = []
        seen_by_after = []
        echo = Tool("echo", "Echo.", {"type": "object", "properties": {"text": {"type": "string"}},
                                      "required": ["text"]},
                    lambda arguments: received.append(arguments) or arguments["text"])

        def before(tool_call, context):
            self.assertIsInstance(context, ToolContext)
            return {"deny": "not allowed here", "silent": False, "replace": {"text": "replaced"},
                    "invalid": {"text": 5}}.get(tool_call.arguments["text"])

        def after(tool_call, result, context):
            seen_by_after.append((tool_call.arguments["text"], result))
            if result["output"] == "replaced":
                return "redacted"
            if tool_call.arguments["text"] == "flip":
                return {"output": "now an error", "isError": True}

        provider = ScriptedProvider([call("echo", text=text) for text in
                                     ("deny", "silent", "replace", "invalid", "flip", "plain")] + ["done"])
        events = []
        Agent(model="offline", provider=provider, tools=[echo], before_tool=before,
              after_tool=after).run("go", on_event=events.append)
        outputs = [(event.data["output"], event.data["isError"]) for event in events
                   if event.type == "tool_execution_end"]
        self.assertEqual(outputs[:3], [("not allowed here", True), ("Tool call denied", True), ("redacted", False)])
        self.assertIn("Invalid arguments for echo", outputs[3][0])  # replaced arguments are validated again
        self.assertEqual(outputs[4:], [("now an error", True), ("plain", False)])
        self.assertEqual(received, [{"text": "replaced"}, {"text": "flip"}, {"text": "plain"}])
        self.assertEqual(provider.requests[3]["history"][-1]["output"], "redacted")  # what the model sees
        # Version 2 (H11): after_tool sees settled calls, failed ones included, but not denied ones.
        self.assertEqual([text for text, _ in seen_by_after], ["replace", "invalid", "flip", "plain"])
        self.assertTrue(seen_by_after[1][1]["isError"])

    def test_d27_a_hook_that_raises_fails_the_run_with_hook_error(self):
        def broken(*arguments):
            raise ValueError("bug in the hook")

        for hooks in ({"before_tool": broken}, {"after_tool": broken}):
            events = []
            agent = Agent(model="offline", provider=ScriptedProvider([call(), "never"]), tools=[tool()], **hooks)
            with self.assertRaisesRegex(HookError, "bug in the hook"):
                agent.run("go", on_event=events.append)
            self.assertEqual(events[-1].data["error"]["code"], "hook_error")

    def test_d27_bash_approve_and_before_tool_deny_the_same_way(self):
        provider = ScriptedProvider([call("bash", command="echo one"), call("bash", command="echo two"), "done"])
        agent = Agent(model="offline", provider=provider,
                      tools=[tiny_agent.bash_tool(approve=lambda command: "not two" if "two" in command else True)],
                      before_tool=lambda tool_call, context: "not one" if "one" in tool_call.arguments["command"] else None)
        agent.run("go")
        observations = [request["history"][-1] for request in provider.requests[1:]]
        self.assertEqual([(item["output"], item["isError"]) for item in observations],
                         [("not one", True), ("Rejected by user: not two", True)])


class PauseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ta-pause-")
        self.addCleanup(temp.cleanup)
        self.directory = temp.name

    def pause_in_another_process(self):
        subprocess.run([sys.executable, "-c", PAUSING, self.directory], check=True, timeout=30,
                       env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})

    def reopen(self, turns, **options):
        self.provider = ScriptedProvider(turns)
        session = Session.open(asking_agent(self.provider), self.directory, "s_paused", **options)
        self.addCleanup(session.close)
        return session

    def test_d28_pause_from_a_handler_survives_a_new_process_and_resumes_with_a_result(self):
        self.pause_in_another_process()
        session = self.reopen(["Blue it is."], on_uncertain="reconcile")
        self.assertEqual(session.paused, {"toolCallId": "q1", "toolName": "ask",
                                          "payload": {"question": "Which colour?"}})
        self.assertTrue(session.resumable)
        self.assertFalse(session.recovery_required)  # paused is not uncertain, even with the strict gate
        self.assertEqual(session.inspect()["paused"], session.paused)
        with self.assertRaises(ResumeRequiredError):
            session.run("something else")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            session.resume()
        self.assertEqual(session.paused["toolCallId"], "q1")  # a refused resume changes nothing
        events = []
        result = session.resume(result={"colour": "blue"}, on_event=events.append)
        self.assertEqual((result.text, result.status), ("Blue it is.", "completed"))
        self.assertEqual([event.type for event in events],
                         ["agent_start", "tool_execution_start", "tool_execution_end", "text_delta", "agent_end"])
        observation = self.provider.requests[0]["history"][-1]
        self.assertEqual((observation["callId"], observation["output"], observation["isError"]),
                         ("q1", '{"colour": "blue"}', False))
        self.assertIsNone(session.paused)
        self.assertFalse(session.resumable)
        self.assertEqual([item["role"] for item in session.history],
                         ["user", "assistant", "tool_result", "assistant"])

    def test_d28_resume_with_an_error_continues(self):
        self.pause_in_another_process()
        session = self.reopen(["I will pick one myself."])
        result = session.resume(error="The user did not answer.")
        self.assertEqual(result.text, "I will pick one myself.")
        observation = self.provider.requests[0]["history"][-1]
        self.assertEqual((observation["output"], observation["isError"]), ("The user did not answer.", True))

    def test_d28_pause_from_before_tool_ends_the_run_paused_without_running_the_tool(self):
        ran = []
        provider = ScriptedProvider([call("deploy", identifier="d1", target="prod"), "Deployed."])
        agent = Agent(model="offline", provider=provider,
                      tools=[tool("deploy", lambda arguments: ran.append(arguments) or "deployed")],
                      before_tool=lambda tool_call, context: pause({"approve": tool_call.arguments}))
        session = Session(agent)
        events = []
        result = session.run("ship it", on_event=events.append)
        stopped = {"toolCallId": "d1", "toolName": "deploy", "payload": {"approve": {"target": "prod"}}}
        self.assertEqual((result.status, result.pause, session.paused), ("paused", stopped, stopped))
        self.assertEqual((events[-1].type, events[-1].data["status"], events[-1].data["pause"]),
                         ("agent_end", "paused", stopped))
        self.assertEqual([record["type"] for record in session.records[-4:]],
                         ["item", "tool_intent", "tool_paused", "run_paused"])
        self.assertEqual((ran, session.history), ([], []))
        self.assertEqual([(event["type"], event.get("status")) for event in session.events()["events"][-3:]],
                         [("tool_execution_start", None), ("tool_execution_end", None), ("agent_end", "paused")])
        self.assertEqual(session.resume(result="approved and deployed by the operator").text, "Deployed.")
        self.assertEqual(ran, [])
        self.assertEqual(session.records[-1]["type"], "run_commit")

    def test_d28_pause_over_rpc_and_http(self):
        storage = MemoryStorage()
        provider = ScriptedProvider([call("ask", identifier="q1"), "Thanks.", call("ask", identifier="q2"), "Fine."])
        agent = asking_agent(provider)
        emitted = []
        rpc = tiny_agent.RpcSession(agent, emitted.append, storage=storage)

        def command(**request):
            del emitted[:]
            rpc.handle({"id": "1", **request})
            self.assertTrue(rpc.wait_idle(5))
            return emitted[0].get("data", emitted[0].get("error")), emitted[1:]

        _, events = command(type="prompt", message="go")
        self.assertEqual((events[-1]["status"], events[-1]["pause"]["payload"]), ("paused", "unused"))
        state, _ = command(type="get_state")
        self.assertEqual((state["paused"]["toolCallId"], state["resumable"]), ("q1", True))
        self.assertEqual(command(type="prompt", message="other")[0]["code"], "resume_required")
        _, events = command(type="resume_run", result="blue")
        self.assertEqual((events[-1]["status"], events[-1]["text"]), ("completed", "Thanks."))
        _, events = command(type="prompt", message="again")
        self.assertEqual(events[-1]["status"], "paused")
        identifier = rpc.session.id
        rpc.close()

        server = serve(agent, port=0, storage=storage)
        self.addCleanup(server.close)

        def request(method, path, body=None):
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", int(server.url.rsplit(":", 1)[1]), timeout=10)
            self.addCleanup(connection.close)
            connection.request(method, path, None if body is None else json.dumps(body),
                               {"Content-Type": "application/json"} if body is not None else {})
            return [json.loads(line) for line in connection.getresponse().read().splitlines()]

        request("POST", "/sessions", {"sessionId": identifier})
        self.assertEqual(request("GET", f"/sessions/{identifier}")[0]["paused"]["toolCallId"], "q2")
        events = request("POST", f"/sessions/{identifier}/resume", {"error": "no answer"})
        self.assertEqual((events[2]["type"], events[2]["isError"], events[-1]["text"]),
                         ("tool_execution_end", True, "Fine."))
        self.assertIsNone(request("GET", f"/sessions/{identifier}")[0]["paused"])


class OutputSchemaTests(unittest.TestCase):
    SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
              "required": ["city", "days"], "additionalProperties": False}

    def final(self, **arguments):
        return call("final_output", **arguments)

    def test_d29_valid_output_completes_the_run(self):
        provider = ScriptedProvider([call(), self.final(city="Oslo", days=3)])
        session = Session(Agent(model="offline", provider=provider, tools=[tool()]))
        events = []
        result = session.run("plan", output_schema=self.SCHEMA, on_event=events.append)
        self.assertEqual((result.value, result.status), ({"city": "Oslo", "days": 3}, "completed"))
        self.assertEqual(result.output, [])  # keeps its version 1 meaning: the provider's raw output items
        self.assertEqual(events[-1].data["value"], {"city": "Oslo", "days": 3})
        self.assertNotIn("output", events[-1].data)
        self.assertEqual(provider.requests[0]["tools"], ["effect", "final_output"])
        self.assertEqual(session.events()["events"][-1]["value"], {"city": "Oslo", "days": 3})
        with self.assertRaisesRegex(ValueError, "JSON object"):
            session.run("plan", output_schema={"type": "string"})
        # The synthetic call is answered in history, so the next prompt is a valid request.
        self.assertEqual([item["role"] for item in session.history][-2:], ["assistant", "tool_result"])
        provider.turns.append("plain")
        self.assertEqual(session.run("and now in words").text, "plain")
        self.assertEqual(provider.requests[-1]["tools"], ["effect"])

    def test_d29_invalid_output_is_a_recoverable_tool_error(self):
        provider = ScriptedProvider([self.final(city="Oslo", days="three"), self.final(city="Oslo", days=3)])
        result = Agent(model="offline", provider=provider).run("plan", output_schema=self.SCHEMA)
        self.assertEqual(result.value, {"city": "Oslo", "days": 3})
        observation = provider.requests[1]["history"][-1]
        self.assertTrue(observation["isError"])
        self.assertIn("Invalid arguments for final_output/days", observation["output"])

    def test_d29_text_only_answer_is_nudged_once(self):
        provider = ScriptedProvider(["Oslo for three days.", self.final(city="Oslo", days=3)])
        result = Agent(model="offline", provider=provider).run("plan", output_schema=self.SCHEMA)
        self.assertEqual(result.value, {"city": "Oslo", "days": 3})
        self.assertEqual(provider.requests[1]["history"][-1],
                         {"role": "user", "content": "Call the final_output tool with your final answer."})

    def test_d29_second_miss_ends_with_output_missing(self):
        provider = ScriptedProvider(["Oslo.", "Still Oslo.", "never"])
        events = []
        with self.assertRaises(OutputMissingError):
            Agent(model="offline", provider=provider).run("plan", output_schema=self.SCHEMA, on_event=events.append)
        self.assertEqual((events[-1].data["error"]["code"], len(provider.requests)), ("output_missing", 2))


class ContextTests(unittest.TestCase):
    def test_d30_context_reaches_handlers_and_hooks_and_is_never_stored_or_sent(self):
        secret = {"user": "u_42", "token": "host-secret-value"}
        seen = []
        provider = ScriptedProvider([call(), "done", call(), "done"])
        agent = Agent(model="offline", provider=provider,
                      tools=[tool(handler=lambda arguments, context: seen.append(("handler", context.context)) or "ok")],
                      before_tool=lambda tool_call, context: seen.append(("before", context.context)),
                      after_tool=lambda tool_call, result, context: seen.append(("after", context.context)))
        storage = MemoryStorage()
        session = Session(agent, storage=storage)
        session.run("go", context=secret)
        self.assertEqual([kind for kind, _ in seen], ["before", "handler", "after"])
        self.assertTrue(all(value is secret for _, value in seen))
        self.assertNotIn("host-secret-value", json.dumps(storage.load(session.id)))
        self.assertNotIn("host-secret-value", json.dumps(provider.requests))
        self.assertNotIn("host-secret-value", json.dumps(session.events()))
        del seen[:]
        agent.run("go")
        self.assertEqual(seen, [("before", None), ("handler", None), ("after", None)])

    def test_d31_reopening_after_changing_instructions_and_tools_succeeds_and_is_recorded(self):
        storage = MemoryStorage()
        first = Session(Agent(model="offline", instructions="Version one.", provider=ScriptedProvider(["one"])),
                        storage=storage)
        first.run("hello")
        provider = ScriptedProvider(["two", "three"])
        changed = Agent(model="offline", instructions="Version two.", provider=provider, tools=[tool("added")])
        session = Session.open(changed, storage, first.id)
        self.assertEqual(session.records[-1]["type"], "configuration")
        self.assertNotEqual(session.records[-1]["configuration"], session.records[0]["configuration"])
        self.assertEqual(session.run("again").text, "two")
        self.assertEqual((provider.requests[0]["instructions"], provider.requests[0]["tools"]),
                         ("Version two.", ["added"]))
        count = len(session.records)
        self.assertEqual(len(Session.open(changed, storage, first.id).records), count)  # recorded once
        session.run("override", instructions="Just this once.")
        self.assertEqual(provider.requests[1]["instructions"], "Just this once.")
        self.assertEqual(sum(record["type"] == "configuration" for record in session.records), 1)


class TimeoutTests(unittest.TestCase):
    def test_d35_tool_timeout_fires_cancellation_and_yields_the_timed_out_error(self):
        fired = []

        def slow(arguments, context):
            fired.append(context.cancellation.wait(5))
            context.raise_if_cancelled()
            return "too late"

        def stubborn(arguments, context):
            context.cancellation.wait(5)
            return "finished anyway"

        provider = ScriptedProvider([call("slow"), call("stubborn"), call("quick"), "done"])
        agent = Agent(model="offline", provider=provider,
                      tools=[tool("slow", slow, timeout_seconds=0.05), tool("stubborn", stubborn, timeout_seconds=0.05),
                             tool("quick", lambda arguments: "in time", timeout_seconds=5)])
        began = time.monotonic()
        events = []
        result = agent.run("go", on_event=events.append)
        self.assertLess(time.monotonic() - began, 2)
        self.assertEqual((result.text, fired), ("done", [True]))
        outputs = [(event.data["output"], event.data["isError"]) for event in events
                   if event.type == "tool_execution_end"]
        self.assertEqual(outputs, [("timed out after 0.05 s", True), ("finished anyway", False), ("in time", False)])

    def test_d35_cancelling_the_run_is_not_reported_as_a_timeout(self):
        token = tiny_agent.CancellationToken()

        def slow(arguments, context):
            token.cancel()
            context.raise_if_cancelled()

        agent = Agent(model="offline", provider=ScriptedProvider([call("slow")]),
                      tools=[tool("slow", slow, timeout_seconds=5)])
        events = []
        with self.assertRaises(tiny_agent.CancelledError):
            agent.run("go", cancellation=token, on_event=events.append)
        self.assertEqual([event.data.get("output") for event in events[-2:]], ["run aborted", None])


class ScriptedProviderTests(unittest.TestCase):
    def test_d34_scripted_provider_drives_a_multi_step_run_and_reports_its_requests(self):
        provider = ScriptedProvider([
            {"text": "Looking.", "calls": [{"name": "effect", "arguments": {"n": 1}}], "input_tokens": 3},
            [{"name": "effect", "arguments": {"n": 2}}],
            "Both done.",
        ])
        agent = Agent(model="scripted", instructions="Be brief.", provider=provider,
                      tools=[tool(handler=lambda arguments: f"result {arguments['n']}")])
        result = agent.run("do both", extra={"temperature": 0})
        self.assertEqual((result.text, result.usage["requests"], result.usage["inputTokens"]), ("Both done.", 3, 3))
        provider.assert_done()
        self.assertEqual([request["model"] for request in provider.requests], ["scripted"] * 3)
        self.assertEqual(provider.requests[0], {"model": "scripted", "instructions": "Be brief.",
                                                "history": [{"role": "user", "content": "do both"}],
                                                "tools": ["effect"], "extra": {"temperature": 0}})
        self.assertEqual([item["output"] for item in provider.requests[2]["history"] if item["role"] == "tool_result"],
                         ["result 1", "result 2"])
        with self.assertRaisesRegex(Exception, "no turn for request 4"):
            agent.run("once more")
        unfinished = ScriptedProvider(["one", "two"])
        Agent(model="scripted", provider=unfinished).run("go")
        with self.assertRaisesRegex(AssertionError, "1 scripted turns"):
            unfinished.assert_done()


if __name__ == "__main__":
    unittest.main()


from tinyagent import ChatCompletionsProvider, ProviderError  # noqa: E402
from tinyagent_anthropic import _anthropic_input  # noqa: E402
from tinyagent_chat import _chat_messages  # noqa: E402
from tinyagent_openai import _openai_input  # noqa: E402


def chunk(delta=None, finish=None, **more):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **more}


class ChatCompletionsTests(unittest.TestCase):
    def provider(self, *streams, **options):
        self.payloads = []
        pending = list(streams)

        def transport(payload, cancellation):
            self.payloads.append(json.loads(json.dumps(payload)))
            for item in pending.pop(0):
                if isinstance(item, BaseException):
                    raise item
                yield item

        return ChatCompletionsProvider("key", transport, **options)

    def test_d26_text_with_usage_and_request_shape(self):
        provider = self.provider([chunk({"role": "assistant", "content": "Hel"}), chunk({"content": "lo"}),
                                  chunk(finish="stop"),
                                  {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}],
                                 extra={"temperature": 0})
        events = []
        result = Agent(model="local", instructions="Be kind.", provider=provider,
                       tools=[tool()]).run("hi", on_event=events.append, extra={"max_tokens": 64})
        self.assertEqual(result.text, "Hello")
        self.assertEqual([event.data["delta"] for event in events if event.type == "text_delta"], ["Hel", "lo"])
        self.assertEqual(result.usage, {"inputTokens": 9, "outputTokens": 2, "requests": 1})
        payload = self.payloads[0]
        self.assertEqual(payload["messages"], [{"role": "system", "content": "Be kind."},
                                               {"role": "user", "content": "hi"}])
        self.assertEqual((payload["stream"], payload["temperature"], payload["max_tokens"]), (True, 0, 64))
        self.assertEqual(payload["tools"][0]["function"]["name"], "effect")
        self.assertEqual(provider.endpoint, "https://api.openai.com/v1/chat/completions")

    def test_d26_one_tool_call_with_fragmented_arguments_and_missing_usage(self):
        first = [chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                                        "function": {"name": "effect", "arguments": ""}}]}),
                 chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"ci'}}]}),
                 chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'ty": "Os'}}]}),
                 chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'lo"}'}}]}),
                 chunk(finish="tool_calls")]
        provider = self.provider(first, [chunk({"content": "Done."}), chunk(finish="stop")])
        received = []
        agent = Agent(model="local", provider=provider,
                      tools=[tool(handler=lambda arguments: received.append(arguments) or "sunny")])
        result = agent.run("weather?")
        self.assertEqual((result.text, received), ("Done.", [{"city": "Oslo"}]))
        self.assertEqual(result.usage, {"inputTokens": 0, "outputTokens": 0, "requests": 2})  # the server sent none
        self.assertEqual(self.payloads[1]["messages"][1:], [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_a", "type": "function", "function": {"name": "effect", "arguments": '{"city": "Oslo"}'}}]},
            {"role": "tool", "tool_call_id": "call_a", "content": "sunny"}])

    def test_d26_errors_before_and_during_the_stream(self):
        delays = []
        cases = [([ProviderError("API error 503: busy", status=503, retryable=True)], "503", 4),  # before: retried
                 ([chunk({"content": "par"}), {"error": {"message": "overloaded mid-stream"}}], "overloaded", 1),
                 ([chunk({"content": "par"})], "before a finish reason", 1),
                 ([chunk({"content": "par"}), chunk(finish="length")], "incomplete: length", 1),
                 ([chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "effect", "arguments": "{"}}]}),
                   chunk(finish="tool_calls")], "invalid arguments for tool effect", 1)]
        for stream, message, requests in cases:
            provider = self.provider(*[stream] * 4)
            agent = Agent(model="local", provider=provider, tools=[tool()],
                          sleep=lambda seconds, token: delays.append(seconds))
            with self.assertRaisesRegex(ProviderError, message):
                agent.run("go")
            self.assertEqual(len(self.payloads), requests, message)
        self.assertEqual(delays, [1, 2, 4])

    def test_d26_over_http_with_a_done_line_and_an_error_status(self):
        body = (b'data: {"choices":[{"delta":{"content":"fine"},"finish_reason":null}]}\n\n'
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        server = _ProviderServer([(200, {}, body), (401, {}, b"no key")])
        self.addCleanup(server.close)
        agent = Agent(model="local", provider=ChatCompletionsProvider("secret", base_url=server.url, timeout=5,
                                                                      headers={"X-Route": "a"}))
        self.assertEqual(agent.run("go").text, "fine")
        request = server.requests[0]
        self.assertEqual((request["path"], request["headers"]["Authorization"], request["headers"]["X-Route"]),
                         ("/v1/chat/completions", "Bearer secret", "a"))
        with self.assertRaisesRegex(ProviderError, "401"):
            agent.run("go")
        self.assertEqual(len(server.requests), 2)


class ContentBlockTests(unittest.TestCase):
    PNG = "iVBORw0KGgo="
    PROMPT = [{"type": "text", "text": "What is this?"},
              {"type": "image", "mediaType": "image/png", "data": PNG},
              {"type": "image", "url": "https://example.com/cat.png"}]

    def test_d32_image_input_reaches_each_provider_in_its_native_shape(self):
        history = [{"role": "user", "content": self.PROMPT}]
        data_url = f"data:image/png;base64,{self.PNG}"
        self.assertEqual(_openai_input(history), [{"role": "user", "content": [
            {"type": "input_text", "text": "What is this?"},
            {"type": "input_image", "image_url": data_url},
            {"type": "input_image", "image_url": "https://example.com/cat.png"}]}])
        self.assertEqual(_anthropic_input(history), [{"role": "user", "content": [
            {"type": "text", "text": "What is this?"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": self.PNG}},
            {"type": "image", "source": {"type": "url", "url": "https://example.com/cat.png"}}]}])
        self.assertEqual(_chat_messages(history), [{"role": "user", "content": [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}}]}])

    def test_d32_a_prompt_of_parts_and_a_tool_returning_an_image_are_carried_everywhere(self):
        picture = [{"type": "text", "text": "The chart."}, {"type": "image", "url": "https://example.com/chart.png"}]
        provider = ScriptedProvider([call("chart", identifier="c1"), "A rising line."])
        storage = MemoryStorage()
        session = Session(Agent(model="offline", provider=provider, tools=[tool("chart", lambda arguments: picture)]),
                          storage=storage)
        events = []
        result = session.run(self.PROMPT, on_event=events.append)
        self.assertEqual(result.text, "A rising line.")
        self.assertEqual(provider.requests[0]["history"][0]["content"], self.PROMPT)
        observation = provider.requests[1]["history"][-1]
        self.assertEqual((observation["output"], observation["isError"]), (picture, False))
        self.assertEqual(next(event for event in events if event.type == "tool_execution_end").data["output"], picture)
        reopened = Session.open(session.agent, storage, session.id)
        self.assertEqual((reopened.history[0]["content"], reopened.history[2]["output"]), (self.PROMPT, picture))
        self.assertEqual(reopened.events()["events"][2]["output"], picture)
        history = reopened.history[1:3]
        self.assertEqual(_openai_input(history)[-1], {"type": "function_call_output", "call_id": "c1", "output": [
            {"type": "input_text", "text": "The chart."},
            {"type": "input_image", "image_url": "https://example.com/chart.png"}]})
        self.assertEqual(_anthropic_input(history)[-1]["content"][0]["content"], [
            {"type": "text", "text": "The chart."},
            {"type": "image", "source": {"type": "url", "url": "https://example.com/chart.png"}}])
        self.assertEqual(_chat_messages(history)[1:], [
            {"role": "tool", "tool_call_id": "c1", "content": "The chart."},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/chart.png"}}]}])

    def test_d32_parts_over_rpc_and_http(self):
        provider = ScriptedProvider(["Seen.", "Seen again."])
        agent = Agent(model="offline", provider=provider)
        emitted = []
        rpc = tiny_agent.RpcSession(agent, emitted.append)
        rpc.handle({"id": "1", "type": "prompt", "message": self.PROMPT})
        self.assertTrue(rpc.wait_idle(5))
        rpc.close()
        self.assertEqual(emitted[-1]["text"], "Seen.")
        server = serve(agent, port=0)
        self.addCleanup(server.close)
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", int(server.url.rsplit(":", 1)[1]), timeout=10)
        self.addCleanup(connection.close)
        connection.request("POST", "/sessions")
        identifier = json.loads(connection.getresponse().read())["sessionId"]
        connection = http.client.HTTPConnection("127.0.0.1", int(server.url.rsplit(":", 1)[1]), timeout=10)
        self.addCleanup(connection.close)
        connection.request("POST", f"/sessions/{identifier}/prompts", json.dumps({"message": self.PROMPT}),
                           {"Content-Type": "application/json"})
        self.assertEqual(json.loads(connection.getresponse().read().splitlines()[-1])["text"], "Seen again.")
        self.assertEqual([request["history"][-1]["content"] for request in provider.requests], [self.PROMPT] * 2)


class TraceTests(unittest.TestCase):
    def test_d33_model_request_events_carry_usage_and_the_retry_flag_and_reasoning_is_streamed(self):
        provider = ScriptedProvider([
            ProviderError("busy", status=429, retryable=True),
            {"reasoning": "Let me think.", "calls": call(), "input_tokens": 12, "output_tokens": 5},
            ProviderError("gone", status=401)])
        agent = Agent(model="offline", provider=provider, tools=[tool()], trace=True,
                      sleep=lambda seconds, token: None)
        events = []
        with self.assertRaisesRegex(ProviderError, "gone"):
            agent.run("go", on_event=events.append)
        self.assertEqual([event.type for event in events], [
            "agent_start",
            "model_request_start", "model_request_end",                      # 429, will retry
            "model_request_start", "reasoning_delta", "model_request_end",   # the tool call
            "tool_execution_start", "tool_execution_end",
            "model_request_start", "model_request_end",                      # 401, final
            "agent_end"])
        self.assertEqual([event.sequence for event in events], list(range(1, 12)))
        ends = [event.data for event in events if event.type == "model_request_end"]
        self.assertEqual(ends[0], {"error": {"code": "provider_error", "message": "busy"}, "willRetry": True})
        self.assertEqual(ends[1], {"usage": {"inputTokens": 12, "outputTokens": 5}})
        self.assertEqual(ends[2], {"error": {"code": "provider_error", "message": "gone"}, "willRetry": False})
        self.assertEqual([event.data["attempt"] for event in events if event.type == "model_request_start"],
                         [1, 2, 1])
        self.assertEqual(events[4].data["delta"], "Let me think.")

    def test_d33_without_trace_the_version_1_event_stream_is_unchanged(self):
        provider = ScriptedProvider([{"reasoning": "Hm.", "text": "Hi."}])
        events = []
        Agent(model="offline", provider=provider).run("go", on_event=events.append)
        self.assertEqual([event.type for event in events], ["agent_start", "text_delta", "agent_end"])

    def test_d33_real_providers_stream_reasoning(self):
        def thinking(payload, cancellation):
            yield {"type": "message_start", "message": {"role": "assistant"}}
            yield {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}
            yield {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Hm."}}
            yield {"type": "content_block_stop", "index": 0}
            yield {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
            yield {"type": "message_stop"}

        def summary(payload, cancellation):
            yield {"type": "response.reasoning_summary_text.delta", "delta": "Hm."}
            yield {"type": "response.completed", "response": {"status": "completed", "output": []}}

        def chat(payload, cancellation):
            yield chunk({"reasoning_content": "Hm."})
            yield chunk({"content": "Hi."}, "stop")

        for provider in (AnthropicProvider("k", thinking), OpenAIProvider("k", summary),
                         ChatCompletionsProvider("k", chat)):
            events = []
            Agent(model="m", provider=provider, trace=True).run("go", on_event=events.append)
            self.assertEqual([event.data["delta"] for event in events if event.type == "reasoning_delta"], ["Hm."])


from tinyagent import CancellationToken, CancelledError, ContextBudget, NothingToResumeError  # noqa: E402
from tinyagent_session import INTERRUPTED  # noqa: E402


def open_run(agent, storage, *records):
    """A stored session whose run was cut off after the given records."""
    session = Session(agent, storage=storage)
    session._append({"type": "run_start", "runId": "r_open"})
    session._append({"type": "item", "item": {"role": "user", "content": "left open"}})
    for record in records:
        session._append(record)
    return Session.open(agent, storage, session.id)


ASSISTANT = {"type": "item", "item": {"role": "assistant", "content": [
    {"type": "tool_call", "callId": "c1", "name": "effect", "arguments": {}}]}}
INTENT = {"type": "tool_intent", "callId": "c1", "toolName": "effect", "arguments": {}, "idempotent": False}


class RulingTests(unittest.TestCase):
    def test_d36_abandon_an_open_run_and_a_paused_run_then_prompt(self):
        ran = []
        provider = ScriptedProvider(["fresh start", call("ask", identifier="q1"), "after the pause"])
        agent = Agent(model="offline", provider=provider, tools=[
            tool("effect", lambda arguments: ran.append(True)),
            Tool("ask", "Ask.", {"type": "object"}, lambda arguments: pause("why?"))])
        for mode in ("tell_model", "reconcile"):
            session = open_run(agent, MemoryStorage(), ASSISTANT, INTENT)
            session.on_uncertain = mode
            with self.assertRaises(ResumeRequiredError if mode == "tell_model" else Exception):
                session.run("new work")
            session.abandon("the user gave up")
            self.assertEqual((session.resumable, session.recovery_required, session.history), (False, False, []))
            settled, failed = session.records[-2:]
            self.assertEqual((settled["type"], settled["item"]["output"], settled["item"]["isError"]),
                             ("tool_result", INTERRUPTED, True))
            self.assertEqual((failed["type"], failed["status"], failed["error"]),
                             ("run_failed", "failed", {"code": "abandoned", "message": "the user gave up",
                                                       "retryable": False}))
            with self.assertRaises(NothingToResumeError):
                session.abandon()
        self.assertEqual(session.run("new work").text, "fresh start")
        self.assertEqual(session.run("ask me").status, "paused")
        session.abandon()
        self.assertIsNone(session.paused)
        self.assertEqual(session.records[-1]["error"]["message"], "run abandoned")
        self.assertEqual(session.run("carry on").text, "after the pause")
        self.assertEqual((ran, len(session.history)), ([], 4))

    def test_d36_abandon_over_rpc_http_and_the_interactive_cli(self):
        storage = MemoryStorage()
        agent = Agent(model="offline", provider=ScriptedProvider(["one", "two"]), tools=[tool()])
        identifier = open_run(agent, storage, ASSISTANT, INTENT).id
        emitted = []
        rpc = tiny_agent.RpcSession(agent, emitted.append, storage=storage, session_id=identifier)
        rpc.handle({"id": "1", "type": "abandon_run", "reason": "stale"})
        rpc.handle({"id": "2", "type": "abandon_run"})
        self.assertEqual((emitted[0]["data"], emitted[1]["error"]["code"]), ({"abandoned": True}, "nothing_to_resume"))
        rpc.handle({"id": "3", "type": "prompt", "message": "go"})
        self.assertTrue(rpc.wait_idle(5))
        self.assertEqual(emitted[-1]["text"], "one")
        rpc.close()

        identifier = open_run(agent, storage).id
        server = serve(agent, port=0, storage=storage)
        self.addCleanup(server.close)

        def request(path, body=None):
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", int(server.url.rsplit(":", 1)[1]), timeout=10)
            self.addCleanup(connection.close)
            connection.request("POST", path, json.dumps(body or {}), {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, [json.loads(line) for line in response.read().splitlines()]

        request("/sessions", {"sessionId": identifier})
        self.assertEqual(request(f"/sessions/{identifier}/abandon", {"reason": "stale"}), (200, [{"abandoned": True}]))
        status, body = request(f"/sessions/{identifier}/abandon")
        self.assertEqual((status, body[0]["error"]["code"]), (409, "nothing_to_resume"))
        self.assertEqual(request(f"/sessions/{identifier}/prompts", {"message": "go"})[1][-1]["text"], "two")

    def test_d36_interactive_cli_resume_and_abandon_commands(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory(prefix="ta-cli-") as directory:
            provider = ScriptedProvider([call("ask", identifier="q1"), "Blue it is.", call("ask", identifier="q2"),
                                         "fresh"])
            agent_tools = [Tool("ask", "Ask.", {"type": "object"}, lambda arguments: pause("which?"))]
            answers = iter(["first task", "/resume blue", "second task", "/abandon no longer needed", "third", EOFError()])

            def read(prompt=""):
                answer = next(answers)
                if isinstance(answer, BaseException):
                    raise answer
                return answer

            original = tiny_agent.bash_tool
            tiny_agent.bash_tool = lambda **options: agent_tools[0]
            tiny_agent.input = read
            output = io.StringIO()
            try:
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                    code = tiny_agent.main([], provider=provider, environ={"TINYAGENT_SESSION_DIR": directory})
            finally:
                tiny_agent.bash_tool = original
                del tiny_agent.input
            self.assertEqual(code, 0)
            self.assertIn("Blue it is.", output.getvalue())
            self.assertIn("Abandoned the open run.", output.getvalue())
            self.assertIn("fresh", output.getvalue())
            self.assertEqual(provider.requests[1]["history"][-1]["output"], "blue")
            self.assertEqual(provider.requests[3]["history"][-1]["content"], "third")

    def test_d36_cli_base_url_and_chat_provider(self):
        made = []

        class Recording:
            def __init__(self, key, **options):
                made.append((type(self).__name__, key, options))
                raise SystemExit("stop here")

        originals = tiny_agent.ChatCompletionsProvider, tiny_agent.OpenAIProvider
        tiny_agent.ChatCompletionsProvider = type("Chat", (Recording,), {})
        tiny_agent.OpenAIProvider = type("Responses", (Recording,), {})
        try:
            for name, extra in (("chat", {"TINYAGENT_BASE_URL": "http://localhost:11434/v1"}), ("openai", {})):
                with self.assertRaises(SystemExit):
                    tiny_agent.main(["-p", "x"], environ={"TINYAGENT_PROVIDER": name, "OPENAI_API_KEY": "k",
                                                          "TINYAGENT_MODEL": "some-model", **extra})
        finally:
            tiny_agent.ChatCompletionsProvider, tiny_agent.OpenAIProvider = originals
        self.assertEqual(made, [("Chat", "k", {"base_url": "http://localhost:11434/v1"}), ("Responses", "k", {})])

    def test_d37_events_include_failed_and_aborted_runs_with_their_real_status(self):
        token = CancellationToken()

        def cancel(arguments):
            token.cancel()
            token.raise_if_cancelled()

        provider = ScriptedProvider([tiny_agent.ProviderError("model is down"), call("stop"), "fine"])
        session = Session(Agent(model="offline", provider=provider, tools=[tool("stop", cancel)]))
        live = []
        with self.assertRaises(tiny_agent.ProviderError):
            session.run("first", on_event=live.append)
        with self.assertRaises(CancelledError):
            session.run("second", cancellation=token, on_event=live.append)
        session.run("third", on_event=live.append)
        events = session.events()["events"]
        self.assertEqual([(event["type"], event.get("status")) for event in events], [
            ("agent_start", None), ("agent_end", "failed"),
            ("agent_start", None), ("tool_execution_start", None), ("tool_execution_end", None),
            ("agent_end", "aborted"),
            ("agent_start", None), ("text_delta", None), ("agent_end", "completed")])
        self.assertEqual(events[1]["error"], {"code": "provider_error", "message": "model is down",
                                              "retryable": False})
        self.assertEqual((events[4]["output"], events[4]["isError"]), (INTERRUPTED, True))
        self.assertNotIn("error", events[5])
        # Live terminal events name the same record the replay does, and nothing failed is in history.
        self.assertEqual([event.data["cursor"] for event in live if event.type == "agent_end"],
                         [event["cursor"] for event in events if event["type"] == "agent_end"])
        self.assertEqual([item["content"] for item in session.history if item["role"] == "user"], ["third"])
        self.assertFalse(session.recovery_required)  # F6: a cancelled tool never requires reconcile

    def test_d38_a_resumed_stream_is_well_formed(self):
        def well_formed(events):
            started = set()
            for event in events:
                if event.type == "tool_execution_start":
                    started.add(event.data["toolCallId"])
                elif event.type == "tool_execution_end":
                    self.assertIn(event.data["toolCallId"], started)
            self.assertEqual((events[0].type, events[-1].type), ("agent_start", "agent_end"))
            self.assertEqual([event.sequence for event in events], list(range(1, len(events) + 1)))

        ran = []
        for idempotent, mark in ((False, "interrupted"), (True, "rerun")):
            agent = Agent(model="offline", provider=ScriptedProvider(["done"]),
                          tools=[tool("effect", lambda arguments: ran.append(True) or "ok", idempotent=idempotent)])
            session = open_run(agent, MemoryStorage(), ASSISTANT, {**INTENT, "idempotent": idempotent})
            events = []
            session.resume(on_event=events.append)
            well_formed(events)
            self.assertEqual([event.type for event in events],
                             ["agent_start", "tool_execution_start", "tool_execution_end", "text_delta", "agent_end"])
            self.assertIs(events[1].data[mark], True)
            self.assertEqual(events[1].data["cursor"], 6 if idempotent else 5)
            self.assertEqual(events[2].data["isError"], not idempotent)
        self.assertEqual(ran, [True])
        # A paused call answered on resume is announced the same way, without either mark.
        agent = Agent(model="offline", provider=ScriptedProvider([call("effect"), "done"]), tools=[tool()],
                      before_tool=lambda tool_call, context: pause())
        session = Session(agent)
        session.run("go")
        events = []
        session.resume(result="yes", on_event=events.append)
        well_formed(events)
        self.assertEqual(set(events[1].data) & {"interrupted", "rerun", "resumed"}, {"resumed"})

    def test_d39_trace_off_keeps_the_version_1_event_sequence(self):
        provider = ScriptedProvider([{"reasoning": "Hm.", "calls": call()}, "done"])
        events = []
        Session(Agent(model="offline", provider=provider, tools=[tool()])).run("go", on_event=events.append)
        self.assertEqual([event.type for event in events],
                         ["agent_start", "tool_execution_start", "tool_execution_end", "text_delta", "agent_end"])

    def test_d39_trace_on_every_model_request_start_has_an_end_including_on_cancel(self):
        token = CancellationToken()

        class Cancelled:
            """Streams some text, then the run is cancelled while the request is still open."""

            def stream(self, model, instructions, history, tools, cancellation):
                yield tiny_agent.ProviderEvent("text_delta", "partial")
                token.cancel()
                yield tiny_agent.ProviderEvent("text_delta", "never delivered")

        class Broken:
            def stream(self, model, instructions, history, tools, cancellation):
                raise KeyError("not a provider error")
                yield

        for provider, error, code in ((Cancelled(), CancelledError, "aborted"), (Broken(), KeyError, "provider_error"),
                                      (ScriptedProvider([tiny_agent.ProviderError("down")]), tiny_agent.ProviderError,
                                       "provider_error")):
            events = []
            with self.assertRaises(error):
                # The per-run option overrides the agent's setting.
                Agent(model="offline", provider=provider).run("go", cancellation=token, trace=True,
                                                              on_event=events.append)
            kinds = [event.type for event in events]
            self.assertEqual(kinds.count("model_request_start"), kinds.count("model_request_end"), kinds)
            self.assertEqual((kinds[-2], kinds[-1]), ("model_request_end", "agent_end"))
            self.assertEqual((events[-2].data["error"]["code"], events[-2].data["willRetry"]), (code, False))
            token = CancellationToken()
        quiet = []
        Agent(model="offline", provider=ScriptedProvider(["hi"]), trace=True).run("go", trace=False,
                                                                                   on_event=quiet.append)
        self.assertEqual([event.type for event in quiet], ["agent_start", "text_delta", "agent_end"])

    def test_d39_a_hook_that_raises_leaves_no_call_pending(self):
        def broken(*arguments):
            raise ValueError("bug in the hook")

        for hooks in ({"before_tool": broken}, {"after_tool": broken}):
            session = Session(Agent(model="offline", provider=ScriptedProvider([call(), "next"]), tools=[tool()],
                                    **hooks))
            events = []
            with self.assertRaises(HookError):
                session.run("go", on_event=events.append)
            self.assertEqual([event.type for event in events],
                             ["agent_start", "tool_execution_start", "tool_execution_end", "agent_end"])
            self.assertTrue(events[2].data["isError"])
            self.assertIn("bug in the hook", events[2].data["output"])
            self.assertEqual([record["type"] for record in session.records[-3:]],
                             ["tool_intent", "tool_result", "run_failed"])
            self.assertEqual(session.records[-1]["error"]["code"], "hook_error")
            session.agent.before_tool = session.agent.after_tool = None
            self.assertEqual(session.run("again").text, "next")

    def test_d39_dedupe_marks_agent_start_and_returns_the_stored_value(self):
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
        provider = ScriptedProvider([{"calls": call("final_output", n=7), "input_tokens": 3}])
        session = Session(Agent(model="offline", provider=provider))
        first = session.run("count", message_id="m1", output_schema=schema)
        events = []
        again = session.run("count", message_id="m1", output_schema=schema, on_event=events.append)
        self.assertEqual((again.value, again.output, again.usage, again.repeated), ({"n": 7}, [], first.usage, True))
        self.assertEqual([(event.type, event.data["repeated"]) for event in events],
                         [("agent_start", True), ("agent_end", True)])
        self.assertEqual(events[1].data["value"], {"n": 7})

    def test_d39_limits_and_usage_follow_the_rulings(self):
        # Exactly max_steps requests are allowed; the check before the next one fails.
        provider = ScriptedProvider([call(), call(), "third", "unused"])
        agent = Agent(model="offline", provider=provider, tools=[tool()], max_steps=3)
        self.assertEqual(agent.run("go").usage["requests"], 3)
        provider = ScriptedProvider([tiny_agent.ProviderError("busy", retryable=True), call(), "done"])
        agent = Agent(model="offline", provider=provider, tools=[tool()], max_steps=2,
                      sleep=lambda seconds, token: None)
        self.assertEqual(agent.run("go").usage["requests"], 2)  # the retried request is one step
        # Compaction's summary request is counted in the run that needed it, and in a compaction run.
        class Summaries(ScriptedProvider):
            def stream(self, model, instructions, history, tools, cancellation, extra=None):
                self.turns.insert(0, {"text": "summary" if instructions.startswith("Summarize") else "answer " * 100,
                                      "input_tokens": 100, "output_tokens": 10})
                return super().stream(model, instructions, history, tools, cancellation)

        budget = ContextBudget(2500, 100, lambda request: len(json.dumps(request)), keep_turns=1)
        session = Session(Agent(model="offline", provider=Summaries([])), budget=budget)
        compacted = None
        for index in range(5):
            events = []
            result = session.run(f"prompt {index}", on_event=events.append)
            if any(event.type == "compaction_end" for event in events):
                compacted = (result, events)
                break
        result, events = compacted
        self.assertEqual(result.usage, {"inputTokens": 200, "outputTokens": 20, "requests": 2})
        end = next(event for event in events if event.type == "compaction_end")
        self.assertEqual(session.records[end.data["cursor"] - 1]["type"], "checkpoint")
        session.run("one more")
        self.assertEqual(session.usage["requests"], 1)  # session.usage is the last run's

    def test_d39_timeout_gives_a_handler_one_second_and_then_moves_on(self):
        release = threading.Event()
        self.addCleanup(release.set)
        provider = ScriptedProvider([call("stuck"), "done"])
        agent = Agent(model="offline", provider=provider,
                      tools=[tool("stuck", lambda arguments: release.wait(30), timeout_seconds=0.05)])
        began = time.monotonic()
        events = []
        self.assertEqual(agent.run("go", on_event=events.append).text, "done")
        self.assertTrue(1.0 <= time.monotonic() - began < 3, time.monotonic() - began)
        self.assertEqual(events[2].data["output"], "timed out after 0.05 s")


class HarnessFindingTests(unittest.TestCase):
    """Bugs the black-box harness found (rulings F21 to F23)."""

    def test_f23_follow_joined_while_a_detached_run_commits_still_gets_agent_end(self):
        import http.client
        committing, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class SlowCommit(MemoryStorage):
            def append(self, session_id, expected, record):
                if record["type"] == "run_commit":
                    committing.set()
                    release.wait(5)
                return super().append(session_id, expected, record)

        server = serve(Agent(model="offline", provider=ScriptedProvider(["done", "again"])), port=0,
                       storage=SlowCommit())
        self.addCleanup(server.close)
        port = int(server.url.rsplit(":", 1)[1])

        def request(method, path, body=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            self.addCleanup(connection.close)
            connection.request(method, path, None if body is None else json.dumps(body),
                               {"Content-Type": "application/json"} if body is not None else {})
            return connection.getresponse()

        identifier = json.loads(request("POST", "/sessions").read())["sessionId"]
        prompt = request("POST", f"/sessions/{identifier}/prompts", {"message": "go", "detach": True})
        self.assertTrue(committing.wait(5))
        following = request("GET", f"/sessions/{identifier}/events?after=0&follow=1")
        threading.Timer(0.1, release.set).start()
        followed = [json.loads(line) for line in following.read().splitlines()]
        self.assertEqual([event["type"] for event in followed], ["agent_start", "text_delta", "agent_end"])
        self.assertEqual(followed[-1]["status"], "completed")
        # F21: once either body has ended the session is idle and takes the next prompt.
        prompt.read()
        self.assertEqual(json.loads(request("POST", f"/sessions/{identifier}/cancel").read()),
                         {"accepted": False, "runId": None})
        again = request("POST", f"/sessions/{identifier}/prompts", {"message": "more"})
        self.assertEqual((again.status, json.loads(again.read().splitlines()[-1])["text"]), (200, "again"))
        later = request("GET", f"/sessions/{identifier}/events?after={followed[-1]['cursor']}&follow=1")
        self.assertEqual([json.loads(line)["type"] for line in later.read().splitlines()],
                         ["agent_start", "text_delta", "agent_end"])

    def test_f22_an_open_run_always_knows_its_prompt(self):
        # The prompt is written with run_start, so no crash can separate the two.
        storage = MemoryStorage()
        provider = ScriptedProvider(["answered"])
        agent = Agent(model="offline", provider=provider)

        class DiesAfterFirstRunRecord(Exception):
            pass

        class Dying(MemoryStorage):
            def append(self, session_id, expected, record):
                sequence = storage.append(session_id, expected, record)
                if record["type"] == "run_start":
                    raise DiesAfterFirstRunRecord()
                return sequence

        session = Session(agent, storage=Dying())
        with self.assertRaises(tiny_agent.StorageError):
            session.run("the question")
        reopened = Session.open(agent, storage, session.id)
        self.assertTrue(reopened.resumable)
        self.assertEqual(reopened.resume().text, "answered")
        self.assertEqual(provider.requests[0]["history"], [{"role": "user", "content": "the question"}])
        self.assertEqual(reopened.history[0]["content"], "the question")
