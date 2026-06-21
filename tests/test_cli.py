"""CLI smoke tests for the httpflow package entrypoint."""

import subprocess
import sys
import unittest


class TestCLISmoke(unittest.TestCase):
    def _run(self, args):
        return subprocess.run(
            [sys.executable, "-m", "httpflow", *args],
            capture_output=True, text=True, timeout=10,
        )

    def test_main_help(self):
        res = self._run(["--help"])
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("run", res.stdout)
        self.assertIn("generate", res.stdout)

    def test_run_help(self):
        res = self._run(["run", "--help"])
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("--file", res.stdout)
        self.assertIn("--step", res.stdout)
        self.assertIn("--quiet", res.stdout)
        self.assertIn("--pretty-json", res.stdout)
        self.assertIn("--no-mask", res.stdout)
        self.assertIn("--blank-line", res.stdout)

    def test_generate_help(self):
        res = self._run(["generate", "--help"])
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("--output", res.stdout)
        self.assertIn("--shebang", res.stdout)
        self.assertIn("--format", res.stdout)
        # bash is the default format
        self.assertIn("default: bash", res.stdout)

    def test_generate_default_format_is_bash(self):
        """`generate` without --format should emit a bash script (curl-based)."""
        import os
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".toml", delete=False, mode="w") as f:
            f.write(
                '[[requests]]\n'
                'name = "ping"\n'
                'method = "GET"\n'
                'url = "http://example.com/ping"\n'
            )
            toml_path = f.name
        out_path = tempfile.mktemp(suffix=".sh")
        try:
            res = self._run(["generate", "-f", toml_path, "-o", out_path])
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            with open(out_path, encoding="utf-8") as g:
                content = g.read()
            self.assertIn("curl", content)
            self.assertIn("step_ping", content)
            self.assertNotIn("import urllib", content)
        finally:
            os.unlink(toml_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_generate_format_python_opt_in(self):
        """`generate --format python` should emit a Python script."""
        import os
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".toml", delete=False, mode="w") as f:
            f.write(
                '[[requests]]\n'
                'name = "ping"\n'
                'method = "GET"\n'
                'url = "http://example.com/ping"\n'
            )
            toml_path = f.name
        out_path = tempfile.mktemp(suffix=".py")
        try:
            res = self._run(["generate", "-f", toml_path, "--format", "python", "-o", out_path])
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            with open(out_path, encoding="utf-8") as g:
                content = g.read()
            self.assertIn("import urllib", content)
            self.assertIn("def step_ping", content)
        finally:
            os.unlink(toml_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_version(self):
        res = self._run(["--version"])
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("httpflow", res.stdout)

    def test_implicit_run_compat(self):
        # Backward-compat: `-f <file>` without subcommand should be treated as `run -f <file>`
        # We use a minimal TOML that references a non-existent local server; the command
        # should fail with a connection error (not an argparse error or missing subcommand).
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".toml", delete=False, mode="w") as f:
            f.write('[[requests]]\nname = "ping"\nmethod = "GET"\nurl = "http://127.0.0.1:1/"\n')
            path = f.name
        import os
        try:
            res = self._run(["-f", path])
            # It should fail because the server is not reachable, NOT because of bad args.
            self.assertEqual(res.returncode, 1, msg=res.stdout + res.stderr)
            self.assertNotIn("invalid choice", res.stderr.lower())
            self.assertNotIn("unrecognized arguments", res.stderr.lower())
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
