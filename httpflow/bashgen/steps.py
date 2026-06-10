from __future__ import annotations

from httpflow.model import FileBody, FormBody, HttpStep, MultipartBody, MultipartField, MultipartFile, SleepStep, TextBody

from .capture import capture_rows
from .conditions import split_until_condition
from .placeholders import PlaceholderRenderer
from .shell import dq_literal, sq



class StepEmitter:
    """Emit bash functions for workflow steps."""

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

    def _body_form_rows(self, fields: dict[str, str]) -> list[str]:
        """Emit body_form rows as tab-separated key/value strings."""
        rows: list[str] = []
        for k, v in fields.items():
            self._validate_no_tabs_newlines(k, f"body_form key {k!r}")
            self._validate_no_tabs_newlines(v, f"body_form value {v!r}")
            rows.append(f"{self._ph.expand(k)}\t{self._ph.expand(v)}")
        return rows

    def _validate_no_tabs_newlines(self, value: str, context: str) -> None:
        """Raise ValueError when *value* contains tab, newline, or double quote."""
        if "\t" in value or "\n" in value or '"' in value:
            raise ValueError(f"{context} must not contain tabs, newlines, or double quotes: {value!r}")

    def _multipart_rows(self, parts: list[MultipartField | MultipartFile]) -> list[str]:
        """Emit multipart rows as tab-separated internal strings."""
        rows: list[str] = []
        for p in parts:
            if isinstance(p, MultipartField):
                kind = "field"
                name = self._ph.expand(p.name)
                value = self._ph.expand(p.value)
                path = ""
                filename = ""
                content_type = ""
            else:
                kind = "file"
                name = self._ph.expand(p.name)
                value_or_path = self._ph.expand(p.path)
                filename = self._ph.expand(p.filename) if p.filename else ""
                content_type = self._ph.expand(p.content_type)
                path = value_or_path
                value = ""

            # Validate no tab/newline in any field
            if kind == "field":
                self._validate_no_tabs_newlines(name, f"multipart field name {p.name!r}")
                self._validate_no_tabs_newlines(value, f"multipart field value {p.value!r}")
            else:
                self._validate_no_tabs_newlines(name, f"multipart file name {p.name!r}")
                self._validate_no_tabs_newlines(path, f"multipart file path {p.path!r}")
                self._validate_no_tabs_newlines(filename, f"multipart file filename {filename!r}")
                self._validate_no_tabs_newlines(content_type, f"multipart file type {content_type!r}")

            # kind<TAB>name<TAB>value_or_path<TAB>filename<TAB>content_type
            if kind == "field":
                rows.append("\t".join([kind, name, value, "", ""]))
            else:
                rows.append("\t".join([kind, name, path, filename, content_type]))
        return rows

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
            f'    local decode_file',
            f'    decode_file=$(mktemp "$HF_TMPDIR/hf_embed.XXXXXX") || return $?',
            f"    printf '%s' \"${{__HF_EMBED_{var_name}}}\" | _hf_b64decode > \"$decode_file\"",
        ]

    def _embed_var_name_file_body(self, function_name: str) -> str:
        """Return the embedded var name for a body_file step."""
        return f"{function_name}_body"

    def _embed_var_name_mp(self, function_name: str, idx: int) -> str:
        """Return the embedded var name for a multipart file part."""
        return f"{function_name}_mp{idx}"

    def emit_http(self, step: HttpStep, function_name: str) -> str:
        """Emit an HTTP step function."""

        # Validate multipart Content-Type before anything else (fail fast).
        if isinstance(step.body, MultipartBody) and self._has_header(step.headers, "Content-Type"):
            raise ValueError(
                f"body_multipart step {step.name!r}: Content-Type is set automatically by curl; "
                f"remove the user-specified Content-Type header"
            )

        out: list[str] = [
            f"{function_name}() {{",
            f'    local url={self._ph.expr(step.url)}',
            "    local body=",
            "    local body_form_text=",
            "    local body_kind=",
            "    local headers_text=",
            "    local captures_text=",
        ]

        if isinstance(step.body, TextBody):
            body_kind = "text"
            out.append("    body=$(cat << EOT")
            out.append(self._ph.expand(step.body.text))
            out.append("EOT")
            out.append(")")
            out.append('    body="${body}$(printf "\\n")"')
        elif isinstance(step.body, FormBody):
            body_kind = "form"
            out.append("    body_form_text=$(cat << EOT")
            out.extend(self._body_form_rows(step.body.fields))
            out.append("EOT")
            out.append(")")
        elif isinstance(step.body, FileBody):
            body_kind = "file"
            embed_var = self._embed_var_name_file_body(function_name)
            decode = self._emit_decode_file(embed_var)
            out.extend(decode)
            if decode:
                out.append(f"    body=\"$decode_file\"")
            else:
                out.append(f"    body={self._ph.expr(step.body.path)}")
        elif isinstance(step.body, MultipartBody):
            body_kind = "multipart"
            multipart_rows = self._multipart_rows(step.body.parts)
            for idx, part in enumerate(step.body.parts):
                if isinstance(part, MultipartFile):
                    embed_var = self._embed_var_name_mp(function_name, idx)
                    decode = self._emit_decode_file(embed_var)
                    out.extend(decode)
                    if decode:
                        new_path = f"$decode_file"
                        # Replace the path in the TSV row for this part
                        for row_idx in range(len(multipart_rows)):
                            parts = multipart_rows[row_idx].split("\t")
                            if len(parts) == 5 and parts[0] == "file" and parts[1] == self._ph.expand(part.name):
                                parts[2] = new_path
                                multipart_rows[row_idx] = "\t".join(parts)
                                break
            out.append("    body_form_text=$(cat << EOT")
            out.extend(multipart_rows)
            out.append("EOT")
            out.append(")")
        else:
            body_kind = "none"
        out.append(f"    body_kind={sq(body_kind)}")

        header_lines = [self._ph.expand(f"{k}: {v}") for k, v in step.headers.items()]
        if isinstance(step.body, FormBody) and not self._has_header(step.headers, "Content-Type"):
            header_lines.append("Content-Type: application/x-www-form-urlencoded")
        if isinstance(step.body, FileBody) and not self._has_header(step.headers, "Content-Type"):
            header_lines.append("Content-Type: application/octet-stream")
        if header_lines:
            out.append("    headers_text=$(cat << EOT")
            out.extend(header_lines)
            out.append("EOT")
            out.append(")")

        capture_lines = capture_rows(step)
        if capture_lines:
            out.append("    captures_text=$(cat <<'EOT'")
            out.extend(capture_lines)
            out.append("EOT")
            out.append(")")

        out.append(
            f"    http_step {sq(step.name)} {sq(step.method.upper())} \"$url\" "
            f"\"$body_kind\" \"$body\" \"$body_form_text\" \"$headers_text\" \"$captures_text\" "
            f"{sq(step.description or '')}"
        )
        out.append("}")
        return "\n".join(out)

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
            f'        if until_eval "$until_lhs" {sq(op)} "$until_rhs"; then',
            '            echo "    * until satisfied on attempt $attempt"',
            "            return 0",
            "        fi",
            '        if [ "$attempt" -lt "$max_attempts" ]; then',
            '            echo "    * until not satisfied (attempt $attempt/$max_attempts), retrying in ${interval}s"',
            '            sleep "$interval"',
            "        fi",
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
