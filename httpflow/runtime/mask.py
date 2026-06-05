"""Masking helpers for log output."""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any

_MASK_PLACEHOLDER = "***"
_MASK_DEFAULTS = frozenset({
    "Authorization",
    "Proxy-Authorization",
    "Cookie",
    "Set-Cookie",
    "X-Api-Key",
    "X-Auth-Token",
    "X-Access-Token",
    "X-Csrf-Token",
    "X-Xsrf-Token",
    "X-Session-Token",
    "X-Session-Id",
    "X-Secret-Key",
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
    "private_key",
    "auth",
    "session",
    "session_id",
    "credit_card",
    "card_number",
    "cvv",
    "cvc",
    "pin",
    "ssn",
})


def _mask_defaults_normalized() -> frozenset[str]:
    """Return the normalized forms of the built-in mask defaults."""
    return frozenset(
        _mask_norm(name)
        for name in _MASK_DEFAULTS
    )


def _mask_norm(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "").replace(" ", "")


def _mask_targets() -> set[str]:
    base = set(_mask_defaults_normalized())
    raw = os.environ.get("HTTPFLOW_MASK_EXTRA", "")
    base |= {_mask_norm(item) for item in raw.split(",") if item.strip()}
    return base


def _mask_obj(obj: Any, targets: set[str]) -> Any:
    if isinstance(obj, dict):
        return {
            k: (_MASK_PLACEHOLDER if isinstance(k, str) and _mask_norm(k) in targets
                else _mask_obj(v, targets))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_mask_obj(item, targets) for item in obj]
    return obj


def mask(text: str, disabled: bool = False) -> str:
    """Best-effort masking for a raw string."""
    if disabled or not text:
        return text
    targets = _mask_targets()
    try:
        return json.dumps(_mask_obj(json.loads(text), targets), ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        pass
    if "=" in text and "\n" not in text and " " not in text:
        try:
            pairs = urllib.parse.parse_qsl(text, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            return text
        masked = [(k, _MASK_PLACEHOLDER if _mask_norm(k) in targets else v) for k, v in pairs]
        return urllib.parse.urlencode(masked, safe="*")
    return text


def mask_url(url: str, disabled: bool = False) -> str:
    """Replace query-parameter values for sensitive keys in ``url``."""
    if disabled:
        return url
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return url
    targets = _mask_targets()
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    masked = [(k, _MASK_PLACEHOLDER if _mask_norm(k) in targets else v) for k, v in pairs]
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(masked, safe="*")))


def mask_value(name: str, value: Any, disabled: bool = False) -> Any:
    """Return ``value`` masked when ``name`` matches a sensitive key."""
    if disabled:
        return value
    if _mask_norm(name) in _mask_targets():
        return _MASK_PLACEHOLDER
    return value
