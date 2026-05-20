"""Command-line interface for httpflow."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from . import __version__
from . import config as config_mod
from . import generator, workflow


def _parse_vars(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for kv in items:
        if "=" not in kv:
            raise SystemExit(f"-v/--var requires key=value, got: {kv!r}")
        k, _, v = kv.partition("=")
        k = k.strip()
        if not k:
            raise SystemExit(f"-v/--var has empty key: {kv!r}")
        out[k] = v
    return out


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="httpflow",
        description="TOML-driven HTTP workflow runner (stdlib-only).",
    )
    parser.add_argument("--version", action="version", version=f"httpflow {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="run a workflow")
    p_run.add_argument("-f", "--file", required=True, help="workflow TOML file")
    p_run.add_argument("-v", "--var", action="append", default=[],
                       help="key=value variable injection (repeatable)")
    p_run.add_argument("-q", "--quiet", action="store_true",
                       help="suppress per-step request/response detail output "
                            "(detail is ON by default)")
    p_run.add_argument("--pretty-json", action="store_true",
                       help="pretty-print JSON request/response bodies with 2-space indent")

    p_gen = sub.add_parser("generate", help="emit a standalone runner script")
    p_gen.add_argument("-f", "--file", required=True, help="workflow TOML file")
    p_gen.add_argument("-o", "--output", default=None,
                       help="output .py file (default: stdout)")
    p_gen.add_argument("-v", "--var", action="append", default=[],
                       help="default variable embedded in the generated script (repeatable)")
    p_gen.add_argument("--shebang", action="store_true",
                       help="prepend #!/usr/bin/env python3 and chmod +x the output file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Backward-compat: treat `-f ...` (no subcommand) as `run -f ...`.
    if argv and argv[0] not in ("run", "generate", "-h", "--help", "--version"):
        argv = ["run", *argv]

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    try:
        cfg = config_mod.load(args.file)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error loading {args.file}: {e}", file=sys.stderr)
        return 1

    if args.command == "run":
        vars_ = _parse_vars(args.var)
        try:
            workflow.run(cfg, vars_, quiet=args.quiet, pretty_json=args.pretty_json)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    if args.command == "generate":
        default_vars = _parse_vars(args.var)
        try:
            script = generator.generate(cfg, default_vars=default_vars,
                                        shebang=args.shebang)
        except Exception as e:
            print(f"error generating script: {e}", file=sys.stderr)
            return 1

        if args.output is None:
            sys.stdout.write(script)
            return 0

        with open(args.output, "w", encoding="utf-8") as f:
            f.write(script)
        if args.shebang:
            import os, stat
            mode = os.stat(args.output).st_mode
            os.chmod(args.output, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return 0

    parser.print_help()
    return 1
