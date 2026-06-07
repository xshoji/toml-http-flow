"""Generate a standalone single-file bash runner from a WorkflowSpec.

This is the *simplified* bash generator: no repeat nor full template
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
from .runner import collect_var_names
from .runtime.mask import _MASK_DEFAULTS

_UNTIL_OPS = [
    (re.compile(r"=="), "=="),
    (re.compile(r"!="), "!="),
    (re.compile(r"\s+in\s+"), "in"),
    (re.compile(r"~"), "~"),
]


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


def _expand_placeholders(s: str, captured_vars: set[str]) -> str:
    """Expand httpflow template placeholders to bash equivalents.

    var.* variable names are upper-cased so that the generated bash script
    references standard-looking environment variables.
    env.* variable names are emitted as regular shell environment variables.
    Captured variables may also be referenced as ``${name}`` without the
    ``var.`` prefix; those are converted as well.
    """
    s = s.replace("${random.UUID_HEX}", "$(uuid_hex)")
    s = s.replace("${random.UUID}", "$(uuid)")
    s = re.sub(r"\$\{env\.([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: f"${{{m.group(1)}}}", s)
    s = re.sub(r"\$\{var\.([\w\-]+)\}", lambda m: f"${{{_env_name('VAR', m.group(1))}}}", s)
    if captured_vars:
        def _repl_captured(m: "re.Match[str]") -> str:
            name = m.group(1)
            return f"${{{_env_name('VAR', name)}}}" if name in captured_vars else m.group(0)
        s = re.sub(r"\$\{([\w\-]+)\}", _repl_captured, s)
    return s


def _render_expr(s: str, captured_vars: set[str]) -> str:
    """Return a readable bash expression with random UUID placeholders expanded."""
    return _bash_dq(_expand_placeholders(s, captured_vars))


def _split_until_condition(condition: str) -> tuple[str, str, str]:
    """Split an until condition into unrendered lhs, operator, and rhs."""
    best: tuple[int, int, str] | None = None
    for pat, op in _UNTIL_OPS:
        m = pat.search(condition)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.end(), op)
    if best is None:
        raise ValueError(
            f"until condition: no operator (==, !=, ~, in) found in {condition!r}"
        )
    start, end, op = best
    return condition[:start], op, condition[end:]


def _has_header(headers: dict[str, str], name: str) -> bool:
    """Return True when headers already define *name* case-insensitively."""
    return any(k.lower() == name.lower() for k in headers)


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


def _capture_kind_and_arg(source: str) -> tuple[str, str]:
    """Return generated bash capture metadata kind and helper argument."""
    if source.startswith("response.header."):
        return "response_header", source.removeprefix("response.header.")
    if source.startswith("request.header."):
        return "request_header", source.removeprefix("request.header.")
    if source == "request.url":
        return "request_url", "-"
    if source == "request.body":
        return "request_body", "-"
    return "json", _jq_filter(_capture_path(source))


def _capture_rows(step: HttpStep) -> list[str]:
    """Emit capture metadata rows for an HTTP step."""
    rows: list[str] = []
    for var, source in step.capture.items():
        if any(ch in var or ch in source for ch in "\t\n"):
            raise ValueError("capture names and sources must not contain tabs or newlines")
        kind, arg = _capture_kind_and_arg(source)
        if "\t" in arg or "\n" in arg:
            raise ValueError("capture helper arguments must not contain tabs or newlines")
        rows.append("\t".join([_env_name("VAR", var), var, kind, source, arg]))
    return rows


def _collect_required_var_names(spec: WorkflowSpec, captured_vars: set[str]) -> set[str]:
    """Return required explicit vars, excluding values captured earlier."""
    return collect_var_names(spec) - captured_vars


def _emit_http(step: HttpStep, fn: str, captured_vars: set[str]) -> str:
    """Emit a simple HTTP step as a bash function."""
    out: list[str] = [
        f"{fn}() {{",
        f"    local url={_render_expr(step.url, captured_vars)}",
        "    local body=",
        "    local headers_text=",
        "    local captures_text=",
    ]

    has_body = False
    match step.body:
        case TextBody(text=t):
            has_body = True
            out.append("    body=$(cat << EOT")
            out.append(_expand_placeholders(t, captured_vars))
            out.append("EOT")
            out.append(')')
            out.append('    body="${body}$(printf "\\n")"')
        case FormBody(fields=f):
            has_body = True
            out.append(f'    body={_render_expr(_urlencode_fields(f), captured_vars)}')
        case _:
            pass

    header_lines = [_expand_placeholders(f"{k}: {v}", captured_vars) for k, v in step.headers.items()]
    if isinstance(step.body, FormBody) and not _has_header(step.headers, "Content-Type"):
        header_lines.append("Content-Type: application/x-www-form-urlencoded")
    if header_lines:
        out.append("    headers_text=$(cat << EOT")
        out.extend(header_lines)
        out.append("EOT")
        out.append(')')

    capture_lines = _capture_rows(step)
    if capture_lines:
        out.append("    captures_text=$(cat <<'EOT'")
        out.extend(capture_lines)
        out.append("EOT")
        out.append(')')

    out.append(
        f"    hf_http_step {_bash_sq(step.name)} {_bash_sq(step.method.upper())} \"$url\" "
        f"{1 if has_body else 0} \"$body\" \"$headers_text\" \"$captures_text\" "
        f"{_bash_sq(step.description or '')}"
    )
    out.append("}")
    return "\n".join(out)


def _emit_http_until(step: HttpStep, fn: str, captured_vars: set[str]) -> str:
    """Emit an HTTP step with an until polling loop as a bash function."""
    assert step.until is not None
    lhs, op, rhs = _split_until_condition(step.until.condition)
    out = _emit_http(step, f"{fn}_attempt", captured_vars).splitlines()
    out.extend([
        "",
        f"{fn}() {{",
        "    local attempt",
        f"    local max_attempts={step.until.max_attempts}",
        f"    local interval={step.until.interval}",
        "    local until_lhs until_rhs",
        "    for ((attempt=1; attempt<=max_attempts; attempt++)); do",
        f"        {fn}_attempt || return $?",
        f"        until_lhs={_render_expr(lhs, captured_vars)}",
        f"        until_rhs={_render_expr(rhs, captured_vars)}",
        f"        if hf_until_eval \"$until_lhs\" {_bash_sq(op)} \"$until_rhs\"; then",
        '            echo "    * until satisfied on attempt $attempt"',
        "            return 0",
        "        fi",
        "        if [ \"$attempt\" -lt \"$max_attempts\" ]; then",
        '            echo "    * until not satisfied (attempt $attempt/$max_attempts), retrying in ${interval}s"',
        '            sleep "$interval"',
        "        fi",
        "    done",
        f"    echo {_bash_dq_lit(f'step {step.name!r}: until condition not satisfied after ')}\"$max_attempts\"{_bash_dq_lit(f' attempts: {step.until.condition!r}')} >&2",
        "    return 1",
        "}",
    ])
    return "\n".join(out)


def _emit_sleep(step: SleepStep, fn: str, captured_vars: set[str]) -> str:
    """Emit a SLEEP step as a bash function."""
    out = [
        f"{fn}() {{",
        f"    seconds={_render_expr(step.seconds, captured_vars)}",
        '    hf_print_blank_lines "${HTTPFLOW_BLANK_LINE:-0}"',
        f'    echo "==> $(hf_now) [{step.name}] SLEEP $seconds"',
    ]
    if step.description:
        for dl in step.description.splitlines():
            out.append(f'    echo "# {dl}"')
    out.append('    sleep "$seconds"')
    out.append(f'    echo "<== $(hf_now) [{step.name}] done"')
    out.append("}")
    return "\n".join(out)


def _emit(step: Step, fn: str, captured_vars: set[str]) -> str:
    """Dispatch emitter based on step type."""
    match step:
        case SleepStep():
            return _emit_sleep(step, fn, captured_vars)
        case HttpStep():
            if step.until is not None:
                return _emit_http_until(step, fn, captured_vars)
            return _emit_http(step, fn, captured_vars)
        case _:
            raise TypeError(f"unknown step type: {type(step).__name__}")


def _bash_dq_lit(s: str) -> str:
    """Double-quote a string for bash, escaping all shell-special chars."""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('`', '\\`') + '"'


def _bash_default_assignment(name: str, value: str) -> str:
    """Emit a bash line that sets a default for an env-style variable."""
    return f': "${{{name}:={_bash_dq_lit(value)[1:-1]}}}"'


def _bash_mask_key_pattern(keys: str) -> str:
    """Return bash sed regex where each hyphen part's first letter is case-insensitive."""
    parts: list[str] = []
    for key in keys.split("|"):
        if not key:
            continue
        key_parts: list[str] = []
        for key_part in key.split("-"):
            if not key_part:
                key_parts.append(key_part)
                continue
            first = key_part[0]
            rest = key_part[1:]
            lower = first.lower()
            upper = first.upper()
            if lower != upper:
                key_parts.append(f"[{lower}{upper}]{rest}")
            else:
                key_parts.append(key_part)
        parts.append("-".join(key_parts))
    return "|".join(parts)


