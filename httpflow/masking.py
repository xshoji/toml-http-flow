"""Mask sensitive header / body / capture values for log output.

Masking only affects what is *printed* to the log; it never changes the
bytes sent over the wire nor captured values stored in ``store["vars"]``.

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

from .embedded_runtime import mask, mask_url, mask_value

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

