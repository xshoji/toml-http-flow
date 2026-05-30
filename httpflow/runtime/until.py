"""Until (polling) condition evaluation helpers."""

from __future__ import annotations

import re
import sys
import time
from typing import Any

from .core import render

_UNTIL_OPS = [
    (re.compile(r"=="), "=="),
    (re.compile(r"!="), "!="),
    (re.compile(r"\s+in\s+"), "in"),
    (re.compile(r"~"), "~"),
]
_UNTIL_REGEX_RHS = re.compile(r"^/(.*)/([a-zA-Z]*)$")
_UNTIL_LIST_RHS = re.compile(r"^\[(.*)\]$")


def _until_flags(spec: str) -> int:
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


def eval_until(condition: str, store: dict[str, Any]) -> bool:
    """Evaluate an until-condition string against the variable store."""
    best = None
    for pat, op in _UNTIL_OPS:
        m = pat.search(condition)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.end(), op)
    if best is None:
        raise ValueError(
            f"until condition: no operator (==, !=, ~, in) found in {condition!r}"
        )
    start, end, op = best
    lhs = render(condition[:start], store).strip()
    rhs = render(condition[end:], store).strip()

    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    if op == "~":
        m = _UNTIL_REGEX_RHS.match(rhs)
        if m is None:
            raise ValueError(f"until condition: '~' RHS must be /pattern/[flags], got {rhs!r}")
        return re.search(m.group(1), lhs, _until_flags(m.group(2))) is not None
    if op == "in":
        m = _UNTIL_LIST_RHS.match(rhs)
        if m is None:
            raise ValueError(f"until condition: 'in' RHS must be [A, B, C], got {rhs!r}")
        items = [x.strip() for x in m.group(1).split(",") if x.strip() != ""]
        return lhs in items
    raise AssertionError(f"unreachable: unknown operator {op!r}")


def poll_until(
    name: str,
    attempt_fn,
    condition: str,
    interval: float,
    max_attempts: int,
    store: dict[str, Any],
    quiet: bool,
    out=None,
) -> None:
    """Re-run ``attempt_fn`` until ``eval_until(condition, store)`` becomes true."""
    out = sys.stdout if out is None else out
    for attempt in range(1, max_attempts + 1):
        attempt_fn()
        if eval_until(condition, store):
            if not quiet:
                print(f"    * until satisfied on attempt {attempt}", file=out)
            return
        if attempt < max_attempts:
            if not quiet:
                print(
                    f"    * until not satisfied (attempt {attempt}/{max_attempts}), "
                    f"retrying in {interval}s",
                    file=out,
                )
            time.sleep(interval)
    raise RuntimeError(
        f"step {name!r}: until condition not satisfied "
        f"after {max_attempts} attempts: {condition!r}"
    )
