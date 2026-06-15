"""Generate a standalone single-file Python runner from a WorkflowSpec.

Each step becomes its own ``step_<name>`` function so that individual
steps can be tweaked or re-run by hand without re-running
``httpflow generate``. The bulk of the work (rendering, sending, logging and
capturing) lives in the ``run_step`` helper inlined from the template, so each
step function stays small and reads as a declaration of the request data.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from . import __version__
from .model import FileBody, FormBody, HttpStep, MultipartBody, MultipartField, MultipartFile, SleepStep, Step, TextBody, WorkflowSpec
from .runner import collect_var_names


_TEMPLATE_PATH = Path(__file__).parent / "templates" / "runner.py.tmpl"
_RUNTIME_DIR = Path(__file__).parent / "runtime"

_RUNTIME_DEPS: dict[str, tuple[str, ...]] = {
    "core": (),
    "mask": (),
    "http": ("core", "mask"),
    "until": ("core",),
}


# ---------------------------------------------------------------- generated-script cleanup


def _deduplicate_imports(src: str) -> str:
    """Collect all top-level import statements, deduplicate, sort, and move to the top."""
    import_re = re.compile(r"^(?:import\s+\S+|from\s+\S+\s+import\s+\S.*)$")
    imports: list[str] = []
    others: list[str] = []
    for line in src.splitlines(keepends=True):
        if import_re.match(line.strip()):
            imports.append(line.strip())
        else:
            others.append(line)

    unique: set[str] = set()
    for line in imports:
        if line.startswith("from "):
            m = re.match(r"from\s+([\w.]+)\s+import\s+(.+)", line)
            if m:
                mod = m.group(1)
                for name in (x.strip() for x in m.group(2).split(",")):
                    if name:
                        unique.add(f"from {mod} import {name}")
                continue
        unique.add(line)

    header = "".join(f"{line}\n" for line in sorted(unique))
    return header + "\n" + "".join(others)


def _cleanup_generated(src: str) -> str:
    """Remove duplicate imports only; docstrings and formatting are harmless."""
    return _deduplicate_imports(src)


def _fix_empty_bodies(src: str) -> str:
    """Insert ``pass`` into class/function bodies that became empty after docstring removal."""
    lines = src.splitlines(keepends=True)
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^(class|def)\s", line):
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and not re.match(r"^\s", lines[j]):
                result.append(line)
                result.append("    pass\n")
                i += 1
                continue
        result.append(line)
        i += 1
    return "".join(result)


# ---------------------------------------------------------------- runtime flattening


def _strip_docstrings(src: str) -> str:
    """Strip module- and function-level docstrings using ``ast``."""
    import ast

    tree = ast.parse(src)
    lines_to_remove: set[int] = set()

    def _collect_docstring_lines(body: list[ast.stmt]) -> None:
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node = body[0]
            start = node.lineno
            end = getattr(node, "end_lineno", start) or start
            lines_to_remove.update(range(start, end + 1))

    _collect_docstring_lines(tree.body)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _collect_docstring_lines(node.body)

    result: list[str] = []
    for idx, line in enumerate(src.splitlines(keepends=True), start=1):
        if idx not in lines_to_remove:
            result.append(line)
    return "".join(result)


def _resolve_runtime_modules(features: set[str]) -> list[str]:
    """Return runtime module names in deterministic dependency order."""
    resolved: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name in seen:
            return
        for dep in _RUNTIME_DEPS.get(name, ()):
            add(dep)
        seen.add(name)
        resolved.append(name)

    for name in ("core", "mask", "http", "until"):
        if name in features:
            add(name)
    return resolved


def _flatten_modules(features: set[str]) -> str:
    """Read selected runtime modules, strip package-only lines and docstrings, and concatenate."""
    modules = _resolve_runtime_modules(features)
    chunks: list[str] = []
    for name in modules:
        src = (_RUNTIME_DIR / f"{name}.py").read_text(encoding="utf-8")
        lines = []
        for line in src.splitlines():
            # Strip controlled package-only lines
            if line == "from __future__ import annotations":
                continue
            if line.strip().startswith("from ."):
                continue
            lines.append(line)
        cleaned = _strip_docstrings("\n".join(lines))
        chunks.append(cleaned)
    return _fix_empty_bodies("\n\n".join(chunks))


# ---------------------------------------------------------------- literal helpers


def _collect_required_var_names(spec: WorkflowSpec, default_vars: dict[str, str]) -> list[str]:
    """Return ``${var.<name>}`` names not embedded in ``DEFAULT_VARS``."""
    return sorted(collect_var_names(spec) - set(default_vars))


def _str_literal(s: str) -> str:
    """Return a Python source literal for ``s`` preferring triple-quoted form
    for multi-line strings, falling back to ``repr`` otherwise."""
    if "\n" in s and '"""' not in s and not s.endswith('"'):
        body = s.replace("\\", "\\\\")
        # The leading backslash-newline keeps the first line aligned without
        # producing an extra blank line inside the literal.
        return '"""\\\n' + body + '"""'
    return repr(s)


