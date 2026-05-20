"""TOML loader and dataclass definitions for the workflow config."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class UntilConfig:
    """Polling configuration for a single request (see design §4.8)."""

    condition: str
    interval: float = 1.0
    max_attempts: int = 10


@dataclass
class RequestConfig:
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
    requests: list[RequestConfig]


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


def _build_request(d: dict[str, Any]) -> RequestConfig:
    for required in ("name", "method", "url"):
        if required not in d:
            raise ValueError(f"missing required field {required!r} in request: {d!r}")

    if "body" in d and "body_form" in d:
        raise ValueError(
            f"request {d.get('name')!r}: 'body' and 'body_form' are mutually exclusive"
        )

    method = str(d["method"]).upper()

    description = d.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError(
            f"request {d['name']!r}: 'description' must be a string"
        )

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
        try:
            float(d["url"])
        except ValueError as exc:
            raise ValueError(
                f"request {d['name']!r}: 'SLEEP' step requires a numeric 'url' "
                f"(seconds), got: {d['url']!r}"
            ) from exc
        return RequestConfig(
            name=str(d["name"]),
            method=method,
            url=str(d["url"]),
            description=description,
        )

    headers = parse_kv_list(d.get("headers", []), ":")
    body = d.get("body")
    body_form = parse_kv_list(d["body_form"], "=") if "body_form" in d else None
    capture = parse_kv_list(d.get("capture", []), "=")
    until = _build_until(d["until"], d["name"]) if "until" in d else None

    if body is not None and not isinstance(body, str):
        raise ValueError(f"request {d['name']!r}: 'body' must be a string")

    return RequestConfig(
        name=str(d["name"]),
        method=method,
        url=str(d["url"]),
        description=description,
        headers=headers,
        body=body,
        body_form=body_form,
        capture=capture,
        until=until,
    )


def _build_until(raw: Any, request_name: str) -> UntilConfig:
    """Parse the ``until = [...]`` array into a :class:`UntilConfig`."""
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

    return UntilConfig(
        condition=parsed["condition"],
        interval=interval,
        max_attempts=max_attempts,
    )


def load(path: str) -> WorkflowConfig:
    """Load a workflow TOML file and return a WorkflowConfig."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    requests_raw = raw.get("requests", [])
    if not isinstance(requests_raw, list):
        raise ValueError("top-level 'requests' must be an array of tables")
    return WorkflowConfig(requests=[_build_request(r) for r in requests_raw])
