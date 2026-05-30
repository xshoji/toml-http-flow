"""Tests for the until (polling) feature."""

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from httpflow import config as cfg_mod
from httpflow import generator, runner
from httpflow.runtime.until import eval_until


# ─── Stateful mock server used by polling tests ─────────────────────────
class _PollHandler(BaseHTTPRequestHandler):
    """Returns status=Pending for the first N GETs, then status=Active."""

    # Class-level state; reset by tests as needed.
    pending_remaining = 0
    job_id = "job-1"

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
        self._send(200, {"data": {"id": _PollHandler.job_id}})

    def do_GET(self):
        if _PollHandler.pending_remaining > 0:
            _PollHandler.pending_remaining -= 1
            self._send(200, {"data": {"status": "Pending"}})
        else:
            self._send(200, {"data": {"status": "Active"}})

    def log_message(self, format, *args):
        return


class _PollServerMixin:
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _PollHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()


# ─── 1. Condition evaluator unit tests ─────────────────────────────────
class TestEvaluateCondition(unittest.TestCase):
    def _store(self, **vars_):
        return {"vars": vars_}

    def test_eq_true(self):
        store = self._store(status="Active")
        self.assertTrue(eval_until("${status} == Active", store))

    def test_eq_false(self):
        store = self._store(status="Pending")
        self.assertFalse(eval_until("${status} == Active", store))

    def test_eq_with_spaces_trimmed(self):
        store = self._store(status="Active")
        self.assertTrue(eval_until("  ${status}   ==   Active  ", store))

    def test_ne(self):
        store = self._store(status="Pending")
        self.assertTrue(eval_until("${status} != Active", store))
        self.assertFalse(eval_until("${status} != Pending", store))

    def test_regex_match(self):
        store = self._store(msg="operation succeeded")
        self.assertTrue(eval_until("${msg} ~ /succe.+/", store))
        self.assertFalse(eval_until("${msg} ~ /^fail/", store))

    def test_regex_case_insensitive_flag(self):
        store = self._store(msg="OK")
        self.assertTrue(eval_until("${msg} ~ /ok/i", store))

    def test_regex_invalid_rhs(self):
        store = self._store(msg="x")
        with self.assertRaises(ValueError):
            eval_until("${msg} ~ no-slashes", store)

    def test_in_list(self):
        store = self._store(code="201")
        self.assertTrue(eval_until("${code} in [200, 201, 204]", store))
        self.assertFalse(eval_until("${code} in [400, 500]", store))

    def test_in_invalid_rhs(self):
        store = self._store(code="200")
        with self.assertRaises(ValueError):
            eval_until("${code} in 200", store)

    def test_no_operator(self):
        store = self._store(s={"x": "y"})
        with self.assertRaises(ValueError):
            eval_until("just a string", store)


# ─── 2. Config parsing tests ───────────────────────────────────────────
class TestConfigUntil(unittest.TestCase):
    def _load(self, body: bytes):
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        self.addCleanup(os.unlink, path)
        return cfg_mod.load(path)

    def test_until_parsed(self):
        wf = self._load(b"""
[[requests]]
name = "poll"
method = "GET"
url = "http://example.com"
until = [
    "condition    = ${status} == Active",
    "interval     = 2.5",
    "max_attempts = 7",
]
""")
        step = wf.steps[0]
        u = step.until
        self.assertIsNotNone(u)
        self.assertEqual(u.condition, "${status} == Active")
        self.assertEqual(u.interval, 2.5)
        self.assertEqual(u.max_attempts, 7)

    def test_until_defaults(self):
        wf = self._load(b"""
[[requests]]
name = "poll"
method = "GET"
url = "http://example.com"
until = ["condition = ${s} == OK"]
""")
        u = wf.steps[0].until
        self.assertEqual(u.interval, 1.0)
        self.assertEqual(u.max_attempts, 10)

    def test_until_requires_condition(self):
        with self.assertRaises(ValueError) as ctx:
            self._load(b"""
[[requests]]
name = "poll"
method = "GET"
url = "http://example.com"
until = ["interval = 2.0"]
""")
        self.assertIn("condition", str(ctx.exception))

    def test_until_rejects_unknown_key(self):
        with self.assertRaises(ValueError) as ctx:
            self._load(b"""
[[requests]]
name = "poll"
method = "GET"
url = "http://example.com"
until = [
    "condition = ${s} == OK",
    "bogus     = 1",
]
""")
        self.assertIn("bogus", str(ctx.exception))

    def test_until_rejects_negative_interval(self):
        with self.assertRaises(ValueError):
            self._load(b"""
[[requests]]
name = "poll"
method = "GET"
url = "http://example.com"
until = [
    "condition = a == b",
    "interval  = -1",
]
""")

    def test_until_rejects_zero_max_attempts(self):
        with self.assertRaises(ValueError):
            self._load(b"""
[[requests]]
name = "poll"
method = "GET"
url = "http://example.com"
until = [
    "condition    = a == b",
    "max_attempts = 0",
]
""")

    def test_sleep_rejects_until(self):
        with self.assertRaises(ValueError):
            self._load(b"""
[[requests]]
name = "bad"
method = "SLEEP"
url = "1"
until = ["condition = a == b"]
""")


