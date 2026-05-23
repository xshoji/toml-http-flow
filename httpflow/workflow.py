"""Workflow step execution engine with a shared variable store."""

from __future__ import annotations

import datetime
import json
import sys
import time
import urllib.parse
from dataclasses import replace
from typing import Any

from .config import SPECIAL_METHODS, RequestConfig, WorkflowConfig
from .httpclient import execute, extract, prepare_request
from .masking import (
    mask,
    mask_url,
    mask_value,
)
from .template import find_repeat_names, render, render_mapping
from .until import evaluate as evaluate_condition


def collect_repeat_names(config: WorkflowConfig) -> set[str]:
    """Return every ``${repeat.<name>}`` referenced anywhere in ``config``."""
    found: set[str] = set()
    for req in config.requests:
        found.update(find_repeat_names(req.url))
        for k, v in req.headers.items():
            found.update(find_repeat_names(k))
            found.update(find_repeat_names(v))
        found.update(find_repeat_names(req.body))
        if req.body_form:
            for k, v in req.body_form.items():
                found.update(find_repeat_names(k))
                found.update(find_repeat_names(v))
        if req.until is not None:
            found.update(find_repeat_names(req.until.condition))
    return found


def build_repeat_iterations(
    repeat_vars: dict[str, list[str]] | None,
    required: set[str],
) -> list[dict[str, str]]:
    """Validate ``--repeat-vars`` input and expand it into per-iteration dicts.

    - Returns ``[{}]`` (one iteration, empty mapping) when nothing is required
      and nothing was supplied.
    - Raises ``ValueError`` when required names are missing, unknown extras
      are supplied, or value-lists have differing lengths.
    """
    repeat_vars = dict(repeat_vars or {})
    missing = required - set(repeat_vars)
    if missing:
        raise ValueError(
            f"--repeat-vars missing for: {sorted(missing)}"
        )
    if not repeat_vars:
        return [{}]
    lengths = {k: len(v) for k, v in repeat_vars.items()}
    distinct = set(lengths.values())
    if len(distinct) != 1:
        raise ValueError(
            f"--repeat-vars value counts must match across all keys, got: {lengths}"
        )
    n = distinct.pop()
    if n == 0:
        raise ValueError("--repeat-vars must supply at least one value per key")
    return [{k: repeat_vars[k][i] for k in repeat_vars} for i in range(n)]


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


def _maybe_pretty_json(text: str, pretty_json: bool) -> str:
    """Return ``text`` re-formatted as 2-space-indent JSON when applicable.

    If ``pretty_json`` is False, or ``text`` is empty / not parseable as JSON,
    the input is returned unchanged.
    """
    if not pretty_json or not text:
        return text
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    return json.dumps(parsed, indent=2, ensure_ascii=False)


def _log_description(req: RequestConfig, out) -> None:
    """Print the optional per-step description right after the ``==>`` line."""
    if not req.description:
        return
    for line in req.description.splitlines() or [""]:
        print(f"    # {line}", file=out)


def _log_request(
    req: RequestConfig, out, *, pretty_json: bool = False, no_mask: bool = False
) -> None:
    """Print the request line and headers that urllib will actually send."""
    request, body_bytes = prepare_request(req)

    # Request line: e.g. POST /auth HTTP/1.1 (query masked)
    parsed = urllib.parse.urlparse(mask_url(req.url, disabled=no_mask))
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    print(f"    > {req.method.upper()} {path} HTTP/1.1", file=out)

    # Host header (estimated)
    print(f"    > Host: {parsed.netloc}", file=out)

    # Explicit user headers (masked)
    for k, v in req.headers.items():
        print(f"    > {k}: {mask_value(k, v, disabled=no_mask)}", file=out)

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

    # Body (masked, then pretty-printed)
    if req.body is not None:
        body_text = _maybe_pretty_json(mask(req.body, disabled=no_mask), pretty_json)
        print("    >", file=out)
        for line in body_text.splitlines() or [""]:
            print(f"    > {line}", file=out)
    elif req.body_form is not None:
        print("    > (form)", file=out)
        for k, v in req.body_form.items():
            print(f"    >   {k} = {mask_value(k, v, disabled=no_mask)}", file=out)


def _log_response(resp, out, *, pretty_json: bool = False, no_mask: bool = False) -> None:
    """Print the HTTP status line and response headers/body."""
    print(f"    < HTTP/1.1 {resp.status} {resp.reason}", file=out)
    for k, v in resp.headers.items():
        print(f"    < {k}: {mask_value(k, v, disabled=no_mask)}", file=out)
    if resp.body_text:
        body_text = _maybe_pretty_json(mask(resp.body_text, disabled=no_mask), pretty_json)
        print("    <", file=out)
        for line in body_text.splitlines():
            print(f"    < {line}", file=out)


