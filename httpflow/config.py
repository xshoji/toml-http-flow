"""TOML loader that returns a normalized WorkflowSpec."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from typing import Any

from .model import FileBody, FormBody, HttpStep, MultipartBody, MultipartField, MultipartFile, MultipartPart, SleepStep, TextBody, UntilSpec, WorkflowSpec


SPECIAL_METHODS = {"SLEEP"}
_UNTIL_KEYS = {"condition", "interval", "max_attempts"}


def parse_kv_list(items: list[str], sep: str) -> dict[str, str]:
    """Parse a list of "Key<sep>Value" strings into a dict.

    Splits on the first occurrence of ``sep`` only, and trims whitespace
    around the key and value.
    """
    result: dict[str, str] = {}
    for raw in items:
        if not isinstance(raw, str):
            raise ValueError(f"expected string entry, got {type(raw).__name__}: {raw!r}")
        if sep not in raw:
            raise ValueError(f"invalid entry (missing {sep!r}): {raw!r}")
        k, v = raw.split(sep, 1)
        key = k.strip()
        val = v.strip()
        if not key:
            raise ValueError(f"empty key in entry: {raw!r}")
        result[key] = val
    return result


def parse_multipart_list(items: list[str]) -> list[MultipartPart]:
    """Parse multipart/form-data entries into ordered parts."""
    parts: list[MultipartPart] = []
    for raw in items:
        if not isinstance(raw, str):
            raise ValueError(f"expected string entry, got {type(raw).__name__}: {raw!r}")
        if "=" not in raw:
            raise ValueError(f"invalid multipart entry (missing '='): {raw!r}")
        key_raw, value_raw = raw.split("=", 1)
        name = key_raw.strip()
        value = value_raw.strip()
        if not name:
            raise ValueError(f"empty multipart field name in entry: {raw!r}")
        if value.startswith("@@"):
            parts.append(MultipartField(name=name, value=value[1:]))
            continue
        if not value.startswith("@"):
            parts.append(MultipartField(name=name, value=value))
            continue

        segments = [seg.strip() for seg in value[1:].split(";")]
        path = segments[0]
        if not path:
            raise ValueError(f"empty multipart file path in entry: {raw!r}")
        filename: str | None = None
        content_type = "application/octet-stream"
        for segment in segments[1:]:
            if not segment:
                continue
            if "=" not in segment:
                raise ValueError(f"invalid multipart file option in entry: {raw!r}")
            opt_key, opt_value = segment.split("=", 1)
            opt_key = opt_key.strip()
            opt_value = opt_value.strip()
            if opt_key == "filename":
                filename = opt_value
            elif opt_key == "type":
                content_type = opt_value
            else:
                raise ValueError(f"unknown multipart file option {opt_key!r} in entry: {raw!r}")
        parts.append(MultipartFile(name=name, path=path, filename=filename, content_type=content_type))
    return parts


# ------------------------------------------------------------------ internal intermediate


@dataclass
class _IntermediateRequest:
    """Temporary representation used while parsing raw TOML dicts."""

    name: str
    method: str
    url: str
    description: str | None = None
    headers: list[str] = field(default_factory=list)
    body: str | None = None
    body_form: list[str] = field(default_factory=list)
    body_file: str | None = None
    body_multipart: list[str] = field(default_factory=list)
    capture: list[str] = field(default_factory=list)
    until: list[str] | None = None


def _build_intermediate(d: dict[str, Any]) -> _IntermediateRequest:
    """Parse a raw TOML request dict into an intermediate representation."""
    for required in ("name", "method", "url"):
        if required not in d:
            raise ValueError(f"missing required field {required!r} in request: {d!r}")

    method = str(d["method"]).upper()

    description = d.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError(f"request {d['name']!r}: 'description' must be a string")

    if method == "SLEEP":
        if (
            d.get("headers")
            or d.get("body")
            or d.get("body_form")
            or d.get("body_file")
            or d.get("body_multipart")
            or d.get("capture")
            or d.get("until")
        ):
            raise ValueError(
                f"request {d['name']!r}: 'SLEEP' step must not specify "
                f"headers, body, body_form, body_file, body_multipart, capture, or until"
            )
        url_val = str(d["url"])
        # Defer numeric validation to runtime when the value contains a template.
        if "${" not in url_val:
            try:
                float(url_val)
            except ValueError as exc:
                raise ValueError(
                    f"request {d['name']!r}: 'SLEEP' step requires a numeric 'url' "
                    f"(seconds), got: {url_val!r}"
                ) from exc
        return _IntermediateRequest(
            name=str(d["name"]),
            method=method,
            url=url_val,
            description=description,
        )

    body = d.get("body")
    body_form = d.get("body_form", [])
    body_file = d.get("body_file")
    body_multipart = d.get("body_multipart", [])
    body_modes = [
        ("body", body is not None),
        ("body_form", bool(body_form)),
        ("body_file", body_file is not None),
        ("body_multipart", bool(body_multipart)),
    ]
    present = [name for name, enabled in body_modes if enabled]
    if len(present) > 1:
        raise ValueError(
            f"request {d.get('name')!r}: body fields are mutually exclusive: "
            f"{', '.join(present)}"
        )

    if body is not None and not isinstance(body, str):
        raise ValueError(f"request {d['name']!r}: 'body' must be a string")
    if body_file is not None and not isinstance(body_file, str):
        raise ValueError(f"request {d['name']!r}: 'body_file' must be a string")

    if not isinstance(body_form, list):
        body_form = [body_form]
    if not isinstance(body_multipart, list):
        body_multipart = [body_multipart]

    return _IntermediateRequest(
        name=str(d["name"]),
        method=method,
        url=str(d["url"]),
        description=description,
        headers=d.get("headers", []),
        body=body,
        body_form=body_form,
        body_file=body_file,
        body_multipart=body_multipart,
        capture=d.get("capture", []),
        until=d["until"] if "until" in d else None,
    )


def _build_until_spec(raw: Any, request_name: str) -> UntilSpec:
    """Parse the ``until = [...]`` array into an :class:`UntilSpec`."""
    if not isinstance(raw, list):
        raise ValueError(
            f"request {request_name!r}: 'until' must be an array of strings"
        )
    parsed = parse_kv_list(raw, "=")

    unknown = set(parsed) - _UNTIL_KEYS
    if unknown:
        raise ValueError(
            f"request {request_name!r}: unknown until keys: {sorted(unknown)}"
        )

    if "condition" not in parsed:
        raise ValueError(
            f"request {request_name!r}: 'until' requires a 'condition' entry"
        )

    interval = 1.0
    if "interval" in parsed:
        try:
            interval = float(parsed["interval"])
        except ValueError as exc:
            raise ValueError(
                f"request {request_name!r}: until.interval must be numeric, "
                f"got {parsed['interval']!r}"
            ) from exc
        if interval < 0:
            raise ValueError(
                f"request {request_name!r}: until.interval must be >= 0, "
                f"got {interval}"
            )

    max_attempts = 10
    if "max_attempts" in parsed:
        try:
            max_attempts = int(parsed["max_attempts"])
        except ValueError as exc:
            raise ValueError(
                f"request {request_name!r}: until.max_attempts must be an integer, "
                f"got {parsed['max_attempts']!r}"
            ) from exc
        if max_attempts < 1:
            raise ValueError(
                f"request {request_name!r}: until.max_attempts must be >= 1, "
                f"got {max_attempts}"
            )

    return UntilSpec(
        condition=parsed["condition"],
        interval=interval,
        max_attempts=max_attempts,
    )


def _intermediate_to_step(inter: _IntermediateRequest) -> HttpStep | SleepStep:
    """Convert an intermediate request into a model step."""
    if inter.method == "SLEEP":
        return SleepStep(
            name=inter.name,
            seconds=inter.url,
            description=inter.description,
        )

    body: TextBody | FormBody | FileBody | MultipartBody | None = None
    if inter.body is not None:
        body = TextBody(text=inter.body)
    elif inter.body_form:
        body = FormBody(fields=parse_kv_list(inter.body_form, "="))
    elif inter.body_file is not None:
        body = FileBody(path=inter.body_file)
    elif inter.body_multipart:
        body = MultipartBody(parts=parse_multipart_list(inter.body_multipart))

    until = None
    if inter.until is not None:
        until = _build_until_spec(inter.until, inter.name)

    return HttpStep(
        name=inter.name,
        method=inter.method,
        url=inter.url,
        description=inter.description,
        headers=parse_kv_list(inter.headers, ":"),
        body=body,
        capture=parse_kv_list(inter.capture, "="),
        until=until,
    )


# ------------------------------------------------------------------ public API


def load(path: str) -> WorkflowSpec:
    """Load a workflow TOML file and return a :class:`WorkflowSpec`."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    requests_raw = raw.get("requests", [])
    if not isinstance(requests_raw, list):
        raise ValueError("top-level 'requests' must be an array of tables")
    return WorkflowSpec(
        steps=[_intermediate_to_step(_build_intermediate(r)) for r in requests_raw]
    )
