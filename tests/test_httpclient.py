import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from apiwf.config import RequestConfig
from apiwf.httpclient import execute, extract


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


class TestHTTPClient(unittest.TestCase):
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
        req = RequestConfig(name="g", method="GET", url=self._url("/x"))
        resp = execute(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body_json, {"ok": True, "path": "/x"})

    def test_post_json_body(self):
        req = RequestConfig(
            name="p", method="POST", url=self._url("/auth"),
            headers={"Content-Type": "application/json"},
            body='{"user":"a"}',
        )
        resp = execute(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body_json["access_token"], "tok-xyz")
        method, path, _hdrs, body = _Handler.received[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/auth")
        self.assertEqual(body, b'{"user":"a"}')

    def test_post_form(self):
        req = RequestConfig(
            name="p", method="PUT", url=self._url("/profile"),
            body_form={"a": "1", "b": "hello world"},
        )
        resp = execute(req)
        self.assertEqual(resp.status, 200)
        method, path, hdrs, body = _Handler.received[-1]
        self.assertEqual(method, "PUT")
        self.assertEqual(hdrs.get("Content-Type"), "application/x-www-form-urlencoded")
        # urlencoded body
        self.assertIn(b"a=1", body)
        self.assertIn(b"b=hello+world", body)


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


if __name__ == "__main__":
    unittest.main()
