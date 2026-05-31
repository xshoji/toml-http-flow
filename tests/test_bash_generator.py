import shutil
import subprocess
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path

from httpflow import config as cfg_mod
from httpflow import bash_generator


@unittest.skipUnless(
    shutil.which("bash") and shutil.which("curl"),
    "bash and curl required",
)
class TestBashGenerator(unittest.TestCase):

    def _generate_and_check(self, toml_text: str, shebang: bool = False):
        """Generate script, check syntax, return script text."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml_text, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf, shebang=shebang)
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
            return script

    def test_simple_get(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_ping()", script)
        self.assertIn('curl -sS -L -v -w "%{http_code}"', script)
        self.assertIn('-X GET', script)

    def test_post_with_body(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "create"
            method = "POST"
            url = "http://example.com/items"
            headers = ["Content-Type: application/json"]
            body = '{"name":"test"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_create()", script)
        self.assertIn("local __BODY=$(cat << EOF", script)
        self.assertIn('{"name":"test"}', script)
        self.assertIn('cmd+=(-d', script)
        self.assertIn('"$__BODY"', script)

    def test_form_body(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "login"
            method = "POST"
            url = "http://example.com/auth"
            body_form = ["user = alice", "pass = secret"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_login()", script)
        self.assertIn('Content-Type: application/x-www-form-urlencoded', script)
        self.assertIn('user=alice&pass=secret', script)

    def test_sleep_step(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "wait"
            method = "SLEEP"
            url = "0.05"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_wait()", script)
        self.assertIn('seconds="0.05"', script)
        self.assertIn('sleep "$seconds"', script)

    def test_sleep_step_with_shell_variable(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "wait"
            method = "SLEEP"
            url = "${WAIT_SECONDS}"
        """)
        script = self._generate_and_check(toml)
        self.assertIn('seconds="${WAIT_SECONDS}"', script)
        self.assertIn('echo "==> [wait] SLEEP $seconds"', script)
        self.assertIn('sleep "$seconds"', script)

    def test_shebang(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping"
        """)
        script = self._generate_and_check(toml, shebang=True)
        self.assertTrue(script.startswith("#!/usr/bin/env bash\n"))

    def test_env_var_in_url(self):
        """URL containing $VAR is emitted as-is for shell expansion."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping?id=$ITEM_ID"
        """)
        script = self._generate_and_check(toml)
        self.assertIn('cmd+=("$url")', script)

    def test_var_and_repeat_placeholders_become_shell_env_names(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "echo"
            method = "POST"
            url = "http://example.com/${var.env}?id=${repeat.id}"
            body = '{"name":"${var.user}","id":"${repeat.id}"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn('url="http://example.com/${VAR_env}?id=${REPEAT_id}"', script)
        self.assertIn('{"name":"${VAR_user}","id":"${REPEAT_id}"}', script)

    def test_no_bash4_features(self):
        """Ensure no bash 4+ only syntax slips in."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping"
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
            method = "GET"
            url = "http://example.com/1"

            [[requests]]
            name = "a_b"
            method = "GET"
            url = "http://example.com/2"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_a_b()", script)
        self.assertIn("step_a_b_2()", script)

    def test_description_is_emitted(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping"
            description = "health check"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("# health check", script)

    def test_random_uuid_placeholders(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "create"
            method = "POST"
            url = "http://example.com/items/${random.UUID_HEX}"
            headers = ["X-Request-Id: ${random.UUID}"]
            body = '{"request_id":"${random.UUID}"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn("uuid()", script)
        self.assertIn("uuid_hex()", script)
        self.assertIn('url="http://example.com/items/$(uuid_hex)"', script)
        self.assertIn('header="X-Request-Id: $(uuid)"', script)
        self.assertIn('cmd+=(-H "$header")', script)
        self.assertIn('{"request_id":"$(uuid)"}', script)

    def test_generated_uuid_helpers_return_valid_values(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", "-c", f"source {script_path} >/dev/null; uuid; uuid_hex"],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        uuid_value, uuid_hex_value = res.stdout.splitlines()
        self.assertEqual(str(uuid.UUID(uuid_value)), uuid_value)
        self.assertEqual(uuid.UUID(hex=uuid_hex_value).hex, uuid_hex_value)

    def test_masking_is_emitted_for_logs(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "login"
            method = "POST"
            url = "http://example.com/auth?token=url-secret&keep=ok"
            headers = ["Authorization: Bearer header-secret"]
            body_form = ["user = alice", "password = body-secret"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("mask()", script)
        self.assertIn('$(mask "$url")', script)
        self.assertIn('echo "> $(mask "$header")"', script)
        self.assertIn('echo "> body: $(mask "$__BODY")"', script)

    def test_generated_mask_helper_masks_simple_values(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {script_path} >/dev/null; "
                    "mask 'token=abc&keep=ok'; "
                    "mask 'Authorization: Bearer secret'; "
                    "mask '{\"password\":\"p\",\"user\":\"u\"}'",
                ],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("token=***", res.stdout)
        self.assertIn("Authorization: ***", res.stdout)
        self.assertIn('"password":***', res.stdout)
        self.assertNotIn("abc", res.stdout)
        self.assertNotIn("Bearer secret", res.stdout)


if __name__ == "__main__":
    unittest.main()
