"""Workflow step execution engine backed by runtime helpers."""

from __future__ import annotations

import sys
from typing import Any, Iterator

from .model import FormBody, HttpStep, SleepStep, TextBody, WorkflowSpec
from .runtime.http import run_step
from .runtime.repeat import build_repeat_iterations
from .runtime.until import poll_until
from .template import find_repeat_names, find_var_names


def _iter_template_strings(spec: WorkflowSpec) -> Iterator[str]:
    """Yield every template-containing string in ``spec``."""
    for step in spec.steps:
        match step:
            case SleepStep():
                yield step.seconds
            case HttpStep():
                yield step.url
                yield from step.headers
                yield from step.headers.values()
                match step.body:
                    case TextBody():
                        yield step.body.text
                    case FormBody():
                        yield from step.body.fields
                        yield from step.body.fields.values()
                    case None:
                        pass
                if step.until is not None:
                    yield step.until.condition


def collect_repeat_names(spec: WorkflowSpec) -> set[str]:
    """Return every ``${repeat.<name>}`` referenced anywhere in ``spec``."""
    return {n for s in _iter_template_strings(spec) for n in find_repeat_names(s)}


def collect_var_names(spec: WorkflowSpec) -> set[str]:
    """Return every explicit ``${var.<name>}`` referenced anywhere in ``spec``."""
    return {n for s in _iter_template_strings(spec) for n in find_var_names(s)}


def validate_required_vars(
    spec: WorkflowSpec,
    vars_: dict[str, str] | None,
) -> None:
    """Raise ValueError if required ``${var.*}`` parameters are missing."""
    missing = collect_var_names(spec) - set(vars_ or {})
    if missing:
        raise ValueError(f"missing required -v/--var for: {sorted(missing)}")


def select_steps(spec: WorkflowSpec, names: list[str]) -> WorkflowSpec:
    """Return a new spec keeping only the named steps, in TOML order."""
    available = [step.name for step in spec.steps]
    missing = [n for n in names if n not in available]
    if missing:
        raise ValueError(
            f"unknown step name(s): {missing} (available: {available})"
        )
    wanted = set(names)
    return WorkflowSpec(steps=[s for s in spec.steps if s.name in wanted])


def run(
    spec: WorkflowSpec,
    vars_: dict[str, str] | None = None,
    *,
    quiet: bool = False,
    pretty_json: bool = False,
    no_mask: bool = False,
    repeat_vars: dict[str, list[str]] | None = None,
    steps: list[str] | None = None,
    blank_line: int = 0,
    out=None,
) -> dict[str, Any]:
    """Run every step in ``spec`` and return the final variable store.

    When ``steps`` is given, only the named steps are executed (in TOML
    order); validation of required vars and repeat names then applies to
    that subset only.
    """
    if steps:
        spec = select_steps(spec, steps)

    required_repeat = collect_repeat_names(spec)
    iterations = build_repeat_iterations(repeat_vars, required_repeat)

    store: dict[str, Any] = {
        "vars": dict(vars_ or {}),
        "repeat": {},
    }
    validate_required_vars(spec, store["vars"])

    out = sys.stdout if out is None else out

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
            no_mask=no_mask, blank_line=blank_line, out=out,
        )

    return store


def _run_once(
    spec: WorkflowSpec,
    store: dict[str, Any],
    *,
    quiet: bool,
    pretty_json: bool,
    no_mask: bool,
    blank_line: int,
    out,
) -> None:
    """Execute every step in ``spec`` exactly once against ``store``."""
    for index, step in enumerate(spec.steps):
        if blank_line and index > 0:
            for _ in range(blank_line):
                print(file=out)
        match step:
            case SleepStep():
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

        assert isinstance(step, HttpStep)
        body: str | None = None
        body_form: dict[str, str] | None = None
        match step.body:
            case TextBody():
                body = step.body.text
            case FormBody():
                body_form = step.body.fields
            case None:
                pass

        if step.until is None:
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
            step.until.condition,
            step.until.interval,
            step.until.max_attempts,
            store,
            quiet,
            out=out,
        )
