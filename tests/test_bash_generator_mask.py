"""Tests for masking behaviour in generated bash scripts."""

from __future__ import annotations

import shlex
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests._bash_generator_helpers import TestBashGeneratorBase


class TestBashGeneratorMask(TestBashGeneratorBase):
    def test_masking_is_emitted_for_logs(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "login"
            request = "POST http://example.com/auth?token=url-secret&keep=ok"
            headers = ["Authorization: Bearer header-secret"]
            body_form = ["user = alice", "password = body-secret"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("mask()", script)
        self.assertIn("mask_lines()", script)
        self.assertIn('$(mask "$url")', script)
        self.assertNotIn('printf "> %s\\n" "$header"', script)
        self.assertIn('printf "%s" "$body_log" | jq_or_cat | prefix_lines "> "', script)
        self.assertIn('printf "%s" "$body_log" | prefix_lines "> "', script)
        self.assertIn("sed -E", script)
        self.assertNotIn("perl -pe", script)
        self.assertIn("MASK_KEYS_DEFAULT='[aA]uthorization|[cC]ookie", script)
        self.assertIn("[sS]et-[cC]ookie", script)
        self.assertNotIn("mask_key_pattern()", script)
        self.assertIn('tee -a "$trace_file"', script)
        self.assertIn("mask_lines", script)

    def test_generated_mask_helper_masks_simple_values(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"HTTPFLOW_NO_MASK=; source {script_path} >/dev/null || true; "
                    "mask 'token=abc'; "
                    "mask 'Token=ABC'; "
                    "mask 'Authorization: Bearer secret'; "
                    "mask 'authorization: Bearer 06a84af6-4f9f-4b84-bfe2-529e310eea12'; "
                    "mask 'Set-Cookie: session=set-cookie-secret'; "
                    "mask '{\"password\":\"p\",\"user\":\"u\"}'",
                ],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("token=***", res.stdout)
        self.assertIn("Token=***", res.stdout)
        self.assertIn("Authorization: ***", res.stdout)
        self.assertIn("authorization: ***", res.stdout)
        self.assertIn("Set-Cookie: ***", res.stdout)
        self.assertIn('"password":***', res.stdout)
        self.assertNotIn("abc", res.stdout)
        self.assertNotIn("ABC", res.stdout)
        self.assertNotIn("secret", res.stdout)
        self.assertNotIn("set-cookie-secret", res.stdout)
        self.assertNotIn("06a84af6", res.stdout)

    def test_generated_mask_helper_masks_header_values_with_symbols(self):
        """Bash mask should hide whole HTTP header values containing symbols."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)
        cases = [
            "Authorization: Bearer abc.def/ghi+jkl=mn_op-qr:st;uv,wx yz",
            "authorization: Basic abc+def/ghi==",
            "Cookie: sid=abc.def/ghi+jkl=mn_op-qr:st;uv,wx yz; theme=dark",
            "Set-Cookie: session=abc.def/ghi+jkl=mn_op-qr:st;uv,wx yz; Path=/; HttpOnly",
            "X-Api-Key: abc.def/ghi+jkl=mn_op-qr:st;uv,wx yz",
            "password: abc.def/ghi+jkl=mn_op-qr:st;uv,wx yz",
        ]

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            quoted = " ".join(shlex.quote(case) for case in cases)
            res = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {shlex.quote(str(script_path))} >/dev/null || true; "
                    f"printf '%s\n' {quoted} | mask_lines",
                ],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        lines = res.stdout.splitlines()
        self.assertEqual(len(lines), len(cases))
        for line in lines:
            self.assertRegex(line, r"^(Authorization|authorization|Cookie|Set-Cookie|X-Api-Key|password): \*\*\*$")
        for raw in cases:
            self.assertNotIn(raw.split(": ", 1)[1], res.stdout)

    def test_generated_mask_helper_masks_comma_separated_header_values(self):
        """Bash mask should hide whole comma-separated sensitive header values."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {shlex.quote(str(script_path))} >/dev/null || true; "
                    "mask 'Authorization: Bearer a,b'; "
                    "mask 'Cookie: a=b, c=d'; "
                    "printf '%s\n' "
                    "'> Authorization: Bearer a,b' "
                    "'> Cookie: a=b, c=d' "
                    "'< Set-Cookie: sid=a,b; Path=/' "
                    "'X-Api-Key: key-a,key-b' "
                    "'Accept: application/json, text/plain' "
                    "| mask_lines",
                ],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("Authorization: ***", res.stdout)
        self.assertIn("Cookie: ***", res.stdout)
        self.assertIn("> Authorization: ***", res.stdout)
        self.assertIn("> Cookie: ***", res.stdout)
        self.assertIn("< Set-Cookie: ***", res.stdout)
        self.assertIn("X-Api-Key: ***", res.stdout)
        self.assertIn("Accept: application/json, text/plain", res.stdout)
        self.assertNotIn("Bearer a,b", res.stdout)
        self.assertNotIn("a=b, c=d", res.stdout)
        self.assertNotIn("sid=a,b", res.stdout)
        self.assertNotIn("key-a,key-b", res.stdout)

    def test_mask_lines_masks_curl_like_output(self):
        """mask_lines should mask sensitive fields in piped curl-like output."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {script_path} >/dev/null || true; "
                    "printf '%s\n' "
                      "\"Authorization: Bearer secret\" "
                      "\"Set-Cookie: session=abc123\" "
                      "\"token=url-secret\" "
                      "\"< HTTP/1.1 200 OK\" "
                      "\"< password: mypass\" "
                      "\"{{\\\"password\\\":\\\"secret\\\"}}\" "
                    "| mask_lines",
                ],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("Authorization: ***", res.stdout)
        self.assertIn("Set-Cookie: ***", res.stdout)
        self.assertIn("token=***", res.stdout)
        self.assertIn('"password":***', res.stdout)
        self.assertIn("< HTTP/1.1 200 OK", res.stdout)
        self.assertNotIn("Bearer secret", res.stdout)
        self.assertNotIn("session=abc123", res.stdout)
        self.assertNotIn("url-secret", res.stdout)
        self.assertNotIn("mypass", res.stdout)
        self.assertNotIn('"password":"secret"', res.stdout)

    def test_mask_lines_masks_json_values_with_spaces(self):
        """Bash mask should hide JSON values containing multiple words."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {script_path} >/dev/null || true; "
                    "printf '%s\\n' "
                    "\"{{\\\"password\\\": \\\"my secret token with spaces\\\"}}\" "
                    "\"{{\\\"token\\\": \\\"foo bar baz qux\\\"}}\" "
                    "\"{{\\\"normal\\\": \\\"keep this value\\\"}}\" "
                    "| mask_lines",
                ],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn('"password": ***', res.stdout)
        self.assertIn('"token": ***', res.stdout)
        self.assertIn('"normal": "keep this value"', res.stdout)
        self.assertNotIn("my secret token with spaces", res.stdout)
        self.assertNotIn("foo bar baz qux", res.stdout)

    def test_mask_lines_masks_values_with_commas(self):
        """Comma inside non-header values must not terminate masking."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {shlex.quote(str(script_path))} >/dev/null || true; "
                    "printf '%s\\n' "
                    "'\"Authorization\":\"Bearer abc,def\"' "
                    "'token=abc,def&key=value' "
                    "'password: abc,def/ghi' "
                    "| mask_lines",
                ],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn('"Authorization":***', res.stdout)
        self.assertIn("token=***&key=value", res.stdout)
        self.assertIn("password: ***", res.stdout)
        self.assertNotIn("abc,def", res.stdout)
        self.assertNotIn("abc", res.stdout)

    def test_mask_extra_env_var_in_bash_script(self):
        """HTTPFLOW_MASK_EXTRA env var extends masking keys in bash script."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash", "-c",
                    f"HTTPFLOW_MASK_EXTRA='[tT]race-id' HTTPFLOW_NO_MASK=; source {script_path} >/dev/null || true; "
                    "mask 'trace-id=secret'; "
                    "mask 'Trace-id=Secret'; "
                    "mask 'token=foo'; "
                    "mask 'Authorization: Bearer bar'",
                ],
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("trace-id=***", res.stdout)
        self.assertIn("Trace-id=***", res.stdout)
        self.assertIn("token=***", res.stdout)
        self.assertIn("Authorization: ***", res.stdout)
        self.assertNotIn("secret", res.stdout)
        self.assertNotIn("Secret", res.stdout)
        self.assertNotIn("foo", res.stdout)
        self.assertNotIn("Bearer bar", res.stdout)

    def test_no_mask_env_var_disables_masking_in_bash_script(self):
        """HTTPFLOW_NO_MASK disables masking in generated bash output."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash", "-c",
                    f"HTTPFLOW_NO_MASK=1; source {script_path} >/dev/null || true; "
                    "mask 'token=foo'; "
                    "printf '%s\n' 'Authorization: Bearer bar' | mask_lines",
                ],
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("token=foo", res.stdout)
        self.assertIn("Authorization: Bearer bar", res.stdout)
        self.assertNotIn("***", res.stdout)

    def test_no_mask_argument_disables_masking_in_bash_script(self):
        """Generated bash accepts --no-mask at runtime."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo"
            headers = ["Authorization: Bearer secret_token"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("--no-mask", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path), "--no-mask"], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("Bearer secret_token", res.stdout)
        self.assertNotIn("***", res.stdout)
