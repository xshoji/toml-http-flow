"""Tests for httpflow.step descriptions (HTTP and SLEEP)."""

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from httpflow import config as cfg_mod
from httpflow import generator, runner


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class _ServerMixin:
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


class TestConfigDescription(unittest.TestCase):
    def _load(self, body: bytes):
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        self.addCleanup(os.unlink, path)
        return cfg_mod.load(path)

    def test_description_parsed(self):
        wf = self._load(b"""
[[requests]]
name        = "ping"
description = "Verify that the API is reachable"
request = "GET http://example.com"
""")
        self.assertEqual(
            wf.steps[0].description,
            "Verify that the API is reachable",
        )

    def test_description_default_is_none(self):
        wf = self._load(b"""
[[requests]]
name   = "ping"
request = "GET http://example.com"
""")
        self.assertIsNone(wf.steps[0].description)

    def test_description_multiline(self):
        wf = self._load(b"""
[[requests]]
name        = "ping"
description = '''
Step 1: ensure the upstream is alive.
Step 2: warm any caches.
'''
request = "GET http://example.com"
""")
        self.assertIn("Step 1", wf.steps[0].description)
        self.assertIn("Step 2", wf.steps[0].description)

    def test_description_must_be_string(self):
        with self.assertRaises(ValueError):
            self._load(b"""
[[requests]]
name        = "ping"
description = 123
request = "GET http://example.com"
""")

    def test_description_on_sleep_step(self):
        wf = self._load(b"""
[[requests]]
name        = "wait"
description = "Wait for downstream to settle"
request = "SLEEP 0.01"
""")
        self.assertEqual(
            wf.steps[0].description,
            "Wait for downstream to settle",
        )


class TestWorkflowDescription(_ServerMixin, unittest.TestCase):
    def test_description_printed_for_http(self):
        path = tempfile.mkstemp(suffix=".toml")[1]
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(f"""\
                [[requests]]
                name = "ping"
                request = "GET http://127.0.0.1:{self.port}/"
                description = "Verify upstream reachability"
            """))
        try:
            cfg = cfg_mod.load(path)
            buf = io.StringIO()
            runner.run(cfg, out=buf)
            out = buf.getvalue()
            self.assertIn("# Verify upstream reachability", out)
            # Description must appear after the ==> line and before the > request line.
            arrow_idx = out.index("==> ")
            desc_idx = out.index("# Verify")
            req_idx = out.index("> GET")
            self.assertLess(arrow_idx, desc_idx)
            self.assertLess(desc_idx, req_idx)
        finally:
            os.unlink(path)

    def test_description_printed_in_quiet_mode(self):
        path = tempfile.mkstemp(suffix=".toml")[1]
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(f"""\
                [[requests]]
                name = "ping"
                request = "GET http://127.0.0.1:{self.port}/"
                description = "Should still show in quiet mode"
            """))
        try:
            cfg = cfg_mod.load(path)
            buf = io.StringIO()
            runner.run(cfg, quiet=True, out=buf)
            out = buf.getvalue()
            self.assertIn("# Should still show in quiet mode", out)
            # quiet still suppresses the detailed `> GET` lines.
            self.assertNotIn("> GET ", out)
        finally:
            os.unlink(path)

    def test_description_multiline_printed_one_line_each(self):
        path = tempfile.mkstemp(suffix=".toml")[1]
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(f"""\
                [[requests]]
                name = "ping"
                request = "GET http://127.0.0.1:{self.port}/"
                description = '''
                line A
                line B
                '''
            """))
        try:
            cfg = cfg_mod.load(path)
            buf = io.StringIO()
            runner.run(cfg, out=buf)
            out = buf.getvalue()
            self.assertIn("# line A", out)
            self.assertIn("# line B", out)
        finally:
            os.unlink(path)

    def test_description_on_sleep_step(self):
        path = tempfile.mkstemp(suffix=".toml")[1]
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent("""\
                [[requests]]
                name = "wait"
                request = "SLEEP 0.01"
                description = "Wait for downstream"
            """))
        try:
            cfg = cfg_mod.load(path)
            buf = io.StringIO()
            start = time.monotonic()
            runner.run(cfg, out=buf)
            self.assertGreaterEqual(time.monotonic() - start, 0.01)
            self.assertIn("# Wait for downstream", buf.getvalue())
        finally:
            os.unlink(path)

    def test_no_description_no_comment_line(self):
        path = tempfile.mkstemp(suffix=".toml")[1]
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(f"""\
                [[requests]]
                name = "ping"
                request = "GET http://127.0.0.1:{self.port}/"
            """))
        try:
            cfg = cfg_mod.load(path)
            buf = io.StringIO()
            runner.run(cfg, out=buf)
            # Make sure we don't print a stray "    # " line when description is unset.
            for line in buf.getvalue().splitlines():
                self.assertFalse(line.startswith("    # "), msg=line)
        finally:
            os.unlink(path)


class TestGeneratorDescription(_ServerMixin, unittest.TestCase):
    def test_generated_script_prints_description(self):
        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name        = "ping"
            description = "Smoke check: API is up"
            request = "GET {base}/"
            [[requests]]
            name        = "wait"
            description = "Pause briefly"
            request = "SLEEP 0.01"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)
            compile(script, "<generated>", "exec")
            script_path = tmp_path / "workflow.py"
            script_path.write_text(script, encoding="utf-8")

            res = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("# Smoke check: API is up", res.stdout)
            self.assertIn("# Pause briefly", res.stdout)


if __name__ == "__main__":
    unittest.main()
