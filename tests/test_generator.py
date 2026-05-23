import io
import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from httpflow import config as cfg_mod
from httpflow import generator


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


class TestGenerator(unittest.TestCase):
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

    def test_generate_runs_standalone(self):
        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "getToken"
            method = "POST"
            url = "{base}/auth"
            headers = ["Content-Type: application/json"]
            body = '''{{"user":"u","pass":"p"}}'''
            capture = ["token = access_token"]

            [[requests]]
            name = "getUser"
            method = "GET"
            url = "{base}/me"
            headers = ["Authorization: Bearer ${{steps.getToken.token}}"]
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf, default_vars={"env": "test"})

            # Must compile as a valid python module.
            compile(script, "<generated>", "exec")

            script_path = tmp_path / "workflow.py"
            script_path.write_text(script, encoding="utf-8")

            # ---- Run #1: default behaviour → masking is ON ----
            res = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            stdout = res.stdout

            # Basic presence checks
            self.assertIn("[getToken]", stdout)
            self.assertIn("[getUser]", stdout)

            # --- curl -vvv detail assertions ---
            # Request line
            self.assertIn("    > POST /auth HTTP/1.1", stdout)
            self.assertIn("    > GET /me HTTP/1.1", stdout)

            # Estimated headers
            self.assertIn("    > Host:", stdout)
            self.assertIn("    > User-Agent: Python-urllib/", stdout)

            # Response status line
            self.assertIn("    < HTTP/1.1 200 OK", stdout)

            # Capture line masked by default
            self.assertIn("* capture token = '***'", stdout)
            self.assertNotIn("gen-tok", stdout)
            # Authorization header masked in second request
            self.assertIn("    > Authorization: ***", stdout)
            self.assertNotIn("Bearer gen-tok", stdout)

            # ---- Run #2: --no-mask → masking disabled ----
            res2 = subprocess.run(
                [sys.executable, str(script_path), "--no-mask"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res2.returncode, 0, msg=res2.stderr)
            stdout2 = res2.stdout
            self.assertIn("* capture token = 'gen-tok'", stdout2)
            self.assertIn("    > Authorization: Bearer gen-tok", stdout2)

    def test_generate_with_sleep_step(self):
        toml_text = textwrap.dedent("""
            [[requests]]
            name = "wait"
            method = "SLEEP"
            url = "0.05"

            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://127.0.0.1:1/ping"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)

            # Must compile
            compile(script, "<generated>", "exec")

            # Check that the sleep step exists and has correct structure
            self.assertIn("time.sleep(seconds)", script)
            self.assertIn("SLEEP", script)
            self.assertIn("done", script)

            # The sleep step should NOT call do_request / log_request with headers
            step_lines = []
            in_step = False
            for line in script.splitlines():
                if line.startswith("def step_wait"):
                    in_step = True
                elif in_step and line.startswith("def "):
                    break
                if in_step:
                    step_lines.append(line)
            step_src = "\n".join(step_lines)
            self.assertNotIn("do_request(", step_src)
            self.assertNotIn("headers", step_src)

    def test_unused_until_helpers_omitted(self):
        """When no request has until, eval_until / poll_until must not appear."""
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://127.0.0.1:1/ping"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)
            compile(script, "<generated>", "exec")

            self.assertNotIn("def eval_until(", script)
            self.assertNotIn("def poll_until(", script)
            self.assertIn("(no until blocks", script)

    def test_unused_repeat_helpers_omitted(self):
        """When no ${repeat.*} is referenced, repeat helpers must not appear."""
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://127.0.0.1:1/ping"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)
            compile(script, "<generated>", "exec")

            self.assertNotIn("def _build_repeat_iterations(", script)
            self.assertNotIn("REQUIRED_REPEAT_VARS", script)
            self.assertNotIn("--repeat-vars", script)
            self.assertIn("(no ${repeat.*} references", script)

    def test_default_vars_embedded(self):
        """-v K=V sets DEFAULT_VARS; script runs without args and can be overridden."""
        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/echo?env=${{vars.env}}&user=${{vars.user}}"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf, default_vars={"env": "prod"})
            compile(script, "<generated>", "exec")

            script_path = tmp_path / "workflow.py"
            script_path.write_text(script, encoding="utf-8")

            help_res = subprocess.run(
                [sys.executable, str(script_path), "--help"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(help_res.returncode, 0, msg=help_res.stderr)
            self.assertIn("  * DEFAULT_VARS (optional parameters)", help_res.stdout)
            self.assertIn("env=prod", help_res.stdout)
            self.assertIn("  * Required parameters (referenced by ${vars.*} but not embedded)", help_res.stdout)
            self.assertIn("    - user", help_res.stdout)

            # Runs without arguments because DEFAULT_VARS supplies env=prod
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

    def test_generated_help_omits_required_vars_block_when_none_required(self):
        """Required parameters block appears only when missing ${vars.*} exist."""
        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/echo?env=${{vars.env}}"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf, default_vars={"env": "prod"})
            compile(script, "<generated>", "exec")

            script_path = tmp_path / "workflow.py"
            script_path.write_text(script, encoding="utf-8")
            help_res = subprocess.run(
                [sys.executable, str(script_path), "--help"],
                capture_output=True, text=True, timeout=10,
            )

            self.assertEqual(help_res.returncode, 0, msg=help_res.stderr)
            self.assertIn("  * DEFAULT_VARS (optional parameters)", help_res.stdout)
            self.assertNotIn("  * Required parameters", help_res.stdout)


if __name__ == "__main__":
    unittest.main()
