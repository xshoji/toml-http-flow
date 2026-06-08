from __future__ import annotations

import datetime

from httpflow.model import WorkflowSpec

from .analysis import GenerateOptions, analyze_workflow
from .placeholders import PlaceholderRenderer
from .script import ScriptRenderer
from .steps import StepEmitter


def generate(
    spec: WorkflowSpec,
    *,
    shebang: bool = False,
    default_vars: dict[str, str] | None = None,
) -> str:
    """Generate a standalone bash script from a workflow spec."""

    options = GenerateOptions(
        shebang=shebang,
        default_vars=dict(default_vars or {}),
    )
    generated_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    analysis = analyze_workflow(spec, options.default_vars)
    placeholders = PlaceholderRenderer(set(analysis.captured_vars))
    emitter = StepEmitter(placeholders)

    step_blocks = [emitter.emit(plan) for plan in analysis.steps]
    calls = [f"    {plan.function_name} || exit $?" for plan in analysis.steps]

    return ScriptRenderer().render(
        analysis,
        step_blocks,
        calls,
        generated_at=generated_at,
        options=options,
    )
