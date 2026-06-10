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
    embed_files: bool = False,
    toml_path: str | None = None,
) -> str:
    """Generate a standalone bash script from a workflow spec."""

    options = GenerateOptions(
        shebang=shebang,
        default_vars=dict(default_vars or {}),
        embed_files=embed_files,
    )
    generated_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    analysis = analyze_workflow(
        spec,
        options.default_vars,
        options=options,
        toml_path=toml_path,
    )
    placeholders = PlaceholderRenderer(set(analysis.captured_vars))

    embedded_map: dict[str, str] = {}
    for ef in analysis.embedded_files:
        embedded_map[ef.var_name] = ef.b64_content
    emitter = StepEmitter(placeholders, embedded_map=embedded_map)

    step_blocks = [emitter.emit(plan) for plan in analysis.steps]
    calls = [f"    {plan.function_name} || exit $?" for plan in analysis.steps]

    return ScriptRenderer().render(
        analysis,
        step_blocks,
        calls,
        generated_at=generated_at,
        options=options,
    )
