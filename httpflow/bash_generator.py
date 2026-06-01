"""Generate a standalone single-file bash runner from a WorkflowSpec.

This is the *simplified* bash generator: no until, repeat, nor full template
rendering engine.  Values are read straight from environment variables and
expanded by the shell itself, with random UUID placeholders handled by small
bash helpers.  JSON capture uses jq when a workflow defines capture entries.
"""

from __future__ import annotations

import datetime
import json
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


def _env_name(prefix: str, name: str) -> str:
    """Return a safe generated bash environment variable name."""
    return f"{prefix}_{re.sub(r'[^A-Za-z0-9_]', '_', name).upper()}"


def _urlencode_fields(fields: dict[str, str]) -> str:
    """Build ``application/x-www-form-urlencoded`` body from field dict."""
    parts: list[str] = []
    for k, v in fields.items():
        parts.append(f"{k}={v}")
    return "&".join(parts)


def _bash_dq(s: str) -> str:
    """Double-quote a string for bash while preserving shell expansion."""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _expand_placeholders(s: str) -> str:
    """Expand httpflow template placeholders to bash equivalents.

    var.* and repeat.* variable names are upper-cased so that the generated
    bash script references standard-looking environment variables.
    """
    s = s.replace("${random.UUID_HEX}", "$(uuid_hex)")
    s = s.replace("${random.UUID}", "$(uuid)")
    s = re.sub(r"\$\{var\.([\w\-]+)\}", lambda m: f"${{{_env_name('VAR', m.group(1))}}}", s)
    s = re.sub(r"\$\{repeat\.([\w\-]+)\}", lambda m: f"${{{_env_name('REPEAT', m.group(1))}}}", s)
    return s


def _render_expr(s: str) -> str:
    """Return a readable bash expression with random UUID placeholders expanded."""
    return _bash_dq(_expand_placeholders(s))


def _capture_path(source: str) -> str:
    """Return JSON path part for response-body capture source."""
    return source.removeprefix("response.body.")


def _is_json_capture_source(source: str) -> bool:
    """Return True when capture source reads the response body JSON."""
    return not (
        source.startswith("response.header.")
        or source.startswith("request.header.")
        or source in {"request.url", "request.body"}
    )


def _jq_filter(path: str) -> str:
    """Convert httpflow JSON path syntax into a jq filter."""
    token_re = re.compile(r"([A-Za-z0-9_\-]+)|(\[(\d+)\])")
    out = ""
    pos = 0
    while pos < len(path):
        if path[pos] == ".":
            pos += 1
            continue
        m = token_re.match(path, pos)
        if not m:
            raise ValueError(f"unsupported JSON capture path: {path!r}")
        if m.group(1) is not None:
            out += f"[{json.dumps(m.group(1))}]?"
        else:
            out += f"[{m.group(3)}]?"
        pos = m.end()
    return "." + out


def _emit_capture(step: HttpStep) -> list[str]:
    """Emit capture assignments for an HTTP step."""
    out: list[str] = []
    for var, source in step.capture.items():
        env = _env_name("VAR", var)
        q_var = _bash_sq(var)
        q_source = _bash_sq(source)
        if source.startswith("response.header."):
            name = source.removeprefix("response.header.")
            out.append(f"    capture_header {env} {q_var} {q_source} \"$__RESP_HEADERS\" {_bash_sq(name)} || return $?")
        elif source.startswith("request.header."):
            name = source.removeprefix("request.header.")
            out.append(f"    capture_header {env} {q_var} {q_source} \"$__REQ_HEADERS\" {_bash_sq(name)} || return $?")
        elif source == "request.url":
            out.append(f"    capture_value {env} {q_var} {q_source} \"$url\" || return $?")
        elif source == "request.body":
            out.append(f"    capture_value {env} {q_var} {q_source} \"$__BODY\" || return $?")
        else:
            out.append(f"    capture_json {env} {q_var} {q_source} \"$__RESP_BODY\" {_bash_sq(_jq_filter(_capture_path(source)))} || return $?")
    return out