def _required_var_check(names: list[str]) -> str:
    """Emit bash statements that fail when required VAR_* values are empty."""
    if not names:
        return ""
    lines = ["", "# ─── required variables ──────────────────────────────────────────────"]
    for name in names:
        env = _env_name("VAR", name)
        lines.extend([
            f'if [ -z "${{{env}:-}}" ]; then',
            f'    echo "error: missing required variable: {name}" >&2',
            f'    echo "Export it before running: export {env}=<value>" >&2',
            "    exit 1",
            "fi",
        ])
    lines.append("")
    return "\n".join(lines)


def _capture_helpers() -> str:
    """Return bash helper functions used by capture-enabled scripts."""
    return r'''
capture_log() {
    local name=$1
    local value=$2
    if printf '%s\n' "$name" | grep -Eiq "^($MASK_KEYS)$"; then
        value="***"
    fi
    printf "* capture %s = '%s'\n" "$name" "$value"
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
    local trace_file=$4
    local filter=$5
    local value
    if ! value=$(hf_trace_response_body "$trace_file" | jq -r "$filter"); then
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
    local input_source=$4
    local header_name=$5
    local value mode
    if [ -f "$input_source" ]; then
        mode=trace
        input_source=$(cat "$input_source")
    else
        mode=text
    fi
    if ! value=$(awk -v name="$header_name" -v mode="$mode" '
            BEGIN { want=tolower(name) ":"; found=0; value="" }
            mode == "trace" && /^< HTTP\// { found=0; value=""; next }
            mode == "trace" && !/^< / { next }
            mode == "trace" && /^< ?\r?$/ { next }
            mode == "trace" { line=substr($0, 3) }
            mode != "trace" { line=$0 }
            /^[[:space:]]*$/ { next }
            { sub(/\r$/, "", line); lower=tolower(line) }
            index(lower, want) == 1 { value=substr(line, length(name) + 2); sub(/^[[:space:]]+/, "", value); found=1 }
            END { if (!found) exit 1; print value }
        ' <<< "$input_source"); then
        echo "capture failed: $display_name <- $source" >&2
        return 1
    fi
    capture_value "$env_name" "$display_name" "$source" "$value"
}
'''


