from __future__ import annotations


def base_helpers(mask_keys_default: str) -> str:
    """Return base bash helpers used by every generated script."""
    return """MASK_KEYS_DEFAULT='""" + mask_keys_default + """'
MASK_KEYS="$MASK_KEYS_DEFAULT${HTTPFLOW_MASK_EXTRA:+|${HTTPFLOW_MASK_EXTRA}}"
MASK_SED_EXPR="s/(\\\"?($MASK_KEYS)\\\"?)([[:space:]]*[:=][[:space:]]*)\\\"?[^& ,}\\\"]+( [^& ,}\\\"]+)?\\\"?/\\1\\3***/g"

mask() {
    if [ -n "${HTTPFLOW_NO_MASK:-}" ]; then
        echo "$1"
        return 0
    fi
    printf '%s\\n' "$1" | sed -E "$MASK_SED_EXPR"
}

mask_lines() {
    while IFS= read -r LINE || [ -n "$LINE" ]; do
        mask "$LINE"
    done
}

print_blank_lines() {
    local count=${1:-0}
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
}

now() {
    if date '+%Y-%m-%d %H:%M:%S.%3N' | grep -Eq '[0-9]{3}$'; then
        date '+%Y-%m-%d %H:%M:%S.%3N'
    else
        date '+%Y-%m-%d %H:%M:%S.000'
    fi
}

uuid() {
    if command -v uuidgen &>/dev/null; then
        uuidgen | awk '{print tolower($1)}'
    else
        awk 'function hex(n, i, s) {for (i = 0; i < n; i++) s = s sprintf("%x", int(rand() * 16));return s;} BEGIN {srand();printf "%s-%s-%s-%s-%s\n", hex(8), hex(4), "4" hex(3), sprintf("%x", int(rand() * 4) + 8) hex(3), hex(12);}'
    fi
}

uuid_hex() {
    uuid | sed "s/-//g"
}

time_date_iso() {
    local ns micro
    ns=$(date '+%N')
    case "$ns" in
        *N*) micro=000000 ;;
        *) micro=${ns:0:6} ;;
    esac
    date "+%Y-%m-%dT%H:%M:%S.${micro}%z" | sed -E 's/([+-][0-9][0-9])([0-9][0-9])$/\\1:\\2/'
}

time_date_ymd() {
    date '+%Y%m%d'
}

time_date_ymdhms() {
    date '+%Y%m%d%H%M%S'
}

"""


def capture_helpers() -> str:
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
    if ! value=$(trace_response_body "$trace_file" | jq -r "$filter"); then
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
            index(lower, want) == 1 {
                value=substr(line, length(name) + 2)
                sub(/^[[:space:]]+/, "", value)
                found=1
            }
            END { if (!found) exit 1; print value }
        ' <<< "$input_source"); then
        echo "capture failed: $display_name <- $source" >&2
        return 1
    fi
    capture_value "$env_name" "$display_name" "$source" "$value"
}
'''


def http_helpers(has_capture: bool) -> str:
    """Return bash helper functions used by generated HTTP steps."""
    capture_dispatch = r'''

run_captures() {
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
trace_response_body() {
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
    trimmed=$(printf '%s' "$input" | sed 's/^[[:space:]]*//' | head -c1)

    if [ -z "${HTTPFLOW_PRETTY_JSON:-}" ]; then
        printf '%s\n' "$input"
        return 0
    fi

    if [ "$trimmed" != "{" ] && [ "$trimmed" != "[" ]; then
        printf '%s\n' "$input"
        return 0
    fi

    if printf '%s\n' "$input" | jq . > /dev/null 2>&1; then
        printf '%s\n' "$input" | jq .
    else
        printf '%s\n' "$input"
    fi
}

prefix_lines() {
    local prefix=$1
    while IFS= read -r line || [ -n "$line" ]; do
        printf "%s%s\n" "$prefix" "$line"
    done
}

http_step() {
    local step_name=$1
    local method=$2
    local url=$3
    local has_body=$4
    local body=$5
    local body_form_text=$6
    local headers_text=$7
    local captures_text=$8
    local description=$9
    local trace_file line header form_key form_value
    local -a cmd
    local boundary_inserted=0

    print_blank_lines "${HTTPFLOW_BLANK_LINE:-0}"

    echo "==> $(now) [$step_name] $method $(mask "$url")"
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

    if [ -n "$body_form_text" ]; then
        notice="Note: Values are shown before URL encoding.
"
        body=
        while IFS=$'\t' read -r form_key form_value || [ -n "$form_key$form_value" ]; do
            [ -z "$form_key" ] && continue
            cmd+=(--data-urlencode "$form_key=$form_value")
            if [ -n "$body" ]; then
                body+="&"
            fi
            body+="$form_key=$form_value"
        done <<< "$body_form_text"
        body="${notice}${body}"
    elif [ "$has_body" = "1" ]; then
        cmd+=(-d "$body")
    fi
    cmd+=("$url")

    # Only curl/pipeline failures fail the step here; HTTP 4xx/5xx responses
    # are preserved for capture/until evaluation and are not treated as errors.
    if ! "${cmd[@]}" \
        | grep -v '^\({\|}\) \[.*bytes data\]' \
        | grep -v '^\*' \
        | sed -e 's/\* Closing.*//' -e 's/\* Connection.*//' \
        | while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in
                "< HTTP/"*)
                    if [ "$boundary_inserted" = "0" ]; then
                        boundary_inserted=1
                        printf "<== %s [%s]\n" "$(now)" "$step_name"
                    fi
                    printf "%s\n" "$line"
                    ;;
                ">"|"> "|$'> \r')
                    printf "%s\n" "$line"
                    if [ "$has_body" = "1" ]; then
                        # Request body echoed by this script; curl -v omits it.
                        printf "%s" "$body" | jq_or_cat | prefix_lines "> "
                    fi
                    ;;
                *)
                    printf "%s\n" "$line" | jq_or_cat | prefix_lines ""
                    ;;
            esac
        done \
        | tee -a "$trace_file" \
        | mask_lines; then
        return 1
    fi

    if [ -n "$captures_text" ]; then
        run_captures "$captures_text" "$url" "$body" "$headers_text" "$trace_file" || {
            return 1
        }
    fi
}
'''


def until_helpers() -> str:
    """Return bash helper functions used by until-enabled scripts."""
    return r'''
trim() {
    local value=$1
    value=${value#"${value%%[![:space:]]*}"}
    value=${value%"${value##*[![:space:]]}"}
    printf '%s' "$value"
}

until_regex() {
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

until_eval() {
    local lhs rhs op item list
    lhs=$(trim "$1")
    op=$2
    rhs=$(trim "$3")
    case "$op" in
        '==') [ "$lhs" = "$rhs" ] ;;
        '!=') [ "$lhs" != "$rhs" ] ;;
        '~') until_regex "$lhs" "$rhs" ;;
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
                item=$(trim "$item")
                [ -z "$item" ] && continue
                [ "$lhs" = "$item" ] && return 0
            done
            return 1
            ;;
        *) echo "until condition: unknown operator $op" >&2; return 2 ;;
    esac
}
'''

