"""Workflow step execution engine backed by embedded_runtime helpers."""

from __future__ import annotations

import sys
import time
from typing import Any

from .config import WorkflowConfig, to_model
from .embedded_runtime import build_repeat_iterations, eval_until, run_step
from .model import FormBody, HttpStep, SleepStep, TextBody, WorkflowSpec
from .template import find_repeat_names


def collect_repeat_names(spec: WorkflowSpec | WorkflowConfig) -> set[str]:
    """Return every ``${repeat.<name>}`` referenced anywhere in ``spec``."""
    if isinstance(spec, WorkflowConfig):
        spec = to_model(spec)
    found: set[str] = set()
    for step in spec.steps:
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
        for attempt in range(1, until.max_attempts + 1):
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
            if eval_until(until.condition, store):
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
                f"step {step.name!r}: until condition not satisfied "
                f"after {until.max_attempts} attempts: {until.condition!r}"
            )
