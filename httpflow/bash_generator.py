"""Generate a standalone single-file bash runner from a WorkflowSpec."""

from __future__ import annotations

from .bashgen import generate as _generate


def generate(
    spec,
    *,
    shebang: bool = False,
    default_vars: dict[str, str] | None = None,
    embed_files: bool = False,
    toml_path: str | None = None,
):
    """Generate a standalone bash script from a workflow spec.

    Parameters are forwarded to :func:`bashgen.generate`.
    """
    return _generate(
        spec,
        shebang=shebang,
        default_vars=default_vars,
        embed_files=embed_files,
        toml_path=toml_path,
    )


__all__ = ["generate"]
