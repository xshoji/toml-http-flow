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
            method = "GET"
            url = "http://example.com/ping"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_ping()", script)
        self.assertIn('cmd=(curl -sS -L -v --no-buffer --stderr -)', script)
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
            method = "GET"
            url = "{base}/echo?iso=${{time.DATE_ISO}}&ymd=${{time.DATE_YMD}}&hms=${{time.DATE_YMDHMS}}"
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
            method = "GET"
            url = "{base}/echo"
        """)
        script = self._generate_and_check(toml)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)

        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        ts = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}"
        self.assertRegex(res.stdout, rf"==> {ts} \[ping\] GET {base}/echo")
        self.assertRegex(res.stdout, rf"<== {ts} \[ping\]")

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
        self.assertIn("body=$(cat << EOT", script)
        self.assertNotIn("__HF_BODY_step_create", script)
        self.assertIn('{"name":"test"}', script)
        self.assertIn('cmd+=(-d "$body")', script)

    def test_post_body_is_inserted_at_curl_request_response_boundary(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "create"
            method = "POST"
            url = "{base}/auth"
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
        self.assertRegex(lines[boundary_idx + 2], r"^<== \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \[create\]")
        self.assertTrue(lines[boundary_idx + 3].startswith("< HTTP/"))

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
        self.assertIn("body_form_text=$(cat << EOT", script)
        self.assertIn("user\talice", script)
        self.assertIn("pass\tsecret", script)
        self.assertIn('cmd+=(--data-urlencode "$multipart_name=$multipart_value")', script)

    def test_form_body_placeholders_expand_before_urlencode(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "first"
            method = "POST"
            url = "http://example.com/first"
            capture = ["authorization = response.body.token"]

            [[requests]]
            name = "login"
            method = "POST"
            url = "http://example.com/auth"
            body_form = [
                "nickname = new_name",
                "email    = test@email.com",
                "args     = ${var.argsAaa}",
                "params   = ${var.paramsParamb}",
                "token    = ${authorization}",
            ]
        """)
        script = self._generate_and_check(toml)
        self.assertIn(
            "nickname\tnew_name\nemail\ttest@email.com\nargs\t${VAR_ARGSAAA}\nparams\t${VAR_PARAMSPARAMB}\ntoken\t${VAR_AUTHORIZATION}",
            script,
        )
        self.assertNotIn("%24%7Bauthorization%7D", script)

    def test_form_body_does_not_duplicate_user_content_type(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "login"
            method = "POST"
            url = "http://example.com/auth"
            headers = ["Content-Type: application/x-www-form-urlencoded"]
            body_form = ["user = alice", "pass = secret"]
        """)
        script = self._generate_and_check(toml)
        self.assertEqual(script.count('Content-Type: application/x-www-form-urlencoded'), 1)

    def test_form_body_content_type_detection_is_case_insensitive(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "login"
            method = "POST"
            url = "http://example.com/auth"
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
            method = "POST"
            url = "http://example.com/${env.USER}"
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
            method = "SLEEP"
            url = "0.05"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("step_wait()", script)
        self.assertIn('seconds="0.05"', script)
        self.assertIn('print_blank_lines "${HTTPFLOW_BLANK_LINE:-0}"', script)
        self.assertIn('SLEEP %s\\n" "$(now)" \'wait\' "$seconds"', script)
        self.assertIn('sleep "$seconds"', script)
        self.assertIn('done\\n" "$(now)" \'wait\'', script)

    def test_sleep_step_with_shell_variable(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "wait"
            method = "SLEEP"
            url = "${WAIT_SECONDS}"
        """)
        script = self._generate_and_check(toml)
        self.assertIn('seconds="${WAIT_SECONDS}"', script)
        self.assertIn('SLEEP %s\\n" "$(now)" \'wait\' "$seconds"', script)
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

    def test_var_placeholders_become_shell_env_names(self):
        toml = textwrap.dedent("""
            [[requests]]
            name = "echo"
            method = "POST"
            url = "http://example.com/${var.env}?id=${var.id}"
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
        self.assertIn("VAR_TOKEN\ttoken\tjson\taccess_token", script)

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
    def test_until_polls_until_capture_matches(self):
        base = f"http://127.0.0.1:{self.port}"
        _CaptureHandler.poll_count = 0
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "poll"
            method = "GET"
            url = "{base}/poll"
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
            method = "GET"
            url = "{base}/poll"
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
            method = "GET"
            url = "{base}/poll404"
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
            method = "GET"
            url = "http://example.com/ping"
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
            method = "GET"
            url = "{base}/poll"
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
            method = "GET"
            url = "{base}/redir"
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
        self.assertIn("http_step 'ping' 'GET'", script)
        self.assertIn("'health check'", script)

    def test_description_is_printed_after_http_start_line(self):
        base = f"http://127.0.0.1:{self.port}"
        toml = textwrap.dedent(f"""
            [[requests]]
            name = "ping"
            method = "GET"
            url = "{base}/echo"
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
            method = "POST"
            url = "http://example.com/items/${random.UUID_HEX}"
            headers = ["X-Request-Id: ${random.UUID}"]
            body = '{"request_id":"${random.UUID}"}'
        """)
        script = self._generate_and_check(toml)
        self.assertIn("uuid()", script)
        self.assertIn("uuid_hex()", script)
        self.assertIn('url="http://example.com/items/$(uuid_hex)"', script)
        self.assertIn("X-Request-Id: $(uuid)", script)
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
        self.assertNotIn('printf "> %s\\n" "$header"', script)
        self.assertIn('printf "%s" "$body_log" | jq_or_cat | prefix_lines "> "', script)
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

    def test_mask_extra_env_var_in_bash_script(self):
        """HTTPFLOW_MASK_EXTRA env var extends masking keys in bash script."""
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
            method = "GET"
            url = "http://example.com/ping"
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
            method = "GET"
            url = "{base}/echo"
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

    def test_default_vars_embedded_in_bash_script(self):
        """-v K=V in generate --format bash embeds default VAR_* values."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "echo"
            method = "POST"
            url = "http://example.com/${var.env}?id=${var.id}"
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

            self.assertIn('if [ -z "${VAR_ENV:-}" ]; then VAR_ENV=\'prod\'; fi', script)
            self.assertIn('if [ -z "${VAR_USER:-}" ]; then VAR_USER=\'alice\'; fi', script)
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
            method = "GET"
            url = "http://example.com/${var.user}"
        """)
        script = self._generate_and_check(toml)
        self.assertIn('if [ -z "${VAR_USER:-}" ]; then', script)
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
            method = "GET"
            url = "{base}/me"
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
            method = "GET"
            url = "{base}/echo"

            [[requests]]
            name = "two"
            method = "GET"
            url = "{base}/echo"
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

    def test_until_function_name_no_collision_with_normal_step(self):
        """until _attempt suffix must not collide with another step's function name."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "poll_attempt"
            method = "GET"
            url = "http://example.com/other"

            [[requests]]
            name = "poll"
            method = "GET"
            url = "http://example.com/poll"
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
            method = "SLEEP"
            url = "0.01"
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
            method = "GET"
            url = "http://example.com/ping"
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
            method = "PUT"
            url = "http://example.com/upload"
            body_file = "/tmp/data.bin"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("body_kind='file'", script)
        self.assertIn("--data-binary", script)
        self.assertIn("body_file:", script)

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
                method = "PUT"
                url = "{base}/upload"
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

    def test_body_file_content_type_auto_added(self):
        """Content-Type: application/octet-stream is added automatically for body_file."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "upload"
            method = "PUT"
            url = "http://example.com/upload"
            body_file = "/tmp/data.bin"
        """)
        script = self._generate_and_check(toml)
        self.assertIn("Content-Type: application/octet-stream", script)

    def test_body_file_respects_user_content_type(self):
        """User-specified Content-Type for body_file is not overridden."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "upload"
            method = "PUT"
            url = "http://example.com/upload"
            headers = ["Content-Type: image/png"]
            body_file = "/tmp/data.bin"
        """)
        script = self._generate_and_check(toml)
        self.assertEqual(script.count("Content-Type:"), 1)
        self.assertIn("Content-Type: image/png", script)
        self.assertNotIn("application/octet-stream", script)

    def test_body_file_missing_fails_at_runtime(self):
        """body_file referencing a non-existent path fails with clear error."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml = textwrap.dedent("""
                [[requests]]
                name = "upload"
                method = "PUT"
                url = "http://example.com/upload"
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
        toml = textwrap.dedent("""
            [[requests]]
            name = "mform"
            method = "POST"
            url = "http://example.com/upload"
            body_multipart = [
                "name = alice",
                "email = alice@example.com",
            ]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("body_kind='multipart'", script)
        self.assertIn("--form-string", script)
        self.assertIn("name\talice", script)
        self.assertIn("email\talice@example.com", script)

    def test_body_multipart_literal_at_sign(self):
        """body_multipart with @@value sends literal @value via --form-string."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "mform"
            method = "POST"
            url = "http://example.com/upload"
            body_multipart = [
                "greeting = @@hello",
            ]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("--form-string", script)
        self.assertIn("greeting\t@hello", script)

    def test_body_multipart_file_field(self):
        """body_multipart file field uses curl -F with @path."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "mform"
            method = "POST"
            url = "http://example.com/upload"
            body_multipart = [
                "file = @/tmp/data.bin; filename=upload.dat; type=image/png",
            ]
        """)
        script = self._generate_and_check(toml)
        self.assertIn("body_kind='multipart'", script)
        self.assertIn("-F ", script)
        self.assertIn("upload.dat", script)
        self.assertIn("image/png", script)

    def test_body_multipart_content_type_is_error(self):
        """body_multipart with user-specified Content-Type raises ValueError."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "mform"
            method = "POST"
            url = "http://example.com/upload"
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
                method = "POST"
                url = "http://example.com/upload"
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
                method = "POST"
                url = "{base}/upload"
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
                method = "POST"
                url = "{base}/upload"
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

    def test_body_file_capture_request_body_is_error(self):
        """capture request.body with body_file raises ValueError."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "upload"
            method = "PUT"
            url = "http://example.com/upload"
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
            method = "POST"
            url = "http://example.com/upload"
            body_multipart = [
                "name = alice",
            ]
            capture = ["sent = request.body"]
        """)
        with self.assertRaises(ValueError) as ctx:
            self._generate_and_check(toml)
        self.assertIn("cannot capture request.body", str(ctx.exception))

    def test_body_multipart_tab_in_name_raises(self):
        """multipart field name with tab raises ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_content = '[[requests]]\nname = "mform"\nmethod = "POST"\nurl = "http://example.com/upload"\nbody_multipart = ["na\\tme = value"]\n'
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml_content, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            with self.assertRaises(ValueError) as ctx:
                bash_generator.generate(wf)
            self.assertIn("must not contain tabs, newlines, or double quotes", str(ctx.exception))

    def test_body_multipart_tab_in_value_raises(self):
        """multipart field value with tab raises ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_content = '[[requests]]\nname = "mform"\nmethod = "POST"\nurl = "http://example.com/upload"\nbody_multipart = ["name = val\\tue"]\n'
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml_content, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            with self.assertRaises(ValueError) as ctx:
                bash_generator.generate(wf)
            self.assertIn("must not contain tabs, newlines, or double quotes", str(ctx.exception))

    def test_body_multipart_tab_in_file_path_raises(self):
        """multipart file path with tab raises ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_content = '[[requests]]\nname = "mform"\nmethod = "POST"\nurl = "http://example.com/upload"\nbody_multipart = ["file = @/tmp/da\\tta.bin"]\n'
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml_content, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            with self.assertRaises(ValueError) as ctx:
                bash_generator.generate(wf)
            self.assertIn("must not contain tabs, newlines, or double quotes", str(ctx.exception))

    def test_body_multipart_double_quote_in_field_name_raises(self):
        """multipart field name with double quote raises ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            toml_content = '[[requests]]\nname = "mform"\nmethod = "POST"\nurl = "http://example.com/upload"\nbody_multipart = ["na\\"me = value"]\n'
            toml_path = tmp_path / "workflow.toml"
            toml_path.write_text(toml_content, encoding="utf-8")
            wf = cfg_mod.load(str(toml_path))
            with self.assertRaises(ValueError) as ctx:
                bash_generator.generate(wf)
            self.assertIn("must not contain tabs, newlines, or double quotes", str(ctx.exception))

    def test_embed_files_body_file(self):
        """--embed-files embeds body_file content as base64 in the script."""
        base = f"http://127.0.0.1:{self.port}"
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp) / "data.bin"
            data_path.write_bytes(b"embedded-binary-data\x00\xff")

            toml = textwrap.dedent(f"""
                [[requests]]
                name = "upload"
                method = "PUT"
                url = "{base}/upload"
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
            self.assertIn("body=\"$decode_file\"", script)

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
                method = "PUT"
                url = "{base}/upload"
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
                method = "POST"
                url = "{base}/upload"
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

    def test_embed_files_placeholder_path_skips_embed(self):
        """--embed-files skips embedding when path contains placeholders."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "upload"
            method = "PUT"
            url = "http://example.com/upload"
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
            method = "GET"
            url = "http://example.com/ping"
        """)
        script = self._generate_and_check(toml)
        self.assertNotIn("_hf_b64decode", script)

    def test_embed_files_body_file_missing_fails_at_gen_time(self):
        """--embed-files raises FileNotFoundError when the file does not exist."""
        toml = textwrap.dedent("""
            [[requests]]
            name = "upload"
            method = "PUT"
            url = "http://example.com/upload"
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
            method = "POST"
            url = "http://example.com/upload"
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
                method = "PUT"
                url = "{base}/poll"
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
                method = "PUT"
                url = "{base}/upload"
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
