import shutil
import shlex
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
    seen_body_bytes = b""
    seen_content_type = ""
    me_count = 0
    poll_count = 0
    multipart_fields: dict[str, str] = {}
    multipart_files: list[dict[str, object]] = []

    def _parse_multipart(self, body: bytes, content_type: str) -> None:
        """Parse multipart/form-data body and store fields/files."""
        ct = content_type or ""
        boundary = ""
        if ";" in ct:
            parts = ct.split(";")
            for part in parts:
                part = part.strip()
                if part.startswith("boundary="):
                    boundary = part.split("=", 1)[1].strip().strip('"')
                    break
        if not boundary:
            return

        boundary_delim = f"--{boundary}".encode()
        # Split by the CRLF-delimited boundary
        raw_parts = body.split(boundary_delim)

        for raw_part in raw_parts:
            if not raw_part or raw_part in (b"\r\n", b""):
                continue
            # Remove trailing CRLF and trailing -- (final boundary marker)
            raw_part = raw_part.rstrip(b"\r\n")
            if raw_part.endswith(b"--"):
                raw_part = raw_part[:-2].rstrip(b"\r\n")
            if not raw_part:
                continue

            # Split header block from body by CRLF CRLF
            header_and_body = raw_part.split(b"\r\n\r\n", 1)
            if len(header_and_body) != 2:
                continue
            header_bytes, part_body = header_and_body

            try:
                header_text = header_bytes.decode("utf-8", errors="replace")
            except Exception:
                continue

            # Parse Content-Disposition header
            cd_line = ""
            for h in header_text.split("\r\n"):
                if h.strip().lower().startswith("content-disposition:"):
                    cd_line = h.strip()
                    break
            if not cd_line:
                continue

            name = ""
            filename = ""
            # Extract name and filename from Content-Disposition: form-data; name="X"; filename="Y"
            for param in cd_line.split(";"):
                param = param.strip()
                if param.lower().startswith('name='):
                    name = param.split('=', 1)[1].strip().strip('"')
                elif param.lower().startswith('filename='):
                    filename = param.split('=', 1)[1].strip().strip('"')

            if filename:
                # Parse content-type for file
                file_ct = ""
                for h in header_text.split("\r\n"):
                    if h.strip().lower().startswith("content-type:"):
                        file_ct = h.strip().split(":", 1)[1].strip()
                        break
                type(self).multipart_files.append({
                    "name": name,
                    "filename": filename,
                    "content_type": file_ct,
                    "data": part_body,
                })
            else:
                type(self).multipart_fields[name] = part_body.decode("utf-8", errors="replace")

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
        raw_body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" in content_type:
            self._parse_multipart(raw_body, content_type)
            type(self).seen_body = "<multipart form data>"
        else:
            type(self).seen_body = raw_body.decode("utf-8", errors="replace")
        if self.path == "/auth":
            self._json({"access_token": "bash-token", "data": {"id": 7}})
        elif self.path == "/edge":
            self._json({"ok": False, "empty": "nil", "items": [{"access-token": "edge-token"}]})
        else:
            self._json({"ok": False})

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).seen_body_bytes = self.rfile.read(length)
        type(self).seen_content_type = self.headers.get("Content-Type", "")
        type(self).seen_body = type(self).seen_body_bytes.decode("utf-8", errors="replace")
        self._json({"ok": True, "size": len(type(self).seen_body_bytes)})

    def do_GET(self):
        if self.path == "/me":
            type(self).me_count += 1
            type(self).seen_auth = self.headers.get("Authorization", "")
            self._json({"ok": True})
        elif self.path.startswith("/echo"):
            self._json({"ok": True})
        elif self.path == "/poll":
            type(self).poll_count += 1
            status = "Active" if type(self).poll_count >= 3 else "Pending"
            self._json({"status": status})
        elif self.path == "/poll404":
            type(self).poll_count += 1
            status = "Active" if type(self).poll_count >= 2 else "Pending"
            body = json.dumps({"status": status}).encode("utf-8")
            self.send_response(200 if status == "Active" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_ping()", script)
        self.assertIn('curl_command=$(cat << \'EOT\'', script)
        self.assertIn('curl -sS -L -v --no-buffer --stderr - -X GET "$url"', script)
        self.assertIn("grep -v '^\\({\\|}\\) \\[.*bytes data\\]'", script)
        self.assertIn("grep -v '^\\*'", script)
        self.assertIn('tee -a "$trace_file"', script)
        self.assertNotIn('-D "$resp_headers" -o "$resp_body"', script)
        self.assertIn("http_step 'ping' 'GET'", script)

    def test_time_placeholders(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "echo"
            request = "GET {base}/echo?iso=${{time.DATE_ISO}}&ymd=${{time.DATE_YMD}}&hms=${{time.DATE_YMDHMS}}"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertRegex(res.stdout, r"iso=\d{4}-\d{2}-\d{2}T\d{2}:")
        self.assertRegex(res.stdout, r"ymd=\d{8}")
        self.assertRegex(res.stdout, r"hms=\d{14}")

    def test_http_summary_lines_include_timestamps(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        ts = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+([+-]\d{2}:\d{2})?"
        self.assertRegex(res.stdout, rf"==> {ts} \[ping\] GET {base}/echo")
        self.assertRegex(res.stdout, rf"<== {ts} \[ping\]")

    def test_post_with_body(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "create"
            request = "POST http://example.com/items"
            headers = ["Content-Type: application/json"]
            body = '{"name":"test"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_create()", script)
        self.assertIn("body=$(cat << EOT", script)
        self.assertNotIn("__HF_BODY_step_create", script)
        self.assertIn('{"name":"test"}', script)
        self.assertIn('-d "$body"', script)

    def test_post_body_is_inserted_at_curl_request_response_boundary(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "create"
            request = "POST {base}/auth"
            headers = ["Content-Type: application/json"]
            body = '{{"name":"test"}}'
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        lines = res.stdout.splitlines()
        boundary_idx = lines.index("> ")
        self.assertEqual(lines[boundary_idx + 1], '> {"name":"test"}')
        self.assertRegex(lines[boundary_idx + 2], r"^<== \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+([+-]\d{2}:\d{2})? \[create\]")
        self.assertTrue(lines[boundary_idx + 3].startswith("< HTTP/"))

    def test_form_body(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "login"
            request = "POST {base}/auth"
            body_form = ["user = alice", "pass = secret"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_login()", script)
        self.assertIn('Content-Type: application/x-www-form-urlencoded', script)
        self.assertNotIn("body_form_text=", script)
        self.assertIn('--data-urlencode "user=alice"', script)
        self.assertIn('--data-urlencode "pass=secret"', script)
        # body_log is derived inside http_step from the parsed curl_command,
        # not pre-built in the step function. The old per-type reconstruction
        # (Note headers, &-joined pairs, etc.) is gone: http_step now emits
        # each body-flag value verbatim, so none of those old strings remain.
        self.assertNotIn("Note: Values are shown before URL encoding.", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        # The runtime body log (built from the expanded curl args) should
        # appear in the request-body section, prefixed with `> `. Each
        # --data-urlencode value is shown verbatim, one per line.
        self.assertIn("> user=alice", res.stdout)
        self.assertIn("> pass=secret", res.stdout)

    def test_form_body_placeholders_expand_before_urlencode(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "first"
            request = "POST http://example.com/first"
            capture = ["authorization = response.body.token"]

            [[requests]]
            name = "login"
            request = "POST http://example.com/auth"
            body_form = [
                "nickname = new_name",
                "email    = test@email.com",
                "args     = ${var.argsAaa}",
                "params   = ${var.paramsParamb}",
                "token    = ${authorization}",
            ]
        """)
        script = self._generate_and_check(toml)
        # Form fields are emitted directly as curl args with placeholders
        # expanded to ${VAR_*} so the shell expands them at runtime (before
        # curl URL-encodes them).
        self.assertIn('--data-urlencode "nickname=new_name"', script)
        self.assertIn('--data-urlencode "email=test@email.com"', script)
        self.assertIn('--data-urlencode "args=${VAR_ARGSAAA}"', script)
        self.assertIn('--data-urlencode "params=${VAR_PARAMSPARAMB}"', script)
        self.assertIn('--data-urlencode "token=${VAR_AUTHORIZATION}"', script)
        self.assertNotIn("%24%7Bauthorization%7D", script)

    def test_form_body_does_not_duplicate_user_content_type(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "login"
            request = "POST http://example.com/auth"
            headers = ["Content-Type: application/x-www-form-urlencoded"]
            body_form = ["user = alice", "pass = secret"]
        """)
        script = self._generate_and_check(toml)
        # The user-specified Content-Type must be the only one passed to curl.
        # It appears once inside the curl_command heredoc.
        self.assertEqual(script.count("Content-Type: application/x-www-form-urlencoded"), 1)
        self.assertIn('-H "Content-Type: application/x-www-form-urlencoded"', script)

    def test_form_body_content_type_detection_is_case_insensitive(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "login"
            request = "POST http://example.com/auth"
            headers = ["content-type: application/x-www-form-urlencoded"]
            body_form = ["user = alice", "pass = secret"]
        """)
        script = self._generate_and_check(toml)
        self.assertNotIn('Content-Type: application/x-www-form-urlencoded', script)
        self.assertIn('content-type: application/x-www-form-urlencoded', script)

    def test_env_placeholders_use_shell_environment_variables(self):
        """${env.NAME} placeholders become ${NAME} in generated bash."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "env"
            request = "POST http://example.com/${env.USER}"
            headers = ["X-User: ${env.USER}"]
            body = '{"user":"${env.USER}"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn('local url="http://example.com/${USER}"', script)
        self.assertIn("X-User: ${USER}", script)
        self.assertIn('{"user":"${USER}"}', script)
        self.assertNotIn("${env.USER}", script)

    def test_sleep_step(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "wait"
            request = "SLEEP 0.05"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_wait()", script)
        self.assertIn('seconds="0.05"', script)
        self.assertIn('print_blank_lines "${HTTPFLOW_BLANK_LINE:-0}"', script)
        self.assertIn('SLEEP %s\\n" "$(time_date_iso)" \'wait\' "$seconds"', script)
        self.assertIn('sleep "$seconds"', script)
        self.assertIn('done\\n" "$(time_date_iso)" \'wait\'', script)

    def test_sleep_step_with_shell_variable(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "wait"
            request = "SLEEP ${WAIT_SECONDS}"
        """)
        script = self._generate_and_check(toml)
        self.assertIn('seconds="${WAIT_SECONDS}"', script)
        self.assertIn('SLEEP %s\\n" "$(time_date_iso)" \'wait\' "$seconds"', script)
        self.assertIn('sleep "$seconds"', script)

    def test_shebang(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml, shebang=True)
        self.assertTrue(script.startswith("#!/usr/bin/env bash\n"))

    def test_env_var_in_url(self):
        """URL containing $VAR is emitted as-is for shell expansion."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping?id=$ITEM_ID"
        """)
        script = self._generate_and_check(toml)
        self.assertIn('"$url"', script)

    def test_var_placeholders_become_shell_env_names(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "echo"
            request = "POST http://example.com/${var.env}?id=${var.id}"
            body = '{"name":"${var.user}","id":"${var.id}"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn('url="http://example.com/${VAR_ENV}?id=${VAR_ID}"', script)
        self.assertIn('{"name":"${VAR_USER}","id":"${VAR_ID}"}', script)

    def test_no_bash4_features(self):
        """Ensure no bash 4+ only syntax slips in."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
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
            request = "POST {base}/auth"
            capture = ["token = access_token", "uid = response.body.data.id"]

            [[requests]]
            name = "me"
            request = "GET {base}/me"
            headers = ["Authorization: Bearer ${{var.token}}", "X-User: ${{var.uid}}"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("jq is required for JSON capture", script)
        self.assertIn("capture_response_body_json 'VAR_TOKEN' 'token' 'access_token'", script)

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
            request = "POST {base}/edge"
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
            request = "POST {base}/echo?x=1"
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
            request = "GET {base}/echo"
            capture = ["ct = response.header.Content-Type"]
        """)
        script = self._generate_and_check(toml)
        self.assertNotIn("jq --version", script)

    @unittest.skipUnless(shutil.which("jq"), "jq required")
    def test_until_polls_until_capture_matches(self):
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.poll_count = 0
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "poll"
            request = "GET {base}/poll"
            capture = ["status = status"]
            until = ["condition = ${{status}} == Active", "interval = 0", "max_attempts = 5"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("until_eval", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertEqual(_CaptureHandler.poll_count, 3)
        self.assertIn("until satisfied on attempt 3", res.stdout)

    @unittest.skipUnless(shutil.which("jq"), "jq required")
    def test_until_exhaustion_fails(self):
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.poll_count = 0
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "poll"
            request = "GET {base}/poll"
            capture = ["status = status"]
            until = ["condition = ${{status}} == Never", "interval = 0", "max_attempts = 2"]
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(_CaptureHandler.poll_count, 2)
        self.assertIn("until condition not satisfied after 2 attempts", res.stderr)

    @unittest.skipUnless(shutil.which("jq"), "jq required")
    def test_until_treats_http_error_as_response(self):
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.poll_count = 0
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "poll"
            request = "GET {base}/poll404"
            capture = ["status = status"]
            until = ["condition = ${{status}} == Active", "interval = 0", "max_attempts = 3"]
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("until satisfied on attempt 2", res.stdout)

    def test_until_helpers_omitted_when_unused(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)
        self.assertNotIn("until_eval", script)

    @unittest.skipUnless(shutil.which("jq"), "jq required")
    def test_until_regex_uses_bash_native_eval(self):
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.poll_count = 0
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "poll"
            request = "GET {base}/poll"
            capture = ["status = status"]
            until = ["condition = ${{status}} ~ /active/i", "interval = 0", "max_attempts = 3"]
        """)
        script = self._generate_and_check(toml)
        self.assertNotIn("python3 is required for until regex", script)
        self.assertNotIn("import re", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("until satisfied on attempt 3", res.stdout)

    @unittest.skipUnless(shutil.which("jq"), "jq required")
    def test_response_header_capture_uses_final_redirect_headers(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "redir"
            request = "GET {base}/redir"
            capture = ["trace = response.header.X-Trace-Id"]
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertIn('/^< HTTP\\// { found=0; value=""; next }', script)

    @unittest.skipUnless(shutil.which("jq"), "jq required")
    def test_capture_failure_stops_later_steps(self):
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.me_count = 0
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "auth"
            request = "POST {base}/auth"
            capture = ["missing = nope"]

            [[requests]]
            name = "me"
            request = "GET {base}/me"
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
            request = "GET http://example.com/1"
            [[requests]]
            name = "a_b"
            request = "GET http://example.com/2"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_a_b()", script)
        self.assertIn("step_a_b_2()", script)

    def test_description_is_emitted(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
            description = "health check"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("http_step 'ping' 'GET'", script)
        self.assertIn("'health check'", script)

    def test_description_is_printed_after_http_start_line(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo"
            description = "health check"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        start = res.stdout.index("[ping] GET")
        desc = res.stdout.index("# health check")
        detail = res.stdout.index("> GET /echo")
        self.assertLess(start, desc)
        self.assertLess(desc, detail)

    def test_random_uuid_placeholders(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "create"
            request = "POST http://example.com/items/${random.UUID_HEX}"
            headers = ["X-Request-Id: ${random.UUID}"]
            body = '{"request_id":"${random.UUID}"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn("uuid()", script)
        self.assertIn("uuid_hex()", script)
        self.assertIn('url="http://example.com/items/$(uuid_hex)"', script)
        self.assertIn('X-Request-Id: $(uuid)', script)
        self.assertIn('{"request_id":"$(uuid)"}', script)

    def test_generated_uuid_helpers_return_valid_values(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
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
            request = "POST http://example.com/auth?token=url-secret&keep=ok"
            headers = ["Authorization: Bearer header-secret"]
            body_form = ["user = alice", "password = body-secret"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("mask()", script)
        self.assertIn("mask_lines()", script)
        self.assertIn('$(mask "$url")', script)
        self.assertNotIn('printf "> %s\\n" "$header"', script)
        self.assertIn('printf "%s" "$body_log" | jq_or_cat | prefix_lines "> "', script)
        self.assertIn('printf "%s" "$body_log" | prefix_lines "> "', script)
        self.assertIn("sed -E", script)
        self.assertNotIn("perl -pe", script)
        self.assertIn("MASK_KEYS_DEFAULT='[aA]uthorization|[cC]ookie", script)
        self.assertIn("[sS]et-[cC]ookie", script)
        self.assertNotIn("mask_key_pattern()", script)
        self.assertIn('tee -a "$trace_file"', script)
        self.assertIn("mask_lines", script)

    def test_generated_mask_helper_masks_simple_values(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"HTTPFLOW_NO_MASK=; source {script_path} >/dev/null || true; "
                    "mask 'token=abc'; "
                    "mask 'Token=ABC'; "
                    "mask 'Authorization: Bearer secret'; "
                    "mask 'authorization: Bearer 06a84af6-4f9f-4b84-bfe2-529e310eea12'; "
                    "mask 'Set-Cookie: session=set-cookie-secret'; "
                    "mask '{\"password\":\"p\",\"user\":\"u\"}'",
                ],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("token=***", res.stdout)
        self.assertIn("Token=***", res.stdout)
        self.assertIn("Authorization: ***", res.stdout)
        self.assertIn("authorization: ***", res.stdout)
        self.assertIn("Set-Cookie: ***", res.stdout)
        self.assertIn('"password":***', res.stdout)
        self.assertNotIn("abc", res.stdout)
        self.assertNotIn("ABC", res.stdout)
        self.assertNotIn("secret", res.stdout)
        self.assertNotIn("set-cookie-secret", res.stdout)
        self.assertNotIn("06a84af6", res.stdout)

    def test_generated_mask_helper_masks_header_values_with_symbols(self):
        """Bash mask should hide whole HTTP header values containing symbols."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)
        cases = [
            "Authorization: Bearer abc.def/ghi+jkl=mn_op-qr:st;uv,wx yz",
            "authorization: Basic abc+def/ghi==",
            "Cookie: sid=abc.def/ghi+jkl=mn_op-qr:st;uv,wx yz; theme=dark",
            "Set-Cookie: session=abc.def/ghi+jkl=mn_op-qr:st;uv,wx yz; Path=/; HttpOnly",
            "X-Api-Key: abc.def/ghi+jkl=mn_op-qr:st;uv,wx yz",
            "password: abc.def/ghi+jkl=mn_op-qr:st;uv,wx yz",
        ]

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            quoted = " ".join(shlex.quote(case) for case in cases)
            res = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {shlex.quote(str(script_path))} >/dev/null || true; "
                    f"printf '%s\n' {quoted} | mask_lines",
                ],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        lines = res.stdout.splitlines()
        self.assertEqual(len(lines), len(cases))
        for line in lines:
            self.assertRegex(line, r"^(Authorization|authorization|Cookie|Set-Cookie|X-Api-Key|password): \*\*\*$")
        for raw in cases:
            self.assertNotIn(raw.split(": ", 1)[1], res.stdout)

    def test_generated_mask_helper_masks_comma_separated_header_values(self):
        """Bash mask should hide whole comma-separated sensitive header values."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {shlex.quote(str(script_path))} >/dev/null || true; "
                    "mask 'Authorization: Bearer a,b'; "
                    "mask 'Cookie: a=b, c=d'; "
                    "printf '%s\n' "
                    "'> Authorization: Bearer a,b' "
                    "'> Cookie: a=b, c=d' "
                    "'< Set-Cookie: sid=a,b; Path=/' "
                    "'X-Api-Key: key-a,key-b' "
                    "'Accept: application/json, text/plain' "
                    "| mask_lines",
                ],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("Authorization: ***", res.stdout)
        self.assertIn("Cookie: ***", res.stdout)
        self.assertIn("> Authorization: ***", res.stdout)
        self.assertIn("> Cookie: ***", res.stdout)
        self.assertIn("< Set-Cookie: ***", res.stdout)
        self.assertIn("X-Api-Key: ***", res.stdout)
        self.assertIn("Accept: application/json, text/plain", res.stdout)
        self.assertNotIn("Bearer a,b", res.stdout)
        self.assertNotIn("a=b, c=d", res.stdout)
        self.assertNotIn("sid=a,b", res.stdout)
        self.assertNotIn("key-a,key-b", res.stdout)

    def test_mask_lines_masks_curl_like_output(self):
        """mask_lines should mask sensitive fields in piped curl-like output."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
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

    def test_mask_lines_masks_json_values_with_spaces(self):
        """Bash mask should hide JSON values containing multiple words."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
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
                    "printf '%s\\n' "
                    "\"{\\\"password\\\": \\\"my secret token with spaces\\\"}\" "
                    "\"{\\\"token\\\": \\\"foo bar baz qux\\\"}\" "
                    "\"{\\\"normal\\\": \\\"keep this value\\\"}\" "
                    "| mask_lines",
                ],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn('"password": ***', res.stdout)
        self.assertIn('"token": ***', res.stdout)
        self.assertIn('"normal": "keep this value"', res.stdout)
        self.assertNotIn("my secret token with spaces", res.stdout)
        self.assertNotIn("foo bar baz qux", res.stdout)

    def test_mask_lines_masks_values_with_commas(self):
        """Comma inside non-header values must not terminate masking."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {shlex.quote(str(script_path))} >/dev/null || true; "
                    "printf '%s\\n' "
                    "'\"Authorization\":\"Bearer abc,def\"' "
                    "'token=abc,def&key=value' "
                    "'password: abc,def/ghi' "
                    "| mask_lines",
                ],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn('"Authorization":***', res.stdout)
        self.assertIn("token=***&key=value", res.stdout)
        self.assertIn("password: ***", res.stdout)
        self.assertNotIn("abc,def", res.stdout)
        self.assertNotIn("abc", res.stdout)

    def test_mask_extra_env_var_in_bash_script(self):
        """HTTPFLOW_MASK_EXTRA env var extends masking keys in bash script."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash", "-c",
                    f"HTTPFLOW_MASK_EXTRA='[tT]race-id' HTTPFLOW_NO_MASK=; source {script_path} >/dev/null || true; "
                    "mask 'trace-id=secret'; "
                    "mask 'Trace-id=Secret'; "
                    "mask 'token=foo'; "
                    "mask 'Authorization: Bearer bar'",
                ],
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("trace-id=***", res.stdout)
        self.assertIn("Trace-id=***", res.stdout)
        self.assertIn("token=***", res.stdout)
        self.assertIn("Authorization: ***", res.stdout)
        self.assertNotIn("secret", res.stdout)
        self.assertNotIn("Secret", res.stdout)
        self.assertNotIn("foo", res.stdout)
        self.assertNotIn("Bearer bar", res.stdout)

    def test_no_mask_env_var_disables_masking_in_bash_script(self):
        """HTTPFLOW_NO_MASK disables masking in generated bash output."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                [
                    "bash", "-c",
                    f"HTTPFLOW_NO_MASK=1; source {script_path} >/dev/null || true; "
                    "mask 'token=foo'; "
                    "printf '%s\n' 'Authorization: Bearer bar' | mask_lines",
                ],
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("token=foo", res.stdout)
        self.assertIn("Authorization: Bearer bar", res.stdout)
        self.assertNotIn("***", res.stdout)

    @unittest.skipUnless(shutil.which("jq"), "jq required")
    def test_pretty_json_argument_formats_bash_response_body(self):
        """Generated bash accepts --pretty-json at runtime."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("jq_or_cat()", script)
        self.assertIn("--pretty-json", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path), "--pretty-json"], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn('{\n  "ok": true\n}', res.stdout)

    def test_no_mask_argument_disables_masking_in_bash_script(self):
        """Generated bash accepts --no-mask at runtime."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            request = "GET {base}/echo"
            headers = ["Authorization: Bearer secret_token"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("--no-mask", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path), "--no-mask"], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("Bearer secret_token", res.stdout)
        self.assertNotIn("***", res.stdout)

    def test_default_vars_embedded_in_bash_script(self):
        """-v K=V in generate --format bash embeds default VAR_* values."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "echo"
            request = "POST http://example.com/${var.env}?id=${var.id}"
            body = '{"name":"${var.user}","id":"${var.id}"}'
        """)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(
                wf,
                default_vars={"env": "prod", "user": "alice"},
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

            self.assertIn('[[ -z "${VAR_ENV:-}" ]] && VAR_ENV=\'prod\'', script)
            self.assertIn('[[ -z "${VAR_USER:-}" ]] && VAR_USER=\'alice\'', script)
            # Ensure they are defined *before* main / step functions so they act as defaults
            defaults_pos = script.find("# ─── defaults")
            steps_pos = script.find("# ─── step functions")
            self.assertGreater(defaults_pos, -1)
            self.assertGreater(steps_pos, -1)
            self.assertLess(defaults_pos, steps_pos)


    def test_required_var_check_fails_with_export_hint(self):
        """Generated bash fails early when required ${var.*} env is empty."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/${var.user}"
        """)
        script = self._generate_and_check(toml)
        self.assertIn('[[ -z "${VAR_USER:-}" ]] && {', script)
        self.assertIn('Export it before running: export VAR_USER=<value>', script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=10
            )

        self.assertEqual(res.returncode, 1)
        self.assertIn("error: missing required variable: user", res.stderr)
        self.assertIn("export VAR_USER=<value>", res.stderr)

    def test_slash_in_step_name_runs_successfully(self):
        """Step names containing '/' must not break temp file creation."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "api/v1/me"
            request = "GET {base}/me"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_api_v1_me()", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=10
            )
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("[api/v1/me] GET", res.stdout)


    def test_blank_line_env_inserts_lines_between_steps(self):
        """HTTPFLOW_BLANK_LINE behaves like --blank-line for generated bash."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "one"
            request = "GET {base}/echo"
            [[requests]]
            name = "two"
            request = "GET {base}/echo"
        """)
        script = self._generate_and_check(toml)
        self.assertEqual(script.count('print_blank_lines "${HTTPFLOW_BLANK_LINE:-0}"'), 1)
        self.assertIn('print_blank_lines "${HTTPFLOW_BLANK_LINE:-0}"\n\n    echo "==>', script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", "-c", f"HTTPFLOW_BLANK_LINE=2 bash {script_path}"],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("\n\n\n==>", res.stdout)

    def test_blank_line_arg_inserts_lines_between_steps(self):
        """--blank-line N behaves like HTTPFLOW_BLANK_LINE for generated bash."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "one"
            request = "GET {base}/echo"
            [[requests]]
            name = "two"
            request = "GET {base}/echo"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("--blank-line", script)
        self.assertIn("usage: $0 [--pretty-json] [--no-mask] [--blank-line N]", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path), "--blank-line", "2"],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("\n\n\n==>", res.stdout)

    def test_blank_line_arg_equals_form(self):
        """--blank-line=N is accepted in addition to the space-separated form."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "one"
            request = "GET {base}/echo"
            [[requests]]
            name = "two"
            request = "GET {base}/echo"
        """)
        script = self._generate_and_check(toml)
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path), "--blank-line=3"],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("\n\n\n\n==>", res.stdout)

    def test_blank_line_arg_invalid_value_fails(self):
        """Non-numeric --blank-line is rejected with a non-zero exit."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "one"
            request = "GET {base}/echo"
        """)
        script = self._generate_and_check(toml)
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path), "--blank-line", "abc"],
                capture_output=True, text=True, timeout=10,
            )

        self.assertNotEqual(res.returncode, 0)
        self.assertIn("non-negative integer", res.stderr)

    def test_blank_line_arg_overrides_env(self):
        """--blank-line takes precedence over HTTPFLOW_BLANK_LINE env var."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "one"
            request = "GET {base}/echo"
            [[requests]]
            name = "two"
            request = "GET {base}/echo"
        """)
        script = self._generate_and_check(toml)
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", "-c", f"HTTPFLOW_BLANK_LINE=1 bash {script_path} --blank-line 3"],
                capture_output=True, text=True, timeout=10,
            )

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        # Arg (3) wins over env (1): the first step is preceded by 3 blank
        # lines, so the output must start with "\n\n\n==>" (3 newlines + banner).
        self.assertTrue(res.stdout.startswith("\n\n\n==>"),
                        msg="expected 3 leading blank lines from --blank-line 3")
        # Sanity: env value 1 alone would produce only 1 leading blank line.
        self.assertFalse(res.stdout.startswith("\n==>"))

    def test_default_vars_overridable_at_runtime(self):
        """Embedded default vars can be overridden by exporting before running."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/${var.env}"
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

    def test_until_function_name_no_collision_with_normal_step(self):
        """until _attempt suffix must not collide with another step's function name."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "poll_attempt"
            request = "GET http://example.com/other"
            [[requests]]
            name = "poll"
            request = "GET http://example.com/poll"
            capture = ["status = status"]
            until = ["condition = ${status} == Active", "interval = 0", "max_attempts = 3"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_poll_attempt()", script)
        self.assertIn("step_poll_attempt_2()", script)
        idx1 = script.index("step_poll_attempt()")
        idx3 = script.index("step_poll_attempt_2()")
        self.assertLess(idx1, idx3)

    def test_sleep_step_name_is_safe_from_shell_injection(self):
        """SLEEP step name with shell metacharacters must not be expanded."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "`id`$(echo injected)&"
            request = "SLEEP 0.01"
            description = "); echo injected2"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("`id`$(echo injected)&", script)
        self.assertNotIn("echo \\\"#>", script)
        self.assertIn('printf "==> %s [%s] SLEEP', script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertNotIn("uid=", res.stdout, msg="shell expansion of $(id) must not happen")
        self.assertNotIn("uid=", res.stdout, msg="command substitution must not happen")

    def test_default_var_containing_brace_generates_valid_bash(self):
        """Default var value containing } must be escaped properly."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(
                wf,
                default_vars={"env": "prod}", "user": "alice{foo}"},
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
            # Also verify the default value is actually stored correctly
            res = subprocess.run(
                ["bash", "-c",
                 f"source {script_path} >/dev/null || true; printf '%s\\n' \"${{VAR_ENV}}\" \"${{VAR_USER}}\""],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("prod}", res.stdout)
            self.assertIn("alice{foo}", res.stdout)

    def test_body_file_generation_syntax(self):
        """body_file generated script passes bash -n."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "upload"
            request = "PUT http://example.com/upload"
            body_file = "/tmp/data.bin"
        """)
        script = self._generate_and_check(toml)
        self.assertNotIn("body_kind=", script)
        self.assertIn('--data-binary "@/tmp/data.bin"', script)
        # body_log is no longer pre-built in the step function; it is derived
        # inside http_step from the parsed curl_command. The body-flag value
        # (@/tmp/data.bin) is emitted verbatim by http_step, so no synthetic
        # "Note: binary body from file:" string is generated anymore.
        self.assertNotIn("Note: binary body from file:", script)
        self.assertNotIn('echo "# body_file:', script)

    def test_body_file_sends_exact_bytes(self):
        """body_file sends exact file content via --data-binary."""
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.seen_body_bytes = b""
        _CaptureHandler.seen_content_type = ""

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_path = tmp_path / "data.bin"
            data_path.write_bytes(b"\x00\x01\x02\xffbinary-data")

            toml = textwrap.dedent(f"""
                [[requests]]
                name = "upload"
                request = "PUT {base}/upload"
                body_file = "{data_path}"
            """)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf)

            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            syntax = subprocess.run(
                ["bash", "-n", str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(syntax.returncode, 0, msg=syntax.stderr)

            res = subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
            self.assertEqual(_CaptureHandler.seen_body_bytes, b"\x00\x01\x02\xffbinary-data")
            self.assertIn("application/octet-stream", _CaptureHandler.seen_content_type)

    def test_body_file_with_template_path_runtime(self):
        """body_file with ${var.data_path} resolves at runtime via env var."""
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.seen_body_bytes = b""
        _CaptureHandler.seen_content_type = ""

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_path = tmp_path / "data.bin"
            data_path.write_bytes(b"\x00\x01\x02\xffbinary-data")

            toml = textwrap.dedent(f"""
                [[requests]]
                name = "upload"
                request = "PUT {base}/upload"
                body_file = "${{var.data_path}}"
            """)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf)

            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            syntax = subprocess.run(
                ["bash", "-n", str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(syntax.returncode, 0, msg=syntax.stderr)

            res = subprocess.run(
                ["bash", str(script_path)],
                env={**dict(__import__("os").environ), "VAR_DATA_PATH": str(data_path)},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
            self.assertEqual(_CaptureHandler.seen_body_bytes, b"\x00\x01\x02\xffbinary-data")
            self.assertIn("application/octet-stream", _CaptureHandler.seen_content_type)

    def test_body_file_content_type_auto_added(self):
        """Content-Type: application/octet-stream is added automatically for body_file."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "upload"
            request = "PUT http://example.com/upload"
            body_file = "/tmp/data.bin"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("Content-Type: application/octet-stream", script)

    def test_body_file_respects_user_content_type(self):
        """User-specified Content-Type for body_file is not overridden."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "upload"
            request = "PUT http://example.com/upload"
            headers = ["Content-Type: image/png"]
            body_file = "/tmp/data.bin"
        """)
        script = self._generate_and_check(toml)
        # The user-specified Content-Type is the only one passed to curl.
        # It appears once inside the curl_command heredoc.
        self.assertEqual(script.count("Content-Type:"), 1)
        self.assertIn('-H "Content-Type: image/png"', script)
        self.assertNotIn("application/octet-stream", script)

    def test_body_file_missing_fails_at_runtime(self):
        """body_file referencing a non-existent path fails with clear error."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml = textwrap.dedent("""
                [[requests]]
                name = "upload"
                request = "PUT http://example.com/upload"
                body_file = "/nonexistent/file.bin"
            """)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf)

            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            syntax = subprocess.run(
                ["bash", "-n", str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(syntax.returncode, 0, msg=syntax.stderr)

            res = subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("body_file not found", res.stderr)

    def test_body_multipart_fields(self):
        """body_multipart with regular text fields uses --form-string."""
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.multipart_fields = {}
        _CaptureHandler.multipart_files = []
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "mform"
            request = "POST {base}/upload"
            body_multipart = [
                "name = alice",
                "email = alice@example.com",
            ]
        """)
        script = self._generate_and_check(toml)
        self.assertNotIn("body_kind=", script)
        self.assertIn("--form-string", script)
        self.assertIn('--form-string "name=alice"', script)
        self.assertIn('--form-string "email=alice@example.com"', script)
        # Part info is no longer pre-built in the step function; http_step
        # derives it from the parsed curl_command. The old prebuilt form
        # appended to body_log inside the step using
        # `body_log="${body_log}\n..."` (literal backslash-n inside the
        # quotes); http_step uses `$'\n'` instead, so this literal append
        # pattern must be absent. The synthetic "(multipart)" header is
        # gone too — each --form-string value is emitted verbatim.
        self.assertNotIn('echo "# multipart field:', script)
        self.assertNotIn('body_log="${body_log}\\n', script)
        self.assertNotIn("(multipart)", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        # Runtime body log (built from expanded curl args) appears after the
        # banner in the request-body section. Each --form-string value is
        # shown verbatim, one per line.
        self.assertIn("> name=alice", res.stdout)
        self.assertIn("> email=alice@example.com", res.stdout)
        # And the fields were actually sent.
        self.assertEqual(_CaptureHandler.multipart_fields["name"], "alice")
        self.assertEqual(_CaptureHandler.multipart_fields["email"], "alice@example.com")

    def test_body_multipart_literal_at_sign(self):
        """body_multipart with @@value sends literal @value via --form-string."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "mform"
            request = "POST http://example.com/upload"
            body_multipart = [
                "greeting = @@hello",
            ]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("--form-string", script)
        self.assertIn('--form-string "greeting=@hello"', script)

    def test_body_multipart_file_field(self):
        """body_multipart file field uses curl -F with @path."""
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.multipart_fields = {}
        _CaptureHandler.multipart_files = []
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_path = tmp_path / "data.bin"
            data_path.write_bytes(b"file-bytes")
            toml = textwrap.dedent(f"""
                [[requests]]
                name = "mform"
                request = "POST {base}/upload"
                body_multipart = [
                    "file = @{data_path}; filename=upload.dat; type=image/png",
                ]
            """)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf)
            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            syntax = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True, timeout=10)
            self.assertEqual(syntax.returncode, 0, msg=syntax.stderr + "\n" + script)

            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertNotIn("body_kind=", script)
        self.assertIn("-F ", script)
        self.assertIn("upload.dat", script)
        self.assertIn("image/png", script)
        # File part info is no longer pre-built in the step function; the
        # -F argument value is emitted verbatim by http_step (no synthetic
        # header, no computed byte count).
        self.assertNotIn('echo "# multipart file:', script)
        self.assertNotIn('bytes=${file_size}', script)
        # Runtime log: the -F value appears after the banner, verbatim as
        # curl received it (path and filename still wrapped in quotes per
        # curl -F quoting rules).
        self.assertIn(f'> file=@"{data_path}";filename="upload.dat";type=image/png', res.stdout)
        # And the file was actually sent.
        self.assertEqual(len(_CaptureHandler.multipart_files), 1)
        self.assertEqual(_CaptureHandler.multipart_files[0]["filename"], "upload.dat")
        self.assertEqual(_CaptureHandler.multipart_files[0]["data"], b"file-bytes")

    def test_body_multipart_content_type_is_error(self):
        """body_multipart with user-specified Content-Type raises ValueError."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "mform"
            request = "POST http://example.com/upload"
            headers = ["Content-Type: multipart/form-data"]
            body_multipart = [
                "name = alice",
            ]
        """)
        with self.assertRaises(ValueError) as ctx:
            self._generate_and_check(toml)
        self.assertIn("Content-Type is set automatically by curl", str(ctx.exception))

    def test_body_multipart_missing_file_fails_runtime(self):
        """body_multipart referencing non-existent file fails with clear error."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml = textwrap.dedent("""
                [[requests]]
                name = "mform"
                request = "POST http://example.com/upload"
                body_multipart = [
                    "file = @/nonexistent/file.bin",
                ]
            """)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf)

            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            syntax = subprocess.run(
                ["bash", "-n", str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(syntax.returncode, 0, msg=syntax.stderr)

            res = subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("multipart file not found", res.stderr)

    def test_body_multipart_template_expansion(self):
        """body_multipart fields with ${var.*}, ${random.*}, ${time.*} expand at runtime."""
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.multipart_fields = {}
        _CaptureHandler.multipart_files = []

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml = textwrap.dedent(f"""
                [[requests]]
                name = "mform"
                request = "POST {base}/upload"
                body_multipart = [
                    "name = ${{var.username}}",
                    "email = test@email.com",
                    "req_id = ${{random.UUID_HEX}}",
                    "ts = ${{time.DATE_ISO}}",
                ]
            """)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf)
            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")

            res = subprocess.run(
                ["bash", str(script_path)],
                env={**dict(__import__("os").environ), "VAR_USERNAME": "alice"},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)

            # Verify fields were expanded, not sent as literals
            self.assertEqual(_CaptureHandler.multipart_fields["name"], "alice")
            self.assertEqual(_CaptureHandler.multipart_fields["email"], "test@email.com")
            # UUID_HEX should have expanded to a 32-char hex string
            self.assertRegex(_CaptureHandler.multipart_fields["req_id"], r"^[0-9a-f]{32}$")
            # time.DATE_ISO should expand to ISO-ish timestamp
            self.assertRegex(_CaptureHandler.multipart_fields["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
            # Verify the script does NOT contain the original placeholders (they were expanded at gen time)
            self.assertNotIn("${var.username}", script)  # TOML placeholder gone
            self.assertIn("${VAR_USERNAME}", script)  # Becomes $VAR_USERNAME for shell expansion

    def test_body_multipart_with_file_field(self):
        """body_multipart with a file field and text fields containing placeholders."""
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.multipart_fields = {}
        _CaptureHandler.multipart_files = []

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            file_path = tmp_path / "upload.dat"
            file_path.write_bytes(b"file-content")

            toml = textwrap.dedent(f"""
                [[requests]]
                name = "mform"
                request = "POST {base}/upload"
                body_multipart = [
                    "username = ${{var.username}}",
                    "avatar = @{file_path}; filename=avatar.png; type=image/png",
                ]
            """)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf)
            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")

            res = subprocess.run(
                ["bash", str(script_path)],
                env={**dict(__import__("os").environ), "VAR_USERNAME": "bob"},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)

            self.assertEqual(_CaptureHandler.multipart_fields["username"], "bob")
            self.assertEqual(len(_CaptureHandler.multipart_files), 1)
            self.assertEqual(_CaptureHandler.multipart_files[0]["filename"], "avatar.png")
            self.assertEqual(_CaptureHandler.multipart_files[0]["data"], b"file-content")

    def test_body_multipart_file_no_filename_uses_basename(self):
        """body_multipart file field without explicit filename uses basename of path."""
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.multipart_fields = {}
        _CaptureHandler.multipart_files = []

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            file_path = tmp_path / "upload.dat"
            file_path.write_bytes(b"file-content")

            toml = textwrap.dedent(f"""
                [[requests]]
                name = "mform"
                request = "POST {base}/upload"
                body_multipart = [
                    "doc = @{file_path}",
                ]
            """)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf)
            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")

            res = subprocess.run(
                ["bash", str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)

            self.assertEqual(len(_CaptureHandler.multipart_files), 1)
            self.assertEqual(_CaptureHandler.multipart_files[0]["filename"], "upload.dat")
            self.assertEqual(_CaptureHandler.multipart_files[0]["data"], b"file-content")

    def test_multipart_body_log_appears_after_banner(self):
        """Multipart part info is printed inside the step's section (after ==>),
        not before the banner where it would look like the previous step's output.
        """
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.multipart_fields = {}
        _CaptureHandler.multipart_files = []

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            file_path = tmp_path / "upload.dat"
            file_path.write_bytes(b"file-content")

            toml = textwrap.dedent(f"""
                [[requests]]
                name = "first"
                request = "POST {base}/upload"
                body = "hello"

                [[requests]]
                name = "second"
                request = "POST {base}/upload"
                body_multipart = [
                    "title = test upload",
                    "file = @{file_path}; filename=upload.dat; type=image/png",
                ]
            """)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf)
            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")

            res = subprocess.run(
                ["bash", str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)

            # The multipart body-log values must appear AFTER the second
            # step's ==> banner, not before it (which would attach them to
            # the first step's output). The banner looks like:
            #   "==> <timestamp> [second] POST <url>"
            banner_pos = res.stdout.find("[second] POST")
            self.assertGreater(banner_pos, 0, "second step banner not found")
            # Each body-flag value is emitted verbatim by http_step, so the
            # --form-string value and the -F value should both appear after
            # the banner (not before, which would misattach to step "first").
            self.assertGreater(res.stdout.find("title=test upload"), banner_pos,
                               "multipart field value appears before the step banner")
            self.assertGreater(
                res.stdout.find('file=@"' + str(file_path)), banner_pos,
                "multipart file value appears before the step banner"
            )

    def test_body_file_capture_request_body_is_error(self):
        """capture request.body with body_file raises ValueError."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "upload"
            request = "PUT http://example.com/upload"
            body_file = "/tmp/data.bin"
            capture = ["sent = request.body"]
        """)
        with self.assertRaises(ValueError) as ctx:
            self._generate_and_check(toml)
        self.assertIn("cannot capture request.body with body_file", str(ctx.exception))

    def test_body_multipart_capture_request_body_is_error(self):
        """capture request.body with body_multipart raises ValueError."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "mform"
            request = "POST http://example.com/upload"
            body_multipart = [
                "name = alice",
            ]
            capture = ["sent = request.body"]
        """)
        with self.assertRaises(ValueError) as ctx:
            self._generate_and_check(toml)
        self.assertIn("cannot capture request.body", str(ctx.exception))

    def test_capture_request_body_json_generates_helper(self):
        """capture request.body.<path> emits capture_request_body_json call."""
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "create"
            request = "POST {base}/auth"
            headers = ["Content-Type: application/json"]
            body = '{{"date":{{"time_DATE_ISO":"2026-06-24"}}}}'
            capture = ["iso = request.body.date.time_DATE_ISO"]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("capture_request_body_json", script)
        self.assertIn("capture_request_body_json 'VAR_ISO' 'iso' 'request.body.date.time_DATE_ISO'", script)
        self.assertIn("'.[\"date\"]?[\"time_DATE_ISO\"]?'", script)
        # http_step must expose HF_BODY_LOG so the capture call (which runs
        # after http_step returns, under `set -u`) does not hit an unbound
        # variable. Regression: previously needs_body_log_var only matched the
        # exact `request.body` source, missing `request.body.<path>`.
        self.assertIn('HF_BODY_LOG="$body_log"', script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("* capture iso = '2026-06-24'", res.stdout)

    def test_body_file_capture_request_body_json_is_error(self):
        """capture request.body.<path> with body_file raises ValueError."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "upload"
            request = "PUT http://example.com/upload"
            body_file = "/tmp/data.bin"
            capture = ["sent = request.body.foo"]
        """)
        with self.assertRaises(ValueError) as ctx:
            self._generate_and_check(toml)
        self.assertIn("cannot capture request.body with body_file", str(ctx.exception))

    def test_body_multipart_capture_request_body_json_is_error(self):
        """capture request.body.<path> with body_multipart raises ValueError."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "mform"
            request = "POST http://example.com/upload"
            body_multipart = [
                "name = alice",
            ]
            capture = ["sent = request.body.foo"]
        """)
        with self.assertRaises(ValueError) as ctx:
            self._generate_and_check(toml)
        self.assertIn("cannot capture request.body", str(ctx.exception))

    def test_body_multipart_tab_in_name_raises(self):
        """multipart field name with tab raises ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_content = '[[requests]]\nname = "mform"\nrequest = "POST http://example.com/upload"\nbody_multipart = ["na\\tme = value"]\n'
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml_content, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            with self.assertRaises(ValueError) as ctx:
                bash_generator.generate(wf)
            self.assertIn("must not contain tabs or newlines", str(ctx.exception))

    def test_body_multipart_tab_in_value_raises(self):
        """multipart field value with tab raises ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_content = '[[requests]]\nname = "mform"\nrequest = "POST http://example.com/upload"\nbody_multipart = ["name = val\\tue"]\n'
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml_content, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            with self.assertRaises(ValueError) as ctx:
                bash_generator.generate(wf)
            self.assertIn("must not contain tabs or newlines", str(ctx.exception))

    def test_body_multipart_tab_in_file_path_raises(self):
        """multipart file path with tab raises ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_content = '[[requests]]\nname = "mform"\nrequest = "POST http://example.com/upload"\nbody_multipart = ["file = @/tmp/da\\tta.bin"]\n'
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml_content, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            with self.assertRaises(ValueError) as ctx:
                bash_generator.generate(wf)
            self.assertIn("must not contain tabs or newlines", str(ctx.exception))

    def test_body_multipart_double_quote_in_file_name_raises(self):
        """multipart file part name with double quote raises ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_content = '[[requests]]\nname = "mform"\nrequest = "POST http://example.com/upload"\nbody_multipart = ["na\\"me = @/tmp/test.bin; type=application/octet-stream"]\n'
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml_content, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            with self.assertRaises(ValueError) as ctx:
                bash_generator.generate(wf)
            self.assertIn("must not contain double quotes", str(ctx.exception))

    def test_embed_files_body_file(self):
        """--embed-files embeds body_file content as base64 in the script."""
        base = f"http://127.0.0.1:{self.port}"
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "data.bin"
            data_path.write_bytes(b"embedded-binary-data\x00\xff")

            toml = textwrap.dedent(f"""
                [[requests]]
                name = "upload"
                request = "PUT {base}/upload"
                body_file = "{data_path}"
            """)
            toml_path = Path(tmp) / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf, embed_files=True, toml_path=str(toml_path))

            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            syntax = subprocess.run(
                ["bash", "-n", str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(syntax.returncode, 0, msg=syntax.stderr)

            self.assertIn("_hf_b64decode", script)
            self.assertIn("__HF_EMBED_step_upload_body", script)
            self.assertIn("readonly __HF_EMBED_step_upload_body=", script)
            self.assertIn("printf '%s' \"${__HF_EMBED_step_upload_body}\" | _hf_b64decode > \"$decode_file\"", script)
            self.assertIn('--data-binary "@$decode_file"', script)

    def test_embed_files_body_file_runtime(self):
        """--embed-files body_file decodes and sends the correct content at runtime."""
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.seen_body_bytes = b""
        _CaptureHandler.seen_content_type = ""
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "data.bin"
            expected = b"\x00\x01\x02\xffembedded-data"
            data_path.write_bytes(expected)

            toml = textwrap.dedent(f"""
                [[requests]]
                name = "upload"
                request = "PUT {base}/upload"
                body_file = "{data_path}"
            """)
            toml_path = Path(tmp) / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf, embed_files=True, toml_path=str(toml_path))

            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
            self.assertEqual(_CaptureHandler.seen_body_bytes, expected)
            self.assertIn("application/octet-stream", _CaptureHandler.seen_content_type)

    def test_embed_files_multipart_file(self):
        """--embed-files embeds multipart file content."""
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.seen_body_bytes = b""
        with tempfile.TemporaryDirectory() as tmp:
            avatar_path = Path(tmp) / "avatar.png"
            avatar_path.write_bytes(b"fake-png-content")

            toml = textwrap.dedent(f"""
                [[requests]]
                name = "mform"
                request = "POST {base}/upload"
                body_multipart = [
                    "name = alice",
                    "file = @{avatar_path}; filename=avatar.png; type=image/png",
                ]
            """)
            toml_path = Path(tmp) / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf, embed_files=True, toml_path=str(toml_path))

            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            syntax = subprocess.run(
                ["bash", "-n", str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(syntax.returncode, 0, msg=syntax.stderr)

            self.assertIn("_hf_b64decode", script)
            self.assertIn("__HF_EMBED_step_mform_mp1", script)
            # The decode temp file path should replace the original path in the multipart TSV
            self.assertNotIn(str(avatar_path), script)

    def test_embed_files_multipart_multiple_files(self):
        """--embed-files embeds multiple multipart files correctly."""
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.multipart_fields = {}
        _CaptureHandler.multipart_files = []
        with tempfile.TemporaryDirectory() as tmp:
            first_path = Path(tmp) / "first.bin"
            first_path.write_bytes(b"AAAA-first")
            second_path = Path(tmp) / "second.bin"
            second_path.write_bytes(b"BBBB-second")

            toml = textwrap.dedent(f"""
                [[requests]]
                name = "mform"
                request = "POST {base}/upload"
                body_multipart = [
                    "first = @{first_path}; filename=first.bin; type=application/octet-stream",
                    "second = @{second_path}; filename=second.bin; type=application/octet-stream",
                ]
            """)
            toml_path = Path(tmp) / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf, embed_files=True, toml_path=str(toml_path))

            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)

            self.assertEqual(len(_CaptureHandler.multipart_files), 2)
            # First file
            self.assertEqual(_CaptureHandler.multipart_files[0]["name"], "first")
            self.assertEqual(_CaptureHandler.multipart_files[0]["filename"], "first.bin")
            self.assertEqual(_CaptureHandler.multipart_files[0]["data"], b"AAAA-first")
            # Second file
            self.assertEqual(_CaptureHandler.multipart_files[1]["name"], "second")
            self.assertEqual(_CaptureHandler.multipart_files[1]["filename"], "second.bin")
            self.assertEqual(_CaptureHandler.multipart_files[1]["data"], b"BBBB-second")

    def test_embed_files_placeholder_path_skips_embed(self):
        """--embed-files skips embedding when path contains placeholders."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "upload"
            request = "PUT http://example.com/upload"
            body_file = "${var.data_path}"
        """)
        with tempfile.TemporaryDirectory() as tmp:
            toml_path = Path(tmp) / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            with self.assertWarns(UserWarning) as w:
                script = bash_generator.generate(wf, embed_files=True, toml_path=str(toml_path))
            self.assertIn("contains placeholders, skipping embed", str(w.warning))

            self.assertNotIn("__HF_EMBED_", script)
            self.assertIn("${VAR_DATA_PATH}", script)
            self.assertNotIn("body=\"$decode_file\"", script)

    def test_embed_files_not_enabled_omits_b64decode_helper(self):
        """Without --embed-files, _hf_b64decode must not appear."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "ping"
            request = "GET http://example.com/ping"
        """)
        script = self._generate_and_check(toml)
        self.assertNotIn("_hf_b64decode", script)

    def test_embed_files_body_file_missing_fails_at_gen_time(self):
        """--embed-files raises FileNotFoundError when the file does not exist."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "upload"
            request = "PUT http://example.com/upload"
            body_file = "/nonexistent/embed-test-file.bin"
        """)
        with tempfile.TemporaryDirectory() as tmp:
            toml_path = Path(tmp) / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            with self.assertRaises(FileNotFoundError):
                bash_generator.generate(wf, embed_files=True, toml_path=str(toml_path))

    def test_embed_files_multipart_file_missing_fails_at_gen_time(self):
        """--embed-files for multipart raises FileNotFoundError when the file does not exist."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "mform"
            request = "POST http://example.com/upload"
            body_multipart = [
                "file = @/nonexistent/missing-file.bin",
            ]
        """)
        with tempfile.TemporaryDirectory() as tmp:
            toml_path = Path(tmp) / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            with self.assertRaises(FileNotFoundError):
                bash_generator.generate(wf, embed_files=True, toml_path=str(toml_path))

    def test_embed_files_with_until_step(self):
        """--embed-files with an until step embeds the file correctly."""
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.poll_count = 0
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "data.bin"
            data_path.write_bytes(b"poll-data")

            toml = textwrap.dedent(f"""
                [[requests]]
                name = "poll"
                request = "PUT {base}/poll"
                body_file = "{data_path}"
                capture = ["status = status"]
                until = ["condition = ${{status}} == Active", "interval = 0", "max_attempts = 3"]
            """)
            toml_path = Path(tmp) / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf, embed_files=True, toml_path=str(toml_path))

            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            syntax = subprocess.run(
                ["bash", "-n", str(script_path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(syntax.returncode, 0, msg=syntax.stderr)

            self.assertIn("__HF_EMBED_step_poll_body", script)
            self.assertIn("_hf_b64decode", script)

    def test_embed_files_relative_path_resolved_via_toml_dir(self):
        """--embed-files resolves relative paths against the TOML file directory."""
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.seen_body_bytes = b""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_path = tmp_path / "relative_data.bin"
            data_path.write_bytes(b"relative-file-content")

            toml = textwrap.dedent(f"""
                [[requests]]
                name = "upload"
                request = "PUT {base}/upload"
                body_file = "relative_data.bin"
            """)
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            script = bash_generator.generate(wf, embed_files=True, toml_path=str(toml_path))

            script_path = tmp_path / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(script_path)], capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
            self.assertEqual(_CaptureHandler.seen_body_bytes, b"relative-file-content")
