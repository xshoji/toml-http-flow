"""Generate a standalone single-file bash runner from a WorkflowSpec."""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from . import __version__
from .model import FormBody, HttpStep, SleepStep, Step, TextBody, WorkflowSpec
from .runner import collect_repeat_names, collect_var_names

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "runner.sh.tmpl"


def _bash_sq(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _enc(key: str) -> str:
    out = []
    for ch in key:
        if ch.isalnum():
            out.append(ch)
        else:
            out.append(f"_{ord(ch):02x}_")
    return "".join(out)


def _step_name(name: str, used: set[str]) -> str:
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


def _jq_path(path: str) -> str:
    parts = []
    i = 0
    while i < len(path):
        m = re.match(r"([A-Za-z_]\w*)", path[i:])
        if m:
            parts.append(f'."{m.group(1)}"')
            i += m.end()
            continue
        m = re.match(r"\[(\d+)\]", path[i:])
        if m:
            parts.append(f"[{m.group(1)}]")
            i += m.end()
            continue
        if path[i] == ".":
            i += 1
            continue
        raise ValueError(f"invalid capture path: {path!r}")
    return "".join(parts)


def _until_parts(cond: str) -> tuple[str, str, str]:
    ops = [(re.compile(r"=="), "=="), (re.compile(r"!="), "!="),
           (re.compile(r"\s+in\s+"), "in"), (re.compile(r"~"), "~")]
    best = None
    for pat, op in ops:
        m = pat.search(cond)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.end(), op)
    if best is None:
        raise ValueError(f"until: no operator found in {cond!r}")
    return cond[:best[0]], best[2], cond[best[1]:]


def _render(var: str, tmpl: str, indent: str = "    ") -> str:
    return f'{indent}__hf_render {var} {_bash_sq(tmpl)}'


def _capture_lines(capture: dict[str, str]) -> list[str]:
    if not capture:
        return []
    out: list[str] = []
    for var, src in capture.items():
        if src.startswith("response.header."):
            hdr = src[len("response.header."):]
            out.append(f'    __hf_cap="$(__hf_get_header "$__hf_rhdr" {_bash_sq(hdr)})"')
        elif src.startswith("request.header."):
            hdr = src[len("request.header."):]
            out.append(f'    __hf_cap="$(__hf_get_header "$__hf_qhdr" {_bash_sq(hdr)})"')
        elif src == "request.url":
            out.append('    __hf_cap="$url"')
        elif src == "request.body":
            out.append('    __hf_cap="$__hf_req_body"')
        elif src.startswith("response.body."):
            p = _jq_path(src[len("response.body."):])
            out.append(f'    __hf_cap="$(jq -r -e {_bash_sq(p)} "$bodyfile")" || {{')
            out.append(f'        printf "%s\\n" "error: capture failed for {var}={src!r}: path not found" >&2')
            out.append('        return 1')
            out.append('    }')
        else:
            p = _jq_path(src)
            out.append(f'    __hf_cap="$(jq -r -e {_bash_sq(p)} "$bodyfile")" || {{')
            out.append(f'        printf "%s\\n" "error: capture failed for {var}={src!r}: path not found" >&2')
            out.append('        return 1')
            out.append('    }')
        out.append(f'    __hf_var_set {_bash_sq(var)} "$__hf_cap"')
        out.append('    if [ "$quiet" -ne 1 ]; then')
        out.append(f'        printf "%s\\n" "    * capture {var} = $(__hf_mask_line "$__hf_cap" "$no_mask")"')
        out.append('    fi')
    return out


def _body_lines(step: HttpStep) -> tuple[list[str], str]:
    """Return lines that set up bodyfile/bodyarg, and the rendered-headers accumulator name."""
    lines: list[str] = []
    hdr_var = "__hf_h"

    match step.body:
        case TextBody(text=t):
            lines.append(_render("body", t))
            lines.append('    bodyfile="$__hf_tmpdir/req_body"')
            lines.append('    printf "%s" "$body" > "$bodyfile"')
            lines.append('    local bflag="--data-binary @$bodyfile"')
        case FormBody(fields=f):
            lines.append('    local form=""')
            for k, v in f.items():
                lines.append(_render("fk", k))
                lines.append(_render("fv", v))
                lines.append('    form="${form}${fk}=${fv}&"')
            lines.append('    form="${form%&}"')
            lines.append('    bodyfile="$__hf_tmpdir/req_body"')
            lines.append('    printf "%s" "$form" > "$bodyfile"')
            lines.append('    local bflag="--data-binary @$bodyfile"')
        case _:
            lines.append('    local bflag=""')
            lines.append('    bodyfile=""')
    return lines, hdr_var


