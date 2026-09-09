"""Unified CLI for godot-qa-toolkit (SEE-1268 M1).

Every subcommand emits the unified JSON contract:
    {"tool": ..., "ok": bool, "summary": {...}, "failures": [...]}

Exit code is 0 on ok, 1 on any failure — the machine verdict consumers rely on.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .complexity.gate import GateConfig, run_gate
from .gherkin.parser import GherkinSyntaxError, parse
from .gherkin.runner import Registry, run_feature


def _emit(result: dict) -> int:
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 1


def cmd_gherkin(args: argparse.Namespace) -> int:
    feature_path = Path(args.feature)
    registry = Registry()
    if args.steps:
        import importlib.util

        spec = importlib.util.spec_from_file_location("gqt_steps", args.steps)
        if spec is None or spec.loader is None:
            print(f"cannot load steps module: {args.steps}", file=sys.stderr)
            return 2
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.register(registry)
    try:
        feature = parse(feature_path.read_text(encoding="utf-8"), path=str(feature_path))
    except GherkinSyntaxError as e:
        result = {
            "tool": "gherkin",
            "ok": False,
            "feature": None,
            "summary": {"total": 0, "passed": 0, "failed": 1},
            "failures": [{"reason": str(e)}],
        }
        return _emit(result)
    return _emit(run_feature(feature, registry))


def cmd_complexity(args: argparse.Namespace) -> int:
    # When --max is lowered below --warn, warn collapses with it: an explicit
    # strict max means "everything above max fails", nothing merely warns.
    warn = min(args.warn, args.max)
    config = GateConfig(warn_complexity=warn, max_complexity=args.max)
    return _emit(run_gate(args.paths, config))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gqt", description="godot-qa-toolkit")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("gherkin", help="run a .feature file")
    g.add_argument("feature", help="path to the .feature file")
    g.add_argument("--steps", help="python module path exposing register(registry)")
    g.set_defaults(func=cmd_gherkin)

    c = sub.add_parser("complexity", help="complexity gate over .gd files")
    c.add_argument("paths", nargs="+", help=".gd files or directories to scan")
    c.add_argument("--warn", type=int, default=10, help="warn threshold (default 10)")
    c.add_argument("--max", type=int, default=15, help="fail threshold (default 15)")
    c.set_defaults(func=cmd_complexity)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
