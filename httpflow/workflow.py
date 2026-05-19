"""Workflow step execution engine with a shared variable store."""

from __future__ import annotations

import datetime
import sys
import time
import urllib.parse
from dataclasses import replace
from typing import Any

from .config import SPECIAL_METHODS, RequestConfig, WorkflowConfig
from .httpclient import execute, extract, prepare_request
from .template import render, render_mapping


def _now() -> str:
    """Local time stamp with millisecond precision, e.g. ``2026-05-19 23:35:49.123``."""
    now = datetime.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def _render_request(req: RequestConfig, store: dict[str, Any]) -> RequestConfig:
    """Return a copy of ``req`` with all string values templated."""
    return replace(
        req,
        url=render(req.url, store),
        headers=render_mapping(req.headers, store),
        body=render(req.body, store) if req.body is not None else None,
        body_form=render_mapping(req.body_form, store) if req.body_form is not None else None,
    )


def _log_request(req: RequestConfig, out) -> None:
    """Print the request line and headers that urllib will actually send."""
    request, body_bytes = prepare_request(req)

    # Request line: e.g. POST /auth HTTP/1.1
    parsed = urllib.parse.urlparse(req.url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    print(f"    > {req.method.upper()} {path} HTTP/1.1", file=out)

    # Host header (estimated)
    print(f"    > Host: {parsed.netloc}", file=out)

    # Explicit user headers
    for k, v in req.headers.items():
        print(f"    > {k}: {v}", file=out)

    # Estimated headers that urllib adds automatically
    if body_bytes is not None:
        print(f"    > Content-Length: {len(body_bytes)}", file=out)

    lower_headers = {h.lower() for h in req.headers}
    if "user-agent" not in lower_headers:
        print(
            f"    > User-Agent: Python-urllib/{sys.version_info.major}.{sys.version_info.minor}",
            file=out,
        )
    if "accept-encoding" not in lower_headers:
        print("    > Accept-Encoding: identity", file=out)

    # Body
    if req.body is not None:
        print("    >", file=out)
        for line in req.body.splitlines() or [""]:
            print(f"    > {line}", file=out)
    elif req.body_form is not None:
        print("    > (form)", file=out)
        for k, v in req.body_form.items():
            print(f"    >   {k} = {v}", file=out)


def _log_response(resp, out) -> None:
    """Print the HTTP status line and response headers/body."""
    print(f"    < HTTP/1.1 {resp.status} {resp.reason}", file=out)
    for k, v in resp.headers.items():
        print(f"    < {k}: {v}", file=out)
    if resp.body_text:
        print("    <", file=out)
        for line in resp.body_text.splitlines():
            print(f"    < {line}", file=out)


def run(
    config: WorkflowConfig,
    vars_: dict[str, str] | None = None,
    *,
    quiet: bool = False,
    out=sys.stdout,
) -> dict[str, Any]:
    """Run every request in ``config`` and return the final variable store.

    By default each step's request and response details are printed to ``out``.
    Pass ``quiet=True`` to print only the one-line summary per step.
    """
    store: dict[str, Any] = {"vars": dict(vars_ or {}), "steps": {}}

    for req in config.requests:
        rendered = _render_request(req, store)

        print(f"==> {_now()} [{rendered.name}] {rendered.method} {rendered.url}", file=out)

        if rendered.method in SPECIAL_METHODS:
            if rendered.method == "SLEEP":
                try:
                    seconds = float(rendered.url)
                except ValueError as exc:
                    raise RuntimeError(
                        f"step {rendered.name!r}: 'SLEEP' url must be numeric, got: {rendered.url!r}"
                    ) from exc
                if not quiet:
                    print(f"    > sleep {seconds} seconds", file=out)
                time.sleep(seconds)
                print(f"<== {_now()} [{rendered.name}] done", file=out)
                store["steps"][rendered.name] = {}
                continue

        if not quiet:
            _log_request(rendered, out)

        resp = execute(rendered)
        print(f"<== {_now()} [{rendered.name}] status={resp.status}", file=out)
        if not quiet:
            _log_response(resp, out)

        captured: dict[str, Any] = {}
        if rendered.capture:
            if resp.body_json is None:
                raise RuntimeError(
                    f"step {rendered.name!r}: capture requested but response is not JSON"
                )
            for var_name, path in rendered.capture.items():
                value = extract(resp.body_json, path)
                captured[var_name] = value
                if not quiet:
                    print(f"    * capture {var_name} = {value!r}", file=out)

        store["steps"][rendered.name] = captured

    return store