def _http_helpers(has_capture: bool) -> str:
    """Return bash helper functions used by generated HTTP steps."""
    capture_dispatch = r'''

hf_run_captures() {
    local captures_text=$1
    local url=$2
    local body=$3
    local req_headers_text=$4
    local trace_file=$5
    local env_name display_name kind source arg

    while IFS=$'\t' read -r env_name display_name kind source arg; do
        [ -z "${env_name:-}" ] && continue
        case "$kind" in
            json)
                capture_json "$env_name" "$display_name" "$source" "$trace_file" "$arg" || return $?
                ;;
            response_header)
                capture_header "$env_name" "$display_name" "$source" "$trace_file" "$arg" || return $?
                ;;
            request_header)
                capture_header "$env_name" "$display_name" "$source" "$req_headers_text" "$arg" || return $?
                ;;
            request_url)
                capture_value "$env_name" "$display_name" "$source" "$url" || return $?
                ;;
            request_body)
                capture_value "$env_name" "$display_name" "$source" "$body" || return $?
                ;;
            *)
                echo "capture failed: $display_name <- $source" >&2
                return 1
                ;;
        esac
    done <<< "$captures_text"
}
''' if has_capture else ""
    return r'''
''' + capture_dispatch + r'''
hf_trace_response_body() {
    awk '
        /^< HTTP\// { in_headers=1; n=0; seen=1; next }
        in_headers && /^< ?\r?$/ { in_headers=0; n=0; next }
        !in_headers && seen { lines[++n]=$0 }
        END {
            while (n > 0 && lines[n] ~ /^\* /) n--
            for (i = 1; i <= n; i++) {
                sub(/\* [^\n\r]*\r?$/, "", lines[i])
                print lines[i]
            }
        }
    ' "$1"
}

jq_or_cat() {
    local input trimmed
    input=$(cat)
    trimmed=$(echo "$input" | sed 's/^[[:space:]]*//' | head -c1)
    if [ -z "${HTTPFLOW_PRETTY_JSON:-}" ] \
        || [ "$trimmed" != "{" ] && [ "$trimmed" != "[" ] \
        || ! echo "$input" | jq . > /dev/null 2>&1; then
        echo "$input"
    else
        echo "$input" | jq .
    fi
}

hf_prefix_lines() {
    local prefix=$1
    while IFS= read -r line || [ -n "$line" ]; do
        printf "%s%s\n" "$prefix" "$line"
    done
}

hf_http_step() {
    local step_name=$1
    local method=$2
    local url=$3
    local has_body=$4
    local body=$5
    local headers_text=$6
    local captures_text=$7
    local description=$8
    local trace_file line header
    local -a cmd
    local boundary_inserted=0

    hf_print_blank_lines "${HTTPFLOW_BLANK_LINE:-0}"

    echo "==> $(hf_now) [$step_name] $method $(mask "$url")"
    if [ -n "$description" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            echo "# $line"
        done <<< "$description"
    fi

    trace_file=$(mktemp "$HF_TMPDIR/hf_trace.XXXXXX")
    : > "$trace_file"

    cmd=(curl -sS -L -v --no-buffer --stderr -)
    cmd+=(-X "$method")

    while IFS= read -r header || [ -n "$header" ]; do
        [ -z "$header" ] && continue
        cmd+=(-H "$header")
    done <<< "$headers_text"

    if [ "$has_body" = "1" ]; then
        cmd+=(-d "$body")
    fi
    cmd+=("$url")

    if ! "${cmd[@]}" \
        | grep -v '^\({\|}\) \[.*bytes data\]' \
        | grep -v '^\*' \
        | sed -e 's/\* Closing.*//' -e 's/\* Connection.*//' \
        | while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in
                "< HTTP/"*)
                    if [ "$boundary_inserted" = "0" ]; then
                        boundary_inserted=1
                        printf "<== %s [%s]\n" "$(hf_now)" "$step_name"
                    fi
                    printf "%s\n" "$line"
                    ;;
                ">"|"> "|$'> \r')
                    printf "%s\n" "$line"
                    if [ "$has_body" = "1" ]; then
                        # Request body echoed by this script; curl -v omits it. This comment is not printed.
                        printf "%s" "$body" | jq_or_cat | hf_prefix_lines "> "
                    fi
                    ;;
                *)
                    printf "%s\n" "$line" | jq_or_cat | hf_prefix_lines ""
                    ;;
            esac
        done \
        | tee -a "$trace_file" \
        | mask_lines; then
        return 1
    fi

    if [ -n "$captures_text" ]; then
        hf_run_captures "$captures_text" "$url" "$body" "$headers_text" "$trace_file" || {
            return 1
        }
    fi
}
'''


