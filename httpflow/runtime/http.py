"""HTTP request helpers and JSON path extraction."""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .core import render, render_mapping
from .mask import mask, mask_url, mask_value

# Matches dotted or indexed path segments (e.g. data.items[0].id)
PATH_TOKEN = re.compile(r"([^\.\[\]]+)|\[(\d+)\]")
LOG_INDENT = "  "


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


# Capture-source namespaces (see design §4.5). A capture entry's right-hand
# side defaults to a response-body JSON path; these prefixes select other
# sources such as response headers or request-time values.
_CAP_RESP_HEADER = "response.header."
_CAP_REQ_HEADER = "request.header."
_CAP_RESP_BODY = "response.body."


def _header_value(headers: dict[str, str], name: str, step_name: str, side: str) -> str:
    """Look up a header value case-insensitively, raising KeyError if absent."""
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    raise KeyError(f"step {step_name!r}: {side} header not found: {name!r}")


def resolve_capture(
    source: str,
    *,
    step_name: str,
    body_json: Any | None,
    resp_headers: dict[str, str],
    req_url: str,
    req_headers: dict[str, str],
    req_body: str,
) -> Any:
    """Resolve a capture source expression into a value.

    With no namespace the source is a JSON path into the response body
    (backward compatible). Namespaced sources read response headers or
    request-time values that never appear in the response.
    """
    if source.startswith(_CAP_RESP_HEADER):
        return _header_value(resp_headers, source[len(_CAP_RESP_HEADER):], step_name, "response")
    if source.startswith(_CAP_REQ_HEADER):
        return _header_value(req_headers, source[len(_CAP_REQ_HEADER):], step_name, "request")
    if source == "request.url":
        return req_url
    if source == "request.body":
        return req_body
    path = source[len(_CAP_RESP_BODY):] if source.startswith(_CAP_RESP_BODY) else source
    if body_json is None:
        raise RuntimeError(
            f"step {step_name!r}: capture {source!r} requires a JSON response body"
        )
    return extract(body_json, path)


