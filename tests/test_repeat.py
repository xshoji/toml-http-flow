"""Tests for the ${repeat.<name>} feature: detection, iteration, validation."""

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

from httpflow import generator
from httpflow.config import RequestConfig, WorkflowConfig
from httpflow.template import find_repeat_names
from httpflow.workflow import (
    build_repeat_iterations,
    collect_repeat_names,
    run,
)


class _EchoHandler(BaseHTTPRequestHandler):
    """Echoes the request path back as JSON."""

    def do_GET(self):
        body = json.dumps({"path": self.path}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class TestFindRepeatNames(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(
            find_repeat_names("/x?id=${repeat.id}&name=${repeat.name}"),
            {"id", "name"},
        )

    def test_none(self):
        self.assertEqual(find_repeat_names(None), set())

    def test_no_match(self):
        self.assertEqual(find_repeat_names("plain ${var.x}"), set())


class TestCollectRepeatNames(unittest.TestCase):
    def test_collects_from_all_fields(self):
        cfg = WorkflowConfig(
            requests=[
                RequestConfig(
                    name="r1",
                    method="POST",
                    url="http://x/${repeat.a}",
                    headers={"X-Tag": "${repeat.b}"},
                    body='{"v":"${repeat.c}"}',
                ),
            ]
        )
        self.assertEqual(collect_repeat_names(cfg), {"a", "b", "c"})


class TestBuildRepeatIterations(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(build_repeat_iterations(None, set()), [{}])

    def test_missing_required(self):
        with self.assertRaisesRegex(ValueError, "missing for"):
            build_repeat_iterations({}, {"a"})

    def test_length_mismatch(self):
        with self.assertRaisesRegex(ValueError, "value counts must match"):
            build_repeat_iterations({"a": ["1", "2"], "b": ["x"]}, {"a", "b"})

    def test_expand(self):
        out = build_repeat_iterations(
            {"a": ["1", "2", "3"], "b": ["x", "y", "z"]},
            {"a", "b"},
        )
        self.assertEqual(out, [
            {"a": "1", "b": "x"},
            {"a": "2", "b": "y"},
            {"a": "3", "b": "z"},
        ])


class TestRunWithRepeat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _EchoHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_workflow_iterates_per_index(self):
        base = f"http://127.0.0.1:{self.port}"
        cfg = WorkflowConfig(
            requests=[
                RequestConfig(
                    name="echo",
                    method="GET",
                    url=f"{base}/echo?id=${{repeat.id}}&label=${{repeat.label}}",
                    capture={"got": "path"},
                ),
            ]
        )
        buf = io.StringIO()
        run(
            cfg,
            repeat_vars={"id": ["1", "2", "3"], "label": ["a", "b", "c"]},
            out=buf,
        )
        output = buf.getvalue()
        self.assertIn("/echo?id=1&label=a", output)
        self.assertIn("/echo?id=2&label=b", output)
        self.assertIn("/echo?id=3&label=c", output)
        self.assertIn("=== repeat iteration 1/3", output)
        self.assertIn("=== repeat iteration 3/3", output)

    def test_missing_repeat_vars_raises(self):
        cfg = WorkflowConfig(
            requests=[
                RequestConfig(
                    name="echo",
                    method="GET",
                    url=f"http://127.0.0.1:{self.port}/echo?id=${{repeat.id}}",
                ),
            ]
        )
        with self.assertRaisesRegex(ValueError, "missing for"):
            run(cfg, out=io.StringIO())

    def test_mismatched_lengths_raises(self):
        cfg = WorkflowConfig(
            requests=[
                RequestConfig(
                    name="echo",
                    method="GET",
                    url=f"http://127.0.0.1:{self.port}/echo?a=${{repeat.a}}&b=${{repeat.b}}",
                ),
            ]
        )
        with self.assertRaisesRegex(ValueError, "value counts must match"):
            run(
                cfg,
                repeat_vars={"a": ["1", "2"], "b": ["x"]},
                out=io.StringIO(),
            )


class TestGeneratedScriptWithRepeat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _EchoHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_generated_script_repeats(self):
        from httpflow import config as cfg_mod

        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "echo"
            method = "GET"
            url = "{base}/echo?id=${{repeat.id}}&label=${{repeat.label}}"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "wf.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)
            compile(script, "<generated>", "exec")

            script_path = tmp_path / "wf.py"
            script_path.write_text(script, encoding="utf-8")

            # Missing --repeat-vars must fail.
            res = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("missing for", res.stderr)

            # Mismatched counts must fail.
            res = subprocess.run(
                [sys.executable, str(script_path),
                 "--repeat-vars", "id=1,2",
                 "--repeat-vars", "label=a"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("value counts must match", res.stderr)

            # Success path: iterates 3 times.
            res = subprocess.run(
                [sys.executable, str(script_path),
                 "--repeat-vars", "id=1,2,3",
                 "--repeat-vars", "label=a,b,c"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("/echo?id=1&label=a", res.stdout)
            self.assertIn("/echo?id=2&label=b", res.stdout)
            self.assertIn("/echo?id=3&label=c", res.stdout)
            self.assertIn("=== repeat iteration 1/3", res.stdout)
            self.assertIn("=== repeat iteration 3/3", res.stdout)

    def test_generated_script_without_repeat_still_works(self):
        """Workflows that don't use ${repeat.*} keep their old single-run shape."""
        from httpflow import config as cfg_mod

        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/ping"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "wf.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(wf)
            compile(script, "<generated>", "exec")

            script_path = tmp_path / "wf.py"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("[ping]", res.stdout)
            # No iteration banner expected for the no-repeat path.
            self.assertNotIn("=== repeat iteration", res.stdout)

    def test_embed_repeat_vars(self):
        """--repeat-vars embeds defaults so the script runs without args."""
        from httpflow import config as cfg_mod

        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "echo"
            method = "GET"
            url = "{base}/echo?id=${{repeat.id}}&label=${{repeat.label}}"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "wf.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(
                wf,
                default_repeat_vars={"id": ["1", "2", "3"], "label": ["a", "b", "c"]},
            )
            compile(script, "<generated>", "exec")

            script_path = tmp_path / "wf.py"
            script_path.write_text(script, encoding="utf-8")

            # No --repeat-vars needed because defaults are embedded
            res = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("/echo?id=1&label=a", res.stdout)
            self.assertIn("/echo?id=2&label=b", res.stdout)
            self.assertIn("/echo?id=3&label=c", res.stdout)

            # Runtime --repeat-vars overrides embedded defaults
            res2 = subprocess.run(
                [sys.executable, str(script_path),
                 "--repeat-vars", "id=x,y",
                 "--repeat-vars", "label=A,B"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res2.returncode, 0, msg=res2.stderr)
            self.assertIn("/echo?id=x&label=A", res2.stdout)
            self.assertIn("/echo?id=y&label=B", res2.stdout)

    def test_partial_embed_repeat_vars(self):
        """Embedding one repeat var and passing the other at runtime works."""
        from httpflow import config as cfg_mod

        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "echo"
            method = "GET"
            url = "{base}/echo?id=${{repeat.id}}&label=${{repeat.label}}"
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "wf.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))
            script = generator.generate(
                wf,
                default_repeat_vars={"id": ["1", "2"]},
            )
            compile(script, "<generated>", "exec")

            script_path = tmp_path / "wf.py"
            script_path.write_text(script, encoding="utf-8")

            # label is required but id has embedded defaults
            res = subprocess.run(
                [sys.executable, str(script_path),
                 "--repeat-vars", "label=a,b"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("/echo?id=1&label=a", res.stdout)
            self.assertIn("/echo?id=2&label=b", res.stdout)


if __name__ == "__main__":
    unittest.main()
