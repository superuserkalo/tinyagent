"""Bounded JSON request bodies and HTTP validation errors."""

import json
import re
from http.server import BaseHTTPRequestHandler

from tinyagent_types import JsonObject, MAX_HTTP_BODY_BYTES


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def read_body(request: BaseHTTPRequestHandler, *, required: bool) -> JsonObject | None:
    content_type = (
        request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    )
    if required and content_type != "application/json":
        raise HttpError(
            415, "unsupported_media_type", "Content-Type must be application/json"
        )
    lengths = request.headers.get_all("Content-Length", [])
    transfers = request.headers.get_all("Transfer-Encoding", [])
    transfer = transfers[0].lower() if transfers else ""
    if len(lengths) > 1 or len(transfers) > 1 or (transfers and lengths):
        raise HttpError(400, "invalid_request", "Ambiguous request body framing")
    if transfers:
        if transfer != "chunked":
            raise HttpError(400, "invalid_request", "Unsupported transfer encoding")
        body = bytearray()
        while True:
            line = request.rfile.readline(8193)
            size_text = line.split(b";", 1)[0].strip()
            if (
                len(line) > 8192
                or not line.endswith(b"\r\n")
                or not re.fullmatch(rb"[0-9a-fA-F]+", size_text)
            ):
                raise HttpError(400, "invalid_request", "Invalid chunk framing")
            size = int(size_text, 16)
            if len(body) + size > MAX_HTTP_BODY_BYTES:
                raise HttpError(
                    413, "request_too_large", "Request body exceeds the byte limit"
                )
            if size == 0:
                trailer_bytes = 0
                while True:
                    trailer = request.rfile.readline(8193)
                    trailer_bytes += len(trailer)
                    if trailer_bytes > 8192 or not trailer.endswith(b"\r\n"):
                        raise HttpError(400, "invalid_request", "Invalid chunk trailer")
                    if trailer == b"\r\n":
                        break
                break
            chunk = request.rfile.read(size)
            if len(chunk) != size or request.rfile.read(2) != b"\r\n":
                raise HttpError(400, "invalid_request", "Incomplete chunk")
            body.extend(chunk)
    else:
        length_text = lengths[0] if lengths else "0"
        if not re.fullmatch(r"[0-9]+", length_text):
            raise HttpError(400, "invalid_request", "Invalid Content-Length")
        length_text = length_text.lstrip("0") or "0"
        if len(length_text) > len(str(MAX_HTTP_BODY_BYTES)):
            raise HttpError(
                413, "request_too_large", "Request body exceeds the byte limit"
            )
        size = int(length_text)
        if size > MAX_HTTP_BODY_BYTES:
            raise HttpError(
                413, "request_too_large", "Request body exceeds the byte limit"
            )
        body = request.rfile.read(size)
        if len(body) != size:
            raise HttpError(400, "invalid_request", "Incomplete request body")
    if not body and not required:
        return None

    def invalid_constant(value: str):
        raise ValueError(f"Invalid JSON number: {value}")

    try:
        value = json.loads(body.decode("utf-8"), parse_constant=invalid_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        # Older Pythons raise RecursionError for deeply nested JSON.
        raise HttpError(
            400, "invalid_json", "Request body must be valid UTF-8 JSON"
        ) from error
    if not isinstance(value, dict):
        raise HttpError(400, "invalid_request", "Request body must be an object")
    return value
