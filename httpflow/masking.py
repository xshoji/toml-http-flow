"""Mask sensitive header / body / capture values for log output.

Masking only affects what is *printed* to the log; it never changes the
bytes sent over the wire nor values stored in ``store["steps"]``.

Configurable via environment variables (all optional):

- ``HTTPFLOW_MASK_DISABLED``        — ``1``/``true``/``yes``/``on`` to disable
- ``HTTPFLOW_MASK_PLACEHOLDER``     — replacement string (default ``"***"``)
- ``HTTPFLOW_MASK_HEADERS``         — comma-separated; **replaces** defaults
- ``HTTPFLOW_MASK_HEADERS_EXTRA``   — comma-separated; **added to** defaults
- ``HTTPFLOW_MASK_BODY_KEYS``       — comma-separated; **replaces** defaults
- ``HTTPFLOW_MASK_BODY_KEYS_EXTRA`` — comma-separated; **added to** defaults

Key comparison normalizes by lower-casing and stripping ``_``/``-``/space,
so ``apiKey`` / ``API-KEY`` / ``api_key`` / ``apikey`` all collapse to the
same canonical form.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any


DEFAULT_SENSITIVE_HEADERS: frozenset[str] = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "x-access-token",
    "x-csrf-token",
    "x-xsrf-token",
    "x-session-token",
    "x-session-id",
    "x-secret-key",
})

DEFAULT_SENSITIVE_BODY_KEYS: frozenset[str] = frozenset({
    "password",
    "passwd",
    "pwd",
    "secret",
    "client_secret",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "auth_token",
    "session_token",
    "api_key",
    "apikey",
    "private_key",
    "authorization",
    "auth",
    "session",
    "session_id",
    "cookie",
    "credit_card",
    "card_number",
    "cvv",
    "cvc",
    "pin",
    "ssn",
})


def _norm(name: str) -> str:
    """Canonicalize a key name for case-/separator-insensitive matching."""
    return name.lower().replace("_", "").replace("-", "").replace(" ", "")


def _split_env(name: str) -> set[str]:
    """Read ``$name`` as a comma-separated list of normalized key names."""
    raw = os.environ.get(name, "")
    return {_norm(item) for item in raw.split(",") if item.strip()}


def _bool_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def is_disabled() -> bool:
    """Return True when masking is globally disabled via env var."""
    return _bool_env("HTTPFLOW_MASK_DISABLED")


def placeholder() -> str:
    """Return the replacement string used in place of masked values."""
    return os.environ.get("HTTPFLOW_MASK_PLACEHOLDER") or "***"


def _resolve(default: frozenset[str], override_env: str, extra_env: str) -> set[str]:
    override = _split_env(override_env)
    base = override if override else {_norm(x) for x in default}
    base |= _split_env(extra_env)
    return base


def sensitive_headers() -> set[str]:
    """Return the active set of header names (normalized) to mask."""
    return _resolve(
        DEFAULT_SENSITIVE_HEADERS,
        "HTTPFLOW_MASK_HEADERS",
        "HTTPFLOW_MASK_HEADERS_EXTRA",
    )


def sensitive_body_keys() -> set[str]:
    """Return the active set of body/query/capture key names (normalized) to mask."""
    return _resolve(
        DEFAULT_SENSITIVE_BODY_KEYS,
        "HTTPFLOW_MASK_BODY_KEYS",
        "HTTPFLOW_MASK_BODY_KEYS_EXTRA",
    )


def mask_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with sensitive header values replaced."""
    if is_disabled():
        return dict(headers)
    targets = sensitive_headers()
    mark = placeholder()
    return {k: (mark if _norm(k) in targets else v) for k, v in headers.items()}


def _mask_obj(obj: Any, targets: set[str], mark: str) -> Any:
    """Recursively mask values whose dict key matches ``targets``."""
    if isinstance(obj, dict):
        return {
            k: (mark if isinstance(k, str) and _norm(k) in targets
                else _mask_obj(v, targets, mark))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_mask_obj(item, targets, mark) for item in obj]
    return obj


def mask_form(form: dict[str, str]) -> dict[str, str]:
    """Return a copy of a form-data mapping with sensitive values replaced."""
    if is_disabled():
        return dict(form)
    targets = sensitive_body_keys()
    mark = placeholder()
    return {k: (mark if _norm(k) in targets else v) for k, v in form.items()}


def mask_url(url: str) -> str:
    """Replace query-parameter values for sensitive keys in ``url``."""
    if is_disabled():
        return url
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return url
    targets = sensitive_body_keys()
    mark = placeholder()
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    masked = [(k, mark if _norm(k) in targets else v) for k, v in pairs]
    new_query = urllib.parse.urlencode(masked)
    return urllib.parse.urlunsplit(parsed._replace(query=new_query))


def mask_capture_value(name: str, value: Any) -> Any:
    """Return ``value`` masked when ``name`` matches a sensitive key."""
    if is_disabled():
        return value
    if _norm(name) in sensitive_body_keys():
        return placeholder()
    return value


def mask_body_text(text: str) -> str:
    """Best-effort masking for a raw body string.

    Tries JSON first, then form-urlencoded; if neither, returns ``text``
    unchanged (plain text bodies are not auto-redacted).
    """
    if is_disabled() or not text:
        return text
    targets = sensitive_body_keys()
    mark = placeholder()
    # JSON?
    try:
        parsed = json.loads(text)
        return json.dumps(_mask_obj(parsed, targets, mark), ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        pass
    # form-urlencoded? (single-line, contains '=' and no whitespace/newlines)
    if "=" in text and "\n" not in text and " " not in text:
        try:
            pairs = urllib.parse.parse_qsl(
                text, keep_blank_values=True, strict_parsing=True
            )
        except ValueError:
            return text
        masked = [(k, mark if _norm(k) in targets else v) for k, v in pairs]
        return urllib.parse.urlencode(masked)
    return text
