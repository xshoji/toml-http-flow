"""Tests for --embed-files in generated bash scripts."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from httpflow import bash_generator
from httpflow import config as cfg_mod

from tests._bash_generator_helpers import TestBashGeneratorBase, _CaptureHandler


class TestBashGeneratorEmbed(TestBashGeneratorBase):
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
            self.assertEqual(_CaptureHandler.multipart_files[0]["name"], "first")
            self.assertEqual(_CaptureHandler.multipart_files[0]["filename"], "first.bin")
            self.assertEqual(_CaptureHandler.multipart_files[0]["data"], b"AAAA-first")
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
            self.assertNotIn('body="$decode_file"', script)

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
