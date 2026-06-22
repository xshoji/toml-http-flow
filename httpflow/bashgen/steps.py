from __future__ import annotations

from httpflow.model import FileBody, FormBody, HttpStep, MultipartBody, MultipartField, MultipartFile, SleepStep, TextBody

from .capture import capture_calls
from .conditions import split_until_condition
from .placeholders import PlaceholderRenderer
from .shell import dq_literal, dq_preserve_expansion, sq


class StepEmitter:
    """Emit bash functions for workflow steps.

    Each HTTP step function assembles its full ``curl`` command as a single
    string stored in ``curl_command`` via a *quoted* heredoc
    (``<< 'EOT'``). Quoting the delimiter suppresses expansion at heredoc
    time so that ``$body``, ``$(uuid)``, ``${VAR_*}`` etc. stay literal in
    the string; ``http_step`` then ``eval``s the string so the expansions
    happen at execution time. Backslash-newline (``\\<newline>``) is used
    inside the heredoc to split the command across lines for readability —
    ``eval`` treats ``\\<newline>`` as a line continuation and joins the
    lines back into a single command.

    The shared ``http_step`` helper is only a thin executor (eval + curl run
    + log + trace file). Capture calls are issued directly after
    ``http_step`` returns, using the trace file path exposed via
    ``$HF_TRACE_FILE``.
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

        The function builds the full ``curl`` command as a single string
        (``curl_command``) from the step definition, calls ``http_step`` to
        ``eval`` and execute it, then issues any ``capture_*`` calls using
        the trace file left in ``$HF_TRACE_FILE``.
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
            "    local curl_command",
        ]

        # Body setup lines + curl args for the body.
        body_args = self._emit_body(step, function_name, out)

        # Header args (inline -H or reference to $h_args) + optional headers_text heredoc for capture.
        header_args = self._emit_headers(step, out)

        # Assemble the full curl command string.
        # Order: base flags, -X METHOD, body args, header args, then "$url" last.
        base = f"curl -sS -L -v --no-buffer --stderr - -X {step.method.upper()}"
        curl_args = body_args + header_args + ['"$url"']
        curl_lines = self._format_curl_command(base, curl_args)

        out.append("    curl_command=$(cat << 'EOT'")
        out.extend(curl_lines)
        out.append("EOT")
        out.append(")")

        out.append(
            f'    http_step {sq(step.name)} {sq(step.method.upper())} "$url" "$body_log" "$has_body" '
            f'{sq(step.description or "")} "$curl_command" || return $?'
        )

        out.extend(capture_calls(step))

        out.append("}")
        return "\n".join(out)

    @staticmethod
    def _format_curl_command(base: str, args: list[str]) -> list[str]:
        """Format base + args as multi-line curl command with ``\\`` continuations.

        Returns the list of physical lines that go between the heredoc
        delimiters. The first line is ``base \\``, each middle arg is
        ``  arg \\``, and the last arg (always ``"$url"``) has no trailing
        backslash. When there is only the url arg, everything stays on one
        line.
        """
        if len(args) <= 1:
            return [f"{base} {args[0]}"] if args else [base]
        lines = [f"{base} \\"]
        for arg in args[:-1]:
            lines.append(f"  {arg} \\")
        lines.append(f"  {args[-1]}")
        return lines

    def _emit_body(self, step: HttpStep, function_name: str, out: list[str]) -> list[str]:
        """Append body setup lines and return the curl arg strings for the body."""
        body = step.body
        if isinstance(body, TextBody):
            return self._emit_text_body(body, out)
        if isinstance(body, FormBody):
            return self._emit_form_body(body, out)
        if isinstance(body, FileBody):
            return self._emit_file_body(body, function_name, out)
        if isinstance(body, MultipartBody):
            return self._emit_multipart_body(body, step, function_name, out)
        return []

    def _emit_text_body(self, body: TextBody, out: list[str]) -> list[str]:
        """Append heredoc/body_log lines and return the ``-d`` curl arg."""
        out.append("    body=$(cat << EOT")
        out.append(self._ph.expand(body.text))
        out.append("EOT")
        out.append(")")
        out.append('    body="${body}$(printf \"\\n\")"')
        out.append('    body_log="$body"')
        out.append("    has_body=1")
        return ['-d "$body"']

    def _emit_form_body(self, body: FormBody, out: list[str]) -> list[str]:
        """Append body_log lines and return ``--data-urlencode`` curl args."""
        args: list[str] = []
        log_parts: list[str] = []
        for k, v in body.fields.items():
            self._validate_no_tabs_newlines(k, f"body_form key {k!r}")
            self._validate_no_tabs_newlines(v, f"body_form value {v!r}")
            k_e = self._ph.expand(k)
            v_e = self._ph.expand(v)
            args.append(f'--data-urlencode {dq_preserve_expansion(f"{k_e}={v_e}")}')
            log_parts.append(f"{k_e}={v_e}")
        body_log_text = "Note: Values are shown before URL encoding.\n" + "&".join(log_parts)
        out.append(f'    body_log={dq_preserve_expansion(body_log_text)}')
        out.append("    has_body=1")
        return args

    def _emit_file_body(self, body: FileBody, function_name: str, out: list[str]) -> list[str]:
        """Append existence check/body_log lines and return the ``--data-binary`` curl arg."""
        embed_var = self._embed_var_name_file_body(function_name)
        decode = self._emit_decode_file(embed_var)
        if decode:
            out.extend(decode)
            path_inner = "$decode_file"
        else:
            path_inner = self._ph.expand(body.path)
        out.append(f'    [[ -f "{path_inner}" ]] || {{ echo "error: body_file not found: {path_inner}" >&2; return 1; }}')
        out.append(f'    file_size=$(($(wc -c < "{path_inner}")))')
        out.append(f'    body_log="Note: binary body from file: {path_inner} (${{file_size}} bytes)"')
        out.append("    has_body=1")
        return [f'--data-binary "@{path_inner}"']

    def _emit_multipart_body(self, body: MultipartBody, step: HttpStep, function_name: str, out: list[str]) -> list[str]:
        """Append body_log/has_body lines and return ``--form-string``/``-F`` curl args."""
        args: list[str] = []
        out.append('    body_log="(multipart)"')
        for idx, part in enumerate(body.parts):
            if isinstance(part, MultipartField):
                args.append(self._emit_multipart_field(part, out))
            else:
                args.append(self._emit_multipart_file(step, function_name, idx, part, out))
        out.append("    has_body=1")
        return args

    def _emit_multipart_field(self, part: MultipartField, out: list[str]) -> str:
        """Append body_log line and return the ``--form-string`` curl arg."""
        name_e = self._ph.expand(part.name)
        value_e = self._ph.expand(part.value)
        self._validate_no_tabs_newlines(name_e, f"multipart field name {part.name!r}")
        self._validate_no_tabs_newlines(value_e, f"multipart field value {part.value!r}")
        out.append(f'    body_log="${{body_log}}\\n  {name_e} = {value_e}"')
        return f'--form-string {dq_preserve_expansion(f"{name_e}={value_e}")}'

    def _emit_multipart_file(
        self,
        step: HttpStep,
        function_name: str,
        idx: int,
        part: MultipartFile,
        out: list[str],
    ) -> str:
        """Append existence check/body_log lines and return the ``-F`` curl arg."""
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
        # Both path and filename are wrapped in `\"` (a single backslash + double
        # quote) so that, after the quoted heredoc preserves them literally and
        # http_step's `eval` re-parses the line, the arg curl receives is
        # `name=@"path";filename="name";type=mime` — the same form the old array
        # approach produced. Using more backslashes here would leave literal
        # backslashes inside the curl `-F` argument.
        f_arg = f'{name_e}=@\\"{path_inner}\\"'
        if filename_e:
            f_arg += f';filename=\\"{filename_e}\\"'
            if type_e:
                f_arg += f';type={type_e}'
        elif type_e:
            f_arg += f';type={type_e}'

        out.append(f'    file_size=$(($(wc -c < "{path_inner}")))')
        # Append to body_log so http_step prints it inside the request-body
        # section (after the ==> banner), not before the banner.
        file_log_entry = f"  {name_e} = @{path_inner}"
        if filename_e:
            file_log_entry += f"; filename={filename_e}"
        if type_e:
            file_log_entry += f"; type={type_e}"
        file_log_entry += f"; bytes=${{file_size}}"
        out.append(f'    body_log="${{body_log}}\\n{file_log_entry}"')

        return f'-F "{f_arg}"'

    def _emit_headers(self, step: HttpStep, out: list[str]) -> list[str]:
        """Return inline ``-H`` curl args and emit ``headers_text`` / ``h_args`` if needed.

        ``headers_text`` is only emitted when the step captures a
        ``request.header.*`` value (the capture helper reads request headers
        from that text variable). When it is emitted we also build an indexed
        array ``h_args`` from ``headers_text`` so the ``curl_command`` string
        references ``"${h_args[@]}"``. After ``eval``, each element is passed
        as a properly quoted argument to ``curl``, avoiding word-splitting
        issues with header values that contain spaces.
        For all other steps the headers live solely in the ``curl_command``
        string, avoiding duplication.
        """
        header_lines = [self._ph.expand(f"{k}: {v}") for k, v in step.headers.items()]
        if isinstance(step.body, FormBody) and not self._has_header(step.headers, "Content-Type"):
            header_lines.append("Content-Type: application/x-www-form-urlencoded")
        if isinstance(step.body, FileBody) and not self._has_header(step.headers, "Content-Type"):
            header_lines.append("Content-Type: application/octet-stream")

        needs_headers_text = any(
            source.startswith("request.header.") for source in step.capture.values()
        )
        if needs_headers_text and header_lines:
            out.append("    headers_text=$(cat << EOT")
            out.extend(header_lines)
            out.append("EOT")
            out.append(")")
            out.append("    declare -a h_args=()")
            out.append('    while IFS= read -r line; do')
            out.append('        [[ -n "$line" ]] && h_args+=("-H" "$line")')
            out.append('    done <<< "${headers_text}"')

        if needs_headers_text:
            return ['"${h_args[@]}"'] if header_lines else []
        return [f'-H {dq_preserve_expansion(h)}' for h in header_lines]

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
