"""urllib-based HTTP client and JSON path extractor."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import RequestConfig


PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


@dataclass
class Response:
    status: int
    reason: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body_text: str = ""
    body_json: Any | None = None


def prepare_request(req: RequestConfig) -> tuple[urllib.request.Request, bytes | None]:
    """Build a urllib Request object plus the body bytes that will be sent."""
    extra: dict[str, str] = {}
    body_bytes: bytes | None = None
    if req.body_form is not None:
        encoded = urllib.parse.urlencode(req.body_form)
        if not any(h.lower() == "content-type" for h in req.headers):
            extra["Content-Type"] = "application/x-www-form-urlencoded"
        body_bytes = encoded.encode("utf-8")
    elif req.body is not None:
        body_bytes = req.body.encode("utf-8")

    headers = dict(req.headers)
    for k, v in extra.items():
        headers.setdefault(k, v)

    request = urllib.request.Request(
        url=req.url,
        data=body_bytes,
        method=req.method.upper(),
        headers=headers,
    )
    return request, body_bytes


def execute(req: RequestConfig, timeout: float | None = None) -> Response:
    """Execute a single HTTP request and return the response."""
    request, _ = prepare_request(req)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
            reason = resp.reason
            resp_headers = {k: v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        body = e.read() if e.fp is not None else b""
        text = body.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"HTTP {e.code} from {req.method} {req.url}: {text}"
        ) from e

    text = raw.decode("utf-8", errors="replace")
    body_json: Any | None
    try:
        body_json = json.loads(text) if text else None
    except json.JSONDecodeError:
        body_json = None

    return Response(
        status=status,
        reason=reason,
        headers=resp_headers,
        body_text=text,
        body_json=body_json,
    )


def extract(body: Any, path: str) -> Any:
    """Extract a value from a parsed JSON body using a dotted/indexed path."""
    cur: Any = body
    for name, idx in PATH_TOKEN.findall(path):
        if name:
            if not isinstance(cur, dict) or name not in cur:
                raise KeyError(f"path not found: {path}")
            cur = cur[name]
        else:
            i = int(idx)
            if not isinstance(cur, list) or i >= len(cur):
                raise IndexError(f"index out of range: {path}")
            cur = cur[i]
    return cur
