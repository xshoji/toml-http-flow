"""Tests for httpflow.runtime.http helpers."""

import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from httpflow.runtime.http import do_request, extract, resolve_capture


class _Handler(BaseHTTPRequestHandler):
    received: list = []

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _Handler.received.append(("GET", self.path, dict(self.headers), b""))
        if self.path == "/missing":
            self._send_json(404, {"error": "not found"})
        else:
            self._send_json(200, {"ok": True, "path": self.path})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        _Handler.received.append(("POST", self.path, dict(self.headers), body))
        self._send_json(200, {"access_token": "tok-xyz", "echo": body.decode("utf-8")})

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        _Handler.received.append(("PUT", self.path, dict(self.headers), body))
        self._send_json(200, {"updated": True})

    def log_message(self, format, *args):  # silence
        return


class TestDoRequest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.received.clear()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_get_json(self):
        status, reason, resp_headers, text, body_json = do_request(
            "GET", self._url("/x"), {}, None
        )
        self.assertEqual(status, 200)
        self.assertEqual(body_json, {"ok": True, "path": "/x"})

    def test_http_error_response_is_returned(self):
        status, reason, resp_headers, text, body_json = do_request(
            "GET", self._url("/missing"), {}, None
        )
        self.assertEqual(status, 404)
        self.assertEqual(reason, "Not Found")
        self.assertEqual(body_json, {"error": "not found"})
        self.assertIn('"error": "not found"', text)

    def test_post_json_body(self):
        status, reason, resp_headers, text, body_json = do_request(
            "POST",
            self._url("/auth"),
            {"Content-Type": "application/json"},
            b'{"user":"a"}',
        )
        self.assertEqual(status, 200)
        self.assertEqual(body_json["access_token"], "tok-xyz")
        method, path, _hdrs, body = _Handler.received[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/auth")
        self.assertEqual(body, b'{"user":"a"}')

    def test_post_form(self):
        import urllib.parse

        body_bytes = urllib.parse.urlencode({"a": "1", "b": "hello world"}).encode("utf-8")
        status, reason, resp_headers, text, body_json = do_request(
            "PUT",
            self._url("/profile"),
            {"Content-Type": "application/x-www-form-urlencoded"},
            body_bytes,
        )
        self.assertEqual(status, 200)
        method, path, hdrs, body = _Handler.received[-1]
        self.assertEqual(method, "PUT")
        self.assertEqual(hdrs.get("Content-Type"), "application/x-www-form-urlencoded")
        # urlencoded body
        self.assertIn(b"a=1", body)
        self.assertIn(b"b=hello+world", body)

    # -- reason field (201 (Created)) --
    def test_post_reason(self):
        class _CreatedHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                body = json.dumps({"id": 42}).encode("utf-8")
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        srv = HTTPServer(("127.0.0.1", 0), _CreatedHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            status, reason, _, _, body_json = do_request(
                "POST",
                f"http://127.0.0.1:{port}/items",
                {"Content-Type": "application/json"},
                b'{"name":"x"}',
            )
            self.assertEqual(status, 201)
            self.assertEqual(reason, "Created")
        finally:
            srv.shutdown()
            srv.server_close()


class TestExtract(unittest.TestCase):
    def test_top_level(self):
        self.assertEqual(extract({"a": 1}, "a"), 1)

    def test_nested(self):
        self.assertEqual(
            extract({"data": {"user": {"id": 42}}}, "data.user.id"),
            42,
        )

    def test_index(self):
        self.assertEqual(
            extract({"items": [{"id": "a1"}, {"id": "a2"}]}, "items[1].id"),
            "a2",
        )

    def test_missing_key(self):
        with self.assertRaises(KeyError):
            extract({"a": 1}, "b")

    def test_index_out_of_range(self):
        with self.assertRaises(IndexError):
            extract({"x": [1]}, "x[5]")


class TestResolveCapture(unittest.TestCase):
    def test_request_body_json_nested(self):
        captured = resolve_capture(
            "request.body.date.time_DATE_ISO",
            step_name="test",
            body_json=None,
            resp_headers={},
            req_url="http://example.com/",
            req_headers={},
            req_body='{"date":{"time_DATE_ISO":"2026-06-24T12:34:56.123456+09:00"}}',
        )
        self.assertEqual(captured, "2026-06-24T12:34:56.123456+09:00")

    def test_request_body_json_array_index(self):
        captured = resolve_capture(
            "request.body.items[0].id",
            step_name="test",
            body_json=None,
            resp_headers={},
            req_url="http://example.com/",
            req_headers={},
            req_body='{"items":[{"id":"a1"},{"id":"a2"}]}',
        )
        self.assertEqual(captured, "a1")

    def test_request_body_json_invalid_body_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            resolve_capture(
                "request.body.foo",
                step_name="test",
                body_json=None,
                resp_headers={},
                req_url="http://example.com/",
                req_headers={},
                req_body="not-json",
            )
        self.assertIn("requires a JSON request body", str(ctx.exception))

    def test_request_body_json_empty_body_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            resolve_capture(
                "request.body.foo",
                step_name="test",
                body_json=None,
                resp_headers={},
                req_url="http://example.com/",
                req_headers={},
                req_body="",
            )
        self.assertIn("requires a JSON request body", str(ctx.exception))

    def test_request_body_whole_string_unchanged(self):
        captured = resolve_capture(
            "request.body",
            step_name="test",
            body_json=None,
            resp_headers={},
            req_url="http://example.com/",
            req_headers={},
            req_body='{"hello":"world"}',
        )
        self.assertEqual(captured, '{"hello":"world"}')


if __name__ == "__main__":
    unittest.main()
