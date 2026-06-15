"""Tests for the until (polling) feature."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from httpflow import config as cfg_mod
from httpflow import generator, runner
from httpflow.runtime.until import eval_until
from tests._helpers import PollHandler, ServerMixin, write_toml


# ------------------------------------------------------------------
# 1. Condition evaluator unit tests
# ------------------------------------------------------------------
class TestEvaluateCondition(unittest.TestCase):
    def _store(self, **vars_):
        return {"vars": vars_}

    def test_eq_true(self):
        self.assertTrue(eval_until("${status} == Active", self._store(status="Active")))

    def test_eq_false(self):
        self.assertFalse(eval_until("${status} == Active", self._store(status="Pending")))

    def test_eq_with_spaces_trimmed(self):
        self.assertTrue(eval_until("  ${status}   ==   Active  ", self._store(status="Active")))

    def test_ne(self):
        self.assertTrue(eval_until("${status} != Active", self._store(status="Pending")))
        self.assertFalse(eval_until("${status} != Pending", self._store(status="Pending")))

    def test_regex_match(self):
        store = self._store(msg="operation succeeded")
        self.assertTrue(eval_until("${msg} ~ /succe.+/", store))
        self.assertFalse(eval_until("${msg} ~ /^fail/", store))

    def test_regex_case_insensitive_flag(self):
        self.assertTrue(eval_until("${msg} ~ /ok/i", self._store(msg="OK")))

    def test_regex_invalid_rhs(self):
        with self.assertRaises(ValueError):
            eval_until("${msg} ~ no-slashes", self._store(msg="x"))

    def test_in_list(self):
        store = self._store(code="201")
        self.assertTrue(eval_until("${code} in [200, 201, 204]", store))
        self.assertFalse(eval_until("${code} in [400, 500]", store))

    def test_in_invalid_rhs(self):
        with self.assertRaises(ValueError):
            eval_until("${code} in 200", self._store(code="200"))

    def test_no_operator(self):
        with self.assertRaises(ValueError):
            eval_until("just a string", self._store(s={"x": "y"}))


# ------------------------------------------------------------------
# 2. Config parsing
# ------------------------------------------------------------------
class TestConfigUntil(unittest.TestCase):
    def _load(self, body: bytes):
        path = write_toml(body)
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
        u = wf.steps[0].until
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


# ------------------------------------------------------------------
# 3. Workflow integration: polling
# ------------------------------------------------------------------
class TestWorkflowPolling(ServerMixin, unittest.TestCase):
    _handler_cls = PollHandler

    def setUp(self):
        PollHandler.pending_remaining = 0

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
        PollHandler.pending_remaining = 2
        path = write_toml(self._make_poll_toml(max_attempts=5))
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        store = runner.run(cfg, out=buf)
        self.assertEqual(store["vars"]["status"], "Active")
        output = buf.getvalue()
        self.assertIn("until satisfied on attempt 3", output)
        self.assertIn("until not satisfied (attempt 1/5)", output)
        self.assertIn("until not satisfied (attempt 2/5)", output)

    def test_polling_succeeds_first_attempt(self):
        PollHandler.pending_remaining = 0
        path = write_toml(self._make_poll_toml(max_attempts=3))
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        store = runner.run(cfg, out=buf)
        self.assertEqual(store["vars"]["status"], "Active")
        self.assertIn("until satisfied on attempt 1", buf.getvalue())

    def test_polling_max_attempts_exceeded(self):
        PollHandler.pending_remaining = 10
        path = write_toml(self._make_poll_toml(max_attempts=3))
        self.addCleanup(os.unlink, path)
        cfg = cfg_mod.load(path)
        buf = io.StringIO()
        with self.assertRaises(RuntimeError) as ctx:
            runner.run(cfg, out=buf)
        self.assertIn("not satisfied after 3 attempts", str(ctx.exception))


# ------------------------------------------------------------------
# 4. Generated script polling
# ------------------------------------------------------------------
class TestGeneratorPolling(ServerMixin, unittest.TestCase):
    _handler_cls = PollHandler

    def setUp(self):
        PollHandler.pending_remaining = 0

    def test_generated_script_polls(self):
        PollHandler.pending_remaining = 2
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
        PollHandler.pending_remaining = 2
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

            PollHandler.pending_remaining = 2
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


# ------------------------------------------------------------------
# 5. eval_until parity between package and generated script
# ------------------------------------------------------------------
class TestUntilEquivalence(unittest.TestCase):
    def test_equivalence_against_inline_runner(self):
        path = write_toml(textwrap.dedent("""\
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
        self.addCleanup(os.unlink, path)
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


if __name__ == "__main__":
    unittest.main()
