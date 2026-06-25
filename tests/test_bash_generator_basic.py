"""Tests for basic bash script generation: HTTP steps, sleep, descriptions, placeholders."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path

from httpflow import bash_generator
from httpflow import config as cfg_mod

from tests._bash_generator_helpers import TestBashGeneratorBase, _CaptureHandler


class TestBashGeneratorBasic(TestBashGeneratorBase):
    def test_simple_get(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_ping()", script)
        self.assertIn("curl_command=$(cat << 'EOT'", script)
        self.assertIn('curl -sS -L -v --no-buffer --stderr - -X GET "$url"', script)
        self.assertIn("grep -v '^\\({\\|}\\) \\[.*bytes data\\]'", script)
        self.assertIn("grep -v '^\\*'", script)
        self.assertIn('tee -a "$trace_file"', script)
        self.assertNotIn('-D "$resp_headers" -o "$resp_body"', script)
        self.assertIn("http_step 'ping' 'GET'", script)

    def test_time_placeholders(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "echo"
            request = "GET {base}/echo?iso=${{time.DATE_ISO}}&ymd=${{time.DATE_YMD}}&hms=${{time.DATE_YMDHMS}}"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertRegex(res.stdout, r"iso=\d{4}-\d{2}-\d{2}T\d{2}:")
        self.assertRegex(res.stdout, r"ymd=\d{8}")
        self.assertRegex(res.stdout, r"hms=\d{14}")

    def test_http_summary_lines_include_timestamps(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        ts = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+([+-]\d{2}:\d{2})?"
        self.assertRegex(res.stdout, rf"==> {ts} \[ping\] GET {base}/echo")
        self.assertRegex(res.stdout, rf"<== {ts} \[ping\]")

    def test_post_with_body(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "create"
            request = "POST http://example.com/items"
            headers = ["Content-Type: application/json"]
            body = '{"name":"test"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_create()", script)
        self.assertIn("body=$(cat << EOT", script)
        self.assertNotIn("__HF_BODY_step_create", script)
        self.assertIn('{"name":"test"}', script)
        self.assertIn('-d "$body"', script)

    def test_post_body_is_inserted_at_curl_request_response_boundary(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "create"
            request = "POST {base}/auth"
            headers = ["Content-Type: application/json"]
            body = '{{"name":"test"}}'
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        lines = res.stdout.splitlines()
        boundary_idx = lines.index("> ")
        self.assertEqual(lines[boundary_idx + 1], '> {"name":"test"}')
        self.assertRegex(lines[boundary_idx + 2], r"^<== \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+([+-]\d{2}:\d{2})? \[create\]")
        self.assertTrue(lines[boundary_idx + 3].startswith("< HTTP/"))

    def test_form_body(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "login"
            request = "POST {base}/auth"
            body_form = ["user = alice", "pass = secret"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_login()", script)
        self.assertIn('Content-Type: application/x-www-form-urlencoded', script)
        self.assertNotIn("body_form_text=", script)
        self.assertIn('--data-urlencode "user=alice"', script)
        self.assertIn('--data-urlencode "pass=secret"', script)
        self.assertNotIn("Note: Values are shown before URL encoding.", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("> user=alice", res.stdout)
        self.assertIn("> pass=secret", res.stdout)

    def test_form_body_placeholders_expand_before_urlencode(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "first"
            request = "POST http://example.com/first"
            capture = ["authorization = response.body.token"]

            [[requests]]
            name = "login"
            request = "POST http://example.com/auth"
            body_form = [
                "nickname = new_name",
                "email    = test@email.com",
                "args     = ${var.argsAaa}",
                "params   = ${var.paramsParamb}",
                "token    = ${authorization}",
            ]
        """)
        script = self._generate_and_check(toml)
        self.assertIn('--data-urlencode "nickname=new_name"', script)
        self.assertIn('--data-urlencode "email=test@email.com"', script)
        self.assertIn('--data-urlencode "args=${VAR_ARGSAAA}"', script)
        self.assertIn('--data-urlencode "params=${VAR_PARAMSPARAMB}"', script)
        self.assertIn('--data-urlencode "token=${VAR_AUTHORIZATION}"', script)
        self.assertNotIn("%24%7Bauthorization%7D", script)

    def test_form_body_does_not_duplicate_user_content_type(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "login"
            request = "POST http://example.com/auth"
            headers = ["Content-Type: application/x-www-form-urlencoded"]
            body_form = ["user = alice", "pass = secret"]
        """)
        script = self._generate_and_check(toml)
        self.assertEqual(script.count("Content-Type: application/x-www-form-urlencoded"), 1)
        self.assertIn('-H "Content-Type: application/x-www-form-urlencoded"', script)

    def test_form_body_content_type_detection_is_case_insensitive(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "login"
            request = "POST http://example.com/auth"
            headers = ["content-type: application/x-www-form-urlencoded"]
            body_form = ["user = alice", "pass = secret"]
        """)
        script = self._generate_and_check(toml)
        self.assertNotIn('Content-Type: application/x-www-form-urlencoded', script)
        self.assertIn('content-type: application/x-www-form-urlencoded', script)

    def test_env_placeholders_use_shell_environment_variables(self):
        """${env.NAME} placeholders become ${NAME} in generated bash."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "env"
            request = "POST http://example.com/${env.USER}"
            headers = ["X-User: ${env.USER}"]
            body = '{"user":"${env.USER}"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn('local url="http://example.com/${USER}"', script)
        self.assertIn("X-User: ${USER}", script)
        self.assertIn('{"user":"${USER}"}', script)
        self.assertNotIn("${env.USER}", script)

    def test_sleep_step(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "wait"
            request = "SLEEP 0.05"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_wait()", script)
        self.assertIn('seconds="0.05"', script)
        self.assertIn('print_blank_lines "${HTTPFLOW_BLANK_LINE:-0}"', script)
        self.assertIn('SLEEP %s\\n" "$(time_date_iso)" \'wait\' "$seconds"', script)
        self.assertIn('sleep "$seconds"', script)
        self.assertIn('done\\n" "$(time_date_iso)" \'wait\'', script)

    def test_sleep_step_with_shell_variable(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "wait"
            request = "SLEEP ${WAIT_SECONDS}"
        """)
        script = self._generate_and_check(toml)
        self.assertIn('seconds="${WAIT_SECONDS}"', script)
        self.assertIn('SLEEP %s\\n" "$(time_date_iso)" \'wait\' "$seconds"', script)
        self.assertIn('sleep "$seconds"', script)

    def test_shebang(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml, shebang=True)
        self.assertTrue(script.startswith("#!/usr/bin/env bash\n"))

    def test_env_var_in_url(self):
        """URL containing $VAR is emitted as-is for shell expansion."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping?id=$ITEM_ID"
        """)
        script = self._generate_and_check(toml)
        self.assertIn('"$url"', script)

    def test_var_placeholders_become_shell_env_names(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "echo"
            request = "POST http://example.com/${var.env}?id=${var.id}"
            body = '{"name":"${var.user}","id":"${var.id}"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn('url="http://example.com/${VAR_ENV}?id=${VAR_ID}"', script)
        self.assertIn('{"name":"${VAR_USER}","id":"${VAR_ID}"}', script)

    def test_no_bash4_features(self):
        """Ensure no bash 4+ only syntax slips in."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)
        forbidden = ["declare -A", "mapfile", "readarray", "local -n"]
        for pat in forbidden:
            self.assertNotIn(pat, script, msg=f"bash 4+ feature found: {pat}")

    def test_step_name_collision(self):
        """Duplicate sanitized names get numeric suffixes."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "a-b"
            request = "GET http://example.com/1"
            [[requests]]
            name = "a_b"
            request = "GET http://example.com/2"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_a_b()", script)
        self.assertIn("step_a_b_2()", script)

    def test_description_is_emitted(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
            description = "health check"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("http_step 'ping' 'GET'", script)
        self.assertIn("'health check'", script)

    def test_description_is_printed_after_http_start_line(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo"
            description = "health check"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        start = res.stdout.index("[ping] GET")
        desc = res.stdout.index("# health check")
        detail = res.stdout.index("> GET /echo")
        self.assertLess(start, desc)
        self.assertLess(desc, detail)

    def test_random_uuid_placeholders(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "create"
            request = "POST http://example.com/items/${random.UUID_HEX}"
            headers = ["X-Request-Id: ${random.UUID}"]
            body = '{"request_id":"${random.UUID}"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn("uuid()", script)
        self.assertIn("uuid_hex()", script)
        self.assertIn('url="http://example.com/items/$(uuid_hex)"', script)
        self.assertIn('X-Request-Id: $(uuid)', script)
        self.assertIn('{"request_id":"$(uuid)"}', script)

    def test_generated_uuid_helpers_return_valid_values(self):
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
                ["bash", "-c", f"source {script_path} >/dev/null || true; uuid; uuid_hex"],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        uuid_value, uuid_hex_value = res.stdout.splitlines()
        self.assertEqual(str(uuid.UUID(uuid_value)), uuid_value)
        self.assertEqual(uuid.UUID(hex=uuid_hex_value).hex, uuid_hex_value)

    def test_slash_in_step_name_runs_successfully(self):
        """Step names containing '/' must not break temp file creation."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "api/v1/me"
            request = "GET {base}/me"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_api_v1_me()", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=10
            )
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("[api/v1/me] GET", res.stdout)

    def test_sleep_step_name_is_safe_from_shell_injection(self):
        """SLEEP step name with shell metacharacters must not be expanded."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "`id`$(echo injected)&"
            request = "SLEEP 0.01"
            description = "); echo injected2"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("`id`$(echo injected)&", script)
        self.assertNotIn("echo \"#>", script)
        self.assertIn('printf "==> %s [%s] SLEEP', script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertNotIn("uid=", res.stdout, msg="shell expansion of $(id) must not happen")
        self.assertNotIn("uid=", res.stdout, msg="command substitution must not happen")

    def test_default_var_containing_brace_generates_valid_bash(self):
        """Default var value containing } must be escaped properly."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(
                wf,
                default_vars={"env": "prod}", "user": "alice{foo}"},
            )
            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")

            syntax = subprocess.run(
                ["bash", "-n", str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(
                syntax.returncode, 0,
                msg=f"syntax error:\n{syntax.stderr}\n--- script ---\n{script}",
            )
            res = subprocess.run(
                ["bash", "-c",
                 f"source {script_path} >/dev/null || true; printf '%s\\n' \"${{VAR_ENV}}\" \"${{VAR_USER}}\""],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("prod}", res.stdout)
            self.assertIn("alice{foo}", res.stdout)
