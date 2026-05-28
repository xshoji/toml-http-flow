import io
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
from httpflow.httpclient import extract
from httpflow.template import TemplateError, render


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
            headers = ["Authorization: Bearer ${{token}}"]
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
            self.assertIn("> POST /auth HTTP/1.1", stdout)
            self.assertIn("> GET /me HTTP/1.1", stdout)

            # Estimated headers
            self.assertIn("> Host:", stdout)
            self.assertIn("> User-Agent: Python-urllib/", stdout)

            # Response status line
            self.assertIn("< HTTP/1.1 200 OK", stdout)

            # Capture line masked by default
            self.assertIn("* capture token = '***'", stdout)
            self.assertNotIn("gen-tok", stdout)
            # Authorization header masked in second request
            self.assertIn("> Authorization: ***", stdout)
            self.assertNotIn("Bearer gen-tok", stdout)

            # ---- Run #2: --no-mask → masking disabled ----
            res2 = subprocess.run(
                [sys.executable, str(script_path), "--no-mask"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res2.returncode, 0, msg=res2.stderr)
            stdout2 = res2.stdout
            self.assertIn("* capture token = 'gen-tok'", stdout2)
            self.assertIn("> Authorization: Bearer gen-tok", stdout2)

    def test_generated_script_treats_http_error_response_as_normal(self):
        srv = HTTPServer(("127.0.0.1", 0), _HttpErrorThenOkHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        _HttpErrorThenOkHandler.count = 0
        try:
            toml_text = textwrap.dedent(f"""
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
            """).encode("utf-8")

            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                toml_path = tmp_path / "workflow.toml"
                toml_path.write_bytes(toml_text)
                wf = cfg_mod.load(str(toml_path))
                script_path = tmp_path / "workflow.py"
                script_path.write_text(generator.generate(wf), encoding="utf-8")

                res = subprocess.run(
                    [sys.executable, str(script_path)],
                    capture_output=True, text=True, timeout=10,
                )

            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("[poll] status=404", res.stdout)
            self.assertIn("[poll] status=200", res.stdout)
            self.assertIn("* until satisfied on attempt 2", res.stdout)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_generated_random_uuid(self):
        toml_text = textwrap.dedent("""
            [[requests]]
            name = "echo"
            method = "GET"
            url = "http://127.0.0.1/${random.UUID}"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)

        ns = {"__name__": "generated_uuid_test"}
        exec(script, ns)
        out = ns["render"]("${random.UUID}", {"vars": {}, "steps": {}})
        self.assertEqual(str(uuid.UUID(out)), out)

    def test_generated_random_uuid_hex(self):
        toml_text = textwrap.dedent("""
            [[requests]]
            name = "echo"
            method = "GET"
            url = "http://127.0.0.1/${random.UUID_HEX}"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)

        ns = {"__name__": "generated_uuid_hex_test"}
        exec(script, ns)
        out = ns["render"]("${random.UUID_HEX}", {"vars": {}, "steps": {}})
        self.assertEqual(len(out), 32)
        self.assertEqual(uuid.UUID(hex=out).hex, out)

    def test_generated_env_var(self):
        toml_text = textwrap.dedent("""
            [[requests]]
            name = "echo"
            method = "GET"
            url = "http://127.0.0.1/${env.HTTPFLOW_TEST_USER}"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)

        ns = {"__name__": "generated_env_test"}
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

    def test_generated_render_matches_package_render(self):
        toml_text = textwrap.dedent("""
            [[requests]]
            name = "echo"
            method = "GET"
            url = "http://127.0.0.1/${var.env}"
        """).encode("utf-8")
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
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)

        ns = {"__name__": "generated_render_parity_test"}
        exec(script, ns)
        for text in cases:
            self.assertEqual(ns["render"](text, store), render(text, store))
        with self.assertRaises(TemplateError):
            render("${var.missing}", store)
        with self.assertRaises(ns["TemplateError"]):
            ns["render"]("${var.missing}", store)

    def test_generated_extract_matches_package_extract(self):
        toml_text = textwrap.dedent("""
            [[requests]]
            name = "echo"
            method = "GET"
            url = "http://127.0.0.1/"
        """).encode("utf-8")
        body = {"data": {"user": {"id": 42}}, "items": [{"id": "a1"}, {"id": "a2"}]}
        cases = ["data.user.id", "items[1].id"]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)

        ns = {"__name__": "generated_extract_parity_test"}
        exec(script, ns)
        for path in cases:
            self.assertEqual(ns["extract"](body, path), extract(body, path))
        with self.assertRaises(KeyError):
            ns["extract"](body, "data.missing")
        with self.assertRaises(IndexError):
            ns["extract"](body, "items[9].id")

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
        """When no request has until, no extra polling section should be emitted."""
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

            self.assertIn("(no until blocks", script)

    def test_unused_repeat_helpers_omitted(self):
        """When no ${repeat.*} is referenced, no extra repeat section should be emitted."""
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

            self.assertIn("(no ${repeat.*} references", script)

    def test_default_vars_embedded(self):
        """-v K=V sets DEFAULT_VARS; script runs without args and can be overridden."""
        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/echo?env=${{var.env}}&user=${{var.user}}"
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
            self.assertIn("  * Required parameters (referenced by ${var.*} but not embedded)", help_res.stdout)
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

            missing = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(missing.returncode, 1)
            self.assertIn("missing required -v/--var for: ['user']", missing.stderr)
            self.assertNotIn("==>", missing.stdout)

    def test_generated_help_omits_required_vars_block_when_none_required(self):
        """Required parameters block appears only when missing ${var.*} exist."""
        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/echo?env=${{var.env}}"
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

    def test_generated_help_shows_default_repeat_vars(self):
        """--repeat-vars help lists embedded DEFAULT_REPEAT_VARS when present."""
        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/echo?id=${{repeat.id}}&label=${{repeat.label}}"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(
                wf,
                default_repeat_vars={"id": ["1", "2"], "label": ["a", "b"]},
            )
            script_path = tmp_path / "workflow.py"
            script_path.write_text(script, encoding="utf-8")

            help_res = subprocess.run(
                [sys.executable, str(script_path), "--help"],
                capture_output=True, text=True, timeout=10,
            )

            self.assertEqual(help_res.returncode, 0, msg=help_res.stderr)
            self.assertIn("  * DEFAULT_REPEAT_VARS (optional parameters)", help_res.stdout)
            self.assertIn("    - id=1,2", help_res.stdout)
            self.assertIn("    - label=a,b", help_res.stdout)

    def test_generated_help_shows_required_repeat_vars(self):
        """--repeat-vars help lists missing ${repeat.*} names."""
        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/echo?id=${{repeat.id}}&label=${{repeat.label}}"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf, default_repeat_vars={"id": ["1", "2"]})
            script_path = tmp_path / "workflow.py"
            script_path.write_text(script, encoding="utf-8")

            help_res = subprocess.run(
                [sys.executable, str(script_path), "--help"],
                capture_output=True, text=True, timeout=10,
            )

            self.assertEqual(help_res.returncode, 0, msg=help_res.stderr)
            self.assertIn("  * DEFAULT_REPEAT_VARS (optional parameters)", help_res.stdout)
            self.assertIn("    - id=1,2", help_res.stdout)
            self.assertIn("  * Required parameters (referenced by ${repeat.*} but not embedded)", help_res.stdout)
            self.assertIn("    - label", help_res.stdout)

    def test_generated_parity_pretty_json_and_masking(self):
        """Generated script must produce identical log output for --pretty-json / --no-mask."""
        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
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
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf, default_vars={"env": "test"})
            script_path = tmp_path / "workflow.py"
            script_path.write_text(script, encoding="utf-8")

            # Must NOT contain any httpflow import
            self.assertNotIn("import httpflow", script)
            self.assertNotIn("from httpflow", script)

            # --- Run #1: default (masking ON) ---
            res = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            stdout1 = res.stdout
            self.assertIn("> POST /auth HTTP/1.1", stdout1)
            self.assertIn("> GET /me HTTP/1.1", stdout1)
            self.assertIn("* capture token = '***'", stdout1)
            self.assertIn("> Authorization: ***", stdout1)

            # --- Run #2: --no-mask ---
            res2 = subprocess.run(
                [sys.executable, str(script_path), "--no-mask"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res2.returncode, 0, msg=res2.stderr)
            stdout2 = res2.stdout
            self.assertIn("* capture token = 'gen-tok'", stdout2)
            self.assertIn("> Authorization: Bearer gen-tok", stdout2)

            # --- Run #3: --pretty-json ---
            res3 = subprocess.run(
                [sys.executable, str(script_path), "--pretty-json"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res3.returncode, 0, msg=res3.stderr)
            stdout3 = res3.stdout
            # request body pretty-printed with 2-space indent
            self.assertIn('  "user": "u"', stdout3)
            # response body pretty-printed
            self.assertIn('  "user":', stdout3)

            # --- Run #4: --quiet ---
            res4 = subprocess.run(
                [sys.executable, str(script_path), "--quiet"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res4.returncode, 0, msg=res4.stderr)
            stdout4 = res4.stdout
            # Must NOT contain detailed request/response lines
            self.assertNotIn("> POST", stdout4)
            self.assertNotIn("< HTTP/1.1", stdout4)
            # But summary lines should still be present
            self.assertIn("[getToken] POST ", stdout4)
            self.assertIn("[getToken] status=200", stdout4)

            # --- Run outside repo directory (self-containment) ---
            import os
            repo_top = os.getcwd()
            env = dict(os.environ)
            # Ensure PYTHONPATH does NOT include repo top (prevent incidental import)
            if "PYTHONPATH" in env:
                env["PYTHONPATH"] = env["PYTHONPATH"].replace(repo_top, "").strip(":")
            res5 = subprocess.run(
                [sys.executable, str(script_path), "--quiet"],
                capture_output=True, text=True, timeout=10,
                # Run from root /tmp to be outside the repo checkout
                cwd="/tmp",
                env=env,
            )
            self.assertEqual(res5.returncode, 0, msg=res5.stderr)

    def test_generated_script_omits_until_when_not_used(self):
        """Workflow without until must not contain until-specific helpers."""
        toml_text = textwrap.dedent("""
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
        self.assertNotIn("def eval_until", script)
        self.assertNotIn("def poll_until", script)
        self.assertNotIn("_UNTIL_OPS", script)

    def test_generated_script_includes_until_when_used(self):
        """Workflow with until must include until helpers."""
        toml_text = textwrap.dedent("""
            [[requests]]
            name = "poll"
            method = "GET"
            url = "http://127.0.0.1:1/poll"
            until = ["condition = ${status} == Active", "interval = 0", "max_attempts = 1"]
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)

        compile(script, "<generated>", "exec")
        self.assertIn("def eval_until", script)
        self.assertIn("def poll_until", script)
        self.assertIn("_UNTIL_OPS", script)
        # until-only workflow should not include argparse --repeat-vars
        self.assertNotIn('p.add_argument("--repeat-vars"', script)

    def test_generated_script_includes_repeat_when_used(self):
        """Workflow with ${repeat.*} must include repeat helpers and CLI option."""
        toml_text = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://127.0.0.1:1/ping?id=${repeat.id}"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)

        compile(script, "<generated>", "exec")
        self.assertIn("def parse_repeat_args", script)
        self.assertIn("def build_repeat_iterations_from_args", script)
        self.assertIn("--repeat-vars", script)

    def test_generated_script_never_contains_httpflow_imports(self):
        """Generated script must be free of any httpflow or relative package imports."""
        toml_text = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://127.0.0.1:1/ping?id=${repeat.id}"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)

        compile(script, "<generated>", "exec")
        # Enhanced import guards: allow indentation and word boundaries
        self.assertNotRegex(script, r"(?m)^\s*from\s+\.")
        self.assertNotRegex(script, r"(?m)^\s*import\s+httpflow\b")
        self.assertNotRegex(script, r"(?m)^\s*from\s+httpflow\b")

    def test_embedded_runtime_helpers_compile_cleanly(self):
        """Flattened runtime modules compile and contain no package imports."""
        from pathlib import Path
        from httpflow.generator import _flatten_modules

        # Exercise the full runtime matrix that may be embedded
        src = _flatten_modules({"core", "mask", "http", "until", "repeat"})
        compile(src, "<flattened runtime>", "exec")
        self.assertNotRegex(src, r"(?m)^\s*from\s+\.")
        self.assertNotRegex(src, r"(?m)^\s*import\s+httpflow\b")
        self.assertNotRegex(src, r"(?m)^\s*from\s+httpflow\b")

    def test_empty_workflow_compiles(self):
        """A workflow with no steps still produces valid Python."""
        toml_text = b""

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)

        compile(script, "<generated>", "exec")


if __name__ == "__main__":
    unittest.main()