def _execute_http_attempt(
    req: RequestConfig,
    store: dict[str, Any],
    *,
    quiet: bool,
    out,
    pretty_json: bool = False,
    no_mask: bool = False,
) -> None:
    """Render, send, log, and capture a single HTTP attempt.

    On return, ``store["steps"][req.name]`` is updated with captured values.
    """
    rendered = _render_request(req, store)
    print(
        f"==> {_now()} [{rendered.name}] {rendered.method} {mask_url(rendered.url, disabled=no_mask)}",
        file=out,
    )
    _log_description(rendered, out)

    if not quiet:
        _log_request(rendered, out, pretty_json=pretty_json, no_mask=no_mask)

    resp = execute(rendered)
    print(f"<== {_now()} [{rendered.name}] status={resp.status}", file=out)
    if not quiet:
        _log_response(resp, out, pretty_json=pretty_json, no_mask=no_mask)

    captured: dict[str, Any] = {}
    if rendered.capture:
        if resp.body_json is None:
            raise RuntimeError(
                f"step {rendered.name!r}: capture requested but response is not JSON"
            )
        for var_name, path in rendered.capture.items():
            value = extract(resp.body_json, path)
            captured[var_name] = value
            store["vars"][var_name] = value
            if not quiet:
                shown = mask_value(var_name, value, disabled=no_mask)
                print(f"    * capture {var_name} = {shown!r}", file=out)

    store["steps"][rendered.name] = captured


def run(
    config: WorkflowConfig,
    vars_: dict[str, str] | None = None,
    *,
    quiet: bool = False,
    pretty_json: bool = False,
    no_mask: bool = False,
    repeat_vars: dict[str, list[str]] | None = None,
    out=sys.stdout,
) -> dict[str, Any]:
    """Run every request in ``config`` and return the final variable store.

    By default each step's request and response details are printed to ``out``.
    Pass ``quiet=True`` to print only the one-line summary per step.
    Pass ``pretty_json=True`` to pretty-print JSON bodies with 2-space indent.
    Pass ``no_mask=True`` to disable masking of sensitive fields in log output.
    Pass ``repeat_vars`` to iterate the entire workflow once per index of the
    supplied value lists; ``${repeat.<name>}`` references inside the TOML
    resolve to the value for the current iteration.
    """
    required_repeat = collect_repeat_names(config)
    iterations = build_repeat_iterations(repeat_vars, required_repeat)

    store: dict[str, Any] = {
        "vars": dict(vars_ or {}),
        "steps": {},
        "repeat": {},
    }

    total = len(iterations)
    for idx, repeat_iter in enumerate(iterations, start=1):
        store["repeat"] = dict(repeat_iter)
        if repeat_iter:
            store["steps"] = {}
            print(
                f"=== repeat iteration {idx}/{total} {repeat_iter} ===",
                file=out,
            )
        _run_once(
            config, store, quiet=quiet, pretty_json=pretty_json,
            no_mask=no_mask, out=out,
        )

    return store


def _run_once(
    config: WorkflowConfig,
    store: dict[str, Any],
    *,
    quiet: bool,
    pretty_json: bool,
    no_mask: bool,
    out,
) -> None:
    """Execute every request in ``config`` exactly once against ``store``."""
    for req in config.requests:
        # SLEEP step: no HTTP, no until.
        if req.method in SPECIAL_METHODS:
            if req.method == "SLEEP":
                rendered_url = render(req.url, store)
                print(
                    f"==> {_now()} [{req.name}] {req.method} {rendered_url}",
                    file=out,
                )
                _log_description(req, out)
                try:
                    seconds = float(rendered_url)
                except ValueError as exc:
                    raise RuntimeError(
                        f"step {req.name!r}: 'SLEEP' url must be numeric, got: {rendered_url!r}"
                    ) from exc
                if not quiet:
                    print(f"    > sleep {seconds} seconds", file=out)
                time.sleep(seconds)
                print(f"<== {_now()} [{req.name}] done", file=out)
                store["steps"][req.name] = {}
                continue

        # Plain HTTP step (no polling).
        if req.until is None:
            _execute_http_attempt(req, store, quiet=quiet, out=out, pretty_json=pretty_json, no_mask=no_mask)
            continue

        # HTTP step with `until` polling.
        until = req.until
        for attempt in range(1, until.max_attempts + 1):
            _execute_http_attempt(req, store, quiet=quiet, out=out, pretty_json=pretty_json, no_mask=no_mask)
            if evaluate_condition(until.condition, store):
                if not quiet:
                    print(
                        f"    * until satisfied on attempt {attempt}",
                        file=out,
                    )
                break
            if attempt < until.max_attempts:
                if not quiet:
                    print(
                        f"    * until not satisfied "
                        f"(attempt {attempt}/{until.max_attempts}), "
                        f"retrying in {until.interval}s",
                        file=out,
                    )
                time.sleep(until.interval)
        else:
            raise RuntimeError(
                f"step {req.name!r}: until condition not satisfied "
                f"after {until.max_attempts} attempts: {until.condition!r}"
            )
