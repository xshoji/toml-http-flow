"""Condition evaluator for the ``until`` (polling) feature.

Supports a small set of comparison operators between two
template-expanded strings:

* ``==``  string equality
* ``!=``  string inequality
* ``~``   regex match (RHS in ``/pattern/[flags]`` form)
* ``in``  membership in a ``[A, B, C]`` list

See ``docs/design.md`` §4.8 for the full specification.
"""

from __future__ import annotations

import re
from typing import Any

from .template import render


# Operator detection patterns, longest/most-specific first so e.g. ``==`` is
# preferred over ``=``. ``in`` requires surrounding whitespace to avoid
# false matches inside template paths.
_OPERATORS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"==") , "=="),
    (re.compile(r"!=") , "!="),
    (re.compile(r"\s+in\s+"), "in"),
    (re.compile(r"~")  , "~"),
]

_REGEX_RHS = re.compile(r"^/(.*)/([a-zA-Z]*)$")
_LIST_RHS = re.compile(r"^\[(.*)\]$")


def _find_operator(text: str) -> tuple[int, int, str]:
    """Return ``(start, end, op)`` for the left-most operator in ``text``."""
    best: tuple[int, int, str] | None = None
    for pattern, op in _OPERATORS:
        m = pattern.search(text)
        if m is None:
            continue
        if best is None or m.start() < best[0]:
            best = (m.start(), m.end(), op)
    if best is None:
        raise ValueError(
            f"until condition: no operator (==, !=, ~, in) found in {text!r}"
        )
    return best


def _regex_flags(spec: str) -> int:
    flags = 0
    for ch in spec:
        if ch == "i":
            flags |= re.IGNORECASE
        elif ch == "m":
            flags |= re.MULTILINE
        elif ch == "s":
            flags |= re.DOTALL
        else:
            raise ValueError(f"until condition: unknown regex flag {ch!r}")
    return flags


def evaluate(condition: str, store: dict[str, Any]) -> bool:
    """Render ``condition`` against ``store`` and evaluate it as a boolean."""
    start, end, op = _find_operator(condition)
    lhs_raw = condition[:start]
    rhs_raw = condition[end:]

    lhs = render(lhs_raw, store).strip()
    rhs = render(rhs_raw, store).strip()

    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    if op == "~":
        m = _REGEX_RHS.match(rhs)
        if m is None:
            raise ValueError(
                f"until condition: '~' RHS must be /pattern/[flags], got {rhs!r}"
            )
        pattern, flag_spec = m.group(1), m.group(2)
        return re.search(pattern, lhs, _regex_flags(flag_spec)) is not None
    if op == "in":
        m = _LIST_RHS.match(rhs)
        if m is None:
            raise ValueError(
                f"until condition: 'in' RHS must be [A, B, C], got {rhs!r}"
            )
        items = [x.strip() for x in m.group(1).split(",") if x.strip() != ""]
        return lhs in items
    raise AssertionError(f"unreachable: unknown operator {op!r}")
