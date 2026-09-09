"""Bounded, authenticated loopback transport for channel events."""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from collections.abc import Callable
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)
_MAX_BODY = 32768
_READ_TIMEOUT = 5
_META_KEY = re.compile(r"[A-Za-z0-9_]{1,64}\Z", re.ASCII)
_CONTENT_TYPE = re.compile(
    r'application/json(?:\s*;\s*charset\s*=\s*(?:[A-Za-z0-9_-]+|"[A-Za-z0-9_-]+"))?\s*\Z',
    re.IGNORECASE | re.ASCII,
)


class WebhookRejected(Exception):
    """Reject a validated event with an explicit HTTP error status."""

    def __init__(self, status: int, message: str):
        if type(status) is not int or not 400 <= status <= 599:
            raise ValueError("Rejection status must be an HTTP error status")
        if not isinstance(message, str):
            raise TypeError("Rejection message must be a string")
        super().__init__(message)
        self.status = status
        self.message = message


def _reject_constant(value: str):
    raise ValueError("Nonfinite JSON constant")


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _validate(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.keys() - {"id", "content", "meta"}:
        raise ValueError("Invalid event fields")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip() or len(content) > 8000:
        raise ValueError("Invalid event content")
    if "id" in payload:
        event_id = payload["id"]
        if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 128:
            raise ValueError("Invalid event id")
    if "meta" in payload:
        meta = payload["meta"]
        if not isinstance(meta, dict) or len(meta) > 16:
            raise ValueError("Invalid event metadata")
        for key, value in meta.items():
            if (
                not _META_KEY.fullmatch(key)
                or not isinstance(value, str)
                or len(value) > 512
            ):
                raise ValueError("Invalid event metadata")
    return payload


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 8

    def __init__(self, port: int, token: bytes, callback: Callable[[dict], dict]):
        self.token = token
        self.callback = callback
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", port), _Handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(_READ_TIMEOUT)
        return request, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 31\r\nConnection: close\r\n\r\n"
                    b'{"error":"Service unavailable"}'
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        logger.error("Webhook server failure")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _HTTPServer

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self._respond(code, {"error": "Invalid HTTP request"})

    def _respond(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8")
        self.close_connection = True
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str):
        self._respond(status, {"error": message})

    def _unsupported(self):
        self._error(405, "Method not allowed")

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._unsupported
        raise AttributeError(name)

    def do_POST(self):
        try:
            self._post()
        except Exception:
            # Callback exceptions may embed credentials or request bodies.
            logger.exception(
                "Webhook server failure",
                exc_info=(
                    RuntimeError,
                    RuntimeError("Webhook processing failed; details withheld"),
                    None,
                ),
            )
            try:
                self._error(500, "Server failure")
            except OSError:
                self.close_connection = True

    def _post(self):
        if self.path != "/webhook":
            self._error(404, "Not found")
            return
        hosts = self.headers.get_all("Host", [])
        allowed_hosts = {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }
        if len(hosts) != 1 or hosts[0] not in allowed_hosts or "Origin" in self.headers:
            self._error(403, "Forbidden")
            return
        authorization = self.headers.get_all("Authorization", [])
        supplied = b""
        if len(authorization) == 1:
            with suppress(UnicodeEncodeError):
                supplied = authorization[0].encode("ascii")
        if not hmac.compare_digest(supplied, self.server.token):
            self._error(401, "Unauthorized")
            return
        if "Transfer-Encoding" in self.headers:
            self._error(400, "Transfer encoding is not supported")
            return
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1 or not _CONTENT_TYPE.fullmatch(content_types[0]):
            self._error(415, "JSON content type required")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0], re.ASCII):
            self._error(400, "Invalid content length")
            return
        # Avoid parsing arbitrarily large decimal integers from a header.
        digits = lengths[0].lstrip("0")
        if len(digits) > 5 or (digits and int(digits) > _MAX_BODY):
            self._error(413, "Request body too large")
            return
        length = int(digits or "0")
        if length <= 0:
            self._error(400, "Invalid content length")
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete body")
            payload = _validate(
                json.loads(
                    body.decode("utf-8"),
                    parse_constant=_reject_constant,
                    object_pairs_hook=_json_object,
                )
            )
        except (UnicodeDecodeError, ValueError, RecursionError, TimeoutError):
            self._error(400, "Invalid JSON event")
            return
        try:
            result = self.server.callback(payload)
        except WebhookRejected as rejection:
            self._error(rejection.status, rejection.message)
            return
        if not isinstance(result, dict):
            raise TypeError("Webhook callback must return a JSON object")
        self._respond(202, result)


class WebhookServer:
    """Start explicitly after channel delivery is confirmed; never publishes on its own."""

    def __init__(self, token: str, callback: Callable[[dict], dict], port: int = 0):
        if (
            not isinstance(token, str)
            or len(token) < 32
            or not token.isascii()
            or any(ord(character) <= 32 or ord(character) >= 127 for character in token)
        ):
            raise ValueError(
                "Webhook token requires at least 32 printable ASCII characters"
            )
        if not callable(callback):
            raise TypeError("Webhook callback must be callable")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Invalid webhook port")
        self._token = b"Bearer " + token.encode("ascii")
        self._callback = callback
        self._requested_port = port
        self._port: int | None = None
        self._server: _HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("Webhook server has not started")
        return self._port

    def start(self) -> int:
        with self._lock:
            if self._server is not None:
                return self.port
            server = _HTTPServer(self._requested_port, self._token, self._callback)
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="tandem-webhook",
                daemon=True,
            )
            try:
                thread.start()
            except Exception:
                server.server_close()
                raise
            self._server = server
            self._thread = thread
            self._port = server.server_port
            return self.port

    def stop(self) -> None:
        with self._lock:
            if self._server is None:
                return
            self._server.shutdown()
            self._server.server_close()
            if self._thread is not None:
                self._thread.join()
            self._server = None
            self._thread = None
