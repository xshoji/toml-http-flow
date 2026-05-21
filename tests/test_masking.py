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
    """Force-clear every HTTPFLOW_MASK_* env var to defaults."""
    return _EnvScope(
        HTTPFLOW_MASK_DISABLED=None,
        HTTPFLOW_MASK_PLACEHOLDER=None,
        HTTPFLOW_MASK_HEADERS=None,
        HTTPFLOW_MASK_HEADERS_EXTRA=None,
        HTTPFLOW_MASK_BODY_KEYS=None,
        HTTPFLOW_MASK_BODY_KEYS_EXTRA=None,
    )


# ----------------------------------------------------------------- unit tests


class TestNorm(unittest.TestCase):
    def test_case_separator_insensitive(self):
        self.assertEqual(masking._norm("API_Key"), "apikey")
        self.assertEqual(masking._norm("X-Auth-Token"), "xauthtoken")
        self.assertEqual(masking._norm("api key"), "apikey")


class TestMaskHeaders(unittest.TestCase):
    def test_defaults_mask_authorization_and_cookie(self):
        with _clean_env():
            out = masking.mask_headers({
                "Authorization": "Bearer secret-token",
                "Cookie": "sid=abc",
                "Accept": "application/json",
            })
        self.assertEqual(out["Authorization"], "***")
        self.assertEqual(out["Cookie"], "***")
        self.assertEqual(out["Accept"], "application/json")

    def test_case_and_separator_variants(self):
        with _clean_env():
            out = masking.mask_headers({
                "x-api-key": "v1",
                "X_API_KEY": "v2",
                "X-Api-Key": "v3",
            })
        self.assertEqual(out, {"x-api-key": "***", "X_API_KEY": "***", "X-Api-Key": "***"})

    def test_disabled_env(self):
        with _clean_env(), _EnvScope(HTTPFLOW_MASK_DISABLED="1"):
            out = masking.mask_headers({"Authorization": "Bearer x"})
        self.assertEqual(out, {"Authorization": "Bearer x"})

    def test_custom_placeholder(self):
        with _clean_env(), _EnvScope(HTTPFLOW_MASK_PLACEHOLDER="<redacted>"):
            out = masking.mask_headers({"Authorization": "Bearer x"})
        self.assertEqual(out["Authorization"], "<redacted>")

    def test_replace_defaults_env(self):
        with _clean_env(), _EnvScope(HTTPFLOW_MASK_HEADERS="X-Trace-Id"):
            out = masking.mask_headers({
                "Authorization": "Bearer x",     # NOT masked anymore
                "X-Trace-Id": "trace-1",         # masked
            })
        self.assertEqual(out["Authorization"], "Bearer x")
        self.assertEqual(out["X-Trace-Id"], "***")

    def test_extra_env_adds_to_defaults(self):
        with _clean_env(), _EnvScope(HTTPFLOW_MASK_HEADERS_EXTRA="X-Trace-Id"):
            out = masking.mask_headers({
                "Authorization": "Bearer x",     # still masked
                "X-Trace-Id": "trace-1",         # newly masked
            })
        self.assertEqual(out["Authorization"], "***")
        self.assertEqual(out["X-Trace-Id"], "***")


class TestMaskBodyText(unittest.TestCase):
    def test_json_recursive(self):
        with _clean_env():
            out = masking.mask_body_text(
                '{"user":"u","password":"p","nested":{"access_token":"t","keep":"k"}}'
            )
        parsed = json.loads(out)
        self.assertEqual(parsed["user"], "u")
        self.assertEqual(parsed["password"], "***")
        self.assertEqual(parsed["nested"]["access_token"], "***")
        self.assertEqual(parsed["nested"]["keep"], "k")

    def test_form_urlencoded(self):
        with _clean_env():
            out = masking.mask_body_text("user=u&password=p&token=abc")
        self.assertIn("user=u", out)
        self.assertIn("password=%2A%2A%2A", out)
        self.assertIn("token=%2A%2A%2A", out)

    def test_plain_text_untouched(self):
        with _clean_env():
            out = masking.mask_body_text("just a plain message")
        self.assertEqual(out, "just a plain message")

    def test_disabled(self):
        with _clean_env(), _EnvScope(HTTPFLOW_MASK_DISABLED="true"):
            text = '{"password":"p"}'
            self.assertEqual(masking.mask_body_text(text), text)

    def test_list_of_dicts(self):
        with _clean_env():
            out = masking.mask_body_text(
                '{"items":[{"name":"a","secret":"s1"},{"name":"b","secret":"s2"}]}'
            )
        parsed = json.loads(out)
        self.assertEqual(parsed["items"][0]["secret"], "***")
        self.assertEqual(parsed["items"][1]["name"], "b")


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


class TestMaskCaptureValue(unittest.TestCase):
    def test_sensitive_name_masked(self):
        with _clean_env():
            self.assertEqual(masking.mask_capture_value("token", "abc"), "***")
            self.assertEqual(masking.mask_capture_value("access_token", "abc"), "***")

    def test_non_sensitive_kept(self):
        with _clean_env():
            self.assertEqual(masking.mask_capture_value("user_id", 7), 7)


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

    def _run(self, env=None):
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
            store = run(cfg, out=buf)
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

    def test_disable_via_env(self):
        output, _ = self._run({"HTTPFLOW_MASK_DISABLED": "1"})
        self.assertIn("Bearer real-secret", output)
        self.assertIn("hunter2", output)
        self.assertIn("tok-xyz", output)
        self.assertIn("token=qparam-secret", output)

    def test_custom_placeholder(self):
        output, _ = self._run({"HTTPFLOW_MASK_PLACEHOLDER": "<X>"})
        self.assertIn("Authorization: <X>", output)
        self.assertIn("* capture token = '<X>'", output)

    def test_extra_headers_env_adds_x_trace_id(self):
        output, _ = self._run({"HTTPFLOW_MASK_HEADERS_EXTRA": "X-Trace-Id"})
        self.assertIn("X-Trace-Id: ***", output)
        # default behaviour for Authorization preserved
        self.assertIn("Authorization: ***", output)

    def test_replace_body_keys_env(self):
        # Replace defaults with only "user" → 'password' stops being masked,
        # 'user' starts being masked.
        output, _ = self._run({"HTTPFLOW_MASK_BODY_KEYS": "user"})
        self.assertIn("hunter2", output)              # password no longer masked
        self.assertNotIn('"user": "alice"', output)   # user value masked
        self.assertIn('"user": "***"', output)


if __name__ == "__main__":
    unittest.main()