def _http_step(step: HttpStep, fn: str) -> str:
    body_lines, _ = _body_lines(step)
    out: list[str] = [
        f"{fn}() {{",
        f'    # [[requests]] name = {step.name!r}',
        f'    local quiet="${{1:-0}}" pretty_json="${{2:-0}}" no_mask="${{3:-0}}"',
        f'    local url body bodyfile bflag __hf_cap __hf_ts __hf_qhdr="" __hf_req_body=""',
        f'    local -a __hf_args=()',
        f'    __hf_args+=("-X" "{step.method.upper()}")',
        _render("url", step.url),
    ]

    # headers
    if step.headers:
        for k, v in step.headers.items():
            out.append(_render("hk", k))
            out.append(_render("hv", v))
            out.append('    __hf_args+=("-H" "${hk}: ${hv}")')
            out.append(r'    __hf_qhdr="${__hf_qhdr}${hk}: ${hv}"$' + r"'\n'")

    # body
    out.extend(body_lines)

    # save request body before it gets overwritten for capture
    if step.capture and "request.body" in step.capture.values():
        out.append('    if [ -n "$bodyfile" ] && [ -s "$bodyfile" ]; then')
        out.append('        __hf_req_body="$(cat "$bodyfile")"')
        out.append('    fi')

    # auto content-type for form
    if isinstance(step.body, FormBody):
        if step.headers:
            out.append('    # auto-add Content-Type for form if missing')
            out.append('    local has_ct=0')
            out.append('    for h in ${__hf_args[@]+"${__hf_args[@]}"}; do')
            out.append('        case "$h" in')
            out.append('            [Cc][Oo][Nn][Tt][Ee][Nn][Tt]-[Tt][Yy][Pp][Ee]:*) has_ct=1 ;;')
            out.append('        esac')
            out.append('    done')
            out.append('    if [ "$has_ct" -eq 0 ]; then')
            out.append('        __hf_args+=("-H" "Content-Type: application/x-www-form-urlencoded")')
            out.append(r'        __hf_qhdr="${__hf_qhdr}Content-Type: application/x-www-form-urlencoded"$' + r"'\n'")
            out.append('    fi')
        else:
            out.append('    __hf_args+=("-H" "Content-Type: application/x-www-form-urlencoded")')
            out.append(r'    __hf_qhdr="${__hf_qhdr}Content-Type: application/x-www-form-urlencoded"$' + r"'\n'")

    # Build body flag
    out.extend([
        '    if [ -n "$bflag" ]; then',
        '        __hf_args+=("--data-binary" "@$bodyfile")',
        '    fi',
        '    __hf_args+=("$url")',
    ])

    # summary
    out.extend([
        '    __hf_ts="$(__hf_ts)"',
        f'    printf "%s\\n" "==> $__hf_ts [{step.name}] {step.method.upper()} $url"',
    ])
    if step.description:
        for dl in step.description.splitlines():
            out.append(f'    printf "%s\\n" "    # {dl}"')

    # curl with -w http_code
    out.extend([
        '    local rhdr="$__hf_tmpdir/resp_hdr" rbody="$__hf_tmpdir/resp_body" cerr="$__hf_tmpdir/curl_err"',
        '    local sta',
        "    sta=\"$(curl -sS -v -D \"$rhdr\" -o \"$rbody\" -w '%{http_code}' ${__hf_args[@]+\"${__hf_args[@]}\"} 2>\"$cerr\")\"",
        '    local crc=$?',
        '    if [ "$crc" -ne 0 ]; then',
        '        printf "%s\\n" "error: curl failed (exit $crc)" >&2',
        '        return 1',
        '    fi',
        '    __hf_ts="$(__hf_ts)"',
        f'    printf "%s\\n" "<== $__hf_ts [{step.name}] status=$sta"',
    ])

    # detail log
    out.extend([
        '    if [ "$quiet" -ne 1 ]; then',
        '        while IFS= read -r line; do',
        '            printf "%s\\n" "    $(__hf_mask_line "$line" "$no_mask")"',
        '        done < <(grep "^> " "$cerr" || true)',
        '        if [ -n "$bodyfile" ] && [ -s "$bodyfile" ]; then',
        '            local rtxt="$(cat "$bodyfile")"',
        '            rtxt="$(__hf_pretty "$rtxt" "$pretty_json")"',
        '            while IFS= read -r line; do',
        '                printf "%s\\n" "    > $(__hf_mask_line "$line" "$no_mask")"',
        '            done <<< "$rtxt"',
        '        fi',
        '        while IFS= read -r line; do',
        '            printf "%s\\n" "    $(__hf_mask_line "$line" "$no_mask")"',
        '        done < <(grep "^< " "$cerr" || true)',
        '        if [ -s "$rbody" ]; then',
        '            local rtxt="$(cat "$rbody")"',
        '            rtxt="$(__hf_mask_json "$rtxt" "$no_mask")"',
        '            rtxt="$(__hf_pretty "$rtxt" "$pretty_json")"',
        '            while IFS= read -r line; do',
        '                printf "%s\\n" "    < $(__hf_mask_line "$line" "$no_mask")"',
        '            done <<< "$rtxt"',
        '        fi',
        '    fi',
    ])

    # capture (uses response body file)
    if step.capture:
        out.append('    bodyfile="$rbody"')
        out.extend(_capture_lines(step.capture))

    out.append("}")
    return "\n".join(out)


