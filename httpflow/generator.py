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
from .config import WorkflowConfig
from .model import HttpStep, SleepStep, Step, WorkflowSpec, from_config
from .template import PATTERN


_TEMPLATE_PATH = Path(__file__).parent / "templates" / "runner.py.tmpl"
_EMBEDDED_RUNTIME_PATH = Path(__file__).parent / "embedded_runtime.py"

_NO_REPEAT_HELPERS = '''\
# (no ${repeat.*} references — helpers omitted)
REQUIRED_REPEAT_VARS = set()
'''

_ARGPARSE_REPEAT = '''    p.add_argument("--repeat-vars", action="append", default=[], metavar="K=V1,V2,...",
                   help=_default_repeat_vars_help())'''

_MAIN_REPEAT_SETUP = '''    try:
        iterations = build_repeat_iterations_from_args(args.repeat_vars, DEFAULT_REPEAT_VARS, REQUIRED_REPEAT_VARS)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    total = len(iterations)

    for _idx, _repeat_iter in enumerate(iterations, start=1):
        store["repeat"] = dict(_repeat_iter)
        if _repeat_iter:
            print(f"=== repeat iteration {_idx}/{total} {_repeat_iter} ===")'''

_MAIN_NO_REPEAT_SETUP = "    store['repeat'] = {}"


# ---------------------------------------------------------------- literal helpers


def _collect_required_var_names(spec: WorkflowSpec, default_vars: dict[str, str]) -> list[str]:
    """Return ``${var.<name>}`` names not embedded in ``DEFAULT_VARS``."""
    found: set[str] = set()

    def scan(text: str | None) -> None:
        if not text:
            return
        for match in PATTERN.finditer(text):
            path = match.group(1)
            if not path:
                continue
            parts = path.split(".")
            if len(parts) == 2 and parts[0] == "var":
                found.add(parts[1])

    for step in spec.steps:
        if isinstance(step, HttpStep):
            scan(step.url)
            for k, v in step.headers.items():
                scan(k)
                scan(v)
            if step.body is not None:
                if hasattr(step.body, "text"):
                    scan(step.body.text)
                elif hasattr(step.body, "fields"):
                    for k, v in step.body.fields.items():
                        scan(k)
                        scan(v)
            if step.until is not None:
                scan(step.until.condition)
    return sorted(found - set(default_vars))


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
    if capture:
        args.append("capture=" + _dict_literal(capture, indent=pad))
    if description is not None:
        args.append(f"description={description!r}")
    args.append("quiet=quiet, pretty_json=pretty_json, no_mask=no_mask")
    return f"{indent}run_step(\n" + ",\n".join(f"{pad}{a}" for a in args) + f",\n{indent})"


def _emit_sleep_step(step: SleepStep, func_name: str) -> str:
    """Sleep step: a single ``run_step(...)`` call."""
    return "\n".join([
        f"def {func_name}(store, quiet=False, pretty_json=False, no_mask=False):",
        f'    """[[requests]] name = {step.name!r} — SLEEP {step.seconds}"""',
        _emit_run_step_call(
            step.name,
            "SLEEP",
            step.seconds,
            description=step.description,
            indent="    ",
        ),
    ])


def _emit_http_step(step: HttpStep, func_name: str) -> str:
    """Plain HTTP step: docstring + a single ``run_step(...)`` call."""
    body: str | None = None
    body_form: dict[str, str] | None = None
    if step.body is not None:
        if hasattr(step.body, "text"):
            body = step.body.text
        elif hasattr(step.body, "fields"):
            body_form = step.body.fields
    return "\n".join([
        f"def {func_name}(store, quiet=False, pretty_json=False, no_mask=False):",
        f'    """[[requests]] name = {step.name!r} — {step.method.upper()} {step.url}"""',
        _emit_run_step_call(
            step.name,
            step.method,
            step.url,
            headers=step.headers or None,
            body=body,
            body_form=body_form,
            capture=step.capture or None,
            description=step.description,
            indent="    ",
        ),
    ])


def _emit_until_step(step: HttpStep, func_name: str) -> str:
    """HTTP step wrapped in a ``poll_until`` loop."""
    assert step.until is not None
    u = step.until
    body: str | None = None
    body_form: dict[str, str] | None = None
    if step.body is not None:
        if hasattr(step.body, "text"):
            body = step.body.text
        elif hasattr(step.body, "fields"):
            body_form = step.body.fields
    return "\n".join([
        f"def {func_name}(store, quiet=False, pretty_json=False, no_mask=False):",
        f'    """[[requests]] name = {step.name!r} — {step.method.upper()} {step.url}"""',
        "    def attempt():",
        _emit_run_step_call(
            step.name,
            step.method,
            step.url,
            headers=step.headers or None,
            body=body,
            body_form=body_form,
            capture=step.capture or None,
            description=step.description,
            indent="        ",
        ),
        f"    poll_until({step.name!r}, attempt, {u.condition!r}, "
        f"{u.interval!r}, {u.max_attempts!r}, store, quiet)",
    ])


