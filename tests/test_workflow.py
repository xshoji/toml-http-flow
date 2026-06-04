"""Tests for httpflow.runner workflow execution."""

import io
import json
import os
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from httpflow import config as cfg_mod
from httpflow import runner


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.path == "/auth":
            self._send(200, {"access_token": "tok-abc"})
        else:
            self._send(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/me":
            auth = self.headers.get("Authorization", "")
            self._send(200, {"user": {"id": 7, "auth_seen": auth}})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, format, *args):
        return


class TestWorkflow(unittest.TestCase):
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

    def _write(self, toml: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "wb") as f:
            f.write(toml.encode("utf-8"))
        self.addCleanup(os.unlink, path)
        return path

    def test_body_form_template_rendering_with_hyphen_step(self):
        """body_form values must have ${...} expanded, including when the
        referenced step name contains a hyphen (regression: the template
        regex used to reject hyphens)."""
        from httpflow.runtime.core import render_mapping

        store = {"vars": {"argsAaa2": "hello-world"}}
        rendered_body_form = render_mapping(
            {"nickname": "new_name", "email": "test@email.com", "args": "${argsAaa2}"},
            store,
        )
        self.assertEqual(rendered_body_form, {
            "nickname": "new_name",
            "email": "test@email.com",
            "args": "hello-world",
        })

    def test_two_step_capture_and_template(self):
        base = f"http://127.0.0.1:{self.port}"
        path = self._write(textwrap.dedent(f"""\
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
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        store = runner.run(cfg, {"env": "test"}, out=buf)

        self.assertEqual(store["vars"], {"env": "test", "token": "tok-abc", "uid": 7, "echoed_auth": "Bearer tok-abc"})

        # Each request and response summary line must include a local
        # timestamp like "==> 2026-05-19 23:35:49.123 [getToken] ...".
        import re as _re
        ts = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}"
        output = buf.getvalue()
        self.assertRegex(output, rf"==> {ts} \[getToken\] POST ")
        self.assertRegex(output, rf"<== {ts} \[getToken\]")
        self.assertRegex(output, rf"==> {ts} \[getUser\] GET ")
        self.assertRegex(output, rf"<== {ts} \[getUser\]")

    def test_missing_required_var_fails_before_request(self):
        base = f"http://127.0.0.1:{self.port}"
        path = self._write(textwrap.dedent(f"""\
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/me?user=${{var.user}}"
        """))
        cfg = cfg_mod.load(path)

        with self.assertRaisesRegex(ValueError, "missing required variable\(s\): user"):
            runner.run(cfg, out=io.StringIO())

    # --- curl -vvv detailed output assertions ---

    def test_request_line_and_estimated_headers(self):
        """The detailed output must include the request line, Host,
        and estimated User-Agent/Accept-Encoding when not supplied."""
        base = f"http://127.0.0.1:{self.port}"
        path = self._write(textwrap.dedent(f"""\
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/me"
        """))
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        runner.run(cfg, out=buf)
        output = buf.getvalue()
        self.assertIn("> GET /me HTTP/1.1", output)
        self.assertIn("> Host:", output)
        self.assertIn("> User-Agent: Python-urllib/", output)
        self.assertIn("> Accept-Encoding: identity", output)

    def test_response_status_line(self):
        """The detailed output must include the HTTP/1.1 status line."""
        base = f"http://127.0.0.1:{self.port}"
        path = self._write(textwrap.dedent(f"""\
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/me"
        """))
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        runner.run(cfg, out=buf)
        output = buf.getvalue()
        self.assertIn("< HTTP/1.1 200 OK", output)

    def test_step_selection_runs_only_named_steps(self):
        base = f"http://127.0.0.1:{self.port}"
        path = self._write(textwrap.dedent(f"""\
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
        """))
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        store = runner.run(cfg, out=buf, steps=["getUser"])
        output = buf.getvalue()
        self.assertIn("[getUser]", output)
        self.assertNotIn("[getToken]", output)
        # getToken's capture must not have run.
        self.assertNotIn("token", store["vars"])

    def test_step_selection_preserves_toml_order(self):
        base = f"http://127.0.0.1:{self.port}"
        path = self._write(textwrap.dedent(f"""\
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
        """))
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        # -s order is reversed; execution must still follow TOML order.
        runner.run(cfg, out=buf, steps=["getUser", "getToken"])
        output = buf.getvalue()
        self.assertLess(output.index("[getToken]"), output.index("[getUser]"))

    def test_blank_line_separates_step_logs(self):
        base = f"http://127.0.0.1:{self.port}"
        path = self._write(textwrap.dedent(f"""\
            [[requests]]
            name = "one"
            method = "GET"
            url = "{base}/me"

            [[requests]]
            name = "two"
            method = "GET"
            url = "{base}/me"
        """))
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        runner.run(cfg, out=buf, blank_line=2, quiet=True)
        output = buf.getvalue()
        self.assertRegex(output, r"\[one\][\s\S]*\n\n\n==> .*\[two\]")

    def test_step_selection_unknown_name_fails(self):
        base = f"http://127.0.0.1:{self.port}"
        path = self._write(textwrap.dedent(f"""\
            [[requests]]
            name = "getUser"
            method = "GET"
            url = "{base}/me"
        """))
        cfg = cfg_mod.load(path)
        with self.assertRaisesRegex(ValueError, "unknown step name"):
            runner.run(cfg, out=io.StringIO(), steps=["nope"])

    def test_step_selection_scopes_required_var_validation(self):
        base = f"http://127.0.0.1:{self.port}"
        path = self._write(textwrap.dedent(f"""\
            [[requests]]
            name = "needsVar"
            method = "GET"
            url = "{base}/me?user=${{var.user}}"

            [[requests]]
            name = "getUser"
            method = "GET"
            url = "{base}/me"
        """))
        cfg = cfg_mod.load(path)
        # Selecting only getUser must not require ${var.user}.
        buf = io.StringIO()
        runner.run(cfg, out=buf, steps=["getUser"])
        self.assertIn("[getUser]", buf.getvalue())

    def test_capture_response_header(self):
        base = f"http://127.0.0.1:{self.port}"
        path = self._write(textwrap.dedent(f"""\
            [[requests]]
            name = "getUser"
            method = "GET"
            url = "{base}/me"
            # case-insensitive header name lookup
            capture = ["ct = response.header.content-type"]
        """))
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        store = runner.run(cfg, out=buf)
        self.assertEqual(store["vars"]["ct"], "application/json")

    def test_capture_request_header_and_url(self):
        base = f"http://127.0.0.1:{self.port}"
        path = self._write(textwrap.dedent(f"""\
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
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        store = runner.run(cfg, out=buf)
        # The captured request header reflects the rendered (sent) value.
        self.assertEqual(store["vars"]["sent_auth"], "Bearer tok-abc")
        self.assertEqual(store["vars"]["called_url"], f"{base}/me")

    def test_capture_request_body(self):
        base = f"http://127.0.0.1:{self.port}"
        path = self._write(textwrap.dedent(f"""\
            [[requests]]
            name = "getToken"
            method = "POST"
            url = "{base}/auth"
            headers = ["Content-Type: application/json"]
            body = '{{"user":"u","pass":"p"}}'
            capture = ["sent_body = request.body"]
        """))
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        store = runner.run(cfg, out=buf)
        self.assertEqual(store["vars"]["sent_body"], '{"user":"u","pass":"p"}')

    def test_capture_missing_response_header_fails(self):
        base = f"http://127.0.0.1:{self.port}"
        path = self._write(textwrap.dedent(f"""\
            [[requests]]
            name = "getUser"
            method = "GET"
            url = "{base}/me"
            capture = ["x = response.header.X-Does-Not-Exist"]
        """))
        cfg = cfg_mod.load(path)
        with self.assertRaisesRegex(KeyError, "response header not found"):
            runner.run(cfg, out=io.StringIO())

    def test_header_capture_works_without_json_body(self):
        """Header/request captures must not require a JSON response body."""
        class _PlainHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"not json at all"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("X-Trace-Id", "abc-123")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        srv = HTTPServer(("127.0.0.1", 0), _PlainHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            path = self._write(textwrap.dedent(f"""\
                [[requests]]
                name = "ping"
                method = "GET"
                url = "http://127.0.0.1:{port}/x"
                capture = ["trace = response.header.X-Trace-Id"]
            """))
            cfg = cfg_mod.load(path)
            buf = io.StringIO()
            store = runner.run(cfg, out=buf)
            self.assertEqual(store["vars"]["trace"], "abc-123")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_http_error_response_continues_and_can_be_captured(self):
        base = f"http://127.0.0.1:{self.port}"
        path = self._write(textwrap.dedent(f"""\
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
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        store = runner.run(cfg, out=buf)
        output = buf.getvalue()
        self.assertEqual(store["vars"]["message"], "not found")
        self.assertIn("<== ", output)
        self.assertIn("[missing]", output)
        self.assertIn('"error": "not found"', output)
        self.assertIn("[getUser]", output)


if __name__ == "__main__":
    unittest.main()
