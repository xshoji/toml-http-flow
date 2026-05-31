"""Generate a standalone single-file bash runner from a WorkflowSpec.

This is the *simplified* bash generator: no capture, mask, until, repeat,
nor full template rendering engine.  Values are read straight from environment
variables and expanded by the shell itself, with only random UUID placeholders
handled by small bash helpers.
"""

from __future__ import annotations

import datetime
import re

from . import __version__
from .model import FormBody, HttpStep, SleepStep, Step, TextBody, WorkflowSpec


def _bash_sq(s: str) -> str:
    """Single-quote a string for bash (handles embedded ')."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _step_name(name: str, used: set[str]) -> str:
    """Sanitise a step name into a valid bash function identifier."""
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


def _urlencode_fields(fields: dict[str, str]) -> str:
    """Build ``application/x-www-form-urlencoded`` body from field dict."""
    parts: list[str] = []
    for k, v in fields.items():
        parts.append(f"{k}={v}")
    return "&".join(parts)


def _bash_dq(s: str) -> str:
    """Double-quote a string for bash while preserving shell expansion."""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _render_expr(s: str) -> str:
    """Return a readable bash expression with random UUID placeholders expanded."""
    return _bash_dq(
        s.replace("${random.UUID_HEX}", "$(uuid_hex)")
        .replace("${random.UUID}", "$(uuid)")
    )


def _emit_http(step: HttpStep, fn: str) -> str:
    """Emit a simple HTTP step as a bash function."""
    out: list[str] = [
        f"{fn}() {{",
        f"    url={_render_expr(step.url)}",
        f'    echo "==> [{step.name}] {step.method.upper()} $(mask "$url")"',
    ]

    if step.description:
        for dl in step.description.splitlines():
            out.append(f'    echo "    # {dl}"')

    # body setup via heredocument or inline form string
    body_var: str | None = None
    has_body = False

    match step.body:
        case TextBody(text=t):
            has_body = True
            body_var = "__BODY"
            out.append(f'    read -r -d "" {body_var} <<EOF')
            out.append(
                t.replace("${random.UUID_HEX}", "$(uuid_hex)")
                .replace("${random.UUID}", "$(uuid)")
            )
            out.append("EOF")
            # Add trailing newline to match curl --data behaviour
            out.append(f'    {body_var}="${{{body_var}}}$(printf "\\n")"')
            out.append(f'    echo "> body: $(mask "${body_var}")"')
        case FormBody(fields=f):
            has_body = True
            body_var = "__BODY"
            out.append(f'    {body_var}={_render_expr(_urlencode_fields(f))}')
            out.append(f'    echo "> body: $(mask "${body_var}")"')
        case _:
            pass

    # Build curl command line in a bash array for readability
    out.append('    local -a cmd=(curl -sS -L -w "%{http_code}")')
    out.append(f'    cmd+=(-X {step.method.upper()})')

    for k, v in step.headers.items():
        header_expr = _render_expr(f"{k}: {v}")
        out.append(f"    header={header_expr}")
        out.append('    echo "> $(mask "$header")"')
        out.append('    cmd+=(-H "$header")')

    if isinstance(step.body, FormBody):
        out.append('    cmd+=(-H "Content-Type: application/x-www-form-urlencoded")')

    if has_body and body_var:
        out.append(f'    cmd+=(-d "${body_var}")')

    out.append(f'    cmd+=("$url")')

    # Execute
    out.append('    "${cmd[@]}"')
    out.append("}")
    return "\n".join(out)


def _emit_sleep(step: SleepStep, fn: str) -> str:
    """Emit a SLEEP step as a bash function."""
    out = [
        f"{fn}() {{",
        f"    seconds={_render_expr(step.seconds)}",
        f'    echo "==> [{step.name}] SLEEP $seconds"',
    ]
    if step.description:
        for dl in step.description.splitlines():
            out.append(f'    echo "    # {dl}"')
    out.append('    sleep "$seconds"')
    out.append("}")
    return "\n".join(out)


def _emit(step: Step, fn: str) -> str:
    """Dispatch emitter based on step type."""
    match step:
        case SleepStep():
            return _emit_sleep(step, fn)
        case HttpStep():
            return _emit_http(step, fn)
        case _:
            raise TypeError(f"unknown step type: {type(step).__name__}")


def generate(
    spec: WorkflowSpec,
    *,
    shebang: bool = False,
) -> str:
    """Generate a minimal bash script from *spec*.

    Generated scripts expect values via environment variables.  The user is
    responsible for ``export``-ing (or otherwise setting) them before running.
    """
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    used: set[str] = set()
    blocks: list[str] = []
    calls: list[str] = []

    for s in spec.steps:
        fn = _step_name(s.name, used)
        blocks.append(_emit(s, fn))
        calls.append(f"    {fn}")

    shebang_line = "#!/usr/bin/env bash\n" if shebang else ""

    header = f"""{shebang_line}# Generated by toml-http-flow {__version__} at {ts}
set -uo pipefail

# Dependencies
curl --version >/dev/null || {{ echo "curl is required" >&2; exit 1; }}

MASK_KEYS='authorization|cookie|set-cookie|password|passwd|pwd|secret|client_secret|token|access_token|refresh_token|id_token|auth_token|session_token|api_key|apikey|private_key|pass'

mask() {{
    sed -E 's/("?('"$MASK_KEYS"')"?)([[:space:]]*[:=][[:space:]]*|=)"?[^& ,}}"]+"?/'"'\\1\\3***'"'/Ig'
}}

uuid() {{
    python3 -c 'import uuid; print(uuid.uuid4())'
}}

uuid_hex() {{
    python3 -c 'import uuid; print(uuid.uuid4().hex)'
}}
"""

    if blocks:
        funcs = "\n\n".join(blocks)
        calls_src = "\n".join(calls)
    else:
        funcs = "# (no steps)"
        calls_src = "    :  # no steps"

    script = f"""{header}
# ─── step functions ─────────────────────────────────────────────────

{funcs}

# ─── main ───────────────────────────────────────────────────────────
main() {{
{calls_src}
}}

main "$@"
"""
    return script