def _emit_step(step: Step, func_name: str) -> str:
    if isinstance(step, SleepStep):
        return _emit_sleep_step(step, func_name)
    if isinstance(step, HttpStep):
        if step.until is not None:
            return _emit_until_step(step, func_name)
        return _emit_http_step(step, func_name)
    raise TypeError(f"unknown step type: {type(step).__name__}")


# ---------------------------------------------------------------- public API


def _list_dict_literal(d: dict[str, list[str]], indent: str = "    ") -> str:
    if not d:
        return "{}"
    inner = indent + "    "
    lines = ["{"]
    for k, v in d.items():
        items = ", ".join(repr(x) for x in v)
        lines.append(f"{inner}{k!r}: [" + items + "],")
    lines.append(f"{indent}}}")
    return "\n".join(lines)


def generate(
    spec: WorkflowSpec | WorkflowConfig,
    *,
    default_vars: dict[str, str] | None = None,
    default_repeat_vars: dict[str, list[str]] | None = None,
    shebang: bool = False,
) -> str:
    """Return the source of a self-contained runner script."""
    if isinstance(spec, WorkflowConfig):
        spec = from_config(spec)

    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    embedded_runtime = "\n".join(
        line for line in _EMBEDDED_RUNTIME_PATH.read_text(encoding="utf-8").splitlines()
        if line != "from __future__ import annotations"
    )
    timestamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    default_vars = dict(default_vars or {})

    used: set[str] = set()
    step_blocks: list[str] = []
    step_calls: list[str] = []

    for step in spec.steps:
        fn = _sanitize_ident(step.name, used)
        step_blocks.append(_emit_step(step, fn))
        step_calls.append(
            f"{fn}(store, quiet=args.quiet, pretty_json=args.pretty_json, no_mask=args.no_mask)"
        )

    if not step_blocks:
        step_functions_src = "# (no [[requests]] blocks in source TOML)"
        step_calls_src = "pass  # no steps"
    else:
        step_functions_src = "\n\n\n".join(step_blocks)
        step_calls_src = "\n".join(step_calls)

    # Required ${repeat.<name>} variables
    from .runner import collect_repeat_names
    required_repeat = sorted(collect_repeat_names(spec))
    needs_repeat = bool(required_repeat)
    if required_repeat:
        repeat_lit = "{" + ", ".join(repr(n) for n in required_repeat) + "}"
    else:
        repeat_lit = "set()"

    has_until = any(isinstance(step, HttpStep) and step.until is not None for step in spec.steps)
    until_helpers = (
        "# (poll_until is provided by embedded_runtime)"
        if has_until else "# (no until blocks — helpers omitted)"
    )
    repeat_helpers = (
        "# Names referenced by ${repeat.<name>} in the source TOML. Each one MUST be\n"
        "# supplied at runtime via --repeat-vars NAME=v1,v2,... unless it is embedded\n"
        f"# in DEFAULT_REPEAT_VARS below.\nREQUIRED_REPEAT_VARS = {repeat_lit}"
        if needs_repeat else _NO_REPEAT_HELPERS
    )
    argparse_repeat = _ARGPARSE_REPEAT if needs_repeat else ""
    main_repeat_setup = _MAIN_REPEAT_SETUP if needs_repeat else _MAIN_NO_REPEAT_SETUP

    # Indent step calls for repeat loop nesting
    if needs_repeat:
        step_calls_src = "\n".join(
            f"        {line}" for line in step_calls_src.splitlines()
        )
        step_calls_src = (
            "        # === Workflow ===\n"
            "        # Comment out a line to skip that step. "
            "Reorder lines to change execution order.\n"
            + step_calls_src
        )
    else:
        step_calls_src = (
            "    # === Workflow ===\n"
            "    # Comment out a line to skip that step. "
            "Reorder lines to change execution order.\n"
            + "\n".join(
                f"    {line}" for line in step_calls_src.splitlines()
            )
        )

    rendered = (
        template
        .replace("{{VERSION}}", __version__)
        .replace("{{GENERATED_AT}}", timestamp)
        .replace("{{EMBEDDED_RUNTIME}}", embedded_runtime)
        .replace("{{DEFAULT_VARS}}", _dict_literal(default_vars, indent=""))
        .replace("{{REQUIRED_VARS}}", _list_literal(_collect_required_var_names(spec, default_vars), indent=""))
        .replace("{{DEFAULT_REPEAT_VARS}}", _list_dict_literal(dict(default_repeat_vars or {}), indent=""))
        .replace("{{UNTIL_HELPERS}}", until_helpers)
        .replace("{{REPEAT_HELPERS}}", repeat_helpers)
        .replace("{{ARGPARSE_REPEAT}}", argparse_repeat)
        .replace("{{MAIN_REPEAT_SETUP}}", main_repeat_setup)
        .replace("{{STEP_FUNCTIONS}}", step_functions_src)
        .replace("{{STEP_CALLS}}", step_calls_src)
    )

    if shebang:
        rendered = "#!/usr/bin/env python3\n" + rendered

    compile(rendered, "<generated httpflow runner>", "exec")
    return rendered
