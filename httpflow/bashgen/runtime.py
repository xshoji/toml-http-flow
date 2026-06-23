from __future__ import annotations


def base_helpers(mask_keys_default: str, *, include_b64decode: bool = False) -> str:
    """Return base bash helpers used by every generated script."""
    b64decode_impl = r"""
_hf_b64decode() {
    if base64 -d /dev/null 2>/dev/null; then
        base64 -d
    else
        base64 -D
    fi
}
""" if include_b64decode else ""
    return b64decode_impl + """MASK_KEYS_DEFAULT='""" + mask_keys_default + """'
MASK_KEYS="$MASK_KEYS_DEFAULT${HTTPFLOW_MASK_EXTRA:+|${HTTPFLOW_MASK_EXTRA}}"
MASK_SED_EXPR="s/(\\\"?($MASK_KEYS)\\\"?)([[:space:]]*[:=][[:space:]]*)\\\"?[^& }\\\"]+( [^& }\\\"]+)*\\\"?/\\1\\3***/g"
MASK_HEADER_EXPR="s/^([[:space:]]*[<>]?[[:space:]]*($MASK_KEYS)[[:space:]]*:[[:space:]]*).*/\\1***/"

mask() {
    printf '%s\\n' "$1" | mask_lines
}

mask_lines() {
    [[ -n "${HTTPFLOW_NO_MASK:-}" ]] && { cat; return 0; }
    sed -E "$MASK_HEADER_EXPR; $MASK_SED_EXPR"
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
    printf '%s\n' "$name" | grep -Eiq "^($MASK_KEYS)$" && value="***"
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
    ! value=$(trace_response_body "$trace_file" | jq -r "$filter") && {
        echo "capture failed: $display_name <- $source" >&2
        return 1
    }
    [[ -z "$value" || "$value" == "null" ]] && {
        echo "capture failed: $display_name <- $source" >&2
        return 1
    }
    capture_value "$env_name" "$display_name" "$source" "$value"
}

capture_header() {
    local env_name=$1
    local display_name=$2
    local source=$3
    local input_source=$4
    local header_name=$5
    local value mode
    [[ -f "$input_source" ]] && { mode=trace; input_source=$(cat "$input_source"); } || mode=text
    if ! value=$(awk -v name="$header_name" -v mode="$mode" '
            BEGIN { want=tolower(name) ":"; found=0; value="" }
            mode == "trace" && /^< HTTP\// { found=0; value=""; next }
            mode == "trace" && !/^< / { next }
            mode == "trace" && /^< ?\r?$/ { next }
            mode == "trace" { line=substr($0, 3) }
            mode != "trace" && !/-H "/ { next }
            mode != "trace" { match($0, /-H "([^"]+)"/); line=substr($0, RSTART + 4, RLENGTH - 5) }
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


def http_helpers() -> str:
    """Return bash helper functions used by generated HTTP steps.

    ``http_step`` is a thin executor: it receives the fully-assembled curl
    command as a single string (``curl_command``) built by the step
    function, ``eval``s it to run curl through the log/mask pipeline, and
    exposes the trace file path via ``HF_TRACE_FILE`` so the step function
    can issue ``capture_*`` calls afterwards.
    """
    return r'''
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
    [[ -z "${HTTPFLOW_PRETTY_JSON:-}" ]] && { cat; return 0; }

    local input trimmed
    input=$(cat)
    trimmed=${input#"${input%%[![:space:]]*}"}
    trimmed=${trimmed:0:1}

    [[ "$trimmed" != "{" && "$trimmed" != "[" ]] && {
        printf '%s\n' "$input"
        return 0
    }

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
    local body_log=$4
    local has_body=$5
    local description=$6
    local curl_command=$7
    local trace_file boundary_inserted=0

    print_blank_lines "${HTTPFLOW_BLANK_LINE:-0}"

    echo "==> $(time_date_iso) [$step_name] $method $(mask "$url")"
    [[ -n "$description" ]] && {
        while IFS= read -r line || [ -n "$line" ]; do
            echo "# $line"
        done <<< "$description"
    }

    trace_file=$(mktemp "$HF_TMPDIR/hf_trace.XXXXXX")
    : > "$trace_file"
    HF_TRACE_FILE="$trace_file"

    # Only curl/pipeline failures fail the step here; HTTP 4xx/5xx responses
    # are preserved for capture/until evaluation and are not treated as errors.
    if ! eval "${curl_command//$'\n'/ }" \
        | grep -v '^\({\|}\) \[.*bytes data\]' \
        | grep -v '^\*' \
        | sed -e 's/\* Closing.*//' -e 's/\* Connection.*//' \
        | while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in
                "< HTTP/"*)
                    [[ "$boundary_inserted" == "0" ]] && {
                        boundary_inserted=1
                        printf "<== %s [%s]\n" "$(time_date_iso)" "$step_name"
                    }
                    printf "%s\n" "$line"
                    ;;
                ">"|"> "|$'> \r')
                    printf "%s\n" "$line"
                    if [ "$has_body" = "1" ]; then
                        # Request body echoed by this script; curl -v omits it.
                        if [ -n "${HTTPFLOW_PRETTY_JSON:-}" ]; then
                            printf "%s" "$body_log" | jq_or_cat | prefix_lines "> "
                        else
                            printf "%s" "$body_log" | prefix_lines "> "
                        fi
                    fi
                    ;;
                *)
                    if [ -n "${HTTPFLOW_PRETTY_JSON:-}" ]; then
                        printf '%s\n' "$line" | jq_or_cat | prefix_lines ""
                    else
                        printf '%s\n' "$line"
                    fi
                    ;;
            esac
        done \
        | tee -a "$trace_file" \
        | mask_lines; then
        return 1
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
    [[ "$flags" == *i* ]] && shopt -s nocasematch
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
