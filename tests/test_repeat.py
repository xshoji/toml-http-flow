"""Tests for the ${repeat.<name>} feature: detection, iteration, validation."""

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from httpflow import config as cfg_mod
from httpflow import generator
from httpflow.model import HttpStep, TextBody
from httpflow.runner import collect_repeat_names, run
from httpflow.template import find_repeat_names


class _EchoHandler(BaseHTTPRequestHandler):
    """Echoes the request path back as JSON."""

    def do_GET(self):
        body = json.dumps({"path": self.path}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class TestFindRepeatNames(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(
            find_repeat_names("/x?id=${repeat.id}&name=${repeat.name}"),
            {"id", "name"},
        )

    def test_none(self):
        self.assertEqual(find_repeat_names(None), set())

    def test_no_match(self):
        self.assertEqual(find_repeat_names("plain ${var.x}"), set())


class TestCollectRepeatNames(unittest.TestCase):
    def test_collects_from_all_fields(self):
        from httpflow.model import WorkflowSpec

        spec = WorkflowSpec(
            steps=[
                HttpStep(
                    name="r1",
                    method="POST",
                    url="http://x/${repeat.a}",
                    headers={"X-Tag": "${repeat.b}"},
                    body=TextBody(text='{"v":"${repeat.c}"}'),
                ),
            ]
        )
        self.assertEqual(collect_repeat_names(spec), {"a", "b", "c"})

    def test_collects_from_mapping_keys(self):
        from httpflow.model import WorkflowSpec

        spec = WorkflowSpec(
            steps=[
                HttpStep(
                    name="r1",
                    method="POST",
                    url="http://x",
                    headers={"X-${repeat.key}": "v"},
                ),
            ]
        )
        self.assertEqual(collect_repeat_names(spec), {"key"})


class TestRepeatTemplates(unittest.TestCase):
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



if __name__ == "__main__":
    unittest.main()