def _until_helpers() -> str:
    """Return bash helper functions used by until-enabled scripts."""
    return r'''
hf_trim() {
    local value=$1
    value=${value#"${value%%[![:space:]]*}"}
    value=${value%"${value##*[![:space:]]}"}
    printf '%s' "$value"
}

hf_until_regex() {
    local lhs=$1
    local rhs=$2
    local pattern flags old_nocasematch result
    case "$rhs" in
        /*/) pattern=${rhs:1:${#rhs}-2}; flags= ;;
        /*/[a-zA-Z]*)
            pattern=${rhs%/*}
            pattern=${pattern:1}
            flags=${rhs##*/}
            ;;
        *) echo "until condition: '~' RHS must be /pattern/[flags], got '$rhs'" >&2; return 2 ;;
    esac

    case "$flags" in
        *[!ims]*) echo "until condition: unknown regex flag '${flags//[ims]/}'" >&2; return 2 ;;
    esac

    old_nocasematch=$(shopt -p nocasematch || true)
    if [[ "$flags" == *i* ]]; then
        shopt -s nocasematch
    fi
    [[ "$lhs" =~ $pattern ]]
    result=$?
    eval "$old_nocasematch"
    return "$result"
}

hf_until_eval() {
    local lhs rhs op item list
    lhs=$(hf_trim "$1")
    op=$2
    rhs=$(hf_trim "$3")
    case "$op" in
        '==') [ "$lhs" = "$rhs" ] ;;
        '!=') [ "$lhs" != "$rhs" ] ;;
        '~') hf_until_regex "$lhs" "$rhs" ;;
        'in')
            case "$rhs" in
                '['*']') ;;
                *) echo "until condition: 'in' RHS must be [A, B, C], got '$rhs'" >&2; return 2 ;;
            esac
            list=${rhs#'['}
            list=${list%']'}
            while [ -n "$list" ]; do
                item=${list%%,*}
                if [ "$item" = "$list" ]; then
                    list=
                else
                    list=${list#*,}
                fi
                item=$(hf_trim "$item")
                [ -z "$item" ] && continue
                [ "$lhs" = "$item" ] && return 0
            done
            return 1
            ;;
        *) echo "until condition: unknown operator $op" >&2; return 2 ;;
    esac
}
'''


