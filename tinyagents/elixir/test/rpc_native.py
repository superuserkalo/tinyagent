"""Native JSONL black-box checks against the supplied compiled Elixir SDK."""

import json
import os
from pathlib import Path
import signal
import sys
import time

from rpc_client import Client, LIMIT


def check(mode, client):
    if mode == "shared":
        fixture = Path(__file__).resolve().parents[3] / "conformance/rpc_v1.json"
        expected = []
        for scenario in json.loads(fixture.read_text())["scenarios"]:
            if "expected" in scenario:
                for command in scenario["commands"]:
                    client.send(command)
                expected += scenario["expected"]
        assert client.finish() == expected
        assert not client.stderr
    elif mode == "completion":
        client.prompt("go")
        values = client.terminal()
        assert values[0]["command"] == "prompt" and values[0]["success"]
        assert [v["type"] for v in values[1:]] == [
            "agent_start",
            "text_delta",
            "agent_end",
        ]
        assert [v["sequence"] for v in values[1:]] == [1, 2, 3]
        assert {v["runId"] for v in values[1:]} == {values[0]["data"]["runId"]}
        assert (
            values[-1]["text"] == "done\ncafé" and values[-1]["status"] == "completed"
        )
        assert client.idle()["messageCount"] == 2
        client.prompt("next")
        client.terminal()
        assert client.idle()["messageCount"] == 4
        assert client.finish() == []
        assert "provider:next:3" in client.stderr
    elif mode == "abort":
        client.prompt("wait")
        assert client.value()["command"] == "prompt"
        assert client.value()["type"] == "agent_start"
        assert client.value()["delta"] == "waiting"
        client.prompt("busy", "busy")
        assert client.value()["error"]["code"] == "busy"
        client.send({"id": "abort", "type": "abort"})
        values = [client.value(), client.value()]
        response = next(v for v in values if v.get("id") == "abort")
        assert response["data"]["accepted"]
        assert (
            next(v for v in values if v.get("type") == "agent_end")["status"]
            == "aborted"
        )
        assert client.idle()["messageCount"] == 0
        client.prompt("go")
        client.terminal()
        assert client.idle()["messageCount"] == 2
        client.finish()
    elif mode in {"eof", "interrupt"}:
        client.prompt("wait")
        for _ in range(3):
            client.value()
        if mode == "interrupt":
            client.process.send_signal(signal.SIGINT)
            client.process.wait(timeout=3)
        values = client.finish()
        assert len(values) == 1 and values[0]["status"] == "aborted", values
        assert "cleanup:wait" in client.stderr
    elif mode == "errors":
        client.prompt("go")
        client.terminal()
        client.idle()
        client.prompt("error")
        assert client.terminal()[-1]["status"] == "failed"
        assert client.idle()["messageCount"] == 2
        client.prompt("huge")
        values = [client.value() for _ in range(3)]
        assert values[-1]["error"]["code"] == "output_too_large", values
        assert client.idle()["messageCount"] == 2
        client.finish()
    elif mode == "framing":
        client.send_bytes(b"{\nNaN\n1e9999\n")
        for _ in range(3):
            assert client.value()["error"]["code"] == "invalid_json"
        command = json.dumps(
            {"type": "get_state", "id": "line\ncafé"}, ensure_ascii=False
        ).encode()
        split = command.index(b"\xc3") + 1
        client.send_bytes(command[:split])
        client.send_bytes(command[split:] + b"\r\n")
        assert client.value()["id"] == "line\ncafé"
        client.send_bytes(b'{"id":"last","type":"get_state"}')
        assert client.finish()[0]["id"] == "last"
    elif mode in {"utf8", "oversized_input"}:
        client.send_bytes(b"\xff\n" if mode == "utf8" else b" " * (LIMIT + 1))
        values = client.finish()
        assert len(values) == 1, values
        assert values[0]["error"]["code"] == (
            "invalid_utf8" if mode == "utf8" else "line_too_large"
        )
    elif mode == "exact_limit":
        prefix = b'{"type":"get_state","extra":"'
        suffix = b'"}'
        client.send_bytes(
            prefix + b"x" * (LIMIT - len(prefix) - len(suffix)) + suffix + b"\n"
        )
        assert client.value()["data"]["status"] == "idle"
        client.finish()
    elif mode == "reset_cleanup":
        client.prompt("held")
        for _ in range(3):
            client.value()
        assert client.line("stderr").startswith(b"provider:held:")
        client.send({"id": "reset", "type": "new_session"})
        assert client.line("stderr") == b"cleanup:held"
        time.sleep(0.05)
        assert client.process.poll() is None
        os.write(client.control, b"x")
        values = [client.value(), client.value()]
        assert next(v for v in values if v.get("id") == "reset")["data"]["abortedRun"]
        assert (
            next(v for v in values if v.get("type") == "agent_end")["status"]
            == "aborted"
        )
        assert client.idle()["messageCount"] == 0
        client.finish()
    else:
        raise AssertionError(mode)


def main():
    mode, beam, jason = sys.argv[1:]
    client = Client(beam, jason, control=mode == "reset_cleanup")
    try:
        check(mode, client)
        print(mode + " passed")
    finally:
        client.close()


if __name__ == "__main__":
    main()
