from __future__ import annotations

import json
import re

from httpflow.model import FileBody, HttpStep, MultipartBody

from .names import env_name
from .shell import sq

def is_json_capture_source(source: str) -> bool:
    """Return True when capture source reads the response body JSON."""
    return not (
        source.startswith("response.header.")
        or source.startswith("request.header.")
        or source in {"request.url", "request.body"}
        or source.startswith("request.body.")
    )



def capture_path(source: str) -> str:
    """Return JSON path part for response-body capture source."""
    return source.removeprefix("response.body.")



def jq_filter(path: str) -> str:
    """Convert httpflow JSON path syntax into a jq filter."""
    token_re = re.compile(r"([A-Za-z0-9_\-]+)|(\[(\d+)\])")
    out = ""
    pos = 0
    while pos < len(path):
        if path[pos] == ".":
            pos += 1
            continue
        m = token_re.match(path, pos)
        if not m:
            raise ValueError(f"unsupported JSON capture path: {path!r}")
        if m.group(1) is not None:
            out += f"[{json.dumps(m.group(1))}]?"
        else:
            out += f"[{m.group(3)}]?"
        pos = m.end()
    return "." + out



_CAP_REQ_BODY = "request.body."


def capture_kind_and_arg(source: str) -> tuple[str, str]:
    """Return generated bash capture metadata kind and helper argument."""
    if source.startswith("response.header."):
        return "response_header", source.removeprefix("response.header.")
    if source.startswith("request.header."):
        return "request_header", source.removeprefix("request.header.")
    if source == "request.url":
        return "request_url", "-"
    if source == "request.body":
        return "request_body", "-"
    if source.startswith(_CAP_REQ_BODY):
        return "request_body_json", jq_filter(source.removeprefix(_CAP_REQ_BODY))
    return "json", jq_filter(capture_path(source))



def capture_calls(
    step: HttpStep,
    *,
    url_expr: str = '"$url"',
    body_log_expr: str = '"$HF_BODY_LOG"',
    trace_file_expr: str = '"$HF_TRACE_FILE"',
    curl_command_expr: str = '"$curl_command"',
    indent: str = "    ",
) -> list[str]:
    """Emit per-capture ``capture_*`` call lines for an HTTP step.

    Each line is a complete bash statement (e.g.
    ``capture_response_body_json 'VAR_FOO' 'foo' 'foo' "$HF_TRACE_FILE" '.["foo"]?' || return $?``)
    intended to be placed in the step function body right after the
    ``http_step`` call. The ``*_expr`` parameters are the bash expressions
    referencing variables exposed by ``http_step`` (``HF_TRACE_FILE``,
    ``HF_BODY_LOG``, ``$url``, ``$curl_command``).
    """
    lines: list[str] = []
    for var, source in step.capture.items():
        if any(ch in var or ch in source for ch in "\t\n"):
            raise ValueError("capture names and sources must not contain tabs or newlines")

        if isinstance(step.body, (FileBody, MultipartBody)) and (
            source == "request.body" or source.startswith("request.body.")
        ):
            raise ValueError(
                f"step {step.name!r}: cannot capture request.body with "
                f"body_file or body_multipart in bash generation"
            )

        kind, arg = capture_kind_and_arg(source)
        if "\t" in arg or "\n" in arg:
            raise ValueError("capture helper arguments must not contain tabs or newlines")

        env = env_name("VAR", var)
        if kind == "json":
            lines.append(
                f"{indent}capture_response_body_json {sq(env)} {sq(var)} {sq(source)} {trace_file_expr} {sq(arg)} || return $?"
            )
        elif kind == "response_header":
            lines.append(
                f"{indent}capture_request_response_header {sq(env)} {sq(var)} {sq(source)} {trace_file_expr} {sq(arg)} || return $?"
            )
        elif kind == "request_header":
            lines.append(
                f"{indent}capture_request_response_header {sq(env)} {sq(var)} {sq(source)} {curl_command_expr} {sq(arg)} || return $?"
            )
        elif kind == "request_url":
            lines.append(
                f"{indent}capture_value {sq(env)} {sq(var)} {sq(source)} {url_expr} || return $?"
            )
        elif kind == "request_body":
            lines.append(
                f"{indent}capture_value {sq(env)} {sq(var)} {sq(source)} {body_log_expr} || return $?"
            )
        elif kind == "request_body_json":
            lines.append(
                f"{indent}capture_request_body_json {sq(env)} {sq(var)} {sq(source)} {body_log_expr} {sq(arg)} || return $?"
            )
        else:  # pragma: no cover - capture_kind_and_arg is exhaustive
            raise ValueError(f"unknown capture kind: {kind!r}")
    return lines
