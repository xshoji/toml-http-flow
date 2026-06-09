"""Workflow step execution engine backed by runtime helpers."""

from __future__ import annotations

import sys
from typing import Any, Iterator

from .model import FileBody, FormBody, HttpStep, MultipartBody, MultipartField, MultipartFile, SleepStep, TextBody, WorkflowSpec
from .runtime.http import run_step
from .runtime.until import poll_until
from .template import find_var_names


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
                    case FileBody():
                        yield step.body.path
                    case MultipartBody():
                        for part in step.body.parts:
                            yield part.name
                            match part:
                                case MultipartField():
                                    yield part.value
                                case MultipartFile():
                                    yield part.path
                                    if part.filename is not None:
                                        yield part.filename
                                    yield part.content_type
                    case None:
                        pass
                if step.until is not None:
                    yield step.until.condition


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
        examples = " ".join(f'--var "{name}=<value>"' for name in sorted(missing))    
        raise ValueError(
            f"missing required variable(s): {', '.join(sorted(missing))}\n"
            f"Example: {examples}"
        )

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
    steps: list[str] | None = None,
    blank_line: int = 0,
    out=None,
) -> dict[str, Any]:
    """Run every step in ``spec`` and return the final variable store.

    When ``steps`` is given, only the named steps are executed (in TOML
    order); validation of required vars then applies to that subset only.
    """
    if steps:
        spec = select_steps(spec, steps)

    store: dict[str, Any] = {
        "vars": dict(vars_ or {}),
    }
    validate_required_vars(spec, store["vars"])

    out = sys.stdout if out is None else out

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
        body_file: str | None = None
        body_multipart: list[dict[str, str | None]] | None = None
        match step.body:
            case TextBody():
                body = step.body.text
            case FormBody():
                body_form = step.body.fields
            case FileBody():
                body_file = step.body.path
            case MultipartBody():
                body_multipart = []
                for part in step.body.parts:
                    match part:
                        case MultipartField():
                            body_multipart.append({"kind": "field", "name": part.name, "value": part.value})
                        case MultipartFile():
                            body_multipart.append({
                                "kind": "file",
                                "name": part.name,
                                "path": part.path,
                                "filename": part.filename,
                                "content_type": part.content_type,
                            })
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
                body_file=body_file,
                body_multipart=body_multipart,
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
                body_file=body_file,
                body_multipart=body_multipart,
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
