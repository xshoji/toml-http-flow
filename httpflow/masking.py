"""Mask sensitive header / body / capture values for log output.

Masking only affects what is *printed* to the log; it never changes the
bytes sent over the wire nor values stored in ``store["steps"]``.

The only configurable environment variable (all optional):

- ``HTTPFLOW_MASK_EXTRA`` — comma-separated key names added to the default
  sensitive-key set.  Applies to headers, body, query, and capture alike.

Key comparison normalizes by lower-casing and stripping ``_``/``-``/space,
so ``apiKey`` / ``API-KEY`` / ``api_key`` / ``apikey`` all collapse to the
same canonical form.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any

_PLACEHOLDER = "***"

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


def _extra_set() -> set[str]:
    """Read ``HTTPFLOW_MASK_EXTRA`` as a comma-separated list of normalized keys."""
    raw = os.environ.get("HTTPFLOW_MASK_EXTRA", "")
    return {_norm(item) for item in raw.split(",") if item.strip()}


def _all_targets() -> set[str]:
    """Return the complete set of normalized key names to mask."""
    base = {_norm(x) for x in DEFAULT_SENSITIVE_HEADERS}
    base |= {_norm(x) for x in DEFAULT_SENSITIVE_BODY_KEYS}
    base |= _extra_set()
    return base


def _mask_obj(obj: Any, targets: set[str]) -> Any:
    """Recursively mask values whose dict key matches ``targets``."""
    if isinstance(obj, dict):
        return {
            k: (_PLACEHOLDER if isinstance(k, str) and _norm(k) in targets
                else _mask_obj(v, targets))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_mask_obj(item, targets) for item in obj]
    return obj


def mask(text: str, disabled: bool = False) -> str:
    """Best-effort masking for a raw string.

    Tries JSON first, then form-urlencoded; if neither, returns ``text``
    unchanged (plain text bodies are not auto-redacted).
    """
    if disabled or not text:
        return text
    targets = _all_targets()
    try:
        parsed = json.loads(text)
        return json.dumps(_mask_obj(parsed, targets), ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        pass
    if "=" in text and "\n" not in text and " " not in text:
        try:
            pairs = urllib.parse.parse_qsl(
                text, keep_blank_values=True, strict_parsing=True
            )
        except ValueError:
            return text
        masked = [(k, _PLACEHOLDER if _norm(k) in targets else v) for k, v in pairs]
        return urllib.parse.urlencode(masked)
    return text


def mask_url(url: str, disabled: bool = False) -> str:
    """Replace query-parameter values for sensitive keys in ``url``."""
    if disabled:
        return url
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return url
    targets = _all_targets()
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    masked = [(k, _PLACEHOLDER if _norm(k) in targets else v) for k, v in pairs]
    new_query = urllib.parse.urlencode(masked)
    return urllib.parse.urlunsplit(parsed._replace(query=new_query))


def mask_value(name: str, value: Any, disabled: bool = False) -> Any:
    """Return ``value`` masked when ``name`` matches a sensitive key."""
    if disabled:
        return value
    if _norm(name) in _all_targets():
        return _PLACEHOLDER
    return value
