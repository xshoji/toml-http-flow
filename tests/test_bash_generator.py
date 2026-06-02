import shutil
import subprocess
import tempfile
import textwrap
import unittest
import uuid
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from httpflow import config as cfg_mod
from httpflow import bash_generator


class _CaptureHandler(BaseHTTPRequestHandler):
    seen_auth = ""
    seen_body = ""
    me_count = 0

    def _json(self, payload: dict[str, object], *, trace: str = "trace-1") -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Trace-Id", trace)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).seen_body = self.rfile.read(length).decode("utf-8")
        if self.path == "/auth":
            self._json({"access_token": "bash-token", "data": {"id": 7}})
        elif self.path == "/edge":
            self._json({"ok": False, "empty": "nil", "items": [{"access-token": "edge-token"}]})
        else:
            self._json({"ok": False})

    def do_GET(self):
        if self.path == "/me":
            type(self).me_count += 1
            type(self).seen_auth = self.headers.get("Authorization", "")
            self._json({"ok": True})
        elif self.path.startswith("/echo"):
            self._json({"ok": True})
        elif self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.send_header("X-Trace-Id", "redirect-trace")
            self.end_headers()
        elif self.path == "/final":
            self._json({"ok": True})
        else:
            self._json({"ok": False})

    def log_message(self, format, *args):
        return