def generate(
    spec: WorkflowSpec,
    *,
    shebang: bool = False,
    default_vars: dict[str, str] | None = None,
) -> str:
    """Generate a minimal bash script from *spec*.

    Generated scripts expect values via environment variables.  The user is
    responsible for ``export``-ing (or otherwise setting) them before running.
    """
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    used: set[str] = set()
    blocks: list[str] = []
    calls: list[str] = []
    captured_vars: set[str] = set(
        var
        for s in spec.steps
        if isinstance(s, HttpStep)
        for var in s.capture.keys()
    )
    has_capture = any(isinstance(s, HttpStep) and bool(s.capture) for s in spec.steps)
    has_until = any(isinstance(s, HttpStep) and s.until is not None for s in spec.steps)
    needs_jq = any(
        isinstance(s, HttpStep) and any(_is_json_capture_source(src) for src in s.capture.values())
        for s in spec.steps
    )

    for s in spec.steps:
        fn = _step_name(s.name, used)
        blocks.append(_emit(s, fn, captured_vars))
        calls.append(f"    {fn} || exit $?")

    shebang_line = "#!/usr/bin/env bash\n" if shebang else ""

    # Build default variable assignments
    default_lines: list[str] = []
    default_vars = dict(default_vars or {})
    if default_vars:
        for k, v in sorted(default_vars.items()):
            default_lines.append(_bash_default_assignment(_env_name("VAR", k), v))

    defaults_block = "\n".join(default_lines)
    if defaults_block:
        defaults_block = f"\n# ─── defaults (can be overridden by exporting beforehand) ───────────\n{defaults_block}\n"

    required_vars_block = _required_var_check(sorted(_collect_required_var_names(spec, captured_vars) - set(default_vars)))

    mask_keys_default = "|".join(sorted(_MASK_DEFAULTS))
    bash_mask_keys_default = _bash_mask_key_pattern(mask_keys_default)

    header = f"""{shebang_line}# Generated by toml-http-flow {__version__} at {ts}
set -uo pipefail

# Dependencies
curl --version >/dev/null || {{ echo "curl is required" >&2; exit 1; }}
{('jq --version >/dev/null || { echo "jq is required for JSON capture" >&2; exit 1; }' if needs_jq else '')}

MASK_KEYS_DEFAULT='{bash_mask_keys_default}'
MASK_KEYS="$MASK_KEYS_DEFAULT${{HTTPFLOW_MASK_EXTRA:+|${{HTTPFLOW_MASK_EXTRA}}}}"

mask() {{
    if [ -n "${{HTTPFLOW_NO_MASK:-}}" ]; then
        echo "$1"
        return 0
    fi
    printf '%s\\n' "$1" | sed -E "s/(\\\"?($MASK_KEYS)\\\"?)([[:space:]]*[:=][[:space:]]*)\\\"?[^& ,}}\\\"]+( [^& ,}}\\\"]+)?\\\"?/\\1\\3***/g"
}}

mask_lines() {{
    while IFS= read -r LINE || [ -n "$LINE" ]; do
        mask "$LINE"
    done
}}

hf_print_blank_lines() {{
    local count=${{1:-0}}
    case "$count" in
        ''|*[!0-9]*)
            echo "error: HTTPFLOW_BLANK_LINE must be a non-negative integer" >&2
            exit 1
            ;;
    esac
    while [ "$count" -gt 0 ]; do
        printf '\n'
        count=$((count - 1))
    done
}}

hf_now() {{
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import datetime; n = datetime.datetime.now(); print(n.strftime("%Y-%m-%d %H:%M:%S.") + f"{{n.microsecond // 1000:03d}}")'
    elif date '+%Y-%m-%d %H:%M:%S.%3N' | grep -Eq '[0-9]{{3}}$'; then
        date '+%Y-%m-%d %H:%M:%S.%3N'
    else
        date '+%Y-%m-%d %H:%M:%S.000'
    fi
}}

uuid() {{
  if command -v uuidgen &>/dev/null; then
    uuidgen |awk '{{print tolower($1)}}'
  else
    perl -e 'open(my$f,"<:raw","/dev/urandom");read($f,my$b,16);vec($b,13,4)=4;vec($b,16,2)=2;printf"%s-%s-%s-%s-%s\n",unpack"H8 H4 H4 H4 H12",$b'
  fi
}}

uuid_hex() {{
    uuid | sed "s/-//g"
}}
{_capture_helpers() if has_capture else ''}
{_http_helpers(has_capture)}
{_until_helpers() if has_until else ''}
{defaults_block}
{required_vars_block}"""

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
    for arg in "$@"; do
        case "$arg" in
            --pretty-json)
                HTTPFLOW_PRETTY_JSON=1
                ;;
            -h|--help)
                echo "usage: $0 [--pretty-json]"
                exit 0
                ;;
            *)
                echo "error: unknown argument: $arg" >&2
                exit 1
                ;;
        esac
    done
    export HTTPFLOW_PRETTY_JSON=${{HTTPFLOW_PRETTY_JSON:-}}

    HF_TMPDIR=$(mktemp -d)
    export HF_TMPDIR
    trap 'rm -rf "$HF_TMPDIR"' EXIT
{calls_src}
}}

main "$@"
"""
    return script
