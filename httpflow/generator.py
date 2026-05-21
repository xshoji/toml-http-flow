"""Generate a standalone single-file Python runner from a WorkflowConfig.

Each ``[[requests]]`` block becomes its own ``step_<name>`` function so that
individual steps can be tweaked or re-run by hand without re-running
``httpflow generate``. The bulk of the work (rendering, sending, logging and
capturing) lives in the ``run_step`` helper inlined from the template, so each
step function stays small and reads as a declaration of the request data.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from . import __version__
from .config import SPECIAL_METHODS, RequestConfig, WorkflowConfig


_TEMPLATE_PATH = Path(__file__).parent / "templates" / "runner.py.tmpl"


# ---------------------------------------------------------------- literal helpers


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


def _emit_sleep_step(req: RequestConfig, func_name: str) -> str:
    """SLEEP: kept inline (no headers / no do_request) for human readability."""
    lines = [
        f"def {func_name}(store, quiet=False, pretty_json=False, no_mask=False):",
        f'    """[[requests]] name = {req.name!r} — SLEEP {req.url}"""',
        f"    name = {req.name!r}",
        f"    url = render({_str_literal(req.url)}, store)",
        '    print(f"==> {_now()} [{name}] SLEEP {url}")',
    ]
    if req.description:
        lines.append(f"    for _ln in {req.description!r}.splitlines() or ['']:")
        lines.append('        print(f"    # {_ln}")')
    lines += [
        "    seconds = float(url)",
        "    if not quiet:",
        '        print(f"    > sleep {seconds} seconds")',
        "    time.sleep(seconds)",
        '    print(f"<== {_now()} [{name}] done")',
        '    store["steps"][name] = {}',
    ]
    return "\n".join(lines)


def _emit_run_step_call(req: RequestConfig, indent: str = "    ") -> str:
    """Build a ``run_step(...)`` invocation for an HTTP request."""
    pad = indent + "    "
    args: list[str] = [
        f"store, {req.name!r}, {req.method.upper()!r}, {_str_literal(req.url)}"
    ]
    if req.headers:
        args.append("headers=" + _dict_literal(req.headers, indent=pad))
    if req.body is not None:
        args.append("body=" + _str_literal(req.body))
    if req.body_form is not None:
        args.append("body_form=" + _dict_literal(req.body_form, indent=pad))
    if req.capture:
        args.append("capture=" + _dict_literal(req.capture, indent=pad))
    if req.description:
        args.append(f"description={req.description!r}")
    args.append("quiet=quiet, pretty_json=pretty_json, no_mask=no_mask")
    return f"{indent}run_step(\n" + ",\n".join(f"{pad}{a}" for a in args) + f",\n{indent})"


def _emit_http_step(req: RequestConfig, func_name: str) -> str:
    """Plain HTTP step: docstring + a single ``run_step(...)`` call."""
    return "\n".join([
        f"def {func_name}(store, quiet=False, pretty_json=False, no_mask=False):",
        f'    """[[requests]] name = {req.name!r} — {req.method.upper()} {req.url}"""',
        _emit_run_step_call(req, indent="    "),
    ])


def _emit_until_step(req: RequestConfig, func_name: str) -> str:
    """HTTP step wrapped in a ``poll_until`` loop."""
    assert req.until is not None
    u = req.until
    return "\n".join([
        f"def {func_name}(store, quiet=False, pretty_json=False, no_mask=False):",
        f'    """[[requests]] name = {req.name!r} — {req.method.upper()} {req.url}"""',
        "    def attempt():",
        _emit_run_step_call(req, indent="        "),
        f"    poll_until({req.name!r}, attempt, {u.condition!r}, "
        f"{u.interval!r}, {u.max_attempts!r}, store, quiet)",
    ])


def _emit_step(req: RequestConfig, func_name: str) -> str:
    if req.method in SPECIAL_METHODS:
        if req.method == "SLEEP":
            return _emit_sleep_step(req, func_name)
    if req.until is not None:
        return _emit_until_step(req, func_name)
    return _emit_http_step(req, func_name)


# ---------------------------------------------------------------- public API


def generate(
    cfg: WorkflowConfig,
    *,
    default_vars: dict[str, str] | None = None,
    shebang: bool = False,
) -> str:
    """Return the source of a self-contained runner script."""
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    timestamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    used: set[str] = set()
    step_blocks: list[str] = []
    step_calls: list[str] = []
    # 8-space indent: step calls live inside the per-iteration ``for`` loop
    # in ``main()`` so they can re-execute when --repeat-vars is supplied.
    for req in cfg.requests:
        fn = _sanitize_ident(req.name, used)
        step_blocks.append(_emit_step(req, fn))
        step_calls.append(
            f"        {fn}(store, quiet=args.quiet, pretty_json=args.pretty_json, no_mask=args.no_mask)"
        )

    if not step_blocks:
        step_functions_src = "# (no [[requests]] blocks in source TOML)"
        step_calls_src = "        pass  # no steps"
    else:
        step_functions_src = "\n\n\n".join(step_blocks)
        step_calls_src = "\n".join(step_calls)

    # Required ${repeat.<name>} variables, embedded as a Python set literal so
    # the generated script can validate --repeat-vars at runtime.
    from .workflow import collect_repeat_names
    required_repeat = sorted(collect_repeat_names(cfg))
    if required_repeat:
        repeat_lit = "{" + ", ".join(repr(n) for n in required_repeat) + "}"
    else:
        repeat_lit = "set()"

    rendered = (
        template
        .replace("{{VERSION}}", __version__)
        .replace("{{GENERATED_AT}}", timestamp)
        .replace("{{DEFAULT_VARS}}", _dict_literal(dict(default_vars or {}), indent=""))
        .replace("{{REQUIRED_REPEAT_VARS}}", repeat_lit)
        .replace("{{STEP_FUNCTIONS}}", step_functions_src)
        .replace("{{STEP_CALLS}}", step_calls_src)
    )

    if shebang:
        rendered = "#!/usr/bin/env python3\n" + rendered

    return rendered
