"""Tests for body_file and body_multipart in generated bash scripts."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from httpflow import bash_generator
from httpflow import config as cfg_mod

from tests._bash_generator_helpers import TestBashGeneratorBase, _CaptureHandler


class TestBashGeneratorBody(TestBashGeneratorBase):
    # ── body_file ───────────────────────────────────────────────────

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

    # ── body_multipart ──────────────────────────────────────────────

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
        self.assertNotIn('echo "# multipart field:', script)
        self.assertNotIn('body_log="${body_log}\\n', script)
        self.assertNotIn("(multipart)", script)

        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "workflow.sh"
            script_path.write_text(script, encoding="utf-8")
            res = subprocess.run(["bash", str(script_path)], capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, msg=res.stderr + res.stdout)
        self.assertIn("> name=alice", res.stdout)
        self.assertIn("> email=alice@example.com", res.stdout)
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
        self.assertNotIn('echo "# multipart file:', script)
        self.assertNotIn('bytes=${file_size}', script)
        self.assertIn(f'> file=@"{data_path}";filename="upload.dat";type=image/png', res.stdout)
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

            self.assertEqual(_CaptureHandler.multipart_fields["name"], "alice")
            self.assertEqual(_CaptureHandler.multipart_fields["email"], "test@email.com")
            self.assertRegex(_CaptureHandler.multipart_fields["req_id"], r"^[0-9a-f]{32}$")
            self.assertRegex(_CaptureHandler.multipart_fields["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
            self.assertNotIn("${var.username}", script)
            self.assertIn("${VAR_USERNAME}", script)

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
        """Multipart part info is printed inside the step's section (after ==>)."""
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

            banner_pos = res.stdout.find("[second] POST")
            self.assertGreater(banner_pos, 0, "second step banner not found")
            self.assertGreater(res.stdout.find("title=test upload"), banner_pos,
                               "multipart field value appears before the step banner")
            self.assertGreater(
                res.stdout.find('file=@"' + str(file_path)), banner_pos,
                "multipart file value appears before the step banner"
            )

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
            toml_content = '[[requests]]\nname = "mform"\nrequest = "POST http://example.com/upload"\nbody_multipart = ["na\tme = value"]\n'
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
            toml_content = '[[requests]]\nname = "mform"\nrequest = "POST http://example.com/upload"\nbody_multipart = ["name = val\tue"]\n'
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
            toml_content = '[[requests]]\nname = "mform"\nrequest = "POST http://example.com/upload"\nbody_multipart = ["file = @/tmp/da\tta.bin"]\n'
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