def _emit_http(step: HttpStep, fn: str) -> str:
    """Emit a simple HTTP step as a bash function."""
    out: list[str] = [
        f"{fn}() {{",
        f"    url={_render_expr(step.url)}",
        "    local __BODY=",
        "    local __RESP_HEADERS=$(mktemp)",
        "    local __RESP_BODY=$(mktemp)",
        "    local __CURL_ERR=$(mktemp)",
        "    local __REQ_HEADERS=$(mktemp)",
        "    trap 'rm -f \"${__RESP_HEADERS:-}\" \"${__RESP_BODY:-}\" \"${__CURL_ERR:-}\" \"${__REQ_HEADERS:-}\"' RETURN",
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
            out.append(f'    {body_var}=$(cat << EOF')
            out.append(_expand_placeholders(t))
            out.append("EOF)")
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
    out.append('    local -a cmd=(curl -sS -L -D "$__RESP_HEADERS" -o "$__RESP_BODY" -w "%{http_code}")')
    out.append(f'    cmd+=(-X {step.method.upper()})')

    for k, v in step.headers.items():
        header_expr = _render_expr(f"{k}: {v}")
        out.append(f"    header={header_expr}")
        out.append('    echo "> $(mask "$header")"')
        out.append('    printf "%s\\n" "$header" >> "$__REQ_HEADERS"')
        out.append('    cmd+=(-H "$header")')

    if isinstance(step.body, FormBody):
        out.append('    cmd+=(-H "Content-Type: application/x-www-form-urlencoded")')
        out.append('    printf "%s\\n" "Content-Type: application/x-www-form-urlencoded" >> "$__REQ_HEADERS"')

    if has_body and body_var:
        out.append(f'    cmd+=(-d "${body_var}")')

    out.append(f'    cmd+=("$url")')

    # Execute
    out.append('    local status')
    out.append('    if ! status=$("${cmd[@]}" 2>"$__CURL_ERR"); then')
    out.append('        mask_lines < "$__CURL_ERR" >&2')
    out.append('        return 1')
    out.append('    fi')
    out.append(f'    echo "<== [{step.name}] status=$status"')
    out.append('    mask_lines < "$__RESP_BODY"')
    if step.capture:
        out.extend(_emit_capture(step))
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


def _bash_dq_lit(s: str) -> str:
    """Double-quote a string for bash, escaping all shell-special chars."""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('`', '\\`') + '"'


def _bash_default_assignment(name: str, value: str) -> str:
    """Emit a bash line that sets a default for an env-style variable."""
    return f': "${{{name}:={_bash_dq_lit(value)[1:-1]}}}"'


def _capture_helpers() -> str:
    """Return bash helper functions used by capture-enabled scripts."""
    return r'''
capture_log() {
    local name=$1
    local value=$2
    if printf '%s\n' "$name" | grep -Eiq "^($MASK_KEYS)$"; then
        value="***"
    fi
    printf "    * capture %s = '%s'\n" "$name" "$value"
}

capture_value() {
    local env_name=$1
    local display_name=$2
    local source=$3
    local value=$4
    printf -v "$env_name" '%s' "$value"
    export "$env_name"
    capture_log "$display_name" "$value"
}

capture_json() {
    local env_name=$1
    local display_name=$2
    local source=$3
    local body_file=$4
    local filter=$5
    local value
    if ! value=$(jq -r "$filter" "$body_file"); then
        echo "capture failed: $display_name <- $source" >&2
        return 1
    fi
    if [ -z "$value" ] || [ "$value" = "null" ]; then
        echo "capture failed: $display_name <- $source" >&2
        return 1
    fi
    capture_value "$env_name" "$display_name" "$source" "$value"
}

capture_header() {
    local env_name=$1
    local display_name=$2
    local source=$3
    local header_file=$4
    local header_name=$5
    local value
    if ! value=$(awk -v name="$header_name" '
        BEGIN { want=tolower(name) ":"; found=0; value="" }
        tolower($0) ~ /^http\// { found=0; value=""; next }
        /^[[:space:]]*$/ { next }
        { line=$0; sub(/\r$/, "", line); lower=tolower(line) }
        index(lower, want) == 1 { value=substr(line, length(name) + 2); sub(/^[[:space:]]+/, "", value); found=1 }
        END { if (!found) exit 1; print value }
    ' "$header_file"); then
        echo "capture failed: $display_name <- $source" >&2
        return 1
    fi
    capture_value "$env_name" "$display_name" "$source" "$value"
}
'''


def generate(
    spec: WorkflowSpec,
    *,
    shebang: bool = False,
    default_vars: dict[str, str] | None = None,
    default_repeat_vars: dict[str, list[str]] | None = None,
) -> str:
    """Generate a minimal bash script from *spec*.

    Generated scripts expect values via environment variables.  The user is
    responsible for ``export``-ing (or otherwise setting) them before running.
    """
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    used: set[str] = set()
    blocks: list[str] = []
    calls: list[str] = []
    has_capture = any(isinstance(s, HttpStep) and bool(s.capture) for s in spec.steps)
    needs_jq = any(
        isinstance(s, HttpStep) and any(_is_json_capture_source(src) for src in s.capture.values())
        for s in spec.steps
    )

    for s in spec.steps:
        fn = _step_name(s.name, used)
        blocks.append(_emit(s, fn))
        calls.append(f"    {fn} || exit $?")

    shebang_line = "#!/usr/bin/env bash\n" if shebang else ""

    # Build default variable assignments
    default_lines: list[str] = []
    if default_vars:
        for k, v in sorted(default_vars.items()):
            default_lines.append(_bash_default_assignment(_env_name("VAR", k), v))
    if default_repeat_vars:
        for k, vals in sorted(default_repeat_vars.items()):
            joined = ",".join(vals)
            default_lines.append(_bash_default_assignment(_env_name("REPEAT", k), joined))

    defaults_block = "\n".join(default_lines)
    if defaults_block:
        defaults_block = f"\n# ─── defaults (can be overridden by exporting beforehand) ───────────\n{defaults_block}\n"

    header = f"""{shebang_line}# Generated by toml-http-flow {__version__} at {ts}
set -uo pipefail

# Dependencies
curl --version >/dev/null || {{ echo "curl is required" >&2; exit 1; }}
{('jq --version >/dev/null || { echo "jq is required for JSON capture" >&2; exit 1; }' if needs_jq else '')}

MASK_KEYS='authorization|cookie|set-cookie|password|passwd|pwd|secret|client_secret|token|access_token|refresh_token|id_token|auth_token|session_token|api_key|apikey|private_key|pass'

mask() {{
    echo "$1" | sed -E 's/("?('"$MASK_KEYS"')"?)([[:space:]]*[:=][[:space:]]*|=)"?[^& ,}}"]+"?/'"'\\1\\3***'"'/Ig'
}}

mask_lines() {{
    while IFS= read -r LINE || [ -n "$LINE" ]; do
        mask "$LINE"
    done
}}

uuid() {{
    python3 -c 'import uuid; print(uuid.uuid4())'
}}

uuid_hex() {{
    python3 -c 'import uuid; print(uuid.uuid4().hex)'
}}
{_capture_helpers() if has_capture else ''}
{defaults_block}"""

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
