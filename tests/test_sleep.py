import io
import os
import tempfile
import unittest

from httpflow.config import SPECIAL_METHODS, RequestConfig, WorkflowConfig, load as toml_load
from httpflow.model import SleepStep
from httpflow.workflow import run


class TestSleepStep(unittest.TestCase):
    def test_sleep_step(self):
        """SLEEP step pauses execution for the given seconds."""
        import time

        cfg = WorkflowConfig(
            requests=[
                RequestConfig(name="wait1", method="SLEEP", url="0.1"),
            ]
        )
        buf = io.StringIO()
        start = time.monotonic()
        store = run(cfg, out=buf)
        elapsed = time.monotonic() - start

        self.assertGreaterEqual(elapsed, 0.1)
        output = buf.getvalue()
        self.assertIn("[wait1] SLEEP 0.1", output)
        self.assertIn("[wait1] done", output)
        self.assertIn("> sleep 0.1 seconds", output)
        self.assertNotIn("steps", store)

    def test_sleep_step_with_template(self):
        """SLEEP url can use template variables."""
        import time

        cfg = WorkflowConfig(
            requests=[
                RequestConfig(name="wait", method="SLEEP", url="${var.delay}"),
            ]
        )
        start = time.monotonic()
        store = run(cfg, {"delay": "0.02"}, out=io.StringIO())
        elapsed = time.monotonic() - start

        self.assertGreaterEqual(elapsed, 0.02)
        self.assertNotIn("steps", store)

    def test_sleep_step_quiet(self):
        """SLEEP step in quiet mode prints no detail."""
        cfg = WorkflowConfig(
            requests=[
                RequestConfig(name="qwait", method="SLEEP", url="0.01"),
            ]
        )
        buf = io.StringIO()
        run(cfg, quiet=True, out=buf)
        output = buf.getvalue()
        self.assertIn("[qwait] SLEEP", output)
        self.assertNotIn("sleep 0.01 seconds", output)


class TestSpecialMethodsSet(unittest.TestCase):
    def test_special_methods_contains_sleep(self):
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
        wf = toml_load(path)
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
        wf = toml_load(path)
        self.assertEqual(len(wf.steps), 1)
        step = wf.steps[0]
        self.assertIsInstance(step, SleepStep)
        assert isinstance(step, SleepStep)
        self.assertEqual(step.seconds, "${repeat.delay}")


if __name__ == "__main__":
    unittest.main()
