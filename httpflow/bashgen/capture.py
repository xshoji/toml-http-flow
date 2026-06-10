from __future__ import annotations

import json
import re

from httpflow.model import HttpStep

from .names import env_name

def is_json_capture_source(source: str) -> bool:
    """Return True when capture source reads the response body JSON."""
    return not (
        source.startswith("response.header.")
        or source.startswith("request.header.")
        or source in {"request.url", "request.body"}
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
    return "json", jq_filter(capture_path(source))



def capture_rows(step: HttpStep) -> list[str]:
    """Emit capture metadata rows for an HTTP step."""
    from httpflow.model import FileBody, MultipartBody

    rows: list[str] = []
    for var, source in step.capture.items():
        if any(ch in var or ch in source for ch in "\t\n"):
            raise ValueError("capture names and sources must not contain tabs or newlines")

        if isinstance(step.body, (FileBody, MultipartBody)) and source == "request.body":
            raise ValueError(
                f"step {step.name!r}: cannot capture request.body with "
                f"body_file or body_multipart in bash generation"
            )

        kind, arg = capture_kind_and_arg(source)
        if "\t" in arg or "\n" in arg:
            raise ValueError("capture helper arguments must not contain tabs or newlines")
        rows.append("\t".join([env_name("VAR", var), var, kind, source, arg]))
    return rows


