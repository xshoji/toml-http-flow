"""Tests for --pretty-json output."""

import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from httpflow import config as cfg_mod
from httpflow import runner
from httpflow.runtime.http import _pretty as _maybe_pretty_json


class TestMaybePrettyJson(unittest.TestCase):
    def test_disabled_returns_text_unchanged(self):
        text = '{"a":1}'
        self.assertEqual(_maybe_pretty_json(text, False), text)

    def test_empty_text_returns_empty(self):
        self.assertEqual(_maybe_pretty_json("", True), "")

    def test_invalid_json_returns_unchanged(self):
        text = "not json"
        self.assertEqual(_maybe_pretty_json(text, True), text)

    def test_valid_json_pretty_printed(self):
        text = '{"a":1}'
        result = _maybe_pretty_json(text, True)
        self.assertEqual(result, '{\n  "a": 1\n}')

    def test_unicode_not_escaped(self):
        text = '{"msg":"hello \\u4e16\\u754c"}'
        result = _maybe_pretty_json(text, True)
        self.assertIn('"hello 世界"', result)


class _JsonEchoHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        payload = json.loads(body)
        response = json.dumps({"echo": payload})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(response.encode("utf-8"))

    def log_message(self, format, *args):
        return


class TestPrettyJsonWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _JsonEchoHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_pretty_json_request_body(self):
        base = f"http://127.0.0.1:{self.port}"
        path = tempfile.mkstemp(suffix=".toml")[1]
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"""\
[[requests]]
name = "echo"
method = "POST"
url = "{base}/"
headers = ["Content-Type: application/json"]
body = '{{"user":"test"}}'
""")
        try:
            cfg = cfg_mod.load(path)
            buf = io.StringIO()
            runner.run(cfg, out=buf, pretty_json=True)
            output = buf.getvalue()
            # request body should be pretty-printed
            self.assertIn('  "user": "test"', output)
            # response body should also be pretty-printed
            self.assertIn('  "echo":', output)
        finally:
            os.unlink(path)

    def test_plain_body_unchanged_when_pretty_json(self):
        class _PlainHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, format, *args):
                return
        srv = HTTPServer(("127.0.0.1", 0), _PlainHandler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{port}"
            path = tempfile.mkstemp(suffix=".toml")[1]
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"""\
[[requests]]
name = "plain"
method = "POST"
url = "{base}/"
body = "hello=world"
""")
            try:
                cfg = cfg_mod.load(path)
                buf = io.StringIO()
                runner.run(cfg, out=buf, pretty_json=True)
                output = buf.getvalue()
                self.assertIn("hello=world", output)
                self.assertIn("ok", output)
                # it should not be wrapped in json quotes or altered
                self.assertNotIn('"hello=world"', output)
            finally:
                os.unlink(path)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
