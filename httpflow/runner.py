"""Workflow step execution engine backed by runtime helpers."""

from __future__ import annotations

import sys
from typing import Any

from .config import WorkflowConfig, to_model
from .model import FormBody, HttpStep, SleepStep, TextBody, WorkflowSpec
from .runtime.http import run_step
from .runtime.repeat import build_repeat_iterations
from .runtime.until import poll_until
from .template import find_repeat_names, find_var_names


def collect_repeat_names(spec: WorkflowSpec | WorkflowConfig) -> set[str]:
    """Return every ``${repeat.<name>}`` referenced anywhere in ``spec``."""
    if isinstance(spec, WorkflowConfig):
        spec = to_model(spec)
    found: set[str] = set()
    for step in spec.steps:
        if isinstance(step, SleepStep):
            found.update(find_repeat_names(step.seconds))
            continue
        if isinstance(step, HttpStep):
            found.update(find_repeat_names(step.url))
            for k, v in step.headers.items():
                found.update(find_repeat_names(k))
                found.update(find_repeat_names(v))
            if step.body is not None:
                if isinstance(step.body, TextBody):
                    found.update(find_repeat_names(step.body.text))
                elif isinstance(step.body, FormBody):
                    for k, v in step.body.fields.items():
                        found.update(find_repeat_names(k))
                        found.update(find_repeat_names(v))
            if step.until is not None:
                found.update(find_repeat_names(step.until.condition))
    return found


def collect_var_names(spec: WorkflowSpec | WorkflowConfig) -> set[str]:
    """Return every explicit ``${var.<name>}`` referenced anywhere in ``spec``."""
    if isinstance(spec, WorkflowConfig):
        spec = to_model(spec)
    found: set[str] = set()
    for step in spec.steps:
        if isinstance(step, SleepStep):
            found.update(find_var_names(step.seconds))
            continue
        if isinstance(step, HttpStep):
            found.update(find_var_names(step.url))
            for k, v in step.headers.items():
                found.update(find_var_names(k))
                found.update(find_var_names(v))
            if step.body is not None:
                if isinstance(step.body, TextBody):
                    found.update(find_var_names(step.body.text))
                elif isinstance(step.body, FormBody):
                    for k, v in step.body.fields.items():
                        found.update(find_var_names(k))
                        found.update(find_var_names(v))
            if step.until is not None:
                found.update(find_var_names(step.until.condition))
    return found


def validate_required_vars(
    spec: WorkflowSpec | WorkflowConfig,
    vars_: dict[str, str] | None,
) -> None:
    """Raise ValueError if required ``${var.*}`` parameters are missing."""
    missing = collect_var_names(spec) - set(vars_ or {})
    if missing:
        raise ValueError(f"missing required -v/--var for: {sorted(missing)}")


def run(
    spec: WorkflowSpec | WorkflowConfig,
    vars_: dict[str, str] | None = None,
    *,
    quiet: bool = False,
    pretty_json: bool = False,
    no_mask: bool = False,
    repeat_vars: dict[str, list[str]] | None = None,
    out=sys.stdout,
) -> dict[str, Any]:
    """Run every step in ``spec`` and return the final variable store."""
    if isinstance(spec, WorkflowConfig):
        spec = to_model(spec)

    required_repeat = collect_repeat_names(spec)
    iterations = build_repeat_iterations(repeat_vars, required_repeat)

    store: dict[str, Any] = {
        "vars": dict(vars_ or {}),
        "repeat": {},
    }
    validate_required_vars(spec, store["vars"])

    total = len(iterations)
    for idx, repeat_iter in enumerate(iterations, start=1):
        store["repeat"] = dict(repeat_iter)
        if repeat_iter:
            print(
                f"=== repeat iteration {idx}/{total} {repeat_iter} ===",
                file=out,
            )
        _run_once(
            spec, store, quiet=quiet, pretty_json=pretty_json,
            no_mask=no_mask, out=out,
        )

    return store


def _run_once(
    spec: WorkflowSpec,
    store: dict[str, Any],
    *,
    quiet: bool,
    pretty_json: bool,
    no_mask: bool,
    out,
) -> None:
    """Execute every step in ``spec`` exactly once against ``store``."""
    for step in spec.steps:
        if isinstance(step, SleepStep):
            run_step(
                store,
                step.name,
                "SLEEP",
                step.seconds,
                description=step.description,
                quiet=quiet,
                pretty_json=pretty_json,
                no_mask=no_mask,
                out=out,
            )
            continue

        # HttpStep
        assert isinstance(step, HttpStep)
        body: str | None = None
        body_form: dict[str, str] | None = None
        if isinstance(step.body, TextBody):
            body = step.body.text
        elif isinstance(step.body, FormBody):
            body_form = step.body.fields

        until = step.until
        if until is None:
            run_step(
                store,
                step.name,
                step.method,
                step.url,
                headers=step.headers,
                body=body,
                body_form=body_form,
                capture=step.capture,
                description=step.description,
                quiet=quiet,
                pretty_json=pretty_json,
                no_mask=no_mask,
                out=out,
            )
            continue

        # HTTP step with `until` polling.
        def attempt() -> None:
            run_step(
                store,
                step.name,
                step.method,
                step.url,
                headers=step.headers,
                body=body,
                body_form=body_form,
                capture=step.capture,
                description=step.description,
                quiet=quiet,
                pretty_json=pretty_json,
                no_mask=no_mask,
                out=out,
            )

        poll_until(
            step.name,
            attempt,
            until.condition,
            until.interval,
            until.max_attempts,
            store,
            quiet,
            out=out,
        )
