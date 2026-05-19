import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from apiwf.config import RequestConfig, WorkflowConfig
from apiwf.workflow import run


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

    def test_body_form_template_rendering_with_hyphen_step(self):
        """body_form values must have ${...} expanded, including when the
        referenced step name contains a hyphen (regression: the template
        regex used to reject hyphens)."""
        from apiwf.workflow import _render_request

        req = RequestConfig(
            name="next",
            method="POST",
            url="http://example.com/x",
            body_form={
                "nickname": "new_name",
                "email": "test@email.com",
                "args": "${steps.httpbinorg-post.argsAaa2}",
            },
        )
        store = {
            "vars": {},
            "steps": {"httpbinorg-post": {"argsAaa2": "hello-world"}},
        }
        rendered = _render_request(req, store)
        self.assertEqual(rendered.body_form, {
            "nickname": "new_name",
            "email": "test@email.com",
            "args": "hello-world",
        })

    def test_two_step_capture_and_template(self):
        base = f"http://127.0.0.1:{self.port}"
        cfg = WorkflowConfig(
            requests=[
                RequestConfig(
                    name="getToken",
                    method="POST",
                    url=f"{base}/auth",
                    headers={"Content-Type": "application/json"},
                    body='{"user":"u","pass":"p"}',
                    capture={"token": "access_token"},
                ),
                RequestConfig(
                    name="getUser",
                    method="GET",
                    url=f"{base}/me",
                    headers={"Authorization": "Bearer ${steps.getToken.token}"},
                    capture={"uid": "user.id", "echoed_auth": "user.auth_seen"},
                ),
            ]
        )
        buf = io.StringIO()
        store = run(cfg, {"env": "test"}, out=buf)

        self.assertEqual(store["steps"]["getToken"]["token"], "tok-abc")
        self.assertEqual(store["steps"]["getUser"]["uid"], 7)
        self.assertEqual(store["steps"]["getUser"]["echoed_auth"], "Bearer tok-abc")
        self.assertEqual(store["vars"], {"env": "test"})

        # Each request and response summary line must include a local
        # timestamp like "==> 2026-05-19 23:35:49.123 [getToken] ...".
        import re as _re
        ts = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}"
        output = buf.getvalue()
        self.assertRegex(output, rf"==> {ts} \[getToken\] POST ")
        self.assertRegex(output, rf"<== {ts} \[getToken\] status=200")
        self.assertRegex(output, rf"==> {ts} \[getUser\] GET ")
        self.assertRegex(output, rf"<== {ts} \[getUser\] status=200")


if __name__ == "__main__":
    unittest.main()
