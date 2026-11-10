"""Request-body framing tests independent of OS socket permissions."""

import io
import unittest
from http.client import HTTPMessage
from types import SimpleNamespace

from tinyagent_http_body import HttpError, read_body
from tinyagent_types import MAX_HTTP_BODY_BYTES


def request(body, **headers):
    message = HTTPMessage()
    message["Content-Type"] = "application/json"
    for key, value in headers.items():
        message[key.replace("_", "-")] = str(value)
    return SimpleNamespace(headers=message, rfile=io.BytesIO(body))


class HttpParsingTests(unittest.TestCase):
    def test_content_length_and_utf8(self):
        body = '{"message":"café"}'.encode()
        self.assertEqual(
            read_body(request(body, Content_Length=len(body)), required=True),
            {"message": "café"},
        )

    def test_chunked_request_and_trailers(self):
        body = b'10;name=value\r\n{"message":"go"}\r\n0\r\nX-Trace: test\r\n\r\n'
        self.assertEqual(
            read_body(request(body, Transfer_Encoding="chunked"), required=True),
            {"message": "go"},
        )

    def test_optional_empty_body(self):
        self.assertIsNone(read_body(request(b""), required=False))
        with self.assertRaises(HttpError) as caught:
            read_body(request(b"", Transfer_Encoding=""), required=False)
        self.assertEqual(caught.exception.status, 400)

    def test_invalid_json_and_utf8(self):
        for body in (b"{", b"[]", b"null", b"\xff", b'{"message":"go","invalid":NaN}'):
            with self.subTest(body=body), self.assertRaises(HttpError) as caught:
                read_body(request(body, Content_Length=len(body)), required=True)
            self.assertEqual(caught.exception.status, 400)

    def test_ambiguous_and_incomplete_framing(self):
        inputs = [
            request(b"{}", Content_Length="-1"),
            request(b"{}", Content_Length="+2"),
            request(b"{}", Content_Length="4"),
            request(b"{}", Content_Length="2", Transfer_Encoding="chunked"),
            request(b"{}", Transfer_Encoding="gzip"),
            request(b"GG\r\n", Transfer_Encoding="chunked"),
            request(b"2\r\n{}xx", Transfer_Encoding="chunked"),
            request(b"0\r\n", Transfer_Encoding="chunked"),
        ]
        duplicate = request(b"{}", Content_Length="2")
        duplicate.headers["Content-Length"] = "2"
        inputs.append(duplicate)
        duplicate_transfer = request(
            b"2\r\n{}\r\n0\r\n\r\n", Transfer_Encoding="chunked"
        )
        duplicate_transfer.headers["Transfer-Encoding"] = "gzip"
        inputs.append(duplicate_transfer)
        for value in inputs:
            with self.subTest(headers=list(value.headers.items())), self.assertRaises(
                HttpError
            ) as caught:
                read_body(value, required=True)
            self.assertEqual(caught.exception.status, 400)

    def test_limits_apply_before_large_reads(self):
        for value in (
            request(b"", Content_Length=MAX_HTTP_BODY_BYTES + 1),
            request(
                f"{MAX_HTTP_BODY_BYTES + 1:x}\r\n".encode(), Transfer_Encoding="chunked"
            ),
        ):
            with self.assertRaises(HttpError) as caught:
                read_body(value, required=True)
            self.assertEqual(caught.exception.status, 413)

    def test_long_decimal_lengths_are_checked_without_unbounded_integer_conversion(
        self,
    ):
        with self.assertRaises(HttpError) as caught:
            read_body(request(b"", Content_Length="9" * 5000), required=True)
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(
            read_body(request(b"{}", Content_Length="0" * 5000 + "2"), required=True),
            {},
        )


if __name__ == "__main__":
    unittest.main()
