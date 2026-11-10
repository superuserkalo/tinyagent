"""HTTP routes through native client/server parsing over in-memory streams."""

import json
import unittest

from http_fixture import HttpFixture
from tinyagent import Agent, MAX_HTTP_BODY_BYTES, ProviderTurn


class HttpRouteTests(unittest.TestCase):
    def test_authentication_covers_every_route_and_errors_have_no_cors(self):
        fixture = HttpFixture(
            Agent(model="test", transport=lambda payload: []), "secret"
        )
        self.addCleanup(fixture.close)
        auth = [("Authorization", "Bearer secret")]
        session = fixture.session(auth)
        routes = [
            ("GET", "/health"),
            ("POST", "/sessions"),
            ("POST", f"/sessions/{session}/prompts"),
            ("POST", f"/sessions/{session}/cancel"),
            ("DELETE", f"/sessions/{session}"),
        ]
        for method, path in routes:
            for headers in ([], [("Authorization", "Bearer wrong")], auth + auth):
                with self.subTest(method=method, path=path, headers=headers):
                    response = fixture.call(method, path, headers=headers)
                    self.assertEqual(response.status, 401)
                    self.assertEqual(
                        json.loads(response.read())["error"]["code"], "unauthorized"
                    )
                    self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
                    self.assertEqual(response.getheader("Cache-Control"), "no-store")
        health = fixture.call("GET", "/health", headers=auth)
        self.assertEqual(
            json.loads(health.read()), {"status": "ok", "protocolVersion": 1}
        )
        idle = fixture.call("POST", f"/sessions/{session}/cancel", headers=auth)
        self.assertEqual(json.loads(idle.read()), {"accepted": False, "runId": None})
        for method, path in [
            ("OPTIONS", "/health"),
            ("GET", "/missing"),
            ("POST", "/sessions/missing/prompts"),
        ]:
            response = fixture.call(method, path, headers=auth)
            self.assertEqual(response.status, 404)
            self.assertIn("error", json.loads(response.read()))
            self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        deleted = fixture.call("DELETE", f"/sessions/{session}", headers=auth)
        self.assertEqual(deleted.status, 204)
        deleted.read()

    def test_fixed_and_chunked_bodies_preserve_multibyte_text(self):
        observed = []

        class Provider:
            def stream(self, model, instructions, history, tools, token):
                observed.append(history[-1]["content"])
                yield from ()
                return ProviderTurn("ok", {"role": "assistant", "content": []}, [], [])

        fixture = HttpFixture(Agent(model="test", provider=Provider()))
        self.addCleanup(fixture.close)
        session = fixture.session()
        data = '{"message":"café"}'.encode()
        for chunked in (False, True):
            with self.subTest(chunked=chunked):
                framing = (
                    ("Transfer-Encoding", "chunked")
                    if chunked
                    else ("Content-Length", str(len(data)))
                )
                client = fixture.headers(
                    "POST",
                    f"/sessions/{session}/prompts",
                    [framing, ("Content-Type", "application/json")],
                )
                if chunked:
                    # Each byte is a chunk, including the two bytes of the accented letter.
                    for byte in data:
                        client.send(b"1\r\n" + bytes([byte]) + b"\r\n")
                    client.send(b"0\r\nX-Trace: test\r\n\r\n")
                else:
                    client.send(data)
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(response.chunked)
                self.assertEqual(
                    json.loads(response.read().splitlines()[-1])["status"], "completed"
                )
                fixture.wait_idle(session)
        self.assertEqual(observed, ["café", "café"])

    def test_validation_and_framing_errors_use_bounded_json_responses(self):
        fixture = HttpFixture(Agent(model="test", transport=lambda payload: []))
        self.addCleanup(fixture.close)
        session = fixture.session()
        cases = [
            ([], b"", 415),
            ([("Content-Length", "1")], b"{", 400),
            ([("Content-Length", "2")], b"[]", 400),
            ([("Content-Length", "2")], b"{}", 400),
            ([("Content-Length", "1")], b"\xff", 400),
            ([("Content-Length", "3")], b"NaN", 400),
            ([("Content-Length", "20001")], b"[" * 10000 + b"0" + b"]" * 10000, 400),
            ([("Content-Length", str(MAX_HTTP_BODY_BYTES + 1))], b"", 413),
            ([("Content-Length", "9" * 5000)], b"", 413),
            ([("Content-Length", "2"), ("Content-Length", "2")], b"", 400),
            ([("Transfer-Encoding", "chunked"), ("Content-Length", "0")], b"", 400),
            (
                [("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "gzip")],
                b"",
                400,
            ),
            ([("Transfer-Encoding", "")], b"", 400),
            (
                [("Transfer-Encoding", "chunked")],
                f"{MAX_HTTP_BODY_BYTES + 1:x}\r\n".encode(),
                413,
            ),
        ]
        for headers, body, status in cases:
            with self.subTest(headers=headers, status=status):
                if headers:
                    headers = [*headers, ("Content-Type", "application/json")]
                client = fixture.headers(
                    "POST", f"/sessions/{session}/prompts", headers
                )
                if body:
                    client.send(body)
                response = client.getresponse()
                self.assertEqual(response.status, status)
                payload = response.read()
                self.assertLess(len(payload), 1024)
                self.assertEqual(
                    int(response.getheader("Content-Length")), len(payload)
                )
                self.assertIn("error", json.loads(payload))


if __name__ == "__main__":
    unittest.main()