# ─── 3. Workflow integration: polling succeeds ─────────────────────────
class TestWorkflowPolling(_PollServerMixin, unittest.TestCase):
    def setUp(self):
        _PollHandler.pending_remaining = 0

    def _make_poll_toml(self, max_attempts: int = 5) -> str:
        base = f"http://127.0.0.1:{self.port}"
        return textwrap.dedent(f"""\
            [[requests]]
            name = "createJob"
            method = "POST"
            url = "{base}/jobs"
            headers = ["Content-Type: application/json"]
            body = '{{"name":"x"}}'
            capture = ["id = data.id"]

            [[requests]]
            name = "pollStatus"
            method = "GET"
            url = "{base}/jobs/${{id}}"
            capture = ["status = data.status"]
            until = [
                "condition = ${{status}} == Active",
                "interval  = 0.01",
                "max_attempts = {max_attempts}",
            ]
        """)

    def test_polling_succeeds_after_retries(self):
        _PollHandler.pending_remaining = 2
        path = tempfile.mkstemp(suffix=".toml")[1]
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._make_poll_toml(max_attempts=5))
        try:
            cfg = cfg_mod.load(path)
            buf = io.StringIO()
            store = runner.run(cfg, out=buf)
            self.assertEqual(store["vars"]["status"], "Active")
            output = buf.getvalue()
            # First two attempts are Pending, third is Active.
            self.assertIn("until satisfied on attempt 3", output)
            self.assertIn("until not satisfied (attempt 1/5)", output)
            self.assertIn("until not satisfied (attempt 2/5)", output)
        finally:
            os.unlink(path)

    def test_polling_succeeds_first_attempt(self):
        _PollHandler.pending_remaining = 0
        path = tempfile.mkstemp(suffix=".toml")[1]
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._make_poll_toml(max_attempts=3))
        try:
            cfg = cfg_mod.load(path)
            buf = io.StringIO()
            store = runner.run(cfg, out=buf)
            self.assertEqual(store["vars"]["status"], "Active")
            self.assertIn("until satisfied on attempt 1", buf.getvalue())
        finally:
            os.unlink(path)

    def test_polling_max_attempts_exceeded(self):
        _PollHandler.pending_remaining = 10
        path = tempfile.mkstemp(suffix=".toml")[1]
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._make_poll_toml(max_attempts=3))
        try:
            cfg = cfg_mod.load(path)
            buf = io.StringIO()
            with self.assertRaises(RuntimeError) as ctx:
                runner.run(cfg, out=buf)
            self.assertIn("not satisfied after 3 attempts", str(ctx.exception))
        finally:
            os.unlink(path)


# ─── 4. Generated script also polls correctly ──────────────────────────
class TestGeneratorPolling(_PollServerMixin, unittest.TestCase):
    def setUp(self):
        _PollHandler.pending_remaining = 0

    def test_generated_script_polls(self):
        _PollHandler.pending_remaining = 2
        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "createJob"
            method = "POST"
            url = "{base}/jobs"
            headers = ["Content-Type: application/json"]
            body = '''{{"name":"x"}}'''
            capture = ["id = data.id"]

            [[requests]]
            name = "pollStatus"
            method = "GET"
            url = "{base}/jobs/${{id}}"
            capture = ["status = data.status"]
            until = [
                "condition    = ${{status}} == Active",
                "interval     = 0.01",
                "max_attempts = 5",
            ]
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
            self.assertIn("until satisfied on attempt 3", res.stdout)

    def test_generated_script_parity_with_runner_for_until(self):
        _PollHandler.pending_remaining = 2
        base = f"http://127.0.0.1:{self.port}"
        toml_text = textwrap.dedent(f"""
            [[requests]]
            name = "createJob"
            method = "POST"
            url = "{base}/jobs"
            headers = ["Content-Type: application/json"]
            body = '''{{"name":"x"}}'''
            capture = ["id = data.id"]

            [[requests]]
            name = "pollStatus"
            method = "GET"
            url = "{base}/jobs/${{id}}"
            capture = ["status = data.status"]
            until = [
                "condition    = ${{status}} == Active",
                "interval     = 0.01",
                "max_attempts = 5",
            ]
        """).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_bytes(toml_text)
            wf = cfg_mod.load(str(toml_path))

            buf = io.StringIO()
            store = runner.run(wf, out=buf)

            _PollHandler.pending_remaining = 2
            script_path = tmp_path / "workflow.py"
            script_path.write_text(generator.generate(wf), encoding="utf-8")
            res = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True, text=True, timeout=10,
            )

            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("until satisfied on attempt 3", buf.getvalue())
            self.assertIn("until satisfied on attempt 3", res.stdout)
            self.assertEqual(store["vars"]["status"], "Active")


# ─── 5. Logical equivalence: package vs generated runner ──────────────
class TestUntilEquivalence(unittest.TestCase):
    """Generated runner's eval_until must agree with httpflow.runtime.until.eval_until."""

    def test_equivalence_against_inline_runner(self):
        # Generate a script with an until step to force inclusion of eval_until.
        path = tempfile.mkstemp(suffix=".toml")[1]
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent("""\
                [[requests]]
                name = "dummy"
                method = "GET"
                url = "http://example.com"
                until = [
                    "condition = ${x} == 1",
                    "interval = 1.0",
                    "max_attempts = 1",
                ]
            """))
        try:
            wf = cfg_mod.load(path)
            script = generator.generate(wf)
            ns: dict = {}
            exec(compile(script, "<generated>", "exec"), ns)
            gen_eval = ns["eval_until"]

            store = {"vars": {"status": "Active", "code": "201"}}
            cases = [
                "${status} == Active",
                "${status} == Pending",
                "${status} != Pending",
                "${code} in [200, 201, 204]",
                "${code} in [400, 500]",
                "${status} ~ /^Act/",
                "${status} ~ /pending/i",
            ]
            for cond in cases:
                with self.subTest(cond=cond):
                    self.assertEqual(eval_until(cond, store), gen_eval(cond, store))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
