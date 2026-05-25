"""urllib-based HTTP client and JSON path extractor."""

from __future__ import annotations

import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import RequestConfig
from .runtime.http import do_request, extract


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
    status, reason, resp_headers, text, body_json = do_request(
        request.get_method(), request.full_url, dict(request.header_items()), request.data, timeout
    )

    return Response(
        status=status,
        reason=reason,
        headers=resp_headers,
        body_text=text,
        body_json=body_json,
    )
