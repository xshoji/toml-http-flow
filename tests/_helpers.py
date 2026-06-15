"""Shared test helpers: mock servers, env scopes, temp-file utilities."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


# ------------------------------------------------------------------ env scope

class EnvScope:
    """Temporarily set env vars; restore on exit. Supports None to unset."""

    def __init__(self, **kwargs: str | None):
        self._kwargs = kwargs
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> EnvScope:
        for k, v in self._kwargs.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc: object) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def clean_mask_env() -> EnvScope:
    """Force-clear HTTPFLOW_MASK_EXTRA env var."""
    return EnvScope(HTTPFLOW_MASK_EXTRA=None)


# ------------------------------------------------------------------ temp-file utils

def write_toml(content: str | bytes) -> str:
    """Write a TOML file to a temporary path and return it.

    The caller is responsible for deleting the file.
    """
    fd, path = tempfile.mkstemp(suffix=".toml")
    with os.fdopen(fd, "wb") as f:
        f.write(content.encode("utf-8") if isinstance(content, str) else content)
    return path


# ------------------------------------------------------------------ base HTTP handlers

class JsonHandler(BaseHTTPRequestHandler):
    """Minimal JSON handler: POST returns access_token; GET returns user or 404."""

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self._send_json(200, {"access_token": "tok-abc"})

    def do_GET(self) -> None:
        if self.path == "/me":
            auth = self.headers.get("Authorization", "")
            self._send_json(200, {"user": {"id": 7, "auth_seen": auth}})
        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:
        return


class PollHandler(BaseHTTPRequestHandler):
    """Returns status=Pending for the first N GETs, then status=Active."""

    pending_remaining: int = 0
    job_id: str = "job-1"

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self._send_json(200, {"data": {"id": PollHandler.job_id}})

    def do_GET(self) -> None:
        if PollHandler.pending_remaining > 0:
            PollHandler.pending_remaining -= 1
            self._send_json(200, {"data": {"status": "Pending"}})
        else:
            self._send_json(200, {"data": {"status": "Active"}})

    def log_message(self, format: str, *args: Any) -> None:
        return


class EchoJsonHandler(BaseHTTPRequestHandler):
    """Echoes posted JSON back wrapped in {echo: ...}."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        payload = json.dumps({"echo": json.loads(body)})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(payload.encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        return


class PlainTextHandler(BaseHTTPRequestHandler):
    """Returns a plain-text response."""

    body = b"not json at all"
    extra_headers: dict[str, str] = {}

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(self.body)))
        for k, v in self.extra_headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(self.body)

    def do_POST(self) -> None:
        self.do_GET()

    def log_message(self, format: str, *args: Any) -> None:
        return


class UploadHandler(BaseHTTPRequestHandler):
    """Records uploaded PUT/POST bodies and content_type."""

    seen: list[dict[str, str | bytes]] = []

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        UploadHandler.seen.append({
            "path": self.path,
            "content_type": self.headers.get("Content-Type", ""),
            "body": body,
        })
        self._send()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        UploadHandler.seen.append({
            "path": self.path,
            "content_type": self.headers.get("Content-Type", ""),
            "body": body,
        })
        self._send()

    def _send(self) -> None:
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


# ------------------------------------------------------------------ server mixin

class ServerMixin:
    """Mixin that starts a local HTTP server in setUpClass / tearDownClass.

    Subclasses must set ``_handler_cls`` to a BaseHTTPRequestHandler subclass.
    """

    _handler_cls: type[BaseHTTPRequestHandler]

    @classmethod
    def setUpClass(cls: Any) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), cls._handler_cls)  # type: ignore[arg-type]
        cls.port = cls.server.server_address[1]  # type: ignore[attr-defined]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)  # type: ignore[attr-defined]
        cls.thread.start()  # type: ignore[attr-defined]

    @classmethod
    def tearDownClass(cls: Any) -> None:
        cls.server.shutdown()  # type: ignore[attr-defined]
        cls.server.server_close()  # type: ignore[attr-defined]
