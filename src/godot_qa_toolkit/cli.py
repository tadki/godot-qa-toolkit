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

from .doctor import cmd_doctor
from .gherkin.parser import GherkinSyntaxError, parse
from .gherkin.runner import Registry, run_feature

# complexity/coverage/mutation are imported lazily inside their cmd_* handlers:
# they pull in gdtoolkit, so a gdtoolkit-less environment (e.g. a minimal CI
# runner that only needs `gqt gherkin`) must still start the CLI (§SPEC-114,
# SEE-1367).


def _emit(result: dict) -> int:
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 1


def _load_steps_module(path: Path, registry: "Registry") -> None:
    """Exec one steps file and call its register(). Caller validates existence."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(f"gqt_steps_{path.stem}", str(path))
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot build import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses' KW_ONLY sniff reads sys.modules[cls.__module__] during class
    # creation — an exec'd-but-unregistered module makes that lookup None and
    # any @dataclass in the steps file (e.g. a probe result DTO) dies with
    # AttributeError. Register before exec; del afterwards keeps the namespace
    # clean (SEE-1309: real KOL library hits this, toy fixtures do not).
    sys.modules[spec.name] = module
    # spec_from_file_location does not put the module's directory on sys.path;
    # a steps library that imports sibling helpers needs it there for the
    # duration of the exec.
    steps_dir = str(path.parent)
    added = steps_dir not in sys.path
    if added:
        sys.path.insert(0, steps_dir)
    try:
        spec.loader.exec_module(module)
        module.register(registry)
    finally:
        sys.modules.pop(spec.name, None)
        if added:
            sys.path.remove(steps_dir)


def _try_load_steps(path: Path, registry: "Registry", *, label: str) -> bool:
    """Load one steps module; on failure report as caller error (exit 2).

    A steps module that cannot even import (missing sibling helper, missing
    dependency, missing prerequisite like the game-side import cache) is an
    environment/caller problem, NOT a test verdict: returning False lets the
    CLI exit 2 with a stderr-only message instead of fabricating a red run
    (SEE-1306 D2: `gqt gherkin` must not misreport "env not ready" as
    "code regressed").
    """
    try:
        _load_steps_module(path, registry)
    except (ImportError, AttributeError, OSError, ValueError) as exc:
        print(f"cannot load steps module: {label} ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return False
    return True


def cmd_gherkin(args: argparse.Namespace) -> int:
    feature_path = Path(args.feature)
    registry = Registry()
    if args.steps:
        steps_path = Path(args.steps)
        if steps_path.is_dir():
            # Library form (SEE-1306): every public module in the directory is
            # a steps module; underscore-prefixed files are shared helpers and
            # are skipped so their absence of register() is not an error.
            modules = sorted(
                p for p in steps_path.glob("*.py")
                if not p.name.startswith("_")
            )
            if not modules:
                print(f"no steps modules found in directory: {args.steps}", file=sys.stderr)
                return 2
            for mod_path in modules:
                if not _try_load_steps(mod_path, registry, label=args.steps):
                    return 2
        elif steps_path.is_file():
            if not _try_load_steps(steps_path, registry, label=args.steps):
                return 2
        else:
            print(f"cannot load steps module: {args.steps}", file=sys.stderr)
            return 2
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
    return _emit(run_feature(feature, registry, scenario_timeout_s=args.timeout))


def cmd_complexity(args: argparse.Namespace) -> int:
    from .complexity.gate import GateConfig, run_gate

    # When --max is lowered below --warn, warn collapses with it: an explicit
    # strict max means "everything above max fails", nothing merely warns.
    warn = min(args.warn, args.max)
    config = GateConfig(warn_complexity=warn, max_complexity=args.max)
    return _emit(run_gate(args.paths, config))


def cmd_mutation(args: argparse.Namespace) -> int:
    from .mutation.inject import BinaryConfigError, detect_binary_kind

    # §SPEC-012（定案 C 项 a+b）：GQT_GODOT_BIN 覆写限 mutation 子命令——
    # dispatch 前钉死二进制类型（平台分支与 key 二进制成分共用判定）；
    # coverage/gherkin 子命令不触碰该 env，行为面冻结。
    try:
        binary_kind = detect_binary_kind(refresh=True)
    except BinaryConfigError as e:
        # LOW-1：显式配置错误统一 JSON 错误契约（ok:false + 明确 error），
        # 不裸抛 traceback——与「目标文件不存在」一类响亮性/形态对齐。
        return _emit({
            "tool": "mutation",
            "ok": False,
            "summary": {"error": str(e)},
            "failures": [{"reason": str(e)}],
        })
    try:
        result = _dispatch_mutation(args, binary_kind)
    except FileNotFoundError as e:
        result = {
            "tool": "mutation",
            "ok": False,
            # 多文件场景报全部输入（仅报 files[0] 会误导为只缺第一个——LOW-6）
            "summary": {"files": args.files, "error": str(e)},
            "failures": [{"reason": str(e)}],
        }
    except ValueError as e:
        # §SPEC-1420-11：单点寻址无命中（源码漂移/越界行）= 显式错误契约
        result = {
            "tool": "mutation",
            "ok": False,
            "summary": {"files": args.files, "error": str(e)},
            "failures": [{"reason": str(e)}],
        }
    return _emit(result)


def _dispatch_mutation(args: argparse.Namespace, binary_kind: str) -> dict:
    """mutation 子命令派发（串行 / 并发编排两径，<800/复杂度纪律拆分）。"""
    from .timing import TimingStore

    files = args.files
    # §SPEC-1420-11：单点寻址（--mutation/--lines）= 卡尺语义，仅单文件；
    # 走串行路径（1 baseline + N selected，秒级确认）。
    selecting = getattr(args, "mutation", None) or getattr(args, "lines", None)
    if selecting and len(files) > 1:
        return {
            "tool": "mutation",
            "ok": False,
            "summary": {"files": files,
                        "error": "--mutation/--lines target a single file"},
            "failures": [{"reason": "--mutation/--lines require exactly one "
                                    "target file"}],
        }
    # SEE-1413：--jobs 缺省时单文件同样走并发切片路径（默认并行）；显式
    # --jobs 1 在 executor 内强制串行。串行快路径仅留给显式串行请求。
    multi_concurrency = (len(files) > 1 or args.jobs is None or args.jobs > 1)
    if selecting:
        multi_concurrency = False
    if not multi_concurrency:
        return _run_mutation_serial(args, binary_kind)
    from .mutation.executor import run_mutation_files
    # SEE-1413：timing store 只喂退串行路径的 run_mutation（与原串行
    # 快路径同接线）；并发分片路径不接线（worker 分片跳过 store 读写
    # 是既有 SPEC-005/L1 契约，adaptive timeout 语义不静默丢失）。
    timing_store = None
    if not args.dry_run and len(files) == 1:
        timing_store = TimingStore(args.project_root, files[0],
                                   "mutation", args.tests,
                                   binary_kind=binary_kind)
    result = run_mutation_files(
        files, args.project_root, budget=args.budget,
        timeout_s=args.timeout, dry_run=args.dry_run,
        tests_glob=args.tests, jobs=args.jobs, resume=args.resume,
        binary_kind=binary_kind, timing_store=timing_store,
        mutation_warning=args.mutation_warning,
        timeout_factor=args.timeout_factor,
        mutate_all=args.mutate_all,
        reuse_coverage=args.reuse_coverage)
    if len(files) == 1 and args.jobs is None:
        # 缺省单文件对外保持既有 flat CLI 契约，results 信封仅限编排层。
        envelope = result
        result = envelope["results"][0]
        result["summary"].setdefault("jobs", envelope["jobs"])
    return result


def _run_mutation_serial(args: argparse.Namespace, binary_kind: str) -> dict:
    from .mutation.runner import run_mutation
    from .timing import TimingStore

    # SEE-1356 L1：SPEC-009 dry-run 跳过 timing store 读写。
    timing_store = None
    if not args.dry_run:
        timing_store = TimingStore(args.project_root, args.files[0],
                                   "mutation", args.tests,
                                   binary_kind=binary_kind)
    result = run_mutation(
        file_path=args.files[0],
        project_root=args.project_root,
        budget=args.budget,
        timeout_s=args.timeout,
        dry_run=args.dry_run,
        tests_glob=args.tests,
        timing_store=timing_store,
        resume=args.resume,
        binary_kind=binary_kind,
        mutation_warning=args.mutation_warning,
        timeout_factor=args.timeout_factor,
        mutate_all=args.mutate_all,
        select_ids=([args.mutation] if getattr(args, "mutation", None)
                    else None),
        select_lines=([args.lines] if getattr(args, "lines", None)
                      else None),
        reuse_coverage=args.reuse_coverage,
    )
    result["summary"].setdefault("jobs", 1)
    return result


def cmd_coverage(args: argparse.Namespace) -> int:
    from .coverage.runner import run_coverage
    from .timing import TimingStore

    try:
        result = run_coverage(
            file_path=args.file,
            project_root=args.project_root,
            min_percent=args.min_percent,
            timeout_s=args.timeout,
            timing_store=TimingStore(args.project_root, args.file,
                                     "coverage", None),
            tests_glob=args.tests,
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
    g.add_argument("--steps", help="python module path, or directory of steps modules")
    g.add_argument("--timeout", type=float, default=None,
                   help="per-scenario wall-clock budget in seconds")
    g.set_defaults(func=cmd_gherkin)

    c = sub.add_parser("complexity", help="complexity gate over .gd files")
    c.add_argument("paths", nargs="+", help=".gd files or directories to scan")
    c.add_argument("--warn", type=int, default=10, help="warn threshold (default 10)")
    c.add_argument("--max", type=int, default=15, help="fail threshold (default 15)")
    c.set_defaults(func=cmd_complexity)

    m = sub.add_parser("mutation", help="mutation test one or more .gd files")
    m.add_argument("files", nargs="+", help="the .gd file(s) to mutate")
    m.add_argument("--project-root", required=True, help="project root containing addons/gut/")
    m.add_argument("--budget", type=int, default=50, help="max mutants (default 50)")
    m.add_argument("--timeout", type=int, default=60, help="GUT timeout seconds (default 60)")
    m.add_argument("--dry-run", action="store_true",
                   help="list mutation sites without running GUT (SPEC-009)")
    m.add_argument("--tests", default=None,
                   help="scoped GUT dir for affected-test subset, e.g. res://tests/save/ (SPEC-009); "
                        "omitted = auto-scan tests/ references, zero hits falls back to full suite "
                        "(SEE-1321 SPEC-001/002)")
    m.add_argument("--jobs", type=int, default=None,
                   help="worker pool size for per-file AND per-mutant slicing "
                        "(a single file is sliced per-mutant too); default = "
                        "max(2, cpu//2); --jobs 1 forces serial (SEE-1413)")
    m.add_argument("--resume", action="store_true",
                   help="skip mutants whose verdicts are already in the resume store "
                        "(SEE-1400); fingerprint mismatch (source/tests/budget/ops) "
                        "re-runs them — every run persists verdicts, resume just "
                        "turns on the skip")
    m.add_argument("--mutation-warning", type=int, default=50,
                   help="warn on stderr + summary.attention when the pre-truncation "
                        "mutation site count exceeds N (default 50, aligned with "
                        "the default budget; SEE-1420)")
    m.add_argument("--timeout-factor", type=float, default=2.0,
                   help="adaptive per-mutant timeout = max(timeout, baseline*F+1); "
                        "default 2.0 (overrides the built-in scale only, never "
                        "tightens; SEE-1420)")
    m.add_argument("--mutate-all", action="store_true",
                   help="disable differential skipping for this run (SEE-1420): "
                        "diff is ON by default when a v2 resume manifest exists "
                        "(unchanged killed sites are reused, survived/timeout "
                        "always re-run); this flag forces full execution")
    m.add_argument("--mutation", default=None, metavar="ID",
                   help="single-site rerun: '<file>#<scope_id>#<n>' (SEE-1420 "
                        "P3 caliper) — forces fresh execution of that one "
                        "site (1 baseline + 1 mutant), bypassing diff reuse; "
                        "single-file only")
    m.add_argument("--lines", type=int, default=None, metavar="N",
                   help="rerun all mutation sites on source line N (SEE-1420 "
                        "P3); single-file only")
    m.add_argument("--reuse-coverage", action="store_true",
                   help="prefilter mutation sites by line coverage from the "
                        "last `gqt coverage` run for this file (SEE-1420 P2; "
                        "provenance-checked: source/tests/binary must match; "
                        "uncovered sites are marked 'uncovered' and skipped)")
    m.set_defaults(func=cmd_mutation)

    v = sub.add_parser("coverage", help="line coverage of a single .gd file via GUT")
    v.add_argument("file", help="the .gd file to measure")
    v.add_argument("--project-root", required=True, help="project root containing addons/gut/")
    v.add_argument("--min-percent", type=float, default=80.0,
                   help="min coverage percent (default 80)")
    v.add_argument("--timeout", type=int, default=120, help="GUT timeout seconds (default 120)")
    v.add_argument("--tests", default=None,
                   help="scoped GUT dir or single .gd path for per-suite attribution, "
                        "e.g. res://tests/unit/time/ (SEE-1367 §SPEC-017); omitted = full res://tests/")
    v.set_defaults(func=cmd_coverage)

    d = sub.add_parser("doctor", help="self-check editable install vs current worktree gitlink")
    d.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
