from __future__ import annotations

from dataclasses import dataclass

from httpflow.model import HttpStep, Step, WorkflowSpec
from httpflow.runner import collect_var_names

from .capture import is_json_capture_source
from .names import step_function_name


@dataclass(frozen=True)
class GenerateOptions:
    """Options for bash script generation."""

    shebang: bool = False
    default_vars: dict[str, str] | None = None


@dataclass(frozen=True)
class StepPlan:
    """A workflow step paired with its generated bash function name."""

    index: int
    step: Step
    function_name: str
    attempt_function_name: str | None = None


@dataclass(frozen=True)
class WorkflowAnalysis:
    """Precomputed facts needed to render a bash script."""

    steps: tuple[StepPlan, ...]
    captured_vars: frozenset[str]
    required_vars: tuple[str, ...]
    has_capture: bool
    has_until: bool
    needs_jq: bool


def analyze_workflow(spec: WorkflowSpec, default_vars: dict[str, str] | None = None) -> WorkflowAnalysis:
    """Return the bash generation plan and feature flags for a workflow."""
    used: set[str] = set()
    plans: list[StepPlan] = []
    captured_vars: set[str] = set(
        var
        for s in spec.steps
        if isinstance(s, HttpStep)
        for var in s.capture.keys()
    )
    has_capture = any(isinstance(s, HttpStep) and bool(s.capture) for s in spec.steps)
    has_until = any(isinstance(s, HttpStep) and s.until is not None for s in spec.steps)
    needs_jq = any(
        isinstance(s, HttpStep)
        and any(is_json_capture_source(source) for source in s.capture.values())
        for s in spec.steps
    )

    for i, s in enumerate(spec.steps):
        fn = step_function_name(s.name, used)
        attempt_fn = None
        if isinstance(s, HttpStep) and s.until is not None:
            attempt_fn = step_function_name(f"{s.name}_attempt", used)
        plans.append(StepPlan(index=i, step=s, function_name=fn, attempt_function_name=attempt_fn))

    required = sorted(
        collect_var_names(spec) - captured_vars - set(default_vars or {})
    )

    return WorkflowAnalysis(
        steps=tuple(plans),
        captured_vars=frozenset(captured_vars),
        required_vars=tuple(required),
        has_capture=has_capture,
        has_until=has_until,
        needs_jq=needs_jq,
    )
