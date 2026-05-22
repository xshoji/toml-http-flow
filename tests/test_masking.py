"""Tests for httpflow.masking and its integration in workflow output."""

import io
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from httpflow import masking
from httpflow.config import RequestConfig, WorkflowConfig
from httpflow.workflow import run


# ----------------------------------------------------------------- env utils


class _EnvScope:
    """Temporarily set env vars; restore on exit. Supports None to unset."""

    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._saved = {}

    def __enter__(self):
        for k, v in self._kwargs.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _clean_env():
    """Force-clear HTTPFLOW_MASK_EXTRA env var."""
    return _EnvScope(HTTPFLOW_MASK_EXTRA=None)


# ----------------------------------------------------------------- unit tests


class TestNorm(unittest.TestCase):
    def test_case_separator_insensitive(self):
        self.assertEqual(masking._norm("API_Key"), "apikey")
        self.assertEqual(masking._norm("X-Auth-Token"), "xauthtoken")
        self.assertEqual(masking._norm("api key"), "apikey")


class TestMaskJson(unittest.TestCase):
    def test_recursive(self):
        with _clean_env():
            out = masking.mask(
                '{"user":"u","password":"p","nested":{"access_token":"t","keep":"k"}}'
            )
        parsed = json.loads(out)
        self.assertEqual(parsed["user"], "u")
        self.assertEqual(parsed["password"], "***")
        self.assertEqual(parsed["nested"]["access_token"], "***")
        self.assertEqual(parsed["nested"]["keep"], "k")

    def test_list_of_dicts(self):
        with _clean_env():
            out = masking.mask(
                '{"items":[{"name":"a","secret":"s1"},{"name":"b","secret":"s2"}]}'
            )
        parsed = json.loads(out)
        self.assertEqual(parsed["items"][0]["secret"], "***")
        self.assertEqual(parsed["items"][1]["name"], "b")

    def test_disabled(self):
        with _clean_env():
            text = '{"password":"p"}'
            self.assertEqual(masking.mask(text, disabled=True), text)


class TestMaskFormUrlencoded(unittest.TestCase):
    def test_basic(self):
        with _clean_env():
            out = masking.mask("user=u&password=p&token=abc")
        self.assertIn("user=u", out)
        self.assertIn("password=%2A%2A%2A", out)
        self.assertIn("token=%2A%2A%2A", out)


class TestMaskPlainText(unittest.TestCase):
    def test_untouched(self):
        with _clean_env():
            out = masking.mask("just a plain message")
        self.assertEqual(out, "just a plain message")


class TestMaskHeaders(unittest.TestCase):
    def test_defaults_mask_authorization_and_cookie(self):
        with _clean_env():
            headers = {
                "Authorization": "Bearer secret-token",
                "Cookie": "sid=abc",
                "Accept": "application/json",
            }
            out = {k: masking.mask_value(k, v) for k, v in headers.items()}
        self.assertEqual(out["Authorization"], "***")
        self.assertEqual(out["Cookie"], "***")
        self.assertEqual(out["Accept"], "application/json")

    def test_extra_env_adds_x_trace_id(self):
        with _clean_env(), _EnvScope(HTTPFLOW_MASK_EXTRA="X-Trace-Id"):
            headers = {
                "Authorization": "Bearer x",
                "X-Trace-Id": "trace-1",
            }
            out = {k: masking.mask_value(k, v) for k, v in headers.items()}
        self.assertEqual(out["Authorization"], "***")
        self.assertEqual(out["X-Trace-Id"], "***")


class TestMaskUrl(unittest.TestCase):
    def test_query_token_masked(self):
        with _clean_env():
            out = masking.mask_url("https://x/api?token=abc&page=2")
        self.assertIn("token=%2A%2A%2A", out)
        self.assertIn("page=2", out)

    def test_no_query_unchanged(self):
        with _clean_env():
            url = "https://x/api"
            self.assertEqual(masking.mask_url(url), url)


class TestMaskValue(unittest.TestCase):
    def test_sensitive_name_masked(self):
        with _clean_env():
            self.assertEqual(masking.mask_value("token", "abc"), "***")
            self.assertEqual(masking.mask_value("access_token", "abc"), "***")

    def test_non_sensitive_kept(self):
        with _clean_env():
            self.assertEqual(masking.mask_value("user_id", 7), 7)

    def test_extra_env_masks_header_name_too(self):
        with _clean_env(), _EnvScope(HTTPFLOW_MASK_EXTRA="X-Trace-Id"):
            self.assertEqual(masking.mask_value("X-Trace-Id", "trace-1"), "***")


