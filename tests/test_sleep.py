"""Tests for SLEEP step execution and config parsing."""

import io
import os
import tempfile
import time
import unittest

from httpflow import config as cfg_mod
from httpflow import runner
from httpflow.model import SleepStep


class TestSleepStep(unittest.TestCase):
    def test_sleep_step(self):
        """SLEEP step pauses execution for the given seconds."""
        path = tempfile.mkstemp(suffix=".toml")[1]
        with open(path, "w", encoding="utf-8") as f:
            f.write("""\
[[requests]]
name = "wait1"
method = "SLEEP"
url = "0.1"
""")
        try:
            cfg = cfg_mod.load(path)
            buf = io.StringIO()
            start = time.monotonic()
            store = runner.run(cfg, out=buf)
            elapsed = time.monotonic() - start

            self.assertGreaterEqual(elapsed, 0.1)
            output = buf.getvalue()
            self.assertIn("[wait1] SLEEP 0.1", output)
            self.assertIn("[wait1] done", output)
            self.assertIn("> sleep 0.1 seconds", output)
            self.assertNotIn("steps", store)
        finally:
            os.unlink(path)

    def test_sleep_step_with_template(self):
        """SLEEP url can use template variables."""
        path = tempfile.mkstemp(suffix=".toml")[1]
        with open(path, "w", encoding="utf-8") as f:
            f.write("""\
[[requests]]
name = "wait"
method = "SLEEP"
url = "${var.delay}"
""")
        try:
            cfg = cfg_mod.load(path)
            start = time.monotonic()
            store = runner.run(cfg, {"delay": "0.02"}, out=io.StringIO())
            elapsed = time.monotonic() - start

            self.assertGreaterEqual(elapsed, 0.02)
            self.assertNotIn("steps", store)
        finally:
            os.unlink(path)

    def test_sleep_step_quiet(self):
        """SLEEP step in quiet mode prints no detail."""
        path = tempfile.mkstemp(suffix=".toml")[1]
        with open(path, "w", encoding="utf-8") as f:
            f.write("""\
[[requests]]
name = "qwait"
method = "SLEEP"
url = "0.01"
""")
        try:
            cfg = cfg_mod.load(path)
            buf = io.StringIO()
            runner.run(cfg, quiet=True, out=buf)
            output = buf.getvalue()
            self.assertIn("[qwait] SLEEP", output)
            self.assertNotIn("sleep 0.01 seconds", output)
        finally:
            os.unlink(path)


class TestSpecialMethodsSet(unittest.TestCase):
    def test_special_methods_contains_sleep(self):
        from httpflow.config import SPECIAL_METHODS

        self.assertIn("SLEEP", SPECIAL_METHODS)


class TestSleepTOMLLoad(unittest.TestCase):
    def _write(self, content: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        self.addCleanup(os.unlink, path)
        return path

    def test_toml_sleep_accepts_template_var(self):
        toml = b"""
[[requests]]
name = "wait"
method = "SLEEP"
url = "${var.delay}"
"""
        path = self._write(toml)
        wf = cfg_mod.load(path)
        self.assertEqual(len(wf.steps), 1)
        step = wf.steps[0]
        self.assertIsInstance(step, SleepStep)
        assert isinstance(step, SleepStep)
        self.assertEqual(step.seconds, "${var.delay}")

    def test_toml_sleep_accepts_template_repeat(self):
        toml = b"""
[[requests]]
name = "wait"
method = "SLEEP"
url = "${repeat.delay}"
"""
        path = self._write(toml)
        wf = cfg_mod.load(path)
        self.assertEqual(len(wf.steps), 1)
        step = wf.steps[0]
        self.assertIsInstance(step, SleepStep)
        assert isinstance(step, SleepStep)
        self.assertEqual(step.seconds, "${repeat.delay}")


if __name__ == "__main__":
    unittest.main()
