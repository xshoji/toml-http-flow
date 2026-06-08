from __future__ import annotations

import re

_UNTIL_OPS = [
    (re.compile(r"=="), "=="),
    (re.compile(r"!="), "!="),
    (re.compile(r"\s+in\s+"), "in"),
    (re.compile(r"~"), "~"),
]

def split_until_condition(condition: str) -> tuple[str, str, str]:
    """Split an until condition into unrendered lhs, operator, and rhs."""
    best: tuple[int, int, str] | None = None
    for pat, op in _UNTIL_OPS:
        m = pat.search(condition)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.end(), op)
    if best is None:
        raise ValueError(
            f"until condition: no operator (==, !=, ~, in) found in {condition!r}"
        )
    start, end, op = best
    return condition[:start], op, condition[end:]