# ----------------------------------------------------------------- workflow integration


class _EchoHandler(BaseHTTPRequestHandler):
    def _send(self, code, payload, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self._send(
            200,
            {"access_token": "tok-xyz", "user": "alice"},
            headers={"Set-Cookie": "sid=secret-cookie"},
        )

    def log_message(self, format, *args):
        return


class TestWorkflowMasking(unittest.TestCase):
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

    def _run(self, no_mask=False, env=None):
        cfg = WorkflowConfig(requests=[
            RequestConfig(
                name="auth",
                method="POST",
                url=f"http://127.0.0.1:{self.port}/auth?token=qparam-secret&keep=ok",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer real-secret",
                    "X-Trace-Id": "trace-123",
                },
                body='{"user":"alice","password":"hunter2"}',
                capture={"token": "access_token", "user": "user"},
            ),
        ])
        buf = io.StringIO()
        with _clean_env(), _EnvScope(**(env or {})):
            store = run(cfg, out=buf, no_mask=no_mask)
        return buf.getvalue(), store

    def test_defaults_mask_everything_sensible(self):
        output, store = self._run()

        # ---- store remains UNMASKED (only logging is affected) ----
        self.assertEqual(store["steps"]["auth"]["token"], "tok-xyz")
        self.assertEqual(store["steps"]["auth"]["user"], "alice")

        # ---- headers (request + response) masked in log ----
        self.assertNotIn("Bearer real-secret", output)
        self.assertIn("Authorization: ***", output)
        self.assertIn("Set-Cookie: ***", output)
        self.assertNotIn("secret-cookie", output)

        # X-Trace-Id is NOT in defaults → must remain visible
        self.assertIn("X-Trace-Id: trace-123", output)

        # ---- URL query masked in log ----
        self.assertNotIn("token=qparam-secret", output)
        self.assertIn("token=%2A%2A%2A", output)
        self.assertIn("keep=ok", output)

        # ---- request body JSON masked in log ----
        self.assertNotIn("hunter2", output)

        # ---- response body JSON masked in log ----
        self.assertNotIn("tok-xyz", output)
        self.assertIn('"access_token": "***"', output)

        # ---- capture line masked for sensitive var name ----
        self.assertIn("* capture token = '***'", output)
        # non-sensitive var name stays visible
        self.assertIn("* capture user = 'alice'", output)

    def test_disable_via_no_mask_flag(self):
        output, _ = self._run(no_mask=True)
        self.assertIn("Bearer real-secret", output)
        self.assertIn("hunter2", output)
        self.assertIn("tok-xyz", output)
        self.assertIn("token=qparam-secret", output)

    def test_extra_env_masks_header_and_body_alike(self):
        # HTTPFLOW_MASK_EXTRA applies to both headers and body keys (and capture).
        output, _ = self._run(env={"HTTPFLOW_MASK_EXTRA": "X-Trace-Id,user"})
        # X-Trace-Id header masked
        self.assertIn("X-Trace-Id: ***", output)
        # default behaviour for Authorization preserved
        self.assertIn("Authorization: ***", output)
        # "user" body key masked (body+query+capture)
        self.assertNotIn('"user": "alice"', output)
        self.assertIn('"user": "***"', output)
        # capture "user" also masked
        self.assertIn("* capture user = '***'", output)

    def test_form_body_key_based_masking(self):
        """form body values must be masked by key name, not parsed as JSON/form."""
        cfg = WorkflowConfig(requests=[
            RequestConfig(
                name="login",
                method="POST",
                url=f"http://127.0.0.1:{self.port}/auth",
                body_form={
                    "password": "hunter2",
                    "note": "a=b",
                    "metadata": '{"password":"x"}',
                },
            ),
        ])
        buf = io.StringIO()
        with _clean_env():
            run(cfg, out=buf)
        output = buf.getvalue()
        # Sensitive key masked
        self.assertIn("password = ***", output)
        self.assertNotIn("hunter2", output)
        # Non-sensitive key with = in value stays as-is (not url-encoded)
        self.assertIn("note = a=b", output)
        # Non-sensitive key with JSON-looking value stays as-is (not parsed as JSON)
        self.assertIn('metadata = {"password":"x"}', output)


if __name__ == "__main__":
    unittest.main()
