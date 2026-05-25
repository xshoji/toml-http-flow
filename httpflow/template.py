"""Template expansion engine for ${...} placeholders."""

from __future__ import annotations

import re

from .runtime.core import PATTERN, TemplateError, render, render_mapping

# Matches ``${repeat.<name>}`` references, used by the workflow runner to
# decide which repeat variables must be supplied via ``--repeat-vars``.
REPEAT_PATTERN = re.compile(r"\$\{repeat\.([\w\-]+)\}")


def find_repeat_names(text: str | None) -> set[str]:
    """Return the set of ``${repeat.<name>}`` names referenced in ``text``."""
    if not text:
        return set()
    return set(REPEAT_PATTERN.findall(text))
