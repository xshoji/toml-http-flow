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

# ------------------------------------------------------------------ inlined sources

_UNTIL_SRC = '''\
_UNTIL_OPS = [
    (re.compile(r"=="), "=="),
    (re.compile(r"!="), "!="),
    (re.compile(r"\\s+in\\s+"), "in"),
    (re.compile(r"~"), "~"),
]
_UNTIL_REGEX_RHS = re.compile(r"^/(.*)/([a-zA-Z]*)$")
_UNTIL_LIST_RHS = re.compile(r"^\\[(.*)\\]$")


def _until_flags(spec):
    flags = 0
    for ch in spec:
        if ch == "i":
            flags |= re.IGNORECASE
        elif ch == "m":
            flags |= re.MULTILINE
        elif ch == "s":
            flags |= re.DOTALL
        else:
            raise ValueError(f"until condition: unknown regex flag {ch!r}")
    return flags


def eval_until(condition, store):
    """Evaluate an until-condition string against the variable store."""
    best = None
    for pat, op in _UNTIL_OPS:
        m = pat.search(condition)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.end(), op)
    if best is None:
        raise ValueError(
            f"until condition: no operator (==, !=, ~, in) found in {condition!r}"
        )
    start, end, op = best
    lhs = render(condition[:start], store).strip()
    rhs = render(condition[end:], store).strip()

    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    if op == "~":
        m = _UNTIL_REGEX_RHS.match(rhs)
        if m is None:
            raise ValueError(f"until condition: '~' RHS must be /pattern/[flags], got {rhs!r}")
        return re.search(m.group(1), lhs, _until_flags(m.group(2))) is not None
    if op == "in":
        m = _UNTIL_LIST_RHS.match(rhs)
        if m is None:
            raise ValueError(f"until condition: 'in' RHS must be [A, B, C], got {rhs!r}")
        items = [x.strip() for x in m.group(1).split(",") if x.strip() != ""]
        return lhs in items
    raise AssertionError(f"unreachable: unknown operator {op!r}")


def poll_until(name, attempt_fn, condition, interval, max_attempts, store, quiet):
    """Re-invoke ``attempt_fn()`` until ``eval_until(condition, store)`` is true."""
    for attempt in range(1, max_attempts + 1):
        attempt_fn()
        if eval_until(condition, store):
            if not quiet:
                print(f"    * until satisfied on attempt {attempt}")
            return
        if attempt < max_attempts:
            if not quiet:
                print(
                    f"    * until not satisfied (attempt {attempt}/{max_attempts}),"
                    f" retrying in {interval}s"
                )
            time.sleep(interval)
    raise RuntimeError(
        f"step {name!r}: until condition not satisfied "
        f"after {max_attempts} attempts: {condition!r}"
    )
'''

_REPEAT_SRC = '''\
# Names referenced by ${repeat.<name>} in the source TOML. Each one MUST be
# supplied at runtime via --repeat-vars NAME=v1,v2,... unless it is embedded
# in DEFAULT_REPEAT_VARS below.
REQUIRED_REPEAT_VARS = {{REQUIRED_REPEAT_VARS}}


def _build_repeat_iterations(repeat_args, default_repeat_vars=None):
    """Parse --repeat-vars CLI entries and expand them into per-iteration dicts."""
    parsed = {}
    for kv in repeat_args:
        if "=" not in kv:
            raise SystemExit(
                f"error: --repeat-vars requires name=v1,v2,..., got: {kv!r}"
            )
        k, _, v = kv.partition("=")
        k = k.strip()
        if not k:
            raise SystemExit(f"error: --repeat-vars has empty key: {kv!r}")
        if k in parsed:
            raise SystemExit(f"error: --repeat-vars duplicated key: {k!r}")
        values = [x.strip() for x in v.split(",")]
        if not values or any(x == "" for x in values):
            raise SystemExit(
                f"error: --repeat-vars must supply non-empty comma-separated values: {kv!r}"
            )
        parsed[k] = values
    merged = dict(default_repeat_vars or {})
    merged.update(parsed)
    missing = REQUIRED_REPEAT_VARS - set(merged)
    if missing:
        raise SystemExit(f"error: --repeat-vars missing for: {sorted(missing)}")
    if not merged:
        return [{}]
    lengths = {k: len(v) for k, v in merged.items()}
    distinct = set(lengths.values())
    if len(distinct) != 1:
        raise SystemExit(
            f"error: --repeat-vars value counts must match across all keys, got: {lengths}"
        )
    n = distinct.pop()
    return [{k: merged[k][i] for k in merged} for i in range(n)]
'''

_ARGPARSE_REPEAT_SRC = '''    p.add_argument("--repeat-vars", action="append", default=[], metavar="K=V1,V2,...",
                   help="comma-separated values for ${repeat.K} (repeatable). "
                        "All --repeat-vars must share the same number of values; "
                        "the workflow is executed once per index.")'''

_MAIN_REPEAT_SETUP_REPEAT = '''    iterations = _build_repeat_iterations(args.repeat_vars, DEFAULT_REPEAT_VARS)
    total = len(iterations)

    for _idx, _repeat_iter in enumerate(iterations, start=1):
        store["repeat"] = dict(_repeat_iter)
        if _repeat_iter:
            store["steps"] = {}
            print(f"=== repeat iteration {_idx}/{total} {_repeat_iter} ===")
'''

_MAIN_REPEAT_SETUP_NO_REPEAT = "    store['repeat'] = {}"


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
    cfg: WorkflowConfig,
    *,
    default_vars: dict[str, str] | None = None,
    default_repeat_vars: dict[str, list[str]] | None = None,
    shebang: bool = False,
) -> str:
    """Return the source of a self-contained runner script."""
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    timestamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    used: set[str] = set()
    step_blocks: list[str] = []
    step_calls: list[str] = []

    needs_until = False
    for req in cfg.requests:
        if req.until is not None:
            needs_until = True
        fn = _sanitize_ident(req.name, used)
        step_blocks.append(_emit_step(req, fn))
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
    from .workflow import collect_repeat_names
    required_repeat = sorted(collect_repeat_names(cfg))
    needs_repeat = bool(required_repeat)
    if required_repeat:
        repeat_lit = "{" + ", ".join(repr(n) for n in required_repeat) + "}"
    else:
        repeat_lit = "set()"

    # Conditional sections
    until_helpers = _UNTIL_SRC if needs_until else "# (no until blocks — helpers omitted)"
    repeat_helpers = (
        _REPEAT_SRC.replace("{{REQUIRED_REPEAT_VARS}}", repeat_lit)
        if needs_repeat
        else "# (no ${repeat.*} references — helpers omitted)"
    )
    argparse_repeat = _ARGPARSE_REPEAT_SRC if needs_repeat else ""
    main_repeat_setup = (
        _MAIN_REPEAT_SETUP_REPEAT if needs_repeat else _MAIN_REPEAT_SETUP_NO_REPEAT
    )

    # Indent step calls for repeat loop nesting
    if needs_repeat:
        step_calls_src = "\n".join(
            f"        {line}" for line in step_calls_src.splitlines()
        )
        # Move the workflow comment into the loop body so indentation is uniform
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
        .replace("{{DEFAULT_VARS}}", _dict_literal(dict(default_vars or {}), indent=""))
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

    return rendered
