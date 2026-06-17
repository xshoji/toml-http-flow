"""Tests for httpflow.runner workflow execution."""

from __future__ import annotations

import io
import json
import os
import tempfile
import textwrap
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from httpflow import config as cfg_mod
from httpflow import runner
from httpflow.model import WorkflowSpec
from tests._helpers import JsonHandler, PlainTextHandler, ServerMixin, write_toml


# ------------------------------------------------------------------
# 1. Core workflow execution (capture, template, required vars)
# ------------------------------------------------------------------
class TestWorkflowCore(ServerMixin, unittest.TestCase):
    """End-to-end tests covering the main execution path."""

    _handler_cls = JsonHandler

    def test_two_step_capture_and_template(self):
        base = f"http://127.0.0.1:{self.port}"
        path = write_toml(textwrap.dedent(f"""\
            [[requests]]
            name = "getToken"
            method = "POST"
            url = "{base}/auth"
            headers = ["Content-Type: application/json"]
            body = '{{"user":"u","pass":"p"}}'
            capture = ["token = access_token"]

            [[requests]]
            name = "getUser"
            method = "GET"
            url = "{base}/me"
            headers = ["Authorization: Bearer ${{token}}"]
            capture = ["uid = user.id", "echoed_auth = user.auth_seen"]
        """))
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        store = runner.run(cfg, {"env": "test"}, out=buf)

        self.assertEqual(
            store["vars"],
            {"env": "test", "token": "tok-abc", "uid": 7, "echoed_auth": "Bearer tok-abc"},
        )

        ts = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}"
        output = buf.getvalue()
        self.assertRegex(output, rf"==> {ts} \[getToken\] POST ")
        self.assertRegex(output, rf"<== {ts} \[getToken\]")
        self.assertRegex(output, rf"==> {ts} \[getUser\] GET ")
        self.assertRegex(output, rf"<== {ts} \[getUser\]")

    def test_missing_required_var_fails_before_request(self):
        base = f"http://127.0.0.1:{self.port}"
        path = write_toml(textwrap.dedent(f"""\
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/me?user=${{var.user}}"
        """))
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        with self.assertRaisesRegex(ValueError, "missing required variable\\(s\\): user"):
            runner.run(cfg, out=io.StringIO())

    def test_http_error_response_continues_and_can_be_captured(self):
        base = f"http://127.0.0.1:{self.port}"
        # JsonHandler returns 404 for any path other than /auth and /me
        path = write_toml(textwrap.dedent(f"""\
            [[requests]]
            name = "missing"
            method = "GET"
            url = "{base}/missing"
            capture = ["message = error"]

            [[requests]]
            name = "getUser"
            method = "GET"
            url = "{base}/me"
        """))
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        store = runner.run(cfg, out=buf)
        output = buf.getvalue()
        self.assertEqual(store["vars"]["message"], "not found")
        self.assertIn("<== ", output)
        self.assertIn("[missing]", output)
        self.assertIn('"error": "not found"', output)
        self.assertIn("[getUser]", output)


