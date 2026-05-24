"""Backward-compatible re-export of runner functionality.

The real implementation has moved to :mod:`runner` while the codebase
migrates to :class:`WorkflowSpec`.  Importing from here still works for
existing consumers.
"""

from __future__ import annotations

from .runner import collect_repeat_names, run

__all__ = ["collect_repeat_names", "run"]
