"""Normalized workflow models used by both the runner and the generator.

This module is kept free of runtime helpers so that :mod:`config` can
import it without circular dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TextBody:
    """A raw text body (e.g. JSON)."""

    text: str


@dataclass
class FormBody:
    """An ``application/x-www-form-urlencoded`` body."""

    fields: dict[str, str]


type Body = TextBody | FormBody


@dataclass
class UntilSpec:
    """Polling configuration for a single HTTP request."""

    condition: str
    interval: float = 1.0
    max_attempts: int = 10


@dataclass
class HttpStep:
    """A single HTTP request step.

    ``body`` carries exactly one of :class:`TextBody` or :class:`FormBody`
    when present, representing the mutually exclusive body modes at the
    type level.
    """

    name: str
    method: str
    url: str
    description: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: Body | None = None
    capture: dict[str, str] = field(default_factory=dict)
    until: UntilSpec | None = None


@dataclass
class SleepStep:
    """A non-HTTP pause step."""

    name: str
    seconds: str  # template expression, rendered at runtime
    description: str | None = None


type Step = HttpStep | SleepStep


@dataclass
class WorkflowSpec:
    """Validated, normalised view of a workflow ready for execution or emission."""

    steps: list[Step] = field(default_factory=list)


def from_config(config):
    """Convert a legacy :class:`WorkflowConfig` into a :class:`WorkflowSpec`.

    This delegates to :func:`config.to_model` so that normalisation logic
    lives in one place.
    """
    from .config import to_model
    return to_model(config)
