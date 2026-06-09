"""Normalized workflow models used by both the runner and the generator.

This module is kept free of runtime helpers so that :mod:`config` can
import it without circular dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias


@dataclass(slots=True)
class TextBody:
    """A raw text body (e.g. JSON)."""

    text: str


@dataclass(slots=True)
class FormBody:
    """An ``application/x-www-form-urlencoded`` body."""

    fields: dict[str, str]


@dataclass(slots=True)
class FileBody:
    """A raw binary body loaded from a file at runtime."""

    path: str


@dataclass(slots=True)
class MultipartField:
    """A regular multipart/form-data text field."""

    name: str
    value: str


@dataclass(slots=True)
class MultipartFile:
    """A multipart/form-data file field loaded at runtime."""

    name: str
    path: str
    filename: str | None = None
    content_type: str = "application/octet-stream"


MultipartPart: TypeAlias = MultipartField | MultipartFile


@dataclass(slots=True)
class MultipartBody:
    """A multipart/form-data body preserving part order and duplicate names."""

    parts: list[MultipartPart]


Body: TypeAlias = TextBody | FormBody | FileBody | MultipartBody


@dataclass(slots=True)
class UntilSpec:
    """Polling configuration for a single HTTP request."""

    condition: str
    interval: float = 1.0
    max_attempts: int = 10


@dataclass(slots=True)
class HttpStep:
    """A single HTTP request step.

    ``body`` carries exactly one body model when present, representing the
    mutually exclusive body modes at the
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


@dataclass(slots=True)
class SleepStep:
    """A non-HTTP pause step."""

    name: str
    seconds: str  # template expression, rendered at runtime
    description: str | None = None


Step: TypeAlias = HttpStep | SleepStep


@dataclass(slots=True)
class WorkflowSpec:
    """Validated, normalised view of a workflow ready for execution or emission."""

    steps: list[Step] = field(default_factory=list)

