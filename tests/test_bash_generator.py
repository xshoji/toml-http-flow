import shutil
import subprocess
import tempfile
import textwrap
import unittest
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
        self.assertIn('curl -sS -L -w "%{http_code}"', script)
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
        self.assertIn("read -r -d \"\" __BODY <<'EOF'", script)
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
        self.assertIn("sleep 0.05", script)

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
        self.assertIn('"$url"', script)

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


if __name__ == "__main__":
    unittest.main()