# ------------------------------------------------------------------
# 2. Step selection
# ------------------------------------------------------------------
class TestWorkflowStepSelection(ServerMixin, unittest.TestCase):
    _handler_cls = JsonHandler

    def setUp(self):
        self.base = f"http://127.0.0.1:{self.port}"

    def test_runs_only_named_steps(self):
        toml = textwrap.dedent(f"""\
            [[requests]]
            name = "getToken"
            method = "POST"
            url = "{self.base}/auth"
            headers = ["Content-Type: application/json"]
            body = '{{"user":"u","pass":"p"}}'
            capture = ["token = access_token"]

            [[requests]]
            name = "getUser"
            method = "GET"
            url = "{self.base}/me"
        """)
        path = write_toml(toml)
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        store = runner.run(cfg, out=buf, steps=["getUser"])
        output = buf.getvalue()
        self.assertIn("[getUser]", output)
        self.assertNotIn("[getToken]", output)
        self.assertNotIn("token", store["vars"])

    def test_preserves_toml_order(self):
        toml = textwrap.dedent(f"""\
            [[requests]]
            name = "first"
            method = "GET"
            url = "{self.base}/me"

            [[requests]]
            name = "second"
            method = "GET"
            url = "{self.base}/me"
        """)
        path = write_toml(toml)
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        runner.run(cfg, out=buf, steps=["second", "first"])
        output = buf.getvalue()
        self.assertLess(output.index("[first]"), output.index("[second]"))

    def test_unknown_name_fails(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""\
            [[requests]]
            name = "getUser"
            method = "GET"
            url = "{base}/me"
        """)
        path = write_toml(toml)
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        with self.assertRaisesRegex(ValueError, "unknown step name"):
            runner.run(cfg, out=io.StringIO(), steps=["nope"])

    def test_scopes_required_var_validation(self):
        path = write_toml(textwrap.dedent(f"""\
            [[requests]]
            name = "needsVar"
            method = "GET"
            url = "{self.base}/me?user=${{var.user}}"

            [[requests]]
            name = "getUser"
            method = "GET"
            url = "{self.base}/me"
        """))
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        runner.run(cfg, out=buf, steps=["getUser"])
        self.assertIn("[getUser]", buf.getvalue())


# ------------------------------------------------------------------
# 3. Capture behaviour
# ------------------------------------------------------------------
class TestWorkflowCapture(ServerMixin, unittest.TestCase):
    _handler_cls = JsonHandler

    def test_capture_response_header(self):
        base = f"http://127.0.0.1:{self.port}"
        path = write_toml(textwrap.dedent(f"""\
            [[requests]]
            name = "getUser"
            method = "GET"
            url = "{base}/me"
            capture = ["ct = response.header.content-type"]
        """))
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        store = runner.run(cfg, out=buf)
        self.assertEqual(store["vars"]["ct"], "application/json")

    def test_capture_request_header_and_url(self):
        base = f"http://127.0.0.1:{self.port}"
        path = write_toml(textwrap.dedent(f"""\
            [[requests]]
            name = "getToken"
            method = "POST"
            url = "{base}/auth"
            headers = ["Content-Type: application/json"]
            body = '{{"user":"u","pass":"p"}}'
            capture = ["token = access_token"]

            [[requests]]
            name = "getUser"
            method = "GET"
            url = "{base}/me"
            headers = ["Authorization: Bearer ${{token}}"]
            capture = [
                "sent_auth = request.header.Authorization",
                "called_url = request.url",
            ]
        """))
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        store = runner.run(cfg, out=buf)
        self.assertEqual(store["vars"]["sent_auth"], "Bearer tok-abc")
        self.assertEqual(store["vars"]["called_url"], f"{base}/me")

    def test_capture_request_body(self):
        base = f"http://127.0.0.1:{self.port}"
        path = write_toml(textwrap.dedent(f"""\
            [[requests]]
            name = "getToken"
            method = "POST"
            url = "{base}/auth"
            headers = ["Content-Type: application/json"]
            body = '{{"user":"u","pass":"p"}}'
            capture = ["sent_body = request.body"]
        """))
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        store = runner.run(cfg, out=buf)
        self.assertEqual(store["vars"]["sent_body"], '{"user":"u","pass":"p"}')

    def test_capture_missing_response_header_fails(self):
        base = f"http://127.0.0.1:{self.port}"
        path = write_toml(textwrap.dedent(f"""\
            [[requests]]
            name = "getUser"
            method = "GET"
            url = "{base}/me"
            capture = ["x = response.header.X-Does-Not-Exist"]
        """))
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        with self.assertRaisesRegex(KeyError, "response header not found"):
            runner.run(cfg, out=io.StringIO())

    def test_header_capture_works_without_json_body(self):
        class _PlainHandler(BaseHTTPRequestHandler):
            body = b"not json at all"
            extra_headers = {"X-Trace-Id": "abc-123"}

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(self.body)))
                self.send_header("X-Trace-Id", "abc-123")
                self.end_headers()
                self.wfile.write(self.body)

            def log_message(self, format, *args):
                return

        srv = HTTPServer(("127.0.0.1", 0), _PlainHandler)
        port = srv.server_address[1]
        import threading
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            path = write_toml(textwrap.dedent(f"""\
                [[requests]]
                name = "ping"
                method = "GET"
                url = "http://127.0.0.1:{port}/x"
                capture = ["trace = response.header.X-Trace-Id"]
            """))
            self.addCleanup(os.unlink, path)
            cfg = cfg_mod.load(path)
            buf = io.StringIO()
            store = runner.run(cfg, out=buf)
            self.assertEqual(store["vars"]["trace"], "abc-123")
        finally:
            srv.shutdown()
            srv.server_close()


# ------------------------------------------------------------------
# 4. Log output formatting
# ------------------------------------------------------------------
class TestWorkflowLogOutput(ServerMixin, unittest.TestCase):
    _handler_cls = JsonHandler

    def test_blank_line_rejects_negative_value(self):
        with self.assertRaisesRegex(ValueError, "blank_line must be >= 0"):
            runner.run(WorkflowSpec(), out=io.StringIO(), blank_line=-1)

    def test_request_line_and_estimated_headers(self):
        base = f"http://127.0.0.1:{self.port}"
        path = write_toml(textwrap.dedent(f"""\
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/me"
        """))
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        runner.run(cfg, out=buf)
        output = buf.getvalue()
        self.assertIn("> GET /me HTTP/1.1", output)
        self.assertIn("> Host:", output)
        self.assertIn("> User-Agent: Python-urllib/", output)
        self.assertIn("> Accept-Encoding: identity", output)

    def test_response_status_line(self):
        base = f"http://127.0.0.1:{self.port}"
        path = write_toml(textwrap.dedent(f"""\
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/me"
        """))
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        runner.run(cfg, out=buf)
        output = buf.getvalue()
        self.assertIn("< HTTP/1.1 200 OK", output)

    def test_blank_line_separates_step_logs(self):
        base = f"http://127.0.0.1:{self.port}"
        path = write_toml(textwrap.dedent(f"""\
            [[requests]]
            name = "one"
            method = "GET"
            url = "{base}/me"

            [[requests]]
            name = "two"
            method = "GET"
            url = "{base}/me"
        """))
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        runner.run(cfg, out=buf, blank_line=2, quiet=True)
        output = buf.getvalue()
        self.assertRegex(output, r"\[one\][\s\S]*\n\n\n==> .*\[two\]")


if __name__ == "__main__":
    unittest.main()
