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
    headers: dict[str, str] = field(default_factory=dict)
    body_text: str = ""
    body_json: Any | None = None


def _build_body(req: RequestConfig) -> tuple[bytes | None, dict[str, str]]:
    """Return (body_bytes, extra_headers) based on the request body fields."""
    extra: dict[str, str] = {}
    if req.body_form is not None:
        encoded = urllib.parse.urlencode(req.body_form)
        if not any(h.lower() == "content-type" for h in req.headers):
            extra["Content-Type"] = "application/x-www-form-urlencoded"
        return encoded.encode("utf-8"), extra
    if req.body is not None:
        return req.body.encode("utf-8"), extra
    return None, extra


def execute(req: RequestConfig, timeout: float | None = None) -> Response:
    """Execute a single HTTP request and return the response."""
    body_bytes, extra_headers = _build_body(req)

    headers = dict(req.headers)
    for k, v in extra_headers.items():
        headers.setdefault(k, v)

    request = urllib.request.Request(
        url=req.url,
        data=body_bytes,
        method=req.method.upper(),
        headers=headers,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
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
