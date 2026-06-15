"""Tests for the Python script generator (py output)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from httpflow import config as cfg_mod
from httpflow import generator
from httpflow.runtime.http import extract
from httpflow.template import TemplateError, render
from tests._helpers import UploadHandler


class _Handler(BaseHTTPRequestHandler):
    """Mock server for generator tests (tok = gen-tok)."""

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
        self._send(200, {"access_token": "gen-tok"})

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        self._send(200, {"user": {"id": 11, "auth": auth}})

    def log_message(self, format, *args):
        return


class _HttpErrorThenOkHandler(BaseHTTPRequestHandler):
    count = 0

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _HttpErrorThenOkHandler.count += 1
        if _HttpErrorThenOkHandler.count == 1:
            self._send(404, {"data": {"status": "Pending"}})
        else:
            self._send(200, {"data": {"status": "Active"}})

    def log_message(self, format, *args):
        return


def _generate_script(toml_text: str | bytes, *, default_vars: dict[str, str] | None = None) -> str:
    """Load TOML text and return generated script source."""
    with tempfile.TemporaryDirectory() as tmp:
        toml_path = Path(tmp) / "workflow.toml"
        toml_path.write_bytes(toml_text.encode("utf-8") if isinstance(toml_text, str) else toml_text)
        wf = cfg_mod.load(str(toml_path))
        return generator.generate(wf, default_vars=default_vars or {})


def _compile(script: str) -> None:
    compile(script, "<generated>", "exec")


# ------------------------------------------------------------------
# 1. Core generation & self-containment
# ------------------------------------------------------------------
class TestGeneratorCore(unittest.TestCase):
    """Basic properties every generated script must satisfy."""

    def test_empty_workflow_compiles(self):
        script = _generate_script(b"")
        _compile(script)

    def test_generated_script_never_contains_httpflow_imports(self):
        script = _generate_script("""
[[requests]]
name = "ping"
method = "GET"
url = "http://127.0.0.1:1/ping?id=${var.id}"
""")
        _compile(script)
        self.assertNotRegex(script, r"(?m)^\s*from\s+\.")
        self.assertNotRegex(script, r"(?m)^\s*import\s+httpflow\b")
        self.assertNotRegex(script, r"(?m)^\s*from\s+httpflow\b")

    def test_embedded_runtime_helpers_compile_cleanly(self):
        from httpflow.generator import _flatten_modules

        src = _flatten_modules({"core", "mask", "http", "until"})
        _compile(src)
        self.assertNotRegex(src, r"(?m)^\s*from\s+\.")
        self.assertNotRegex(src, r"(?m)^\s*import\s+httpflow\b")
        self.assertNotRegex(src, r"(?m)^\s*from\s+httpflow\b")

    def test_generate_with_sleep_step(self):
        script = _generate_script("""
[[requests]]
name = "wait"
method = "SLEEP"
url = "0.05"

[[requests]]
name = "ping"
method = "GET"
url = "http://127.0.0.1:1/ping"
""")
        _compile(script)
        self.assertIn("time.sleep(seconds)", script)
        self.assertIn("SLEEP", script)
        self.assertIn("done", script)

        step_src = _extract_step_src(script, "def step_wait")
        self.assertNotIn("do_request(", step_src)
        self.assertNotIn("headers", step_src)

    def test_unused_until_helpers_omitted(self):
        script = _generate_script("""
[[requests]]
name = "ping"
method = "GET"
url = "http://127.0.0.1:1/ping"
""")
        _compile(script)
        self.assertIn("(no until blocks", script)

    def test_generated_script_includes_until_when_used(self):
        script = _generate_script("""
[[requests]]
name = "poll"
method = "GET"
url = "http://127.0.0.1:1/poll"
until = ["condition = ${status} == Active", "interval = 0", "max_attempts = 1"]
""")
        _compile(script)
        self.assertIn("def eval_until", script)
        self.assertIn("def poll_until", script)
        self.assertIn("_UNTIL_OPS", script)


# ------------------------------------------------------------------
# 2. Runtime parity (template / extract / env / uuid)
# ------------------------------------------------------------------
class TestGeneratorParity(unittest.TestCase):
    """Generated embedded helpers must match package behaviour."""

    def test_generated_render_matches_package_render(self):
        script = _generate_script("""
