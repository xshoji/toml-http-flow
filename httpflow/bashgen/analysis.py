from __future__ import annotations

from dataclasses import dataclass

import base64
import os
import re
import warnings
from pathlib import Path

from httpflow.model import FileBody, HttpStep, MultipartBody, MultipartFile, Step, WorkflowSpec
from httpflow.runner import collect_var_names

from .capture import is_json_capture_source
from .names import step_function_name

_PLACEHOLDER_RE = re.compile(r'\$\{[^}]+\}')


@dataclass(frozen=True)
class GenerateOptions:
    """Options for bash script generation."""

    shebang: bool = False
    default_vars: dict[str, str] | None = None
    embed_files: bool = False


@dataclass(frozen=True)
class StepPlan:
    """A workflow step paired with its generated bash function name."""

    index: int
    step: Step
    function_name: str
    attempt_function_name: str | None = None


@dataclass(frozen=True)
class EmbeddedFile:
    """Metadata for an embedded file."""

    var_name: str
    b64_content: str


@dataclass(frozen=True)
class WorkflowAnalysis:
    """Precomputed facts needed to render a bash script."""

    steps: tuple[StepPlan, ...]
    captured_vars: frozenset[str]
    required_vars: tuple[str, ...]
    has_capture: bool
    has_until: bool
    needs_jq: bool
    embedded_files: tuple[EmbeddedFile, ...] = ()


def _resolve_path(original_path: str, toml_path: str | None) -> str:
    """Resolve a literal path relative to the TOML file's directory."""
    if os.path.isabs(original_path):
        return original_path
    if toml_path:
        return os.path.normpath(os.path.join(os.path.dirname(toml_path), original_path))
    return original_path


def _is_literal_path(path: str) -> bool:
    """Return True when *path* contains no ${...} placeholder."""
    return not bool(_PLACEHOLDER_RE.search(path))


def _embed_files_for(
    spec: WorkflowSpec,
    options: GenerateOptions,
    plans: list[StepPlan],
    toml_path: str | None = None,
) -> list[EmbeddedFile]:
    """Scan steps for literal file paths, read and base64-encode them.

    Returns a list of EmbeddedFile entries with unique var names.
    """
    if not options.embed_files:
        return []
    embedded: list[EmbeddedFile] = []
    for plan in plans:
        step = plan.step
        if not isinstance(step, HttpStep):
            continue
        fn = plan.function_name

        if isinstance(step.body, FileBody):
            path = step.body.path
            if not _is_literal_path(path):
                warnings.warn(
                    f"body_file path {path!r} contains placeholders, skipping embed"
                )
                continue
            resolved = _resolve_path(path, toml_path)
            with open(resolved, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            embedded.append(EmbeddedFile(var_name=f"{fn}_body", b64_content=b64))

        elif isinstance(step.body, MultipartBody):
            for idx, part in enumerate(step.body.parts):
                if not isinstance(part, MultipartFile):
                    continue
                path = part.path
                if not _is_literal_path(path):
                    warnings.warn(
                        f"multipart file path {path!r} contains placeholders, skipping embed"
                    )
                    continue
                resolved = _resolve_path(path, toml_path)
                with open(resolved, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                embedded.append(
                    EmbeddedFile(var_name=f"{fn}_mp{idx}", b64_content=b64)
                )
    return embedded


def analyze_workflow(
    spec: WorkflowSpec,
    default_vars: dict[str, str] | None = None,
    *,
    options: GenerateOptions | None = None,
    toml_path: str | None = None,
) -> WorkflowAnalysis:
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

    opts = options or GenerateOptions()
    embedded_files = _embed_files_for(spec, opts, plans, toml_path=toml_path)

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
        embedded_files=tuple(embedded_files),
    )
