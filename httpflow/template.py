"""Template expansion engine for ${...} placeholders."""

from __future__ import annotations

import re

from .runtime.core import PATTERN, TemplateError, render, render_mapping

# Matches ``${repeat.<name>}`` references.
REPEAT_PATTERN = re.compile(r"\$\{repeat\.([\w\-]+)\}")
VAR_PATTERN = re.compile(r"\$\{var\.([\w\-]+)\}")


def find_repeat_names(text: str | None) -> set[str]:
    """Return the set of ``${repeat.<name>}`` names referenced in ``text``."""
    if not text:
        return set()
    return set(REPEAT_PATTERN.findall(text))


def find_var_names(text: str | None) -> set[str]:
    """Return the set of ``${var.<name>}`` names referenced in ``text``."""
    if not text:
        return set()
    return set(VAR_PATTERN.findall(text))