[[requests]]
name = "echo"
method = "GET"
url = "http://127.0.0.1/${var.env}"
""")
        store = {
            "vars": {"env": "prod", "token": "abc", "my-key": "ok"},
            "steps": {"login": {"body": {"user": {"id": 7}}}},
        }
        cases = [
            "env=${var.env}",
            "alias=${token}",
            "hyphen=${var.my-key}",
            "nested=${steps.login.body.user.id}",
            "price=$$100",
            "ymd=${time.DATE_YMD}",
        ]

        ns: dict = {}
        exec(script, ns)
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(ns["render"](text, store), render(text, store))

        with self.assertRaises(TemplateError):
            render("${var.missing}", store)
        with self.assertRaises(ns["TemplateError"]):
            ns["render"]("${var.missing}", store)

    def test_generated_extract_matches_package_extract(self):
        script = _generate_script("""
[[requests]]
name = "echo"
method = "GET"
url = "http://127.0.0.1/"
""")
        body = {"data": {"user": {"id": 42}}, "items": [{"id": "a1"}, {"id": "a2"}]}
        cases = ["data.user.id", "items[1].id"]

        ns: dict = {}
        exec(script, ns)
        for path in cases:
            with self.subTest(path=path):
                self.assertEqual(ns["extract"](body, path), extract(body, path))

        with self.assertRaises(KeyError):
            ns["extract"](body, "data.missing")
        with self.assertRaises(IndexError):
            ns["extract"](body, "items[9].id")

    def test_generated_random_uuid(self):
        script = _generate_script('''
[[requests]]
name = "echo"
method = "GET"
url = "http://127.0.0.1/${random.UUID}"
''')
        ns: dict = {}
        exec(script, ns)
        out = ns["render"]("${random.UUID}", {"vars": {}, "steps": {}})
        self.assertEqual(str(uuid.UUID(out)), out)

    def test_generated_random_uuid_hex(self):
        script = _generate_script('''
[[requests]]
name = "echo"
method = "GET"
url = "http://127.0.0.1/${random.UUID_HEX}"
''')
        ns: dict = {}
        exec(script, ns)
        out = ns["render"]("${random.UUID_HEX}", {"vars": {}, "steps": {}})
        self.assertEqual(len(out), 32)
        self.assertEqual(uuid.UUID(hex=out).hex, out)

    def test_generated_env_var(self):
        script = _generate_script('''
[[requests]]
name = "echo"
method = "GET"
url = "http://127.0.0.1/${env.HTTPFLOW_TEST_USER}"
''')
        ns: dict = {}
        exec(script, ns)
        old = os.environ.get("HTTPFLOW_TEST_USER")
        os.environ["HTTPFLOW_TEST_USER"] = "bob"
        try:
            out = ns["render"]("${env.HTTPFLOW_TEST_USER}", {"vars": {}, "steps": {}})
        finally:
            if old is None:
                os.environ.pop("HTTPFLOW_TEST_USER", None)
            else:
                os.environ["HTTPFLOW_TEST_USER"] = old
        self.assertEqual(out, "bob")


# ------------------------------------------------------------------
# 3. End-to-end execution
# ------------------------------------------------------------------
class TestGeneratorE2E(unittest.TestCase):
    """Run generated scripts via subprocess against a real mock server."""

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

    def _script_path(self, script: str) -> Path:
        """Write script to a temp file and return its path."""
        fd, path = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        self.addCleanup(os.unlink, path)
        return Path(path)

    # ---- single combined E2E covering masking, pretty-json, quiet ----
    def test_e2e_masking_and_flags(self):
        base = f"http://127.0.0.1:{self.port}"
        script = _generate_script(textwrap.dedent(f"""
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
        """), default_vars={"env": "test"})

        path = self._script_path(script)

        # Run #1: default (mask ON)
        r1 = subprocess.run(
            [sys.executable, str(path)], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(r1.returncode, 0, msg=r1.stderr)
        self.assertIn("[getToken]", r1.stdout)
        self.assertIn("[getUser]", r1.stdout)
        self.assertIn("> POST /auth HTTP/1.1", r1.stdout)
        self.assertIn("< HTTP/1.1 200 OK", r1.stdout)
        self.assertIn("* capture token = '***'", r1.stdout)
        self.assertIn("> Authorization: ***", r1.stdout)
        self.assertNotIn("gen-tok", r1.stdout)

        # Run #2: --no-mask
        r2 = subprocess.run(
            [sys.executable, str(path), "--no-mask"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(r2.returncode, 0, msg=r2.stderr)
        self.assertIn("* capture token = 'gen-tok'", r2.stdout)
        self.assertIn("> Authorization: Bearer gen-tok", r2.stdout)

        # Run #3: --pretty-json
        r3 = subprocess.run(
            [sys.executable, str(path), "--pretty-json"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(r3.returncode, 0, msg=r3.stderr)
        self.assertIn('  "user": "u"', r3.stdout)

        # Run #4: --quiet
        r4 = subprocess.run(
            [sys.executable, str(path), "--quiet"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(r4.returncode, 0, msg=r4.stderr)
        self.assertNotIn("> POST", r4.stdout)
        self.assertNotIn("< HTTP/1.1", r4.stdout)
        self.assertIn("[getToken] POST ", r4.stdout)

        # Run #5: self-containment (run from /tmp without PYTHONPATH)
        env = dict(os.environ)
        repo_top = os.getcwd()
        if "PYTHONPATH" in env:
            env["PYTHONPATH"] = env["PYTHONPATH"].replace(repo_top, "").strip(":")
        r5 = subprocess.run(
            [sys.executable, str(path), "--quiet"],
            capture_output=True, text=True, timeout=10,
            cwd="/tmp", env=env,
        )
        self.assertEqual(r5.returncode, 0, msg=r5.stderr)

    def test_generated_script_captures_headers_and_request_values(self):
        base = f"http://127.0.0.1:{self.port}"
        script = _generate_script(textwrap.dedent(f"""
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
                "ct        = response.header.Content-Type",
                "sent_auth = request.header.Authorization",
                "called    = request.url",
            ]
        """))
        path = self._script_path(script)
        r = subprocess.run(
            [sys.executable, str(path), "--no-mask"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("* capture ct = 'application/json'", r.stdout)
        self.assertIn("* capture sent_auth = 'Bearer gen-tok'", r.stdout)
        self.assertIn(f"* capture called = '{base}/me'", r.stdout)

    def test_generated_script_treats_http_error_response_as_normal(self):
        srv = HTTPServer(("127.0.0.1", 0), _HttpErrorThenOkHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        _HttpErrorThenOkHandler.count = 0
        try:
            script = _generate_script(textwrap.dedent(f"""
                [[requests]]
                name = "poll"
                method = "GET"
                url = "http://127.0.0.1:{port}/status"
                capture = ["status = data.status"]
                until = [
                    "condition = ${{status}} == Active",
                    "interval = 0",
                    "max_attempts = 2",
                ]
            """))
            path = self._script_path(script)
            r = subprocess.run(
                [sys.executable, str(path)], capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("[poll]", r.stdout)
            self.assertIn("* until satisfied on attempt 2", r.stdout)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_generated_script_uploads_file_and_multipart(self):
        srv = HTTPServer(("127.0.0.1", 0), UploadHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        UploadHandler.seen = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                raw_path = tmp_path / "raw.bin"
                part_path = tmp_path / "part.txt"
                raw_path.write_bytes(b"\x00raw-bytes\xff")
                part_path.write_text("part text", encoding="utf-8")
                toml_path = tmp_path / "workflow.toml"
                toml_path.write_text(textwrap.dedent(f"""
                    [[requests]]
                    name = "raw"
                    method = "PUT"
                    url = "http://127.0.0.1:{port}/raw"
                    body_file = "{raw_path}"

                    [[requests]]
                    name = "multi"
                    method = "POST"
                    url = "http://127.0.0.1:{port}/multi"
                    body_multipart = [
                        "title = hello",
                        "file = @{part_path}; filename=part.txt; type=text/plain",
                    ]
                """), encoding="utf-8")
                wf = cfg_mod.load(str(toml_path))
                script = generator.generate(wf)
                script_path = tmp_path / "workflow.py"
                script_path.write_text(script, encoding="utf-8")
                r = subprocess.run(
                    [sys.executable, str(script_path)],
                    capture_output=True, text=True, timeout=10,
                )
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(len(UploadHandler.seen), 2)
            self.assertEqual(UploadHandler.seen[0]["content_type"], "application/octet-stream")
            self.assertEqual(UploadHandler.seen[0]["body"], b"\x00raw-bytes\xff")
            ct = str(UploadHandler.seen[1]["content_type"])
            self.assertTrue(ct.startswith("multipart/form-data; boundary="))
            multipart_body = UploadHandler.seen[1]["body"]
            assert isinstance(multipart_body, bytes)
            self.assertIn(b'name="title"', multipart_body)
            self.assertIn(b"hello", multipart_body)
            self.assertIn(b'name="file"; filename="part.txt"', multipart_body)
            self.assertIn(b"Content-Type: text/plain", multipart_body)
            self.assertIn(b"part text", multipart_body)
        finally:
            srv.shutdown()
            srv.server_close()


# ------------------------------------------------------------------
# 4. Default vars & CLI help
# ------------------------------------------------------------------
class TestGeneratorDefaultVars(unittest.TestCase):
    """Embedding and overriding default variables in generated scripts."""

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

    def test_default_vars_embedded(self):
        base = f"http://127.0.0.1:{self.port}"
        script = _generate_script(textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/echo?env=${{var.env}}&user=${{var.user}}"
        """), default_vars={"env": "prod"})
        _compile(script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.py"
            script_path.write_text(script, encoding="utf-8")

            help_res = subprocess.run(
                [sys.executable, str(script_path), "--help"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(help_res.returncode, 0, msg=help_res.stderr)
            self.assertIn("  * DEFAULT_VARS (optional parameters)", help_res.stdout)
            self.assertIn("env=prod", help_res.stdout)
            self.assertIn("  * Required parameters (referenced by ${var.*} but not embedded)", help_res.stdout)
            self.assertIn("    - user", help_res.stdout)

            # Runs without args because DEFAULT_VARS supplies env=prod
            res = subprocess.run(
                [sys.executable, str(script_path), "-v", "user=alice"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("/echo?env=prod&user=alice", res.stdout)

            # Runtime -v overrides DEFAULT_VARS
            res2 = subprocess.run(
                [sys.executable, str(script_path), "-v", "env=staging", "-v", "user=bob"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res2.returncode, 0, msg=res2.stderr)
            self.assertIn("/echo?env=staging&user=bob", res2.stdout)

            # Missing required var fails cleanly
            missing = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(missing.returncode, 1)
            self.assertIn("missing required variable(s): user", missing.stderr)
            self.assertIn('Example: --var "user=<value>"', missing.stderr)
            self.assertNotIn("==>", missing.stdout)

    def test_generated_help_omits_required_vars_block_when_none_required(self):
        base = f"http://127.0.0.1:{self.port}"
        script = _generate_script(textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/echo?env=${{var.env}}"
        """), default_vars={"env": "prod"})
        _compile(script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.py"
            script_path.write_text(script, encoding="utf-8")
            help_res = subprocess.run(
                [sys.executable, str(script_path), "--help"],
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(help_res.returncode, 0, msg=help_res.stderr)
        self.assertIn("  * DEFAULT_VARS (optional parameters)", help_res.stdout)
        self.assertNotIn("  * Required parameters", help_res.stdout)


# ------------------------------------------------------------------
# Helper utilities
# ------------------------------------------------------------------

def _extract_step_src(script: str, marker: str) -> str:
    """Return source lines from *marker* up to (but not including) the next ``def ``."""
    lines = script.splitlines()
    result: list[str] = []
    inside = False
    for line in lines:
        if line.startswith(marker):
            inside = True
        elif inside and line.startswith("def "):
            break
        if inside:
            result.append(line)
    return "\n".join(result)


if __name__ == "__main__":
    unittest.main()