def _dict_literal(d: dict[str, str], indent: str = "    ") -> str:
    if not d:
        return "{}"
    inner = indent + "    "
    lines = ["{"]
    for k, v in d.items():
        lines.append(f"{inner}{k!r}: {_str_literal(v)},")
    lines.append(f"{indent}}}")
    return "\n".join(lines)


def _list_literal(items: list[str], indent: str = "") -> str:
    """Return a Python list literal for strings."""
    if not items:
        return "[]"
    inner = indent + "    "
    lines = ["["]
    for item in items:
        lines.append(f"{inner}{item!r},")
    lines.append(f"{indent}]")
    return "\n".join(lines)


def _multipart_literal(parts: list[dict[str, str | None]], indent: str = "    ") -> str:
    """Return a Python list literal for multipart part dictionaries."""
    if not parts:
        return "[]"
    inner = indent + "    "
    lines = ["["]
    for part in parts:
        lines.append(f"{inner}{{")
        for key, value in part.items():
            lines.append(f"{inner}    {key!r}: {value!r},")
        lines.append(f"{inner}}},")
    lines.append(f"{indent}]")
    return "\n".join(lines)


def _sanitize_ident(name: str, used: set[str]) -> str:
    """Turn a step name into a unique python identifier prefixed with ``step_``."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not cleaned or cleaned[0].isdigit():
        cleaned = "_" + cleaned
    base = "step_" + cleaned
    out = base
    i = 2
    while out in used:
        out = f"{base}_{i}"
        i += 1
    used.add(out)
    return out


# ---------------------------------------------------------------- step emitter


def _emit_run_step_call(
    name: str,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    body_form: dict[str, str] | None = None,
    body_file: str | None = None,
    body_multipart: list[dict[str, str | None]] | None = None,
    capture: dict[str, str] | None = None,
    description: str | None = None,
    indent: str = "    ",
) -> str:
    """Build a ``run_step(...)`` invocation."""
    pad = indent + "    "
    args: list[str] = [
        f"store, {name!r}, {method!r}, {_str_literal(url)}"
    ]
    if headers:
        args.append("headers=" + _dict_literal(headers, indent=pad))
    if body is not None:
        args.append("body=" + _str_literal(body))
    if body_form is not None:
        args.append("body_form=" + _dict_literal(body_form, indent=pad))
    if body_file is not None:
        args.append("body_file=" + _str_literal(body_file))
    if body_multipart is not None:
        args.append("body_multipart=" + _multipart_literal(body_multipart, indent=pad))
    if capture:
        args.append("capture=" + _dict_literal(capture, indent=pad))
    if description is not None:
        args.append(f"description={description!r}")
    args.append("quiet=quiet, pretty_json=pretty_json, no_mask=no_mask")
    return f"{indent}run_step(\n" + ",\n".join(f"{pad}{a}" for a in args) + f",\n{indent})"


def _emit_sleep_step(step: SleepStep, func_name: str) -> str:
    """Sleep step: a single ``run_step(...)`` call."""
    return "\n".join([
        f"def {func_name}(store, quiet=False, pretty_json=False, no_mask=False, blank_line=0):",
        f'    """[[requests]] name = {step.name!r} \u2014 SLEEP {step.seconds}"""',
        "    for _ in range(blank_line):",
        "        print()",
        _emit_run_step_call(
            step.name,
            "SLEEP",
            step.seconds,
            description=step.description,
            indent="    ",
        ),
    ])


def _body_parts(step: HttpStep) -> tuple[str | None, dict[str, str] | None, str | None, list[dict[str, str | None]] | None]:
    """Return body mode values for an HttpStep."""
    match step.body:
        case TextBody(text=t):
            return t, None, None, None
        case FormBody(fields=f):
            return None, f, None, None
        case FileBody(path=p):
            return None, None, p, None
        case MultipartBody(parts=parts):
            out: list[dict[str, str | None]] = []
            for part in parts:
                match part:
                    case MultipartField():
                        out.append({"kind": "field", "name": part.name, "value": part.value})
                    case MultipartFile():
                        out.append({
                            "kind": "file",
                            "name": part.name,
                            "path": part.path,
                            "filename": part.filename,
                            "content_type": part.content_type,
                        })
            return None, None, None, out
        case _:
            return None, None, None, None


def _emit_http_step(step: HttpStep, func_name: str) -> str:
    """Plain HTTP step: docstring + a single ``run_step(...)`` call."""
    body, body_form, body_file, body_multipart = _body_parts(step)
    return "\n".join([
        f"def {func_name}(store, quiet=False, pretty_json=False, no_mask=False, blank_line=0):",
        f'    """[[requests]] name = {step.name!r} \u2014 {step.method.upper()} {step.url}"""',
        "    for _ in range(blank_line):",
        "        print()",
        _emit_run_step_call(
            step.name,
            step.method,
            step.url,
            headers=step.headers or None,
            body=body,
            body_form=body_form,
            body_file=body_file,
            body_multipart=body_multipart,
            capture=step.capture or None,
            description=step.description,
            indent="    ",
        ),
    ])


def _emit_until_step(step: HttpStep, func_name: str) -> str:
    """HTTP step wrapped in a ``poll_until`` loop."""
    assert step.until is not None
    body, body_form, body_file, body_multipart = _body_parts(step)
    return "\n".join([
        f"def {func_name}(store, quiet=False, pretty_json=False, no_mask=False, blank_line=0):",
        f'    """[[requests]] name = {step.name!r} \u2014 {step.method.upper()} {step.url}"""',
        "    for _ in range(blank_line):",
        "        print()",
        "    def attempt():",
        _emit_run_step_call(
            step.name,
            step.method,
            step.url,
            headers=step.headers or None,
            body=body,
            body_form=body_form,
            body_file=body_file,
            body_multipart=body_multipart,
            capture=step.capture or None,
            description=step.description,
            indent="        ",
        ),
        f"    poll_until({step.name!r}, attempt, {step.until.condition!r}, "
        f"{step.until.interval!r}, {step.until.max_attempts!r}, store, quiet)",
    ])


def _emit_step(step: Step, func_name: str) -> str:
    match step:
        case SleepStep():
            return _emit_sleep_step(step, func_name)
        case HttpStep(until=None):
            return _emit_http_step(step, func_name)
        case HttpStep():
            return _emit_until_step(step, func_name)
        case _:
            raise TypeError(f"unknown step type: {type(step).__name__}")


# ---------------------------------------------------------------- public API


def generate(
    spec: WorkflowSpec,
    *,
    default_vars: dict[str, str] | None = None,
    shebang: bool = False,
) -> str:
    """Return the source of a self-contained runner script."""
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    timestamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    default_vars = dict(default_vars or {})

    used: set[str] = set()
    step_blocks: list[str] = []
    step_calls: list[str] = []

    for index, step in enumerate(spec.steps):
        fn = _sanitize_ident(step.name, used)
        step_blocks.append(_emit_step(step, fn))
        blank_line_arg = "0" if index == 0 else "args.blank_line"
        step_calls.append(
            f"{fn}(store, quiet=args.quiet, pretty_json=args.pretty_json, "
            f"no_mask=args.no_mask, blank_line={blank_line_arg})"
        )

    if not step_blocks:
        step_functions_src = "# (no [[requests]] blocks in source TOML)"
        step_calls_src = "pass  # no steps"
    else:
        step_functions_src = "\n\n\n".join(step_blocks)
        step_calls_src = "\n".join(step_calls)

    # Feature detection for runtime flattening
    features: set[str] = set()
    if spec.steps:
        features.add("http")
    if any(isinstance(step, HttpStep) and step.until is not None for step in spec.steps):
        features.add("until")

    has_until = "until" in features
    until_helpers = (
        "# (poll_until is provided by runtime helpers)"
        if has_until else "# (no until blocks \u2014 helpers omitted)"
    )
    step_calls_src = (
        "    # === Workflow ===\n"
        "    # Comment out a line to skip that step. "
        "Reorder lines to change execution order.\n"
        + "\n".join(
            f"    {line}" for line in step_calls_src.splitlines()
        )
    )

    runtime_helpers = _flatten_modules(features)

    rendered = (
        template
        .replace("{{VERSION}}", __version__)
        .replace("{{GENERATED_AT}}", timestamp)
        .replace("{{RUNTIME_HELPERS}}", runtime_helpers)
        .replace("{{DEFAULT_VARS}}", _dict_literal(default_vars, indent=""))
        .replace("{{REQUIRED_VARS}}", _list_literal(_collect_required_var_names(spec, default_vars), indent=""))
        .replace("{{UNTIL_HELPERS}}", until_helpers)
        .replace("{{STEP_FUNCTIONS}}", step_functions_src)
        .replace("{{STEP_CALLS}}", step_calls_src)
    )

    rendered = _cleanup_generated(rendered)

    if shebang:
        rendered = "#!/usr/bin/env python3\n" + rendered

    compile(rendered, "<generated httpflow runner>", "exec")
    return rendered
