import os
import tempfile
import unittest

from httpflow import config as cfg_mod
from httpflow.model import FormBody, HttpStep, SleepStep, TextBody, WorkflowSpec


SAMPLE = b"""
[[requests]]
name    = "getToken"
method  = "POST"
url     = "https://api.example.com/auth"
headers = ["Content-Type: application/json"]
body    = '''{"user":"test","pass":"secret"}'''
capture = ["token = access_token"]

[[requests]]
name    = "updateProfile"
method  = "PUT"
url     = "https://api.example.com/profile"
headers = [
    "Authorization: Bearer ${token}",
    "Content-Type: application/x-www-form-urlencoded",
]
body_form = [
    "nickname = new_name",
    "email    = test@example.com",
]
"""


class TestParseKvList(unittest.TestCase):
    def test_colon_with_url_value(self):
        out = cfg_mod.parse_kv_list(["X-Url: https://example.com:8080/path"], ":")
        self.assertEqual(out, {"X-Url": "https://example.com:8080/path"})

    def test_equals_form(self):
        out = cfg_mod.parse_kv_list(["email = test@example.com"], "=")
        self.assertEqual(out, {"email": "test@example.com"})

    def test_missing_sep(self):
        with self.assertRaises(ValueError):
            cfg_mod.parse_kv_list(["no separator"], "=")

    def test_empty_key(self):
        with self.assertRaises(ValueError):
            cfg_mod.parse_kv_list(["= value"], "=")


class TestLoad(unittest.TestCase):
    def _write(self, content: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        self.addCleanup(os.unlink, path)
        return path

    def test_basic_load(self):
        path = self._write(SAMPLE)
        wf = cfg_mod.load(path)
        self.assertIsInstance(wf, WorkflowSpec)
        self.assertEqual(len(wf.steps), 2)

        r0 = wf.steps[0]
        self.assertIsInstance(r0, HttpStep)
        self.assertEqual(r0.name, "getToken")
        self.assertEqual(r0.method, "POST")
        self.assertEqual(r0.headers, {"Content-Type": "application/json"})
        self.assertIsInstance(r0.body, TextBody)
        assert isinstance(r0.body, TextBody)
        self.assertIn('"user":"test"', r0.body.text)
        self.assertEqual(r0.capture, {"token": "access_token"})

        r1 = wf.steps[1]
        self.assertIsInstance(r1, HttpStep)
        self.assertIsInstance(r1.body, FormBody)
        assert isinstance(r1.body, FormBody)
        self.assertEqual(r1.body.fields, {"nickname": "new_name", "email": "test@example.com"})

    def test_body_and_body_form_exclusive(self):
        bad = b"""
[[requests]]
name = "x"
method = "POST"
url = "http://example.com"
body = "abc"
body_form = ["a = b"]
"""
        path = self._write(bad)
        with self.assertRaises(ValueError):
            cfg_mod.load(path)

    def test_sleep_rejects_headers(self):
        bad = b"""
[[requests]]
name = "bad"
method = "SLEEP"
url = "5"
headers = ["X: Y"]
"""
        path = self._write(bad)
        with self.assertRaises(ValueError) as ctx:
            cfg_mod.load(path)
        self.assertIn("SLEEP", str(ctx.exception))

    def test_sleep_rejects_body(self):
        bad = b"""
[[requests]]
name = "bad"
method = "SLEEP"
url = "5"
body = "hi"
"""
        path = self._write(bad)
        with self.assertRaises(ValueError):
            cfg_mod.load(path)

    def test_sleep_accepts_template_url(self):
        path = self._write(b"""
[[requests]]
name = "wait"
method = "SLEEP"
url = "${var.delay}"
""")
        wf = cfg_mod.load(path)
        self.assertEqual(len(wf.steps), 1)
        step = wf.steps[0]
        self.assertIsInstance(step, SleepStep)
        assert isinstance(step, SleepStep)
        self.assertEqual(step.seconds, "${var.delay}")

    def test_sleep_accepts_repeat_template_url(self):
        path = self._write(b"""
[[requests]]
name = "wait"
method = "SLEEP"
url = "${repeat.delay}"
""")
        wf = cfg_mod.load(path)
        self.assertEqual(len(wf.steps), 1)
        step = wf.steps[0]
        self.assertIsInstance(step, SleepStep)
        assert isinstance(step, SleepStep)
        self.assertEqual(step.seconds, "${repeat.delay}")

    def test_sleep_rejects_non_numeric_literal_url(self):
        bad = b"""
[[requests]]
name = "bad"
method = "SLEEP"
url = "abc"
"""
        path = self._write(bad)
        with self.assertRaises(ValueError):
            cfg_mod.load(path)


if __name__ == "__main__":
    unittest.main()
