"""Core template rendering helpers."""

from __future__ import annotations

import os
import re
import datetime
import uuid
from typing import Any

# Path segments inside ${...} may contain letters, digits, underscores and
# hyphens; dots act as the path separator.
PATTERN = re.compile(r"\$(?:\$|\{([\w.\-]+)\})")


class TemplateError(KeyError):
    """Raised when a referenced template variable is not found."""


def _lookup(store: dict, parts: list[str]) -> Any:
    if len(parts) == 2 and parts[0] == "env":
        try:
            return os.environ[parts[1]]
        except KeyError as exc:
            raise TemplateError(".".join(parts)) from exc
    if parts == ["random", "UUID"]:
        return uuid.uuid4()
    if parts == ["random", "UUID_HEX"]:
        return uuid.uuid4().hex
    if parts == ["time", "DATE_ISO"]:
        return datetime.datetime.now().astimezone().isoformat(timespec="microseconds")
    if parts == ["time", "DATE_YMD"]:
        return datetime.datetime.now().astimezone().strftime("%Y%m%d")
    if parts == ["time", "DATE_YMDHMS"]:
        return datetime.datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
    if len(parts) == 2 and parts[0] == "var":
        try:
            return store["vars"][parts[1]]
        except KeyError as exc:
            raise TemplateError(".".join(parts)) from exc
    if len(parts) == 1 and parts[0] in store.get("vars", {}):
        return store["vars"][parts[0]]
    cur: Any = store
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            raise TemplateError(".".join(parts))
        cur = cur[p]
    return cur


def render(text: str, store: dict) -> str:
    """Render ``${path.to.value}`` references in ``text`` using ``store``."""
    def repl(m: re.Match) -> str:
        if m.group(0) == "$$":
            return "$"
        path = m.group(1)
        return str(_lookup(store, path.split(".")))
    return PATTERN.sub(repl, text)


def render_mapping(mapping: dict[str, str], store: dict) -> dict[str, str]:
    """Render every key and value in a string-to-string mapping."""
    return {render(k, store): render(v, store) for k, v in mapping.items()}
