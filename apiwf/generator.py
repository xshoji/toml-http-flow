"""Generate a standalone single-file Python runner from a WorkflowConfig."""

from __future__ import annotations

import datetime
import pprint
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .config import WorkflowConfig


_TEMPLATE_PATH = Path(__file__).parent / "templates" / "runner.py.tmpl"


def _pretty_repr(obj) -> str:
    """Render ``obj`` as a Python literal in a human-readable form."""
    return pprint.pformat(obj, indent=4, width=100, sort_dicts=False)


def _requests_literal(cfg: WorkflowConfig) -> str:
    items = []
    for req in cfg.requests:
        d = asdict(req)
        items.append(
            "    # Source: [[requests]] name = {name!r}\n    {body},".format(
                name=req.name,
                body=_pretty_repr(d).replace("\n", "\n    "),
            )
        )
    if not items:
        return "[]"
    return "[\n" + "\n".join(items) + "\n]"


def generate(
    cfg: WorkflowConfig,
    *,
    default_vars: dict[str, str] | None = None,
    shebang: bool = False,
) -> str:
    """Return the source of a self-contained runner script."""
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    timestamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    rendered = (
        template
        .replace("{{VERSION}}", __version__)
        .replace("{{GENERATED_AT}}", timestamp)
        .replace("{{REQUESTS}}", _requests_literal(cfg))
        .replace("{{DEFAULT_VARS}}", _pretty_repr(dict(default_vars or {})))
    )

    if shebang:
        rendered = "#!/usr/bin/env python3\n" + rendered

    return rendered
