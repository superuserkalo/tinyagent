"""The shared argument contract must hold before native handlers execute."""

import copy
import json
import unittest
from pathlib import Path

from tinyagent import Agent, CancellationToken, ProviderTurn, Tool, ToolCall


FIXTURE = json.loads(
    (Path(__file__).parents[2] / "conformance/tool_arguments.json").read_text()
)


class ToolTests(unittest.TestCase):
    def test_shared_argument_scenarios(self):
        for scenario in FIXTURE["cases"]:
            with self.subTest(scenario=scenario["name"]):
                calls = []
                tool = Tool(
                    "check",
                    "Check",
                    scenario["schema"],
                    lambda args: calls.append(args),
                )
                token = CancellationToken()
                for arguments in scenario["valid"]:
                    tool.execute(arguments, token)
                called_before = len(calls)
                for arguments in scenario["invalid"]:
                    original = copy.deepcopy(arguments)
                    with self.assertRaisesRegex(TypeError, "Invalid arguments"):
                        tool.execute(arguments, token)
                    self.assertEqual(arguments, original)
                self.assertEqual(len(calls), called_before)

    def test_invalid_schemas_fail_before_a_run(self):
        for schema in (
            {"type": "nonsense"},
            {"$ref": "https://example.invalid/schema"},
            {"$async": True},
            {"properties": {"name": {"$ref": "#/$defs/missing"}}},
        ):
            with self.subTest(schema=schema), self.assertRaises(Exception):
                Tool("check", "Check", schema, lambda args: "unused")

    def test_nested_ids_and_recursive_local_references(self):
        schema = {
            "$id": "https://example.invalid/root",
            "type": "object",
            "$defs": {
                "node": {
                    "$id": "node",
                    "type": "object",
                    "properties": {
                        "value": {"type": "integer"},
                        "next": {"$ref": "node"},
                    },
                    "required": ["value"],
                }
            },
            "properties": {"head": {"$ref": "node"}},
            "required": ["head"],
        }
        tool = Tool("check", "Check", schema, lambda args: "ok")
        token = CancellationToken()
        self.assertEqual(
            tool.execute({"head": {"value": 1, "next": {"value": 2}}}, token), "ok"
        )
        with self.assertRaisesRegex(TypeError, "Invalid arguments"):
            tool.execute({"head": {"value": "bad"}}, token)

    def test_schema_failure_is_a_recoverable_observation(self):
        handled = []
        observed = []

        class Provider:
            calls = 0

            def stream(self, model, instructions, history, tools, token):
                self.calls += 1
                yield from ()
                if self.calls == 1:
                    return ProviderTurn(
                        "",
                        {"role": "assistant", "content": []},
                        [],
                        [
                            ToolCall("bad", "echo", {"text": 42}),
                        ],
                    )
                observed.append(history[-1])
                return ProviderTurn(
                    "recovered", {"role": "assistant", "content": []}, [], []
                )

        tool = Tool(
            "echo",
            "Echo",
            FIXTURE["cases"][0]["schema"],
            lambda args: handled.append(args),
        )
        events = []
        result = Agent(model="test", provider=Provider(), tools=[tool]).run(
            "go", on_event=events.append
        )
        self.assertEqual(result.text, "recovered")
        self.assertFalse(handled)
        self.assertTrue(observed[0]["isError"])
        self.assertIn("Invalid arguments", observed[0]["output"])
        self.assertEqual(
            [event.type for event in events],
            [
                "agent_start",
                "tool_execution_start",
                "tool_execution_end",
                "agent_end",
            ],
        )


if __name__ == "__main__":
    unittest.main()