def _sleep_step(step: SleepStep, fn: str) -> str:
    out = [
        f"{fn}() {{",
        f'    # [[requests]] name = {step.name!r}',
        f'    local quiet="${1:-0}" pretty_json="${2:-0}" no_mask="${3:-0}"',
        f'    local __hf_ts',
        f'    __hf_ts="$(__hf_ts)"',
        f'    printf "%s\\n" "==> $__hf_ts [{step.name}] SLEEP {step.seconds}"',
    ]
    if step.description:
        for dl in step.description.splitlines():
            out.append(f'    printf "%s\\n" "    # {dl}"')
    out.extend([
        f'    if [ "$quiet" -ne 1 ]; then printf "%s\\n" "    > sleep {step.seconds} seconds"; fi',
        f'    sleep {step.seconds}',
        f'    __hf_ts="$(__hf_ts)"',
        f'    printf "%s\\n" "<== $__hf_ts [{step.name}] done"',
        "}",
    ])
    return "\n".join(out)


def _until_step(step: HttpStep, fn: str) -> str:
    assert step.until is not None
    lhs, op, rhs = _until_parts(step.until.condition)
    attempt = f"{fn}_attempt"
    out = [
        _http_step(
            HttpStep(
                name=step.name, method=step.method, url=step.url,
                description=step.description, headers=step.headers,
                body=step.body, capture=step.capture, until=None,
            ),
            attempt,
        ),
        "",
        f"{fn}() {{",
        f'    # [[requests]] name = {step.name!r}',
        f'    local quiet="${1:-0}" pretty_json="${2:-0}" no_mask="${3:-0}"',
        f'    local max={step.until.max_attempts} interval={step.until.interval}',
        f'    local i sat lhs rhs',
        f'    for (( i=1; i<=max; i++ )); do',
        f'        {attempt} "$quiet" "$pretty_json" "$no_mask"',
        _render("lhs", lhs, indent="        "),
        '        lhs=$(printf "%s" "$lhs" | sed "s/^[[:space:]]*//;s/[[:space:]]*$//")',
        _render("rhs", rhs, indent="        "),
        '        rhs=$(printf "%s" "$rhs" | sed "s/^[[:space:]]*//;s/[[:space:]]*$//")',
    ]
    if op == "==":
        out.append('        [ "$lhs" = "$rhs" ] && sat=1 || sat=0')
    elif op == "!=":
        out.append('        [ "$lhs" != "$rhs" ] && sat=1 || sat=0')
    elif op == "in":
        out.append('        sat=0')
        out.append('        # rhs is expected to be "[a, b, c]" — strip brackets and split')
        out.append('        local list="${rhs#[}" list="${list%]}" item')
        out.append('        while IFS= read -r -d "," item || [ -n "$item" ]; do')
        out.append('            item="$(printf "%s" "$item" | sed "s/^[[:space:]]*//;s/[[:space:]]*$//")"')
        out.append('            [ "$lhs" = "$item" ] && { sat=1; break; }')
        out.append('        done <<< "$list,"')
    elif op == "~":
        out.append('        # regex via jq test()')
        out.append('        local pat="${rhs# /}" flags=""')
        out.append('        pat="${pat%/}"')
        out.append('        if [[ "$rhs" == */[ims]* ]]; then')
        out.append('            flags="${rhs##*/}"')
        out.append('            pat="${rhs:2:-${#flags}-1}"')
        out.append('        fi')
        out.append('        if jq -n -e --arg s "$lhs" --arg p "$pat" --arg f "$flags"')
        out.append("            '\$s | test(\$p; if \$f == \"\" then \"\" else \$f end)' >/dev/null 2>&1; then")
        out.append('            sat=1')
        out.append('        else')
        out.append('            sat=0')
        out.append('        fi')
    out.extend([
        '        if [ "$sat" -eq 1 ]; then',
        '            [ "$quiet" -ne 1 ] && printf "%s\n" "    * until satisfied on attempt $i"',
        '            return 0',
        '        fi',
        '        if [ "$i" -lt "$max" ]; then',
        '            [ "$quiet" -ne 1 ] && printf "%s\n" "    * until not satisfied (attempt $i/$max), retrying in ${interval}s"',
        '            sleep "$interval"',
        '        fi',
        '    done',
        f'    printf "%s\n" "error: step {step.name!r}: until condition not satisfied after $max attempts" >&2',
        '    return 1',
        '}',
    ])
    return "\n".join(out)


