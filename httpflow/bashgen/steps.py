from __future__ import annotations

from httpflow.model import FileBody, FormBody, HttpStep, MultipartBody, MultipartField, MultipartFile, SleepStep, TextBody

from .capture import capture_calls
from .conditions import split_until_condition
from .placeholders import PlaceholderRenderer
from .shell import dq_literal, dq_preserve_expansion, sq


class StepEmitter:
    """Emit bash functions for workflow steps.

    Each HTTP step function assembles its own ``cmd`` (curl argument) array
    inline so the generated script shows the exact curl command that will
    run. The shared ``http_step`` helper is only a thin executor (curl run +
    log + trace file). Capture calls are issued directly after ``http_step``
    returns, using the trace file path exposed via ``$HF_TRACE_FILE``.
    """

    def __init__(
        self,
        placeholders: PlaceholderRenderer,
        embedded_map: dict[str, str] | None = None,
    ) -> None:
        """Create a step emitter.

        *embedded_map* maps var_name → base64 content for files embedded
        via the ``--embed-files`` option.
        """
        self._placeholders = placeholders
        self._embedded = embedded_map or {}

    @property
    def _ph(self) -> PlaceholderRenderer:
        return self._placeholders

    def _has_header(self, headers: dict[str, str], name: str) -> bool:
        """Return True when headers already define *name* case-insensitively."""
        return any(k.lower() == name.lower() for k in headers)

    def _validate_no_tabs_newlines(self, value: str, context: str) -> None:
        """Raise ValueError when *value* contains tab or newline."""
        if "\t" in value or "\n" in value:
            raise ValueError(f"{context} must not contain tabs or newlines: {value!r}")

    def _validate_curl_form_safe(self, value: str, context: str) -> None:
        """Raise ValueError when *value* would break curl -F quoted syntax."""
        self._validate_no_tabs_newlines(value, context)
        if '"' in value:
            raise ValueError(f"{context} must not contain double quotes: {value!r}")

    def emit(self, plan: object) -> str:
        """Emit a bash function block for one planned step."""
        step = plan.step
        fn = plan.function_name
        if isinstance(step, SleepStep):
            return self.emit_sleep(step, fn)
        if isinstance(step, HttpStep):
            if step.until is not None:
                return self.emit_http_until(step, fn, plan.attempt_function_name)
            return self.emit_http(step, fn)
        raise TypeError(f"unknown step type: {type(step).__name__}")

    def _emit_decode_file(self, var_name: str) -> list[str]:
        """Return bash lines that decode an embedded file to a temp file.

        Returns empty list if *var_name* is not in the embedded map.
        """
        if var_name not in self._embedded:
            return []
        return [
            '    local decode_file',
            '    decode_file=$(mktemp "$HF_TMPDIR/hf_embed.XXXXXX") || return $?',
            f"    printf '%s' \"${{__HF_EMBED_{var_name}}}\" | _hf_b64decode > \"$decode_file\"",
        ]

    def _embed_var_name_file_body(self, function_name: str) -> str:
        """Return the embedded var name for a body_file step."""
        return f"{function_name}_body"

    def _embed_var_name_mp(self, function_name: str, idx: int) -> str:
        """Return the embedded var name for a multipart file part."""
        return f"{function_name}_mp{idx}"

    def emit_http(self, step: HttpStep, function_name: str) -> str:
        """Emit an HTTP step function.

        The function builds the ``cmd`` (curl) array directly from the step
        definition, calls ``http_step`` to execute it, then issues any
        ``capture_*`` calls using the trace file left in ``$HF_TRACE_FILE``.
        """

        # Validate multipart Content-Type before anything else (fail fast).
        if isinstance(step.body, MultipartBody) and self._has_header(step.headers, "Content-Type"):
            raise ValueError(
                f"body_multipart step {step.name!r}: Content-Type is set automatically by curl; "
                f"remove the user-specified Content-Type header"
            )

        out: list[str] = [
            f"{function_name}() {{",
            f'    local url={self._ph.expr(step.url)}',
            "    local body",
            '    local body_log=""',
            "    local has_body=0",
            '    local headers_text=""',
            f'    local -a cmd=(curl -sS -L -v --no-buffer --stderr - -X {step.method.upper()})',
        ]

        self._emit_body(step, function_name, out)
        self._emit_headers(step, out)
        out.append('    cmd+=("$url")')

        out.append(
            f'    http_step {sq(step.name)} {sq(step.method.upper())} "$url" "$body_log" "$has_body" '
            f'{sq(step.description or "")} "${{cmd[@]}}" || return $?'
        )

        out.extend(capture_calls(step))

        out.append("}")
        return "\n".join(out)

    def _emit_body(self, step: HttpStep, function_name: str, out: list[str]) -> None:
        """Append lines that build ``cmd``/``body_log``/``has_body`` for the step body."""
        body = step.body
        if isinstance(body, TextBody):
            out.append("    body=$(cat << EOT")
            out.append(self._ph.expand(body.text))
            out.append("EOT")
            out.append(')')
            out.append('    body="${body}$(printf "\\n")"')
            out.append('    body_log="$body"')
            out.append("    has_body=1")
            out.append('    cmd+=(-d "$body")')
        elif isinstance(body, FormBody):
            log_parts: list[str] = []
            for k, v in body.fields.items():
                self._validate_no_tabs_newlines(k, f"body_form key {k!r}")
                self._validate_no_tabs_newlines(v, f"body_form value {v!r}")
                k_e = self._ph.expand(k)
                v_e = self._ph.expand(v)
                out.append(f'    cmd+=(--data-urlencode {dq_preserve_expansion(f"{k_e}={v_e}")})')
                log_parts.append(f"{k_e}={v_e}")
            body_log_text = "Note: Values are shown before URL encoding.\n" + "&".join(log_parts)
            out.append(f'    body_log={dq_preserve_expansion(body_log_text)}')
            out.append("    has_body=1")
        elif isinstance(body, FileBody):
            embed_var = self._embed_var_name_file_body(function_name)
            decode = self._emit_decode_file(embed_var)
            if decode:
                out.extend(decode)
                path_inner = "$decode_file"
            else:
                path_inner = self._ph.expand(body.path)
            self._emit_file_body(out, path_inner)
        elif isinstance(body, MultipartBody):
            out.append('    body_log="(multipart)"')
            for idx, part in enumerate(body.parts):
                if isinstance(part, MultipartField):
                    self._emit_multipart_field(out, part)
                else:
                    self._emit_multipart_file(out, step, function_name, idx, part)
            out.append("    has_body=1")
        # else: no body, leave has_body=0 and body_log=""

    def _emit_file_body(self, out: list[str], path_inner: str) -> None:
        """Append lines for a body_file step (existence check + curl arg + body_log).

        The file info goes into ``body_log`` so ``http_step`` prints it inside the
        request-body section (after the ``==>`` banner), not before the banner.
        """
        out.append(f'    [[ -f "{path_inner}" ]] || {{ echo "error: body_file not found: {path_inner}" >&2; return 1; }}')
        out.append(f'    cmd+=(--data-binary "@{path_inner}")')
        out.append(f'    file_size=$(($(wc -c < "{path_inner}")))')
        out.append(f'    body_log="Note: binary body from file: {path_inner} (${{file_size}} bytes)"')
        out.append("    has_body=1")

    def _emit_multipart_field(self, out: list[str], part: MultipartField) -> None:
        """Append lines for a multipart field part (--form-string + body_log)."""
        name_e = self._ph.expand(part.name)
        value_e = self._ph.expand(part.value)
        self._validate_no_tabs_newlines(name_e, f"multipart field name {part.name!r}")
        self._validate_no_tabs_newlines(value_e, f"multipart field value {part.value!r}")
        out.append(f'    cmd+=(--form-string {dq_preserve_expansion(f"{name_e}={value_e}")})')
        # Append to body_log so http_step prints it inside the request-body
        # section (after the ==> banner), not before the banner.
        out.append(f'    body_log="${{body_log}}\n  {name_e} = {value_e}"')

    def _emit_multipart_file(
        self,
        out: list[str],
        step: HttpStep,
        function_name: str,
        idx: int,
        part: MultipartFile,
    ) -> None:
        """Append lines for a multipart file part (existence check + curl -F + log)."""
        name_e = self._ph.expand(part.name)
        filename_e = self._ph.expand(part.filename) if part.filename else ""
        type_e = self._ph.expand(part.content_type)
        self._validate_curl_form_safe(name_e, f"multipart file name {part.name!r}")

        embed_var = self._embed_var_name_mp(function_name, idx)
        if embed_var in self._embedded:
            out.append(f"    local decode_file_{idx}")
            out.append(f'    decode_file_{idx}=$(mktemp "$HF_TMPDIR/hf_embed.XXXXXX") || return $?')
            out.append(
                f"    printf '%s' \"${{__HF_EMBED_{embed_var}}}\" | _hf_b64decode > \"$decode_file_{idx}\""
            )
            path_inner = f"$decode_file_{idx}"
        else:
            path_e = self._ph.expand(part.path)
            self._validate_curl_form_safe(path_e, f"multipart file path {part.path!r}")
            path_inner = path_e

        if filename_e:
            self._validate_curl_form_safe(filename_e, f"multipart file filename {part.filename!r}")
        if type_e:
            self._validate_curl_form_safe(type_e, f"multipart file type {part.content_type!r}")

        out.append(f'    [[ -f "{path_inner}" ]] || {{ echo "error: multipart file not found: {path_inner}" >&2; return 1; }}')

        # Wrap path and filename in double quotes so curl treats `;`, `,` and
        # `"` inside them as literal characters rather than option separators
        # (curl 7.55+).
        f_arg = f'{name_e}=@\\"{path_inner}\\"'
        if filename_e:
            f_arg += f';filename=\\"{filename_e}\\"'
            if type_e:
                f_arg += f';type={type_e}'
        elif type_e:
            f_arg += f';type={type_e}'
        out.append(f'    cmd+=(-F "{f_arg}")')
        out.append(f'    file_size=$(($(wc -c < "{path_inner}")))')
        # Append to body_log so http_step prints it inside the request-body
        # section (after the ==> banner), not before the banner.
        file_log_entry = f"  {name_e} = @{path_inner}"
        if filename_e:
            file_log_entry += f"; filename={filename_e}"
        if type_e:
            file_log_entry += f"; type={type_e}"
        file_log_entry += f"; bytes=${{file_size}}"
        out.append(f'    body_log="${{body_log}}\n{file_log_entry}"')

    def _emit_headers(self, step: HttpStep, out: list[str]) -> None:
        """Append lines that build ``headers_text`` and the matching ``cmd+=(-H ...)`` args."""
        header_lines = [self._ph.expand(f"{k}: {v}") for k, v in step.headers.items()]
        if isinstance(step.body, FormBody) and not self._has_header(step.headers, "Content-Type"):
            header_lines.append("Content-Type: application/x-www-form-urlencoded")
        if isinstance(step.body, FileBody) and not self._has_header(step.headers, "Content-Type"):
            header_lines.append("Content-Type: application/octet-stream")
        if not header_lines:
            return
        out.append("    headers_text=$(cat << EOT")
        out.extend(header_lines)
        out.append("EOT")
        out.append(")")
        out.append('    while IFS= read -r line; do')
        out.append('        cmd+=(-H "$line")')
        out.append('    done <<< "$headers_text"')

    def emit_http_until(self, step: HttpStep, function_name: str, attempt_function_name: str | None = None) -> str:
        """Emit an HTTP step wrapped in an until polling loop."""
        assert step.until is not None
        lhs, op, rhs = split_until_condition(step.until.condition)
        attempt_fn = attempt_function_name or f"{function_name}_attempt"
        out = self.emit_http(step, attempt_fn).splitlines()
        out.extend([
            "",
            f"{function_name}() {{",
            "    local attempt",
            f"    local max_attempts={step.until.max_attempts}",
            f"    local interval={step.until.interval}",
            "    local until_lhs until_rhs",
            "    for ((attempt=1; attempt<=max_attempts; attempt++)); do",
            f"        {attempt_fn} || return $?",
            f"        until_lhs={self._ph.expr(lhs)}",
            f"        until_rhs={self._ph.expr(rhs)}",
            f'        until_eval "$until_lhs" {sq(op)} "$until_rhs" && {{ echo "    * until satisfied on attempt $attempt"; return 0; }}',
            '        [[ "$attempt" -lt "$max_attempts" ]] && {',
            '            echo "    * until not satisfied (attempt $attempt/$max_attempts), retrying in ${interval}s"',
            '            sleep "$interval"',
            "        }",
            "    done",
            f"    echo {dq_literal(f'step {step.name!r}: until condition not satisfied after ')}\"$max_attempts\"{dq_literal(f' attempts: {step.until.condition!r}')} >&2",
            "    return 1",
            "}",
        ])
        return "\n".join(out)

    def emit_sleep(self, step: SleepStep, function_name: str) -> str:
        """Emit a sleep step function."""
        out = [
            f"{function_name}() {{",
            f'    local seconds={self._ph.expr(step.seconds)}',
            '    print_blank_lines "${HTTPFLOW_BLANK_LINE:-0}"',
            f'    printf "==> %s [%s] SLEEP %s\\n" "$(time_date_iso)" {sq(step.name)} "$seconds"',
        ]
        if step.description:
            for dl in step.description.splitlines():
                out.append(f'    printf "# %s\\n" {sq(dl)}')
        out.append('    sleep "$seconds"')
        out.append(f'    printf "<== %s [%s] done\\n" "$(time_date_iso)" {sq(step.name)}')
        out.append("}")
        return "\n".join(out)
