"""Shared helpers for bash-generator tests: mock server and base test class."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from httpflow import bash_generator
from httpflow import config as cfg_mod


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
        raw_parts = body.split(boundary_delim)

        for raw_part in raw_parts:
            if not raw_part or raw_part in (b"\r\n", b""):
                continue
            raw_part = raw_part.rstrip(b"\r\n")
            if raw_part.endswith(b"--"):
                raw_part = raw_part[:-2].rstrip(b"\r\n")
            if not raw_part:
                continue

            header_and_body = raw_part.split(b"\r\n\r\n", 1)
            if len(header_and_body) != 2:
                continue
            header_bytes, part_body = header_and_body

            try:
                header_text = header_bytes.decode("utf-8", errors="replace")
            except Exception:
                continue

            cd_line = ""
            for h in header_text.split("\r\n"):
                if h.strip().lower().startswith("content-disposition:"):
                    cd_line = h.strip()
                    break
            if not cd_line:
                continue

            name = ""
            filename = ""
            for param in cd_line.split(";"):
                param = param.strip()
                if param.lower().startswith("name="):
                    name = param.split("=", 1)[1].strip().strip('"')
                elif param.lower().startswith("filename="):
                    filename = param.split("=", 1)[1].strip().strip('"')

            if filename:
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
class TestBashGeneratorBase(unittest.TestCase):
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

    def _generate_var_script(self, toml_text: str):
        """Generate, syntax-check and write to a temp file; return (script, path).

        The temp dir persists for the lifetime of the test (cleaned up via
        addCleanup) so the returned path remains valid when the test later
        runs the script via subprocess.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        tmp_path = Path(tmp)
        toml_path = tmp_path / "workflow.toml"
        toml_path.write_text(toml_text, encoding="utf-8")
        wf = cfg_mod.load(str(toml_path))
        script = bash_generator.generate(wf)
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
        return script, script_path
