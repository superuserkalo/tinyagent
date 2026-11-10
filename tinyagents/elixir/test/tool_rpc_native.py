"""Tool events, bounded recovery, and cancellation through a native RPC process."""
import sys
from rpc_client import Client

beam, jason, *dependencies = sys.argv[1:]
client = Client(beam, jason, fixture="tool_rpc_fixture.exs", arguments=dependencies)
try:
    for text in ["café", "invalid"]:
        client.prompt(text)
        values = client.terminal()
        assert values[0]["command"] == "prompt" and values[0]["success"]
        assert [v["type"] for v in values[1:]] == ["agent_start", "tool_execution_start", "tool_execution_end", "text_delta", "agent_end"]
        assert [v["sequence"] for v in values[1:]] == [1, 2, 3, 4, 5]
        assert len({v["runId"] for v in values[1:]}) == 1
        assert values[2]["arguments"] == {"text": "" if text == "invalid" else text}
        assert values[2]["toolCallId"] == values[3]["toolCallId"]
        assert values[3]["isError"] == (text == "invalid")
        assert values[5]["text"] == values[3]["output"]
        assert values[5]["status"] == "completed"
    assert client.idle()["messageCount"] == 8
    client.prompt("wait")
    client.value()
    assert client.value()["type"] == "agent_start"
    assert client.value()["type"] == "tool_execution_start"
    assert client.line("stderr") == b"tool-waiting"
    client.send({"id": "cancel", "type": "abort"})
    values = [client.value() for _ in range(3)]
    assert next(v for v in values if v.get("id") == "cancel")["data"]["accepted"]
    events = [v for v in values if v["type"] != "response"]
    assert events[0]["type"] == "tool_execution_end" and events[0]["isError"]
    assert events[1]["status"] == "aborted"
    assert [v["sequence"] for v in events] == [3, 4]
    assert client.line("stderr") == b"tool-cleaned"
    assert client.idle()["messageCount"] == 8
    client.prompt("history")
    assert client.terminal()[-1]["text"] == "messages=9"
    assert client.finish() == []
    print("tools passed")
finally:
    client.close()