def _emit(step: Step, fn: str) -> str:
    match step:
        case SleepStep():
            return _sleep_step(step, fn)
        case HttpStep(until=None):
            return _http_step(step, fn)
        case HttpStep():
            return _until_step(step, fn)
        case _:
            raise TypeError(f"unknown step type: {type(step).__name__}")


def _needs_uuid(spec: WorkflowSpec) -> bool:
    """Return True if any template string references random.UUID*."""
    for step in spec.steps:
        match step:
            case SleepStep():
                if "random.UUID" in step.seconds:
                    return True
            case HttpStep():
                for s in (step.url, *step.headers.keys(), *step.headers.values()):
                    if "random.UUID" in s:
                        return True
                match step.body:
                    case TextBody(text=t):
                        if "random.UUID" in t:
                            return True
                    case FormBody(fields=f):
                        for s in (*f.keys(), *f.values()):
                            if "random.UUID" in s:
                                return True
                if step.until is not None and "random.UUID" in step.until.condition:
                    return True
    return False


# ---------------------------------------------------------------- public API


def generate(
    spec: WorkflowSpec,
    *,
    default_vars: dict[str, str] | None = None,
    default_repeat_vars: dict[str, list[str]] | None = None,
    shebang: bool = False,
) -> str:
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    dvars = dict(default_vars or {})

    used: set[str] = set()
    blocks: list[str] = []
    calls: list[str] = []
    for step in spec.steps:
        fn = _step_name(step.name, used)
        blocks.append(_emit(step, fn))
        calls.append(f"        {fn} \"$quiet\" \"$pretty_json\" \"$no_mask\" || return $?")

    if not blocks:
        funcs = "# (no steps)"
        calls_src = "        :  # no steps"
    else:
        funcs = "\n\n".join(blocks)
        calls_src = "\n".join(calls)

    has_until = any(isinstance(s, HttpStep) and s.until is not None for s in spec.steps)
    req_repeat = sorted(collect_repeat_names(spec))
    needs_repeat = bool(req_repeat)

    until_h = ""
    if has_until:
        until_h = """__hf_get_header() {
    local raw="$1" target="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')" line key value
    while IFS= read -r line; do
        [[ "$line" =~ ^HTTP/ ]] && continue
        [ -z "$line" ] && continue
        key="${line%%:*}"; value="${line#*: }"
        [ "$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')" = "$target" ] && { printf '%s' "$value"; return 0; }
    done <<< "$raw"
    return 1
}"""

    needs_uuid = _needs_uuid(spec)
    uuid_dep = ""
    if needs_uuid:
        uuid_dep = 'command -v uuidgen >/dev/null 2>&1 || [ -r /proc/sys/kernel/random/uuid ] || __hf_missing="$__hf_missing uuidgen"'

    repeat_h = ""
    if needs_repeat:
        repeat_h = """__hf_build_repeat() {
    shift
    local keys=() raws=() n=0
    local kv k v
    for kv in "$@"; do
        k="${kv%%=*}"; v="${kv#*=}"
        keys+=("$k"); raws+=("$v")
        local -a parts=()
        IFS=',' read -r -a parts <<< "$v"
        [ "${#parts[@]}" -eq 0 ] && { echo "error: empty repeat values" >&2; return 1; }
        local p; for p in ${parts[@]+"${parts[@]}"}; do
            [ -z "$p" ] && { echo "error: empty repeat element" >&2; return 1; }
        done
        if [ "$n" -eq 0 ]; then n="${#parts[@]}"
        elif [ "${#parts[@]}" -ne "$n" ]; then
            echo "error: repeat value counts must match" >&2; return 1
        fi
    done
    __HF_REPEAT_RESULT=()
    local i j
    for (( i=0; i<n; i++ )); do
        local iter=""
        for (( j=0; j < ${#keys[@]}; j++ )); do
            local -a parts=()
            IFS=',' read -r -a parts <<< "${raws[$j]}"
            local val="${parts[$i]}"
            val="$(printf '%s' "$val" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
            iter="${iter}${keys[$j]}=$val "
        done
        __HF_REPEAT_RESULT+=("$iter")
    done
}"""

    # default vars
    apply_defs = []
    for k, v in dvars.items():
        apply_defs.append(f'    __hf_var_set {_bash_sq(k)} {_bash_sq(v)}')
    apply_default_vars = "\n".join(apply_defs) if apply_defs else "    :"

    req_vars = sorted(collect_var_names(spec) - set(dvars))

    # repeat defaults
    dr_lines = []
    if default_repeat_vars:
        for k, vals in default_repeat_vars.items():
            dr_lines.append(f'    __hf_var_set "__HF_RDEF_{_enc(k)}" "{",".join(vals)}"')
    dr_apply = "\n".join(dr_lines)

    # repeat setup in main
    if needs_repeat:
        main_repeat = """    local -a rargs=()
    local rkv
    for rkv in ${user_repeat_vars[@]+"${user_repeat_vars[@]}"}; do
        rargs+=("$rkv")
    done
    __hf_build_repeat dummy ${rargs[@]+"${rargs[@]}"}
    if [ $? -ne 0 ]; then return 1; fi
    local tot="${#__HF_REPEAT_RESULT[@]}"
    if [ "$tot" -eq 0 ]; then echo "error: no repeat iterations" >&2; return 1; fi
    local idx iter
    for (( idx=0; idx < tot; idx++ )); do
        iter="${__HF_REPEAT_RESULT[$idx]}"
        local pkv pk pv
        for pkv in $iter; do
            pk="${pkv%%=*}"; pv="${pkv#*=}"
            __hf_rep_set "$pk" "$pv"
        done
        printf "%s\\n" "=== repeat iteration $((idx+1))/$tot { $iter } ==="
"""
        main_repeat_done = "    done"
    else:
        main_repeat = "    :  # no repeat"
        main_repeat_done = ""

    rendered = (
        template
        .replace("{{VERSION}}", __version__)
        .replace("{{GENERATED_AT}}", ts)
        .replace("{{UUID_DEP_CHECK}}", uuid_dep)
        .replace("{{UNTIL_HELPERS}}", until_h)
        .replace("{{REPEAT_HELPERS}}", repeat_h)
        .replace("{{REQUIRED_VARS}}", _space_list(req_vars))
        .replace("{{REQUIRED_REPEAT_VARS}}", _space_list(req_repeat))
        .replace("{{APPLY_DEFAULT_VARS}}", apply_default_vars)
        .replace("{{MAIN_REPEAT_SETUP}}", main_repeat)
        .replace("{{STEP_FUNCTIONS}}", funcs)
        .replace("{{STEP_CALLS}}", calls_src)
        .replace("{{MAIN_REPEAT_DONE}}", main_repeat_done)
    )

    if shebang:
        rendered = "#!/usr/bin/env bash\n" + rendered
    return rendered


def _space_list(items: list[str]) -> str:
    return " ".join(items)
