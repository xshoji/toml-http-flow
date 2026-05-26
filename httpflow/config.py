"""TOML loader that returns a normalized WorkflowSpec."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from typing import Any

from .model import FormBody, HttpStep, SleepStep, TextBody, UntilSpec, WorkflowSpec


# ------------------------------------------------------------------ Legacy dataclasses (backward compatible)

@dataclass
class UntilConfig:
    """Polling configuration for a single request (see design §4.8)."""

    condition: str
    interval: float = 1.0
    max_attempts: int = 10


@dataclass
class RequestConfig:
    """Legacy intermediate representation.  Kept for test compatibility."""

    name: str
    method: str
    url: str
    description: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    body_form: dict[str, str] | None = None
    capture: dict[str, str] = field(default_factory=dict)
    until: UntilConfig | None = None


@dataclass
class WorkflowConfig:
    """Legacy intermediate representation.  Kept for test compatibility."""

    requests: list[RequestConfig]


# ------------------------------------------------------------------ internal parsing helpers


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

            # --- SLEEP step validation ---
    if method == "SLEEP":
        if (
            d.get("headers")
            or d.get("body")
            or d.get("body_form")
            or d.get("capture")
            or d.get("until")
        ):
            raise ValueError(
                f"request {d['name']!r}: 'SLEEP' step must not specify "
                f"headers, body, body_form, capture, or until"
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
    if body is not None and body_form:
        raise ValueError(
            f"request {d.get('name')!r}: 'body' and 'body_form' are mutually exclusive"
        )

    if body is not None and not isinstance(body, str):
        raise ValueError(f"request {d['name']!r}: 'body' must be a string")

    if not isinstance(body_form, list):
        body_form = [body_form]

    return _IntermediateRequest(
        name=str(d["name"]),
        method=method,
        url=str(d["url"]),
        description=description,
        headers=d.get("headers", []),
        body=body,
        body_form=body_form,
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

    body: TextBody | FormBody | None = None
    if inter.body is not None:
        body = TextBody(text=inter.body)
    elif inter.body_form:
        body = FormBody(fields=parse_kv_list(inter.body_form, "="))

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


def to_model(cfg: WorkflowConfig) -> WorkflowSpec:
    """Convert a legacy :class:`WorkflowConfig` into a :class:`WorkflowSpec`."""
    steps: list[HttpStep | SleepStep] = []
    for req in cfg.requests:
        if req.method == "SLEEP":
            steps.append(
                SleepStep(
                    name=req.name,
                    seconds=req.url,
                    description=req.description,
                )
            )
        else:
            body: TextBody | FormBody | None = None
            if req.body is not None:
                body = TextBody(text=req.body)
            elif req.body_form is not None:
                body = FormBody(fields=req.body_form)
            until = None
            if req.until is not None:
                until = UntilSpec(
                    condition=req.until.condition,
                    interval=req.until.interval,
                    max_attempts=req.until.max_attempts,
                )
            steps.append(
                HttpStep(
                    name=req.name,
                    method=req.method,
                    url=req.url,
                    description=req.description,
                    headers=req.headers,
                    body=body,
                    capture=req.capture,
                    until=until,
                )
            )
    return WorkflowSpec(steps=steps)