@unittest.skipUnless(
    shutil.which("bash") and shutil.which("curl"),
    "bash and curl required",
)
class TestBashGenerator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _generate_and_check(self, toml_text: str, shebang: bool = False):
        """Generate script, check syntax, return script text."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml_text, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf, shebang=shebang)
            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")

            syntax = subprocess.run(
                ["bash", "-n", str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(
                syntax.returncode, 0,
                msg=f"syntax error:\n{syntax.stderr}\n--- script ---\n{script}",
            )
            return script

    def test_simple_get(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_ping()", script)
        self.assertIn('curl -sS -L -D "$__RESP_HEADERS" -o "$__RESP_BODY" -w "%{http_code}"', script)
        self.assertIn('-X GET', script)

    def test_post_with_body(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "create"
            method = "POST"
            url = "http://example.com/items"
            headers = ["Content-Type: application/json"]
            body = '{"name":"test"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_create()", script)
        self.assertIn("__BODY=$(cat << EOF", script)
        self.assertIn('{"name":"test"}', script)
        self.assertIn('cmd+=(-d', script)
        self.assertIn('"$__BODY"', script)

    def test_form_body(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "login"
            method = "POST"
            url = "http://example.com/auth"
            body_form = ["user = alice", "pass = secret"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_login()", script)
        self.assertIn('Content-Type: application/x-www-form-urlencoded', script)
        self.assertIn('user=alice&pass=secret', script)

    def test_sleep_step(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "wait"
            method = "SLEEP"
            url = "0.05"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_wait()", script)
        self.assertIn('seconds="0.05"', script)
        self.assertIn('sleep "$seconds"', script)

    def test_sleep_step_with_shell_variable(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "wait"
            method = "SLEEP"
            url = "${WAIT_SECONDS}"
        """)
        script = self._generate_and_check(toml)
        self.assertIn('seconds="${WAIT_SECONDS}"', script)
        self.assertIn('echo "==> [wait] SLEEP $seconds"', script)
        self.assertIn('sleep "$seconds"', script)

    def test_shebang(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping"
        """)
        script = self._generate_and_check(toml, shebang=True)
        self.assertTrue(script.startswith("#!/usr/bin/env bash\n"))

    def test_env_var_in_url(self):
        """URL containing $VAR is emitted as-is for shell expansion."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping?id=$ITEM_ID"
        """)
        script = self._generate_and_check(toml)
        self.assertIn('cmd+=("$url")', script)

    def test_var_and_repeat_placeholders_become_shell_env_names(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "echo"
            method = "POST"
            url = "http://example.com/${var.env}?id=${repeat.id}"
            body = '{"name":"${var.user}","id":"${repeat.id}"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn('url="http://example.com/${VAR_ENV}?id=${REPEAT_ID}"', script)
        self.assertIn('{"name":"${VAR_USER}","id":"${REPEAT_ID}"}', script)

    def test_no_bash4_features(self):
        """Ensure no bash 4+ only syntax slips in."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping"
        """)
        script = self._generate_and_check(toml)
        forbidden = ["declare -A", "mapfile", "readarray", "local -n"]
        for pat in forbidden:
            self.assertNotIn(pat, script, msg=f"bash 4+ feature found: {pat}")

    @unittest.skipUnless(shutil.which("jq"), "jq required")
    def test_capture_json_and_reuse_as_var(self):
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.seen_auth = ""
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "auth"
            method = "POST"
            url = "{base}/auth"
            capture = ["token = access_token", "uid = response.body.data.id"]

            [[requests]]
            name = "me"
            method = "GET"
            url = "{base}/me"
            headers = ["Authorization: Bearer ${{var.token}}", "X-User: ${{var.uid}}"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("jq is required for JSON capture", script)
        self.assertIn("capture_json VAR_TOKEN", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("* capture token = '***'", res.stdout)
        self.assertIn("* capture uid = '7'", res.stdout)
        self.assertEqual(_CaptureHandler.seen_auth, "Bearer bash-token")

    @unittest.skipUnless(shutil.which("jq"), "jq required")
    def test_capture_json_false_null_array_and_hyphen_key(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "edge"
            method = "POST"
            url = "{base}/edge"
            capture = [
                "ok = ok",
                "empty = empty",
                "token = items[0].access-token",
            ]
        """)
        script = self._generate_and_check(toml)
        self.assertIn(".[\"items\"]?[0]?[\"access-token\"]?", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("* capture ok = 'false'", res.stdout)
        self.assertIn("* capture empty = 'nil'", res.stdout)
        self.assertIn("* capture token = '***'", res.stdout)

    @unittest.skipUnless(shutil.which("jq"), "jq required")
    def test_capture_headers_and_request_values(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "echo"
            method = "POST"
            url = "{base}/echo?x=1"
            headers = ["Authorization: Bearer abc"]
            body = '{{"hello":"world"}}'
            capture = [
                "ct = response.header.content-type",
                "sent_auth = request.header.Authorization",
                "called = request.url",
                "sent_body = request.body",
            ]
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("* capture ct = 'application/json'", res.stdout)
        self.assertIn("* capture sent_auth = 'Bearer abc'", res.stdout)
        self.assertIn(f"* capture called = '{base}/echo?x=1'", res.stdout)
        self.assertIn("* capture sent_body = '{\"hello\":\"world\"}'", res.stdout)

    def test_header_only_capture_does_not_require_jq(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "echo"
            method = "GET"
            url = "{base}/echo"
            capture = ["ct = response.header.Content-Type"]
        """)
        script = self._generate_and_check(toml)
        self.assertNotIn("jq --version", script)

    @unittest.skipUnless(shutil.which("jq"), "jq required")
    def test_response_header_capture_uses_final_redirect_headers(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "redir"
            method = "GET"
            url = "{base}/redir"
            capture = ["trace = response.header.X-Trace-Id"]
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertIn("tolower($0) ~ /^http", script)

    @unittest.skipUnless(shutil.which("jq"), "jq required")
    def test_capture_failure_stops_later_steps(self):
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.me_count = 0
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "auth"
            method = "POST"
            url = "{base}/auth"
            capture = ["missing = nope"]

            [[requests]]
            name = "me"
            method = "GET"
            url = "{base}/me"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("capture failed: missing <- nope", res.stderr)
        self.assertEqual(_CaptureHandler.me_count, 0)

    def test_step_name_collision(self):
        """Duplicate sanitized names get numeric suffixes."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "a-b"
            method = "GET"
            url = "http://example.com/1"

            [[requests]]
            name = "a_b"
            method = "GET"
            url = "http://example.com/2"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_a_b()", script)
        self.assertIn("step_a_b_2()", script)

    def test_description_is_emitted(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping"
            description = "health check"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("# health check", script)

    def test_random_uuid_placeholders(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "create"
            method = "POST"
            url = "http://example.com/items/${random.UUID_HEX}"
            headers = ["X-Request-Id: ${random.UUID}"]
            body = '{"request_id":"${random.UUID}"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn("uuid()", script)
        self.assertIn("uuid_hex()", script)
        self.assertIn('url="http://example.com/items/$(uuid_hex)"', script)
        self.assertIn('header="X-Request-Id: $(uuid)"', script)
        self.assertIn('cmd+=(-H "$header")', script)
        self.assertIn('{"request_id":"$(uuid)"}', script)

    def test_generated_uuid_helpers_return_valid_values(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", "-c", f"source {script_path} >/dev/null || true; uuid; uuid_hex"],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        uuid_value, uuid_hex_value = res.stdout.splitlines()
        self.assertEqual(str(uuid.UUID(uuid_value)), uuid_value)
        self.assertEqual(uuid.UUID(hex=uuid_hex_value).hex, uuid_hex_value)

    def test_masking_is_emitted_for_logs(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "login"
            method = "POST"
            url = "http://example.com/auth?token=url-secret&keep=ok"
            headers = ["Authorization: Bearer header-secret"]
            body_form = ["user = alice", "password = body-secret"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("mask()", script)
        self.assertIn("mask_lines()", script)
        self.assertIn('$(mask "$url")', script)
        self.assertIn('done < "$__REQ_HEADERS" | mask_lines', script)
        self.assertIn('printf "%s\\n" "$__BODY" | mask_lines', script)
        self.assertIn('mask_lines < "$__RESP_BODY"', script)

    def test_generated_mask_helper_masks_simple_values(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {script_path} >/dev/null || true; "
                    "mask 'token=abc&keep=ok'; "
                    "mask 'Authorization: Bearer secret'; "
                    "mask '{\"password\":\"p\",\"user\":\"u\"}'",
                ],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("token=***", res.stdout)
        self.assertIn("Authorization: ***", res.stdout)
        self.assertIn('"password":***', res.stdout)
        self.assertNotIn("abc", res.stdout)
        self.assertNotIn("Bearer secret", res.stdout)

    def test_mask_lines_masks_curl_like_output(self):
        """mask_lines should mask sensitive fields in piped curl-like output."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {script_path} >/dev/null || true; "
                    "printf '%s\n' "
                      "\"Authorization: Bearer secret\" "
                      "\"Set-Cookie: session=abc123\" "
                      "\"token=url-secret\" "
                      "\"< HTTP/1.1 200 OK\" "
                      "\"< password: mypass\" "
                      "\"{\\\"password\\\":\\\"secret\\\"}\" "
                    "| mask_lines",
                ],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("Authorization: ***", res.stdout)
        self.assertIn("Set-Cookie: ***", res.stdout)
        self.assertIn("token=***", res.stdout)
        self.assertIn('"password":***', res.stdout)
        self.assertIn("< HTTP/1.1 200 OK", res.stdout)
        self.assertNotIn("Bearer secret", res.stdout)
        self.assertNotIn("session=abc123", res.stdout)
        self.assertNotIn("url-secret", res.stdout)
        self.assertNotIn("mypass", res.stdout)
        self.assertNotIn('"password":"secret"', res.stdout)

    def test_default_vars_embedded_in_bash_script(self):
        """-v K=V in generate --format bash embeds default VAR_* values."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "echo"
            method = "POST"
            url = "http://example.com/${var.env}?id=${repeat.id}"
            body = '{"name":"${var.user}","id":"${repeat.id}"}'
        """)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(
                wf,
                default_vars={"env": "prod", "user": "alice"},
                default_repeat_vars={"id": ["1", "2"]},
            )
            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")

            syntax = subprocess.run(
                ["bash", "-n", str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(
                syntax.returncode, 0,
                msg=f"syntax error:\n{syntax.stderr}\n--- script ---\n{script}",
            )

            self.assertIn(': "${VAR_ENV:=prod}"', script)
            self.assertIn(': "${VAR_USER:=alice}"', script)
            self.assertIn(': "${REPEAT_ID:=1,2}"', script)
            # Ensure they are defined *before* main / step functions so they act as defaults
            defaults_pos = script.find("# ─── defaults")
            steps_pos = script.find("# ─── step functions")
            self.assertGreater(defaults_pos, -1)
            self.assertGreater(steps_pos, -1)
            self.assertLess(defaults_pos, steps_pos)

    def test_default_vars_overridable_at_runtime(self):
        """Embedded default vars can be overridden by exporting before running."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "http://example.com/${var.env}"
        """)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(
                wf,
                default_vars={"env": "default_env"},
            )
            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")

            # Without override: url should contain default value
            res = subprocess.run(
                ["bash", "-c", f"source {script_path} >/dev/null || true; echo 'http://example.com/'\"${{VAR_ENV}}\""],
                capture_output=True, text=True, timeout=10,
            )
            self.assertIn("default_env", res.stdout)

            # With override
            res2 = subprocess.run(
                ["bash", "-c",
                 f"VAR_ENV=overridden; source {script_path} >/dev/null || true; echo \"http://example.com/${{VAR_ENV}}\""],
                capture_output=True, text=True, timeout=10,
            )
            self.assertIn("overridden", res2.stdout)