def do_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body_bytes: bytes | None,
    timeout: float | None = None,
) -> tuple[int, str, dict[str, str], str, Any | None]:
    """Send an HTTP request and return status, reason, headers, text, and JSON body."""
    req = urllib.request.Request(url=url, data=body_bytes, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status, reason = resp.status, resp.reason
            resp_headers = dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        try:
            raw = e.read() if e.fp is not None else b""
            status, reason = e.code, e.reason
            resp_headers = dict(e.headers.items()) if e.headers is not None else {}
        finally:
            e.close()
    text = raw.decode("utf-8", errors="replace")
    try:
        body_json = json.loads(text) if text else None
    except json.JSONDecodeError:
        body_json = None
    return status, reason, resp_headers, text, body_json


def _now() -> str:
    """Local time stamp with millisecond precision, e.g. ``2026-05-19 23:35:49.123``."""
    now = datetime.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def _pretty(text: str, enabled: bool) -> str:
    """Re-format ``text`` as 2-space-indent JSON if it parses; else return as-is."""
    if not enabled or not text:
        return text
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        return text


def _print_lines(prefix: str, text: str, *, out=None) -> None:
    """Print ``text`` line-by-line with ``prefix`` (e.g. '    > ' or '    < ')."""
    if out is None:
        out = sys.stdout
    print(f"{LOG_INDENT}{prefix}", file=out)
    for line in text.splitlines() or [""]:
        print(f"{LOG_INDENT}{prefix} {line}", file=out)


def _multipart_quote(value: str) -> str:
    """Quote a multipart Content-Disposition parameter value."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")


def build_multipart_body(
    parts: list[dict[str, str | None]],
    store: dict,
) -> tuple[bytes, str, list[dict[str, str]]]:
    """Build multipart/form-data bytes and a log-safe description."""
    boundary = "----httpflow-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    log_parts: list[dict[str, str]] = []
    for part in parts:
        kind = part.get("kind")
        name = render(str(part.get("name", "")), store)
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        if kind == "field":
            value = render(str(part.get("value", "")), store)
            chunks.append(
                (f'Content-Disposition: form-data; name="{_multipart_quote(name)}"\r\n'
                 "\r\n").encode("utf-8")
            )
            chunks.append(value.encode("utf-8"))
            chunks.append(b"\r\n")
            log_parts.append({"kind": "field", "name": name, "value": value})
            continue
        if kind == "file":
            path = render(str(part.get("path", "")), store)
            filename_raw = part.get("filename") or os.path.basename(path)
            filename = render(str(filename_raw), store)
            content_type = render(str(part.get("content_type") or "application/octet-stream"), store)
            with open(path, "rb") as f:
                data = f.read()
            chunks.append(
                (
                    f'Content-Disposition: form-data; name="{_multipart_quote(name)}"; '
                    f'filename="{_multipart_quote(filename)}"\r\n'
                    f"Content-Type: {content_type}\r\n"
                    "\r\n"
                ).encode("utf-8")
            )
            chunks.append(data)
            chunks.append(b"\r\n")
            log_parts.append({
                "kind": "file",
                "name": name,
                "path": path,
                "filename": filename,
                "content_type": content_type,
                "size": str(len(data)),
            })
            continue
        raise ValueError(f"unknown multipart part kind: {kind!r}")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary, log_parts


def _log_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body_bytes: bytes | None,
    body_form: dict[str, str] | None,
    pretty_json: bool,
    no_mask: bool = False,
    *,
    body_log: dict[str, Any] | None = None,
    out=None,
) -> None:
    out = sys.stdout if out is None else out
    parsed = urllib.parse.urlparse(mask_url(url, disabled=no_mask))
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    print(f"{LOG_INDENT}> {method.upper()} {path} HTTP/1.1", file=out)
    print(f"{LOG_INDENT}> Host: {parsed.netloc}", file=out)
    for k, v in headers.items():
        print(f"{LOG_INDENT}> {k}: {mask_value(k, v, disabled=no_mask)}", file=out)
    lower = {h.lower() for h in headers}
    if body_bytes is not None:
        print(f"{LOG_INDENT}> Content-Length: {len(body_bytes)}", file=out)
    if "user-agent" not in lower:
        print(
            f"{LOG_INDENT}> User-Agent: Python-urllib/{sys.version_info.major}.{sys.version_info.minor}",
            file=out,
        )
    if "accept-encoding" not in lower:
        print(f"{LOG_INDENT}> Accept-Encoding: identity", file=out)
    if body_log and body_log.get("kind") == "file":
        print(f"{LOG_INDENT}> (file)", file=out)
        print(f"{LOG_INDENT}>   path = {body_log.get('path')}", file=out)
        print(f"{LOG_INDENT}>   bytes = {body_log.get('size')}", file=out)
    elif body_log and body_log.get("kind") == "multipart":
        print(f"{LOG_INDENT}> (multipart)", file=out)
        for part in body_log.get("parts", []):
            if part.get("kind") == "field":
                print(f"{LOG_INDENT}>   {part.get('name')} = {mask_value(str(part.get('name')), part.get('value'), disabled=no_mask)}", file=out)
            else:
                print(
                    f"{LOG_INDENT}>   {part.get('name')} = @{part.get('path')}; "
                    f"filename={part.get('filename')}; type={part.get('content_type')}; "
                    f"bytes={part.get('size')}",
                    file=out,
                )
    elif body_form is not None:
        print(f"{LOG_INDENT}> (form)", file=out)
        for k, v in body_form.items():
            print(f"{LOG_INDENT}>   {k} = {mask_value(k, v, disabled=no_mask)}", file=out)
    elif body_bytes is not None:
        try:
            body_text = body_bytes.decode("utf-8", errors="replace")
            _print_lines(">", _pretty(mask(body_text, disabled=no_mask), pretty_json), out=out)
        except UnicodeDecodeError:
            print(f"{LOG_INDENT}> <{len(body_bytes)} bytes>", file=out)


def _log_response(
    status: int,
    reason: str,
    resp_headers: dict[str, str],
    text: str,
    pretty_json: bool,
    no_mask: bool = False,
    *,
    out=None,
) -> None:
    """Print the HTTP status line and response headers/body."""
    out = sys.stdout if out is None else out
    print(f"{LOG_INDENT}< HTTP/1.1 {status} {reason}", file=out)
    for k, v in resp_headers.items():
        print(f"{LOG_INDENT}< {k}: {mask_value(k, v, disabled=no_mask)}", file=out)
    if text:
        _print_lines("<", _pretty(mask(text, disabled=no_mask), pretty_json), out=out)


def run_step(
    store: dict,
    name: str,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    body_form: dict[str, str] | None = None,
    body_file: str | None = None,
    body_multipart: list[dict[str, str | None]] | None = None,
    capture: dict[str, str] | None = None,
    description: str | None = None,
    quiet: bool = False,
    pretty_json: bool = False,
    no_mask: bool = False,
    out=None,
) -> None:
    """Render, send, log, and capture a single HTTP (or SLEEP) attempt.

    On return, ``store["vars"]`` is updated with captured values.
    """
    out = sys.stdout if out is None else out
    url = render(url, store)

    if method == "SLEEP":
        try:
            seconds = float(url)
        except ValueError as exc:
            raise RuntimeError(
                f"step {name!r}: 'SLEEP' url must be numeric, got: {url!r}"
            ) from exc
        print(f"==> {_now()} [{name}] SLEEP {url}", file=out)
        if description:
            for line in description.splitlines() or [""]:
                print(f"{LOG_INDENT}# {line}", file=out)
        if not quiet:
            print(f"{LOG_INDENT}> sleep {seconds} seconds", file=out)
        time.sleep(seconds)
        print(f"<== {_now()} [{name}] done", file=out)
        return

    headers = render_mapping(headers or {}, store)
    body_log: dict[str, Any] | None = None
    if body is not None:
        body_bytes = render(body, store).encode("utf-8")
    elif body_form is not None:
        body_form = render_mapping(body_form, store)
        body_bytes = urllib.parse.urlencode(body_form).encode("utf-8")
        if not any(h.lower() == "content-type" for h in headers):
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body_file is not None:
        path = render(body_file, store)
        with open(path, "rb") as f:
            body_bytes = f.read()
        if not any(h.lower() == "content-type" for h in headers):
            headers["Content-Type"] = "application/octet-stream"
        body_log = {"kind": "file", "path": path, "size": len(body_bytes)}
    elif body_multipart is not None:
        if any(h.lower() == "content-type" for h in headers):
            raise RuntimeError(
                f"step {name!r}: body_multipart auto-generates Content-Type; "
                "remove Content-Type header"
            )
        body_bytes, boundary, log_parts = build_multipart_body(body_multipart, store)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        body_log = {"kind": "multipart", "parts": log_parts}
    else:
        body_bytes = None

    print(f"==> {_now()} [{name}] {method.upper()} {mask_url(url, disabled=no_mask)}", file=out)
    if description:
        for line in description.splitlines() or [""]:
            print(f"{LOG_INDENT}# {line}", file=out)
    if not quiet:
        _log_request(method, url, headers, body_bytes, body_form, pretty_json, no_mask=no_mask, body_log=body_log, out=out)

    status, reason, resp_headers, text, body_json = do_request(method, url, headers, body_bytes)
    print(f"<== {_now()} [{name}]", file=out)
    if not quiet:
        _log_response(status, reason, resp_headers, text, pretty_json, no_mask=no_mask, out=out)

    if capture:
        req_body_text = body_bytes.decode("utf-8", errors="replace") if body_bytes is not None else ""
        for var, source in capture.items():
            captured = resolve_capture(
                source,
                step_name=name,
                body_json=body_json,
                resp_headers=resp_headers,
                req_url=url,
                req_headers=headers,
                req_body=req_body_text,
            )
            store["vars"][var] = captured
            if not quiet:
                shown = mask_value(var, captured, disabled=no_mask)
                print(f"{LOG_INDENT}* capture {var} = {shown!r}", file=out)
