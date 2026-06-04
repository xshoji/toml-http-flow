"""Repeat variable iteration helpers."""

from __future__ import annotations


def build_repeat_iterations(
    repeat_vars: dict[str, list[str]] | None,
    required: set[str],
) -> list[dict[str, str]]:
    """Validate repeat variables and expand them into per-iteration dicts."""
    repeat_vars = dict(repeat_vars or {})
    missing = required - set(repeat_vars)
    if missing:
        examples = " ".join(
            f'--repeat-vars "{name}=value1,value2,value3"'
            for name in sorted(missing)
        )    
        raise ValueError(
            f"missing required repeat variable(s): {', '.join(sorted(missing))}\n"
            f"Example: {examples}"
        )       
    if not repeat_vars:
        return [{}]
    lengths = {k: len(v) for k, v in repeat_vars.items()}
    distinct = set(lengths.values())
    if len(distinct) != 1:
        raise ValueError(
            f"--repeat-vars value counts must match across all keys, got: {lengths}"
        )
    n = distinct.pop()
    if n == 0:
        raise ValueError("--repeat-vars must supply at least one value per key")
    return [{k: repeat_vars[k][i] for k in repeat_vars} for i in range(n)]


def merge_default_repeat_vars(
    repeat_vars: dict[str, list[str]] | None,
    default_repeat_vars: dict[str, list[str]] | None,
) -> dict[str, list[str]]:
    """Merge runtime repeat vars on top of embedded defaults."""
    merged = dict(default_repeat_vars or {})
    merged.update(repeat_vars or {})
    return merged


def build_repeat_iterations_from_args(
    raw_items: list[str],
    default_repeat_vars: dict[str, list[str]] | None,
    required: set[str],
) -> list[dict[str, str]]:
    """Parse raw CLI repeat args, merge defaults, and expand iterations."""
    return build_repeat_iterations(
        merge_default_repeat_vars(parse_repeat_args(raw_items), default_repeat_vars),
        required,
    )


def parse_repeat_args(repeat_args: list[str]) -> dict[str, list[str]]:
    """Parse ``name=v1,v2`` repeat CLI entries into a mapping."""
    parsed: dict[str, list[str]] = {}
    for kv in repeat_args:
        if "=" not in kv:
            raise ValueError(f"--repeat-vars requires name=v1,v2,..., got: {kv!r}")
        k, _, v = kv.partition("=")
        k = k.strip()
        if not k:
            raise ValueError(f"--repeat-vars has empty key: {kv!r}")
        if k in parsed:
            raise ValueError(f"--repeat-vars duplicated key: {k!r}")
        values = [x.strip() for x in v.split(",")]
        if not values or any(x == "" for x in values):
            raise ValueError(f"--repeat-vars must supply non-empty comma-separated values: {kv!r}")
        parsed[k] = values
    return parsed
