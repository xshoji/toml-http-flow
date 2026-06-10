from __future__ import annotations

from httpflow.model import FileBody, FormBody, HttpStep, MultipartBody, MultipartField, MultipartFile, SleepStep, TextBody

from .capture import capture_rows
from .conditions import split_until_condition
from .placeholders import PlaceholderRenderer
from .shell import dq_literal, sq

_BODY_KIND_NONE = "none"
_BODY_KIND_TEXT = "text"
_BODY_KIND_FORM = "form"
_BODY_KIND_FILE = "file"
_BODY_KIND_MULTIPART = "multipart"


class StepEmitter:
    """Emit bash functions for workflow steps."""

    def __init__(self, placeholders: PlaceholderRenderer) -> None:
        """Create a step emitter."""
        self._placeholders = placeholders

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
            if "\t" in k or "\n" in k or "\t" in v or "\n" in v:
                raise ValueError("body_form keys and values must not contain tabs or newlines")
            rows.append(f"{self._ph.expand(k)}\t{self._ph.expand(v)}")
        return rows

    def _validate_no_tabs_newlines(self, value: str, context: str) -> None:
        """Raise ValueError when *value* contains tab or newline."""
        if "\t" in value or "\n" in value:
            raise ValueError(f"{context} must not contain tabs or newlines: {value!r}")

    def _multipart_rows(self, parts: list) -> list[str]:
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
                content_type = p.content_type
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

    def emit_http(self, step: HttpStep, function_name: str) -> str:
        """Emit an HTTP step function."""
        out: list[str] = [
            f"{function_name}() {{",
            f'    local url={self._ph.expr(step.url)}',
            "    local body=",
            "    local body_form_text=",
            "    local body_kind=",
            "    local headers_text=",
            "    local captures_text=",
        ]

        has_body = False
        body_kind = _BODY_KIND_NONE
        if isinstance(step.body, TextBody):
            has_body = True
            body_kind = _BODY_KIND_TEXT
            out.append("    body=$(cat << EOT")
            out.append(self._ph.expand(step.body.text))
            out.append("EOT")
            out.append(")")
            out.append('    body="${body}$(printf "\\n")"')
        elif isinstance(step.body, FormBody):
            has_body = True
            body_kind = _BODY_KIND_FORM
            out.append("    body_form_text=$(cat << EOT")
            out.extend(self._body_form_rows(step.body.fields))
            out.append("EOT")
            out.append(")")
        elif isinstance(step.body, FileBody):
            has_body = True
            body_kind = _BODY_KIND_FILE
            body_file_path = self._ph.expand(step.body.path)
            out.append(f"    body={sq(body_file_path)}")
        elif isinstance(step.body, MultipartBody):
            has_body = True
            body_kind = _BODY_KIND_MULTIPART
            multipart_rows = self._multipart_rows(step.body.parts)
            out.append("    body_form_text=$(cat <<'EOT'")
            out.extend(multipart_rows)
            out.append("EOT")
            out.append(")")
        out.append(f"    body_kind={sq(body_kind)}")

        header_lines = [self._ph.expand(f"{k}: {v}") for k, v in step.headers.items()]
        if isinstance(step.body, FormBody) and not self._has_header(step.headers, "Content-Type"):
            header_lines.append("Content-Type: application/x-www-form-urlencoded")
        if isinstance(step.body, FileBody) and not self._has_header(step.headers, "Content-Type"):
            header_lines.append("Content-Type: application/octet-stream")
        if isinstance(step.body, MultipartBody) and self._has_header(step.headers, "Content-Type"):
            raise ValueError(
                f"body_multipart step {step.name!r}: Content-Type is set automatically by curl; "
                f"remove the user-specified Content-Type header"
            )
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
            f"{1 if has_body else 0} \"$body_kind\" \"$body\" \"$body_form_text\" \"$headers_text\" \"$captures_text\" "
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
            f'    printf "==> %s [%s] SLEEP %s\\n" "$(now)" {sq(step.name)} "$seconds"',
        ]
        if step.description:
            for dl in step.description.splitlines():
                out.append(f'    printf "# %s\\n" {sq(dl)}')
        out.append('    sleep "$seconds"')
        out.append(f'    printf "<== %s [%s] done\\n" "$(now)" {sq(step.name)}')
        out.append("}")
        return "\n".join(out)
