"""Template expansion engine for ${...} placeholders."""

from __future__ import annotations

import re
import uuid
from typing import Any

# Path segments inside ${...} may contain letters, digits, underscores and
# hyphens; dots act as the path separator.
PATTERN = re.compile(r"\$(?:\$|\{([\w.\-]+)\})")

# Matches ``${repeat.<name>}`` references, used by the workflow runner to
# decide which repeat variables must be supplied via ``--repeat-vars``.
REPEAT_PATTERN = re.compile(r"\$\{repeat\.([\w\-]+)\}")


class TemplateError(KeyError):
    """Raised when a referenced template variable is not found."""


def _lookup(store: dict, parts: list[str]) -> Any:
    if parts == ["random", "UUID"]:
        return uuid.uuid4()
    if parts == ["random", "UUID_HEX"]:
        return uuid.uuid4().hex
    if len(parts) == 1 and parts[0] in store.get("vars", {}):
        return store["vars"][parts[0]]
    cur: Any = store
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            raise TemplateError(".".join(parts))
        cur = cur[p]
    return cur


def render(text: str, store: dict) -> str:
    """Render ``${path.to.value}`` references in ``text`` using ``store``.

    ``$$`` is treated as a literal ``$``.
    Unknown references raise :class:`TemplateError`.
    """
    def repl(m: re.Match) -> str:
        if m.group(0) == "$$":
            return "$"
        path = m.group(1)
        return str(_lookup(store, path.split(".")))
    return PATTERN.sub(repl, text)


def render_mapping(mapping: dict[str, str], store: dict) -> dict[str, str]:
    """Render every value in a string-to-string mapping."""
    return {k: render(v, store) for k, v in mapping.items()}


def find_repeat_names(text: str | None) -> set[str]:
    """Return the set of ``${repeat.<name>}`` names referenced in ``text``."""
    if not text:
        return set()
    return set(REPEAT_PATTERN.findall(text))
