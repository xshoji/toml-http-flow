"""Tests for runtime options: pretty-json, blank-line, default/required vars, CLI var injection."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path

from httpflow import bash_generator
from httpflow import config as cfg_mod

from tests._bash_generator_helpers import TestBashGeneratorBase, _CaptureHandler


class TestBashGeneratorRuntime(TestBashGeneratorBase):
    # ── pretty-json ─────────────────────────────────────────────────

    def test_pretty_json_argument_formats_bash_response_body(self):
        """Generated bash accepts --pretty-json at runtime."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("jq_or_cat()", script)
        self.assertIn("--pretty-json", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path), "--pretty-json"], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn('{\n  "ok": true\n}', res.stdout)

    # ── blank-line ──────────────────────────────────────────────────

    def test_blank_line_env_inserts_lines_between_steps(self):
        """HTTPFLOW_BLANK_LINE behaves like --blank-line for generated bash."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "one"
            request = "GET {base}/echo"
            [[requests]]
            name = "two"
            request = "GET {base}/echo"
        """)
        script = self._generate_and_check(toml)
        self.assertEqual(script.count('print_blank_lines "${HTTPFLOW_BLANK_LINE:-0}"'), 1)
        self.assertIn('print_blank_lines "${HTTPFLOW_BLANK_LINE:-0}"\n\n    echo "==>', script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", "-c", f"HTTPFLOW_BLANK_LINE=2 bash {script_path}"],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("\n\n\n==>", res.stdout)

    def test_blank_line_arg_inserts_lines_between_steps(self):
        """--blank-line N behaves like HTTPFLOW_BLANK_LINE for generated bash."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "one"
            request = "GET {base}/echo"
            [[requests]]
            name = "two"
            request = "GET {base}/echo"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("--blank-line", script)
        self.assertIn("usage: <script> [options] [--<name> <value>]...", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path), "--blank-line", "2"],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("\n\n\n==>", res.stdout)

    def test_blank_line_arg_equals_form(self):
        """--blank-line=N is accepted in addition to the space-separated form."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "one"
            request = "GET {base}/echo"
            [[requests]]
            name = "two"
            request = "GET {base}/echo"
        """)
        script = self._generate_and_check(toml)
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path), "--blank-line=3"],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("\n\n\n\n==>", res.stdout)

    def test_blank_line_arg_invalid_value_fails(self):
        """Non-numeric --blank-line is rejected with a non-zero exit."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "one"
            request = "GET {base}/echo"
        """)
        script = self._generate_and_check(toml)
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path), "--blank-line", "abc"],
                capture_output=True, text=True, timeout=10,
            )

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("non-negative integer", res.stderr)

    def test_blank_line_arg_overrides_env(self):
        """--blank-line takes precedence over HTTPFLOW_BLANK_LINE env var."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "one"
            request = "GET {base}/echo"
            [[requests]]
            name = "two"
            request = "GET {base}/echo"
        """)
        script = self._generate_and_check(toml)
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", "-c", f"HTTPFLOW_BLANK_LINE=1 bash {script_path} --blank-line 3"],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertTrue(res.stdout.startswith("\n\n\n==>"),
                        msg="expected 3 leading blank lines from --blank-line 3")
        self.assertFalse(res.stdout.startswith("\n==>"))

    # ── default / required vars ─────────────────────────────────────

    def test_default_vars_embedded_in_bash_script(self):
        """-v K=V in generate --format bash embeds default VAR_* values."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "echo"
            request = "POST http://example.com/${var.env}?id=${var.id}"
            body = '{"name":"${var.user}","id":"${var.id}"}'
        """)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(
                wf,
                default_vars={"env": "prod", "user": "alice"},
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

            self.assertIn('[[ -z "${VAR_ENV:-}" ]] && VAR_ENV=\'prod\'', script)
            self.assertIn('[[ -z "${VAR_USER:-}" ]] && VAR_USER=\'alice\'', script)
            defaults_pos = script.find("# ─── defaults")
            steps_pos = script.find("# ─── step functions")
            self.assertGreater(defaults_pos, -1)
            self.assertGreater(steps_pos, -1)
            self.assertLess(defaults_pos, steps_pos)

    def test_required_var_check_fails_with_export_hint(self):
        """Generated bash fails early when required ${var.*} env is empty."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/${var.user}"
        """)
        script = self._generate_and_check(toml)
        self.assertIn('[[ -z "${VAR_USER:-}" ]] && {', script)
        self.assertIn('Export it before running: export VAR_USER=<value>', script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=10
            )

        self.assertEqual(res.returncode, 1)
        self.assertIn("error: missing required variable: user", res.stderr)
        self.assertIn("export VAR_USER=<value>", res.stderr)

    def test_default_vars_overridable_at_runtime(self):
        """Embedded default vars can be overridden by exporting before running."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/${var.env}"
        """)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(
                wf,
                default_vars={"env": "default_env"},
            )
            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")

            res = subprocess.run(
                ["bash", "-c", f"source {script_path} >/dev/null || true; echo 'http://example.com/'\"${{VAR_ENV}}\""],
                capture_output=True, text=True, timeout=10,
            )
            self.assertIn("default_env", res.stdout)

            res2 = subprocess.run(
                ["bash", "-c",
                 f"VAR_ENV=overridden; source {script_path} >/dev/null || true; echo \"http://example.com/${{VAR_ENV}}\""],
                capture_output=True, text=True, timeout=10,
            )
            self.assertIn("overridden", res2.stdout)

    # ── CLI variable injection (--<name> <value> -> VAR_<NAME>) ─────

    def test_cli_var_injection_space_form_satisfies_required(self):
        """--<name> <value> sets VAR_<NAME> and satisfies the required check."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo?h=${{var.hogehoge}}"
        """)
        script, script_path = self._generate_var_script(toml)
        res = subprocess.run(
            ["bash", str(script_path), "--hogehoge", "hogeValue"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("hogeValue", res.stdout)

    def test_cli_var_injection_equals_form(self):
        """--<name>=<value> is accepted in addition to the space form."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo?h=${{var.hogehoge}}"
        """)
        script, script_path = self._generate_var_script(toml)
        res = subprocess.run(
            ["bash", str(script_path), "--hogehoge=hogeValue"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("hogeValue", res.stdout)

    def test_cli_var_injection_hyphenated_name_maps_to_underscore(self):
        """--foo-bar <value> maps to VAR_FOO_BAR (matching ${var.foo-bar})."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo?f=${{var.foo-bar}}"
        """)
        script, script_path = self._generate_var_script(toml)
        res = subprocess.run(
            ["bash", str(script_path), "--foo-bar", "bazvalue"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("bazvalue", res.stdout)

    def test_cli_var_injection_mixed_case_arg_uppercased(self):
        """Mixed-case option name is uppercased: --HoGe -> VAR_HOGE."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo?h=${{var.hoge}}"
        """)
        script, script_path = self._generate_var_script(toml)
        res = subprocess.run(
            ["bash", str(script_path), "--HoGe", "val"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("val", res.stdout)

    def test_cli_var_injection_overrides_default_vars(self):
        """CLI args take precedence over embedded DEFAULT_VARS."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo?env=${{var.env}}"
        """)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf, default_vars={"env": "default_env"})
            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path), "--env", "cli_env"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
            self.assertIn("cli_env", res.stdout)
            self.assertNotIn("default_env", res.stdout)

    def test_cli_var_injection_overrides_env_var(self):
        """CLI args take precedence over a variable exported before running."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo?env=${{var.env}}"
        """)
        script, script_path = self._generate_var_script(toml)
        res = subprocess.run(
            ["bash", "-c", f"VAR_ENV=fromenv bash {script_path} --env cli_env"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("cli_env", res.stdout)
        self.assertNotIn("fromenv", res.stdout)

    def test_cli_var_injection_missing_value_fails(self):
        """--<name> with no following value fails with a clear error."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo?h=${{var.hogehoge}}"
        """)
        script, script_path = self._generate_var_script(toml)
        res = subprocess.run(
            ["bash", str(script_path), "--hogehoge"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("requires a value argument", res.stderr)

    def test_cli_var_injection_invalid_name_fails(self):
        """An option name with non-[A-Za-z0-9_-] chars is rejected."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo"
        """)
        script, script_path = self._generate_var_script(toml)
        res = subprocess.run(
            ["bash", str(script_path), "--bad.name=v"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("invalid variable name", res.stderr)

    def test_cli_var_injection_help_works_with_required_vars(self):
        """--help is honoured even when the workflow has required variables."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo?h=${{var.hogehoge}}"
        """)
        script, script_path = self._generate_var_script(toml)
        res = subprocess.run(
            ["bash", str(script_path), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("variable injection", res.stdout)
