import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from httpflow import config as cfg_mod
from httpflow import bash_generator


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
        self._send(200, {"access_token": "gen-tok"})

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        self._send(200, {"user": {"id": 11, "auth": auth}})

    def log_message(self, format, *args):
        return


class _HeaderEchoHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        body = b'{"ok":true}'
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        resp = json.dumps({"received_headers": dict(self.headers), "body": body.decode("utf-8")}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a): return


@unittest.skipUnless(
    shutil.which("bash") and shutil.which("curl") and shutil.which("jq"),
    "bash, curl, jq required",
)
class TestBashGenerator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.hserver = HTTPServer(("127.0.0.1", 0), _HeaderEchoHandler)
        cls.hport = cls.hserver.server_address[1]
        cls.htread = threading.Thread(target=cls.hserver.serve_forever, daemon=True)
        cls.htread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.hserver.shutdown()
        cls.hserver.server_close()

    def _generate_and_run(self, toml_text: str, args=None, default_vars=None, default_repeat_vars=None):
        """Generate bash script from TOML and run it, returning (rc, stdout, stderr)."""
        args = args or []
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text.encode("utf-8"))
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(
                wf,
                default_vars=default_vars or {},
                default_repeat_vars=default_repeat_vars,
            )
            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")

            # syntax check
            syntax = subprocess.run(
                ["bash", "-n", str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(syntax.returncode, 0, msg=f"syntax error:\n{syntax.stderr}\n--- script ---\n{script}")

            # run
            res = subprocess.run(
                ["bash", str(script_path), *args],
                capture_output=True, text=True, timeout=30,
            )
            return res.returncode, res.stdout, res.stderr, script

    def test_simple_get(self):
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://127.0.0.1:{self.port}/hello"
        """)
        rc, stdout, stderr, script = self._generate_and_run(toml)
        self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
        self.assertIn("[ping] GET", stdout)
        self.assertIn("status=200", stdout)

    def test_capture_and_reuse(self):
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "getToken"
            method = "POST"
            url = "http://127.0.0.1:{self.port}/auth"
            headers = ["Content-Type: application/json"]
            body = '{{"user":"u","pass":"p"}}'
            capture = ["token = access_token"]

            [[requests]]
            name = "getUser"
            method = "GET"
            url = "http://127.0.0.1:{self.port}/me"
            headers = ["Authorization: Bearer ${{token}}"]
        """)
        rc, stdout, stderr, script = self._generate_and_run(toml)
        self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
        self.assertIn("[getToken] POST", stdout)
        self.assertIn("[getUser] GET", stdout)
        self.assertIn("* capture token", stdout)

    def test_masking_enabled(self):
        """Masking should hide sensitive values in request/response output."""
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "post"
            method = "POST"
            url = "http://127.0.0.1:{self.port}/auth"
            headers = ["Authorization: Bearer secret123"]
            body = '{{"user":"u","pass":"secret"}}'
        """)
        rc, stdout, stderr, script = self._generate_and_run(toml)
        self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
        self.assertNotIn("secret123", stdout, msg="auth token should be masked")
        self.assertIn("***", stdout, msg="masked content should appear")

    def test_no_mask(self):
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "post"
            method = "POST"
            url = "http://127.0.0.1:{self.port}/auth"
            headers = ["Authorization: Bearer secret123"]
        """)
        rc, stdout, stderr, script = self._generate_and_run(toml, args=["--no-mask"])
        self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
        self.assertIn("secret123", stdout, msg="auth token should be visible with --no-mask")

    def test_form_body(self):
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "login"
            method = "POST"
            url = "http://127.0.0.1:{self.port}/auth"
            body_form = ["user = alice", "pass = secret"]
        """)
        rc, stdout, stderr, script = self._generate_and_run(toml)
        self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
        self.assertIn("[login] POST", stdout)

    def test_sleep_step(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "wait"
            method = "SLEEP"
            url = "0.05"
        """)
        rc, stdout, stderr, script = self._generate_and_run(toml)
        self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
        self.assertIn("[wait] SLEEP", stdout)
        self.assertIn("done", stdout)

    def test_var_injection(self):
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://127.0.0.1:{self.port}/hello?id=${{var.id}}"
        """)
        rc, stdout, stderr, script = self._generate_and_run(
            toml, args=["-v", "id=42"]
        )
        self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
        self.assertIn("?id=42", stdout)

    def test_env_var(self):
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://127.0.0.1:{self.port}/hello?id=${{env.HFTEST_ID}}"
        """)
        env = dict(os.environ)
        env["HFTEST_ID"] = "99"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml.encode("utf-8"))
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf)
            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path)],
                capture_output=True, text=True, timeout=30, env=env,
            )
        self.assertEqual(res.returncode, 0, msg=f"stderr: {res.stderr}\nstdout: {res.stdout}")
        self.assertIn("?id=99", res.stdout)

    def test_quiet(self):
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://127.0.0.1:{self.port}/hello"
        """)
        rc, stdout, stderr, script = self._generate_and_run(toml, args=["--quiet"])
        self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
        self.assertIn("[ping] GET", stdout)
        self.assertNotIn("> GET", stdout)

    def test_pretty_json(self):
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://127.0.0.1:{self.port}/hello"
        """)
        rc, stdout, stderr, script = self._generate_and_run(toml, args=["--pretty-json"])
        self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
        # response body should be pretty-printed (contains indented key)
        self.assertIn('  "user":', stdout)

    def test_no_bash4_features(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://127.0.0.1:1/ping"
        """)
        rc, stdout, stderr, script = self._generate_and_run(toml)
        # rc may be non-zero because the test URL is unreachable; we only care about syntax
        forbidden = ["declare -A", "mapfile", "readarray", "local -n"]
        for pat in forbidden:
            self.assertNotIn(pat, script, msg=f"bash 4+ feature found: {pat}")

    def test_array_guards_are_bash32_safe(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "submit"
            method = "POST"
            url = "http://127.0.0.1:1/submit"
            headers = ["X-Test: yes"]
            body_form = ["a=b"]
        """)
        with tempfile.TemporaryDirectory() as tmp:
            toml_path = Path(tmp) / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
        script = bash_generator.generate(wf)
        unsafe = [
            '"${__hf_args[@]+"${__hf_args[@]}"}"',
            '"${parts[@]+"${parts[@]}"}"',
            '"${user_vars[@]+"${user_vars[@]}"}"',
            '"${user_repeat_vars[@]+"${user_repeat_vars[@]}"}"',
        ]
        for pat in unsafe:
            self.assertNotIn(pat, script)
        self.assertIn('${__hf_args[@]+"${__hf_args[@]}"}', script)

    def test_shebang(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://127.0.0.1:1/ping"
        """)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml.encode("utf-8"))
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf, shebang=True)
        self.assertTrue(script.startswith("#!/usr/bin/env bash\n"))

    def test_default_vars_embedded(self):
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://127.0.0.1:{self.port}/hello?id=${{var.id}}"
        """)
        rc, stdout, stderr, script = self._generate_and_run(
            toml, default_vars={"id": "7"}
        )
        self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
        self.assertIn("?id=7", stdout)

    def test_repeat_vars(self):
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://127.0.0.1:{self.port}/hello?id=${{repeat.id}}"
        """)
        rc, stdout, stderr, script = self._generate_and_run(
            toml, args=["--repeat-vars", "id=1,2,3"]
        )
        self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
        self.assertIn("=== repeat iteration 1/3", stdout)
        self.assertIn("=== repeat iteration 3/3", stdout)

    def test_until_poll(self):
        class CountHandler(BaseHTTPRequestHandler):
            count = 0
            def do_GET(self):
                CountHandler.count += 1
                body = json.dumps({"status": "ok" if CountHandler.count >= 2 else "wait"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a): pass

        srv = HTTPServer(("127.0.0.1", 0), CountHandler)
        CountHandler.count = 0
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            toml = textwrap.dedent(f"""
                [[requests]]
                name = "poll"
                method = "GET"
                url = "http://127.0.0.1:{port}/status"
                capture = ["st = status"]
                until = [
                    "condition = ${{st}} == ok",
                    "interval = 0",
                    "max_attempts = 3",
                ]
            """)
            rc, stdout, stderr, script = self._generate_and_run(toml)
            self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
            self.assertIn("until satisfied on attempt 2", stdout)
        finally:
            srv.shutdown()
            srv.server_close()


    def test_request_header_capture(self):
        """request.header.* capture must read the explicitly sent headers."""
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "hdr"
            method = "POST"
            url = "http://127.0.0.1:{self.hport}/echo"
            headers = ["X-Custom: hello"]
            body = '{{"a":1}}'
            capture = ["h = request.header.X-Custom"]
        """)
        rc, stdout, stderr, script = self._generate_and_run(toml)
        self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
        self.assertIn("* capture h = hello", stdout)

    def test_request_body_capture(self):
        """request.body capture must read the request body, not response body."""
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "body"
            method = "POST"
            url = "http://127.0.0.1:{self.hport}/echo"
            body = 'hi-mom'
            capture = ["b = request.body"]
        """)
        rc, stdout, stderr, script = self._generate_and_run(toml)
        self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
        self.assertIn("* capture b = hi-mom", stdout)

    def test_jq_capture_not_found_errors(self):
        """capture from non-existing JSON path must error exit, not silently return empty."""
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "bad"
            method = "GET"
            url = "http://127.0.0.1:{self.port}/hello"
            capture = ["x = response.body.notexist"]
        """)
        rc, stdout, stderr, script = self._generate_and_run(toml)
        self.assertNotEqual(rc, 0, msg="should fail on missing capture path")
        self.assertIn("path not found", stderr)

    def test_repeat_missing_error(self):
        """Running without --repeat-vars when TOML has ${repeat.X} must error."""
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "r"
            method = "GET"
            url = "http://127.0.0.1:{self.port}/hello?id=${{repeat.id}}"
        """)
        rc, stdout, stderr, script = self._generate_and_run(toml)
        self.assertNotEqual(rc, 0, msg="should fail when repeat vars missing")
        self.assertIn("repeat-vars missing for", stderr)

    def test_mask_bearer_with_space(self):
        """Masking must hide the entire value even when it contains spaces."""
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "auth"
            method = "POST"
            url = "http://127.0.0.1:{self.hport}/echo"
            headers = ["Authorization: Bearer abc xyz 123"]
        """)
        rc, stdout, stderr, script = self._generate_and_run(toml)
        self.assertEqual(rc, 0, msg=f"stderr: {stderr}\nstdout: {stdout}")
        self.assertNotIn("abc xyz 123", stdout, msg="value with spaces should be masked")
        self.assertIn("***", stdout, msg="masked marker should appear")

    def test_uuid_dep_check_present(self):
        """Script using ${random.UUID} must include uuidgen dependency check."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "u"
            method = "GET"
            url = "http://127.0.0.1:1/ping?id=${random.UUID}"
        """)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml.encode("utf-8"))
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf)
        self.assertIn("uuidgen", script)


if __name__ == "__main__":
    unittest.main()
