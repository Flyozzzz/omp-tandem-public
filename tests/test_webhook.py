"""Webhook security boundaries exercised through real loopback HTTP requests."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from omp_tandem.webhook import WebhookRejected, WebhookServer


class WebhookServerTests(unittest.TestCase):
    def setUp(self):
        self.token = "test-webhook-secret-" + "a" * 32
        self.received = []
        self.callback = self.accept
        self.server = WebhookServer(self.token, lambda payload: self.callback(payload))
        self.port = self.server.start()
        self.addCleanup(self.server.stop)

    def accept(self, payload):
        self.received.append(payload)
        return {"accepted": True, "event_id": payload.get("id", "generated")}

    def request(
        self, payload=None, *, body=None, headers=None, method="POST", path="/webhook"
    ):
        if body is None:
            body = json.dumps(
                payload if payload is not None else {"content": "hello"}
            ).encode()
        values = {
            "Host": f"127.0.0.1:{self.port}",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }
        values.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.putrequest(
                method, path, skip_host=True, skip_accept_encoding=True
            )
            for key, value in values.items():
                if value is not None:
                    connection.putheader(key, value)
            connection.endheaders(body)
            response = connection.getresponse()
            data = response.read()
            return (
                response.status,
                json.loads(data) if data else None,
                dict(response.getheaders()),
            )
        finally:
            connection.close()

    def assert_rejected(self, status, **kwargs):
        before = len(self.received)
        actual, _, headers = self.request(**kwargs)
        self.assertEqual(actual, status)
        self.assertEqual(headers["Connection"], "close")
        self.assertEqual(len(self.received), before)

    def test_authenticated_delivery_preserves_validated_payload(self):
        payload = {
            "id": "request-1",
            "content": "  café 雪\n",
            "meta": {"source_1": "local"},
        }
        status, result, headers = self.request(
            payload,
            headers={
                "Host": f"localhost:{self.port}",
                "Content-Type": 'application/json; charset="utf-8"',
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(result, {"accepted": True, "event_id": "request-1"})
        self.assertEqual(self.received, [payload])
        self.assertEqual(headers["Connection"], "close")

    def test_missing_wrong_or_non_bearer_authorization_never_delivers(self):
        for authorization in (None, "Bearer wrong", self.token, f"Basic {self.token}"):
            with self.subTest(authorization_kind=authorization is None):
                self.assert_rejected(401, headers={"Authorization": authorization})

    def test_browser_origin_and_non_loopback_hosts_never_deliver(self):
        for origin in ("https://example.com", "null", ""):
            with self.subTest(origin=origin):
                self.assert_rejected(403, headers={"Origin": origin})
        for host in (
            None,
            "example.com",
            "localhost",
            "127.0.0.1:1",
            f"127.0.0.1:{self.port}.evil",
        ):
            with self.subTest(host=host):
                self.assert_rejected(403, headers={"Host": host})

    def test_routes_and_methods_do_not_publish(self):
        for path in ("/", "/webhook/", "/webhook?token=not-a-real-secret"):
            with self.subTest(path=path):
                self.assert_rejected(404, path=path)
        for method in ("GET", "HEAD", "OPTIONS", "PUT", "DELETE", "PATCH", "CONNECT"):
            with self.subTest(method=method):
                self.assert_rejected(405, method=method)

    def test_payload_boundaries(self):
        boundary = {
            "id": "i" * 128,
            "content": "c" * 8000,
            "meta": {f"k{index}": "v" * 512 for index in range(16)},
        }
        status, _, _ = self.request(boundary)
        self.assertEqual(status, 202)
        self.assertEqual(self.received, [boundary])
        self.assertEqual(
            self.request({"content": "ok", "meta": {"k" * 64: ""}})[0], 202
        )
        invalid = [
            [],
            None,
            {"content": "ok", "unexpected": True},
            {},
            {"content": ""},
            {"content": " \n\t"},
            {"content": 7},
            {"content": "x" * 8001},
            {"content": "ok", "id": ""},
            {"content": "ok", "id": " \t"},
            {"content": "ok", "id": "i" * 129},
            {"content": "ok", "id": 1},
            {"content": "ok", "meta": []},
            {"content": "ok", "meta": {f"k{i}": "v" for i in range(17)}},
            {"content": "ok", "meta": {"": "v"}},
            {"content": "ok", "meta": {"k" * 65: "v"}},
            {"content": "ok", "meta": {"unicode_é": "v"}},
            {"content": "ok", "meta": {"with-dash": "v"}},
            {"content": "ok", "meta": {"key": "v" * 513}},
            {"content": "ok", "meta": {"key": 1}},
        ]
        for index, payload in enumerate(invalid):
            with self.subTest(case=index):
                self.assert_rejected(400, body=json.dumps(payload).encode())

    def test_transport_framing_and_json_errors(self):
        for content_type in (None, "text/plain", "application/jsonp"):
            with self.subTest(content_type=content_type):
                self.assert_rejected(415, headers={"Content-Type": content_type})
        for content_length in (None, "0", "-1", "+12", "abc", "1, 1"):
            with self.subTest(content_length=content_length):
                self.assert_rejected(400, headers={"Content-Length": content_length})
        self.assert_rejected(400, headers={"Transfer-Encoding": "chunked"})
        self.assert_rejected(413, body=b" " * 32769)
        self.assert_rejected(413, headers={"Content-Length": "9" * 100})
        for body in (
            b'{"content":',
            b'{"content":"\xff"}',
            b'{"content":NaN}',
            b'{"content":Infinity}',
            b'{"content":-Infinity}',
            b'{"content":"first","content":"second"}',
            b'{"content":"ok","meta":{"a":NaN}}',
        ):
            with self.subTest(body=body):
                self.assert_rejected(400, body=body)
        padded = b'{"content":"ok"}' + b" " * (32768 - len(b'{"content":"ok"}'))
        self.assertEqual(self.request(body=padded)[0], 202)

    def test_callback_rejection_propagates_without_second_delivery(self):
        seen = set()

        def deduplicate(payload):
            if payload["id"] in seen:
                raise WebhookRejected(409, "Duplicate event")
            seen.add(payload["id"])
            return self.accept(payload)

        self.callback = deduplicate
        payload = {"id": "same-event", "content": "hello"}
        self.assertEqual(self.request(payload)[0], 202)
        self.assert_rejected(409, payload=payload)
        self.assertEqual(self.received, [payload])
        for status in (429, 503):

            def reject(payload, status=status):
                raise WebhookRejected(status, "Unavailable")

            self.callback = reject
            self.assert_rejected(status)

    def test_unexpected_callback_error_is_generic_and_does_not_leak(self):
        private = "private-request-marker"

        def fail(payload):
            raise TimeoutError(f"{self.token}: {payload['content']}")

        self.callback = fail
        with self.assertLogs(WebhookServer.__module__, level="ERROR") as captured:
            status, result, _ = self.request({"content": private})
        self.assertEqual(status, 500)
        surface = json.dumps(result) + "\n".join(captured.output)
        self.assertNotIn(self.token, surface)
        self.assertNotIn(private, surface)

    def test_concurrent_handler_limit_rejects_excess_connections(self):
        condition = threading.Condition()
        release = threading.Event()
        entered = 0

        def hold(payload):
            nonlocal entered
            with condition:
                entered += 1
                condition.notify_all()
            release.wait(timeout=4)
            return {"accepted": True}

        self.callback = hold
        with ThreadPoolExecutor(max_workers=8) as executor:
            pending = [executor.submit(self.request) for _ in range(8)]
            try:
                with condition:
                    self.assertTrue(condition.wait_for(lambda: entered == 8, timeout=3))
                # Capacity is rejected before HTTP parsing. Read the early response
                # rather than racing a request body against the server's close.
                with (
                    socket.create_connection(
                        ("127.0.0.1", self.port), timeout=1
                    ) as excess,
                    excess.makefile("rb") as response,
                ):
                    self.assertEqual(response.readline(1024).split()[1], b"503")
            finally:
                release.set()
            self.assertEqual([future.result()[0] for future in pending], [202] * 8)

    def test_stop_is_idempotent_and_releases_listener(self):
        self.assertEqual(self.server.port, self.port)
        self.assertEqual(self.server.start(), self.port)
        self.server.stop()
        self.server.stop()
        with (
            self.assertRaises(OSError),
            socket.create_connection(("127.0.0.1", self.port), timeout=1),
        ):
            self.fail("Stopped listener accepted a connection")

    def test_invalid_tokens_cannot_start_a_listener(self):
        for token in ("short", "é" * 32, "a" * 31 + "\n", " " * 32):
            with (
                self.subTest(token_kind=type(token).__name__),
                self.assertRaises(ValueError),
            ):
                WebhookServer(token, self.accept)


if __name__ == "__main__":
    unittest.main()
