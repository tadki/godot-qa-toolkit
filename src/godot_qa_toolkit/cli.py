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
from .coverage.runner import run_coverage
from .gherkin.parser import GherkinSyntaxError, parse
from .gherkin.runner import Registry, run_feature
from .mutation.runner import run_mutation


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


def cmd_mutation(args: argparse.Namespace) -> int:
    try:
        result = run_mutation(
            file_path=args.file,
            project_root=args.project_root,
            budget=args.budget,
            timeout_s=args.timeout,
        )
    except FileNotFoundError as e:
        result = {
            "tool": "mutation",
            "ok": False,
            "summary": {"file": args.file, "error": str(e)},
            "failures": [{"reason": str(e)}],
        }
    return _emit(result)


def cmd_coverage(args: argparse.Namespace) -> int:
    try:
        result = run_coverage(
            file_path=args.file,
            project_root=args.project_root,
            min_percent=args.min_percent,
            timeout_s=args.timeout,
        )
    except FileNotFoundError as e:
        result = {
            "tool": "coverage",
            "ok": False,
            "summary": {"file": args.file, "error": str(e)},
            "failures": [{"reason": str(e)}],
        }
    return _emit(result)


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

    m = sub.add_parser("mutation", help="mutation test a single .gd file")
    m.add_argument("file", help="the .gd file to mutate")
    m.add_argument("--project-root", required=True, help="project root containing addons/gut/")
    m.add_argument("--budget", type=int, default=50, help="max mutants (default 50)")
    m.add_argument("--timeout", type=int, default=60, help="GUT timeout seconds (default 60)")
    m.set_defaults(func=cmd_mutation)

    v = sub.add_parser("coverage", help="line coverage of a single .gd file via GUT")
    v.add_argument("file", help="the .gd file to measure")
    v.add_argument("--project-root", required=True, help="project root containing addons/gut/")
    v.add_argument("--min-percent", type=float, default=80.0, help="min coverage % (default 80)")
    v.add_argument("--timeout", type=int, default=120, help="GUT timeout seconds (default 120)")
    v.set_defaults(func=cmd_coverage)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
