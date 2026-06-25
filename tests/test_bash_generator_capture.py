"""Tests for capture and until behaviour in generated bash scripts."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests._bash_generator_helpers import TestBashGeneratorBase, _CaptureHandler


class TestBashGeneratorCapture(TestBashGeneratorBase):
    @unittest.skipUnless(
        shutil.which("jq"), "jq required"
    )
    def test_capture_json_and_reuse_as_var(self):
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.seen_auth = ""
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "auth"
            request = "POST {base}/auth"
            capture = ["token = access_token", "uid = response.body.data.id"]

            [[requests]]
            name = "me"
            request = "GET {base}/me"
            headers = ["Authorization: Bearer ${{var.token}}", "X-User: ${{var.uid}}"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("jq is required for JSON capture", script)
        self.assertIn("capture_response_body_json 'VAR_TOKEN' 'token' 'access_token'", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("* capture token = '***'", res.stdout)
        self.assertIn("* capture uid = '7'", res.stdout)
        self.assertEqual(_CaptureHandler.seen_auth, "Bearer bash-token")

    @unittest.skipUnless(
        shutil.which("jq"), "jq required"
    )
    def test_capture_json_false_null_array_and_hyphen_key(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "edge"
            request = "POST {base}/edge"
            capture = [
                "ok = ok",
                "empty = empty",
                "token = items[0].access-token",
            ]
        """)
        script = self._generate_and_check(toml)
        self.assertIn(".[\"items\"]?[0]?[\"access-token\"]?", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("* capture ok = 'false'", res.stdout)
        self.assertIn("* capture empty = 'nil'", res.stdout)
        self.assertIn("* capture token = '***'", res.stdout)

    @unittest.skipUnless(
        shutil.which("jq"), "jq required"
    )
    def test_capture_headers_and_request_values(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "echo"
            request = "POST {base}/echo?x=1"
            headers = ["Authorization: Bearer abc"]
            body = '{{"hello":"world"}}'
            capture = [
                "ct = response.header.content-type",
                "sent_auth = request.header.Authorization",
                "called = request.url",
                "sent_body = request.body",
            ]
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("* capture ct = 'application/json'", res.stdout)
        self.assertIn("* capture sent_auth = 'Bearer abc'", res.stdout)
        self.assertIn(f"* capture called = '{base}/echo?x=1'", res.stdout)
        self.assertIn("* capture sent_body = '{\"hello\":\"world\"}'", res.stdout)

    def test_header_only_capture_does_not_require_jq(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "echo"
            request = "GET {base}/echo"
            capture = ["ct = response.header.Content-Type"]
        """)
        script = self._generate_and_check(toml)
        self.assertNotIn("jq --version", script)

    @unittest.skipUnless(
        shutil.which("jq"), "jq required"
    )
    def test_response_header_capture_uses_final_redirect_headers(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "redir"
            request = "GET {base}/redir"
            capture = ["trace = response.header.X-Trace-Id"]
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertIn('/^< HTTP\\// { found=0; value=""; next }', script)

    @unittest.skipUnless(
        shutil.which("jq"), "jq required"
    )
    def test_capture_failure_stops_later_steps(self):
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.me_count = 0
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "auth"
            request = "POST {base}/auth"
            capture = ["missing = nope"]

            [[requests]]
            name = "me"
            request = "GET {base}/me"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("capture failed: missing <- nope", res.stderr)
        self.assertEqual(_CaptureHandler.me_count, 0)

    # ── until ───────────────────────────────────────────────────────

    @unittest.skipUnless(
        shutil.which("jq"), "jq required"
    )
    def test_until_polls_until_capture_matches(self):
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.poll_count = 0
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "poll"
            request = "GET {base}/poll"
            capture = ["status = status"]
            until = ["condition = ${{status}} == Active", "interval = 0", "max_attempts = 5"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("until_eval", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertEqual(_CaptureHandler.poll_count, 3)
        self.assertIn("until satisfied on attempt 3", res.stdout)

    @unittest.skipUnless(
        shutil.which("jq"), "jq required"
    )
    def test_until_exhaustion_fails(self):
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.poll_count = 0
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "poll"
            request = "GET {base}/poll"
            capture = ["status = status"]
            until = ["condition = ${{status}} == Never", "interval = 0", "max_attempts = 2"]
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(_CaptureHandler.poll_count, 2)
        self.assertIn("until condition not satisfied after 2 attempts", res.stderr)

    @unittest.skipUnless(
        shutil.which("jq"), "jq required"
    )
    def test_until_treats_http_error_as_response(self):
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.poll_count = 0
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "poll"
            request = "GET {base}/poll404"
            capture = ["status = status"]
            until = ["condition = ${{status}} == Active", "interval = 0", "max_attempts = 3"]
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("until satisfied on attempt 2", res.stdout)

    def test_until_helpers_omitted_when_unused(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)
        self.assertNotIn("until_eval", script)

    @unittest.skipUnless(
        shutil.which("jq"), "jq required"
    )
    def test_until_regex_uses_bash_native_eval(self):
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.poll_count = 0
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "poll"
            request = "GET {base}/poll"
            capture = ["status = status"]
            until = ["condition = ${{status}} ~ /active/i", "interval = 0", "max_attempts = 3"]
        """)
        script = self._generate_and_check(toml)
        self.assertNotIn("python3 is required for until regex", script)
        self.assertNotIn("import re", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("until satisfied on attempt 3", res.stdout)

    def test_until_function_name_no_collision_with_normal_step(self):
        """until _attempt suffix must not collide with another step's function name."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "poll_attempt"
            request = "GET http://example.com/other"
            [[requests]]
            name = "poll"
            request = "GET http://example.com/poll"
            capture = ["status = status"]
            until = ["condition = ${status} == Active", "interval = 0", "max_attempts = 3"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_poll_attempt()", script)
        self.assertIn("step_poll_attempt_2()", script)
        idx1 = script.index("step_poll_attempt()")
        idx3 = script.index("step_poll_attempt_2()")
        self.assertLess(idx1, idx3)

    # ── request body capture helpers ────────────────────────────────

    def test_capture_request_body_json_generates_helper(self):
        """capture request.body.<path> emits capture_request_body_json call."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "create"
            request = "POST {base}/auth"
            headers = ["Content-Type: application/json"]
            body = '{{"date":{{"time_DATE_ISO":"2026-06-24"}}}}'
            capture = ["iso = request.body.date.time_DATE_ISO"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("capture_request_body_json", script)
        self.assertIn("capture_request_body_json 'VAR_ISO' 'iso' 'request.body.date.time_DATE_ISO'", script)
        self.assertIn("'.[\"date\"]?[\"time_DATE_ISO\"]?'", script)
        self.assertIn('HF_BODY_LOG="$body_log"', script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("* capture iso = '2026-06-24'", res.stdout)
