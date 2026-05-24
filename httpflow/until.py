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

from typing import Any

from .embedded_runtime import eval_until


def evaluate(condition: str, store: dict[str, Any]) -> bool:
    """Render ``condition`` against ``store`` and evaluate it as a boolean."""
    return eval_until(condition, store)
