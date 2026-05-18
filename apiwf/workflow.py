"""Workflow step execution engine with a shared variable store."""

from __future__ import annotations

import sys
from dataclasses import replace
from typing import Any

from .config import RequestConfig, WorkflowConfig
from .httpclient import execute, extract
from .template import render, render_mapping


def _render_request(req: RequestConfig, store: dict[str, Any]) -> RequestConfig:
    """Return a copy of ``req`` with all string values templated."""
    return replace(
        req,
        url=render(req.url, store),
        headers=render_mapping(req.headers, store),
        body=render(req.body, store) if req.body is not None else None,
        body_form=render_mapping(req.body_form, store) if req.body_form is not None else None,
    )


def run(
    config: WorkflowConfig,
    vars_: dict[str, str] | None = None,
    *,
    verbose: bool = False,
    out=sys.stdout,
) -> dict[str, Any]:
    """Run every request in ``config`` and return the final variable store."""
    store: dict[str, Any] = {"vars": dict(vars_ or {}), "steps": {}}

    for req in config.requests:
        rendered = _render_request(req, store)

        print(f"==> [{rendered.name}] {rendered.method} {rendered.url}", file=out)
        if verbose:
            for k, v in rendered.headers.items():
                print(f"    {k}: {v}", file=out)
            if rendered.body is not None:
                print(f"    body: {rendered.body}", file=out)
            elif rendered.body_form is not None:
                print(f"    body_form: {rendered.body_form}", file=out)

        resp = execute(rendered)
        print(f"<== [{rendered.name}] status={resp.status}", file=out)
        if verbose:
            print(f"    body: {resp.body_text}", file=out)

        captured: dict[str, Any] = {}
        if rendered.capture:
            if resp.body_json is None:
                raise RuntimeError(
                    f"step {rendered.name!r}: capture requested but response is not JSON"
                )
            for var_name, path in rendered.capture.items():
                value = extract(resp.body_json, path)
                captured[var_name] = value
                if verbose:
                    print(f"    capture {var_name} = {value!r}", file=out)

        store["steps"][rendered.name] = captured

    return store
