"""Generate a standalone single-file Python runner from a WorkflowConfig.

The output prioritizes human readability over compactness: each [[requests]]
block becomes its own ``step_<name>`` function so the script can be tweaked or
re-run by hand without re-running ``apiwf generate``.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from . import __version__
from .config import RequestConfig, WorkflowConfig


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


def _emit_step(req: RequestConfig, func_name: str) -> str:
    lines: list[str] = []
    method = req.method.upper()
    lines.append(f"def {func_name}(store, quiet=False):")
    lines.append(
        f'    """[[requests]] name = {req.name!r} — {method} {req.url}"""'
    )
    lines.append(f"    name = {req.name!r}")
    lines.append(f"    method = {method!r}")
    lines.append(f"    url = render({_str_literal(req.url)}, store)")

    if req.headers:
        lines.append(f"    headers = render_mapping({_dict_literal(req.headers)}, store)")
    else:
        lines.append("    headers = {}")

    if req.body is not None:
        lines.append("    body_form = None")
        lines.append(
            f"    body_bytes = render({_str_literal(req.body)}, store).encode(\"utf-8\")"
        )
    elif req.body_form is not None:
        lines.append(f"    body_form = render_mapping({_dict_literal(req.body_form)}, store)")
        lines.append('    body_bytes = urllib.parse.urlencode(body_form).encode("utf-8")')
        lines.append("    apply_form_content_type(headers)")
    else:
        lines.append("    body_form = None")
        lines.append("    body_bytes = None")
    # request header estimation requires method/url/headers/body_bytes
    lines.append("    log_request(name, method, url, headers, body_bytes, body_form, quiet)")
    lines.append(
        "    status, reason, resp_headers, text, body_json = do_request(method, url, headers, body_bytes)"
    )
    lines.append("    log_response(name, status, reason, resp_headers, text, quiet)")
    lines.append("")

    if req.capture:
        lines.append("    if body_json is None:")
        lines.append(
            '        raise RuntimeError(f"step {name!r}: capture requested but response is not JSON")'
        )
        lines.append("    captured = {}")
        for var_name, path in req.capture.items():
            lines.append(f"    captured[{var_name!r}] = extract(body_json, {path!r})")
            lines.append(f"    log_capture({var_name!r}, captured[{var_name!r}], quiet)")
        lines.append('    store["steps"][name] = captured')
    else:
        lines.append('    store["steps"][name] = {}')

    return "\n".join(lines)


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
    for req in cfg.requests:
        fn = _sanitize_ident(req.name, used)
        step_blocks.append(_emit_step(req, fn))
        step_calls.append(f"    {fn}(store, quiet=args.quiet)")

    if not step_blocks:
        step_functions_src = "# (no [[requests]] blocks in source TOML)"
        step_calls_src = "    pass  # no steps"
    else:
        step_functions_src = "\n\n\n".join(step_blocks)
        step_calls_src = "\n".join(step_calls)

    rendered = (
        template
        .replace("{{VERSION}}", __version__)
        .replace("{{GENERATED_AT}}", timestamp)
        .replace("{{DEFAULT_VARS}}", _dict_literal(dict(default_vars or {}), indent=""))
        .replace("{{STEP_FUNCTIONS}}", step_functions_src)
        .replace("{{STEP_CALLS}}", step_calls_src)
    )

    if shebang:
        rendered = "#!/usr/bin/env python3\n" + rendered

    return rendered
