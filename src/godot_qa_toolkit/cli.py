"""Unified CLI for godot-qa-toolkit (SEE-1268 M1).

Every subcommand emits the unified JSON contract:
    {"tool": ..., "ok": bool, "summary": {...}, "failures": [...]}

Exit code is 0 on ok, 1 on any failure — the machine verdict consumers rely on.
The `gherkin` command was retired in SEE-1468 M5-b (runner/expectations
physically deleted; parser promoted to accept.parser; judgement faces live
in `gqt accept` and pytest-bdd)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .doctor import cmd_doctor
# complexity/coverage/mutation are imported lazily inside their cmd_* handlers:
# they pull in gdtoolkit, so a gdtoolkit-less environment (e.g. a minimal CI
# runner) must still start the CLI (§SPEC-114, SEE-1367).


def _emit(result: dict) -> int:
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 1


def _apply_godot_bin_or_contract(tool: str, args: argparse.Namespace) -> int | None:
    """§SPEC-1525-002：--godot-bin 命令级覆盖注入（> GQT_GODOT_BIN > PATH），
    显式配置错误 → 统一 JSON 错误契约（ok:false，与既有 BinaryConfigError
    出口同形态）。返回 None 表示继续派发；返回 int 为已完成的错误响应。

    修复轮 F-1/F-2（hardener 上报）：注入后立即 refresh 重判——(a) 显式 env
    非法路径在此早验证并转契约，先于 coverage/accept 的 GUT/重放等任何其他
    校验（不得被掩盖、不得裸 traceback，与 mutation 同形态）；(b) 同进程切换
    二进制后 binary kind 必须重钉，--path 形态与 timing 键成分取覆盖后真实
    类型（与 mutation 既有 refresh 语义对齐）。"""
    from .mutation.inject import BinaryConfigError, apply_godot_bin_override, \
        detect_binary_kind

    try:
        apply_godot_bin_override(getattr(args, "godot_bin", None))
        detect_binary_kind(refresh=True)
    except BinaryConfigError as e:
        return _emit({
            "tool": tool,
            "ok": False,
            "summary": {"error": str(e)},
            "failures": [{"reason": str(e)}],
        })
    return None


def _add_godot_bin_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--godot-bin", default=None,
                        help="per-command Godot binary override; priority: "
                             "this flag > GQT_GODOT_BIN env > PATH (SEE-1525)")


def _accept_compile(args: argparse.Namespace) -> int:
    from pathlib import Path as _P

    from .accept.compile import compile_feature
    from .accept.suite import (SuiteError, compile_keys_to_suite,
                               create_suite_dir)

    keys = getattr(args, "keys", None)
    out_dir = getattr(args, "out_dir", None)
    if not args.feature and keys:
        # §SPEC-035 keys 模式：目录即选择集——私有目录缺省自动创建
        try:
            suite_dir = (_P(out_dir).resolve() if out_dir
                         else create_suite_dir(args.project_root))
            rec = compile_keys_to_suite(args.project_root,
                                        [k.strip() for k in keys.split(",")
                                         if k.strip()], suite_dir)
        except SuiteError as e:
            return _emit({
                "tool": "accept", "ok": False,
                "summary": {"error": str(e), "suite_error": True},
                "failures": [{"reason": f"suite_error: {e}"}],
            })
        return _emit({
            "tool": "accept", "ok": True,
            "summary": {
                "mode": "private-suite",
                "keys": rec["keys"],
                "suite_dir": rec["suite_dir"],
                "wrote": [f"generated/test_{k}.gd" for k in rec["keys"]],
            },
            "failures": [],
        })
    if not args.feature:
        return _emit({
            "tool": "accept", "ok": False,
            "summary": {"error": "accept compile needs <feature> or --keys"},
            "failures": [{"reason": "accept compile needs <feature> or "
                                    "--keys"}],
        })
    suite, error = compile_feature(
        args.feature, args.project_root,
        write=args.write, clean=not args.no_clean, out_dir=out_dir)
    if error is not None:
        return _emit({
            "tool": "accept", "ok": False,
            "summary": {"error": error["reason"]},
            "failures": [error],
        })
    wrote = []
    if args.write:
        if out_dir:
            base = _P(out_dir).resolve().relative_to(
                _P(args.project_root).resolve()).as_posix()
            wrote = [
                f"{base}/ir/{suite.feature_key}.json",
                f"{base}/generated/test_{suite.feature_key}.gd",
                f"{base}/generated/accept_worker.gd",
                f"{base}/generated/{suite.feature_key}.manifest.json",
            ]
        else:
            wrote = [
                f"acceptance/ir/{suite.feature_key}.json",
                f"acceptance/generated/test_{suite.feature_key}.gd",
                "acceptance/generated/accept_worker.gd",
                f"acceptance/generated/{suite.feature_key}.manifest.json",
            ]
    return _emit({
        "tool": "accept", "ok": True,
        "summary": {
            "feature": suite.ir["feature"],
            "feature_key": suite.feature_key,
            "cases": len(suite.ir["cases"]),
            "ir_hash": suite.manifest["ir_hash"],
            "wrote": wrote,
        },
        "failures": [],
    })


def cmd_accept(args: argparse.Namespace) -> int:
    from .accept.run import AcceptRunError, run_suite
    from .accept.suite import SuiteError, run_acceptance_probe, run_suite_dir

    if args.accept_command == "compile":
        return _accept_compile(args)
    # run / mutate
    early = _apply_godot_bin_or_contract("accept", args)
    if early is not None:
        return early
    probe_keys = getattr(args, "acceptance_probe_keys", None)
    suite_dir_arg = getattr(args, "suite_dir", None)
    if args.accept_command == "run" and (probe_keys or suite_dir_arg):
        if probe_keys and suite_dir_arg:
            return _emit({
                "tool": "accept", "ok": False,
                "summary": {"error": "--acceptance-probe-keys compiles its "
                                     "own private suite dir; --suite-dir "
                                     "is mutually exclusive"},
                "failures": [{"reason": "mutually exclusive flags"}],
            })
        try:
            if probe_keys:
                keys = [k.strip() for k in probe_keys.split(",")
                        if k.strip()]
                if not keys:
                    return _emit({
                        "tool": "accept", "ok": False,
                        "summary": {"error": "no keys parsed"},
                        "failures": [{"reason": "no keys parsed"}],
                    })
                probe = run_acceptance_probe(args.project_root, keys,
                                             timeout_s=args.timeout)
                return _emit({"tool": "accept", "ok": probe["suite_ok"],
                              "summary": probe, "failures": []})
            record = run_suite_dir(args.project_root, suite_dir_arg,
                                   timeout_s=args.timeout)
            return _emit({"tool": "accept", "ok": record["ok"],
                          "summary": record, "failures": []})
        except AcceptRunError as e:
            return _emit({
                "tool": "accept", "ok": False,
                "summary": {"error": str(e), "run_error": True},
                "failures": [{"reason": f"run_error: {e}"}],
            })
        except SuiteError as e:
            return _emit({
                "tool": "accept", "ok": False,
                "summary": {"error": str(e), "suite_error": True},
                "failures": [{"reason": f"suite_error: {e}"}],
            })
    if not args.key:
        return _emit({
            "tool": "accept", "ok": False,
            "summary": {"error": "--key is required without "
                                 "--suite-dir/--acceptance-probe-keys"},
            "failures": [{"reason": "--key is required"}],
        })
    try:
        if args.accept_command == "mutate":
            from .accept.mutate_runner import run_mutate
            return _emit(run_mutate(
                args.directory if args.directory else args.project_root,
                args.key, timeout_s=args.timeout, use_worker=args.worker))
        return _emit(run_suite(args.directory if args.directory
                               else args.project_root, args.key,
                               timeout_s=args.timeout))
    except AcceptRunError as e:
        return _emit({
            "tool": "accept", "ok": False,
            "summary": {"error": str(e), "run_error": True},
            "failures": [{"reason": f"run_error: {e}"}],
        })


def cmd_arch(args: argparse.Namespace) -> int:
    # gdtoolkit 依赖延迟导入（同 complexity/mutation——§SPEC-114 最小 CI 面）。
    from .arch.gate import run_arch_gate

    return _emit(run_arch_gate(args.paths))


def cmd_complexity(args: argparse.Namespace) -> int:
    from .complexity.gate import GateConfig, run_gate

    # When --max is lowered below --warn, warn collapses with it: an explicit
    # strict max means "everything above max fails", nothing merely warns.
    warn = min(args.warn, args.max)
    config = GateConfig(warn_complexity=warn, max_complexity=args.max)
    return _emit(run_gate(args.paths, config))


def cmd_mutation(args: argparse.Namespace) -> int:
    from .mutation.inject import detect_binary_kind

    # §SPEC-1525-002 + 修复轮 F-1/F-2：覆盖注入与二进制早验证/重判统一在
    # _apply_godot_bin_or_contract（三子命令同源同形态）——此处取已重钉的
    # 判定结果即可（平台分支与 key 二进制成分共用判定）。
    early = _apply_godot_bin_or_contract("mutation", args)
    if early is not None:
        return early
    binary_kind = detect_binary_kind()
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


def cmd_risk(args: argparse.Namespace) -> int:
    from .risk.shadow import run_shadow_from_paths

    early = _apply_godot_bin_or_contract("risk", args)
    if early is not None:
        return early
    paths = [Path(p) for p in args.files]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        return _emit(
            {
                "tool": "risk",
                "ok": False,
                "summary": {"error": f"target files not found: {missing}"},
                "failures": [{"reason": f"target files not found: {missing}"}],
            }
        )
    performance_input = None
    if args.performance_file:
        performance_input = json.loads(
            Path(args.performance_file).read_text(encoding="utf-8")
        )
    report = run_shadow_from_paths(
        paths=paths,
        project_root=args.project_root,
        core_tests=args.core_tests,
        full_tests=args.full_tests,
        skip_measure=args.skip_measure,
        performance_input=performance_input,
    )
    report_path = Path(args.report)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    scored = report["ranking"]["top"]
    return _emit(
        {
            "tool": "risk",
            "ok": True,
            "summary": {
                "report": str(report_path),
                "evidence_level": "exploratory",
                "diagnostic_only": True,
                "dynamic_cache": False,
                "records": len(report["records"]),
                "scored": len(scored),
                "unknown_share": report["unknown"]["share"],
                "eligible_files": report["confidence"]["sample_files"],
                "negative_files": report["confidence"]["negative_files"],
            },
            "failures": [],
        }
    )


def cmd_parity(args: argparse.Namespace) -> int:
    from .complexity.parity import parity_paths
    from .registry.snapshot import save_snapshot

    paths = [Path(p) for p in args.paths]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        return _emit(
            {
                "tool": "parity",
                "ok": False,
                "summary": {"error": f"paths not found: {missing}"},
                "failures": [{"reason": f"paths not found: {missing}"}],
            }
        )
    report = parity_paths(paths, args.project_root)
    snapshot_files = report.pop("_snapshot_files", {})
    if args.registry_out:
        save_snapshot(
            {
                "snapshot_version": "func-registry-snapshot/1",
                "project_root": args.project_root,
                # census fields the §SPEC-017 jq gate reads from the snapshot
                "summary": {
                    "func_entries": report["summary"]["func_entries"],
                    "lambda_entries": report["summary"]["lambda_entries"],
                    "unparseable": report["summary"]["unparseable"],
                    "inner_class_files": report["summary"]["inner_class_files"],
                },
                "files": snapshot_files,
            },
            args.registry_out,
        )
    report["summary"]["registry_out"] = args.registry_out
    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report["summary"]["report"] = args.report
    return _emit(report)


def cmd_registry(args: argparse.Namespace) -> int:
    from .registry.snapshot import diff_registries, load_snapshot, save_snapshot

    try:
        old = load_snapshot(args.old)
        new = load_snapshot(args.new)
    except (OSError, ValueError) as e:
        # json.JSONDecodeError is a ValueError subclass: unparseable or
        # missing snapshots fail loud with the unified contract (ok=false)
        return _emit(
            {
                "tool": "registry",
                "ok": False,
                "summary": {"error": str(e)},
                "failures": [{"reason": f"snapshot load failed: {e}"}],
            }
        )
    diff = diff_registries(old, new)
    if args.out:
        save_snapshot(diff, args.out)
        diff["summary"]["out"] = args.out
    return _emit(diff)


def cmd_recipe(args: argparse.Namespace) -> int:
    from .canonical.recipe import build_recipe

    exclusions = []
    for raw in args.exclude:
        # D-W3A-1: entry may contain `://` (res://… schemes) — take scope at
        # the FIRST colon and reason at the LAST; everything between is the
        # entry. Missing any of the three segments stays fail-loud.
        scope, _, remainder = raw.partition(":")
        entry, sep, reason = remainder.rpartition(":")
        if not scope or not sep or not entry or not reason:
            error = (f"--exclude expects scope:entry:reason (entry may contain "
                     f"':' as in res://…), got {raw!r}")
            return _emit({
                "tool": "recipe",
                "ok": False,
                "summary": {"error": error},
                "failures": [{"reason": error}],
            })
        exclusions.append({"scope": scope, "entry": entry, "reason": reason})
    recipe, error = build_recipe(
        args.project_root, exclusions=exclusions,
    )
    if error is not None:
        return _emit({
            "tool": "recipe",
            "ok": False,
            "summary": {"error": error},
            "failures": [{"reason": error}],
        })
    out_path = Path(args.out)
    out_path.write_text(
        json.dumps(recipe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return _emit({
        "tool": "recipe",
        "ok": True,
        "summary": {
            "recipe_version": recipe["recipe_version"],
            "recipe_hash": recipe["recipe_hash"],
            "out": str(out_path),
            "gut_dirs": len(recipe["gut_face"]["dirs"]),
            "acceptance_features": recipe["acceptance_face"]["count"],
            "exclusions": len(recipe["exclusions"]),
        },
        "failures": [],
    })


def cmd_coverage(args: argparse.Namespace) -> int:
    from .coverage.runner import run_coverage
    from .timing import TimingStore

    # §SPEC-1525-001/002：覆盖注入先行——TimingStore 的 key 二进制成分与
    # 启动 argv 解析同源（构造器内 detect_binary_kind 实时解析）。
    early = _apply_godot_bin_or_contract("coverage", args)
    if early is not None:
        return early
    from .coverage.evidence import prune_sinks as _prune, recover_bundle as _recover
    from pathlib import Path as _P

    if not getattr(args, "recover", None) and not getattr(args, "prune_sinks", None)             and not args.file:
        return _emit({
            "tool": "coverage",
            "ok": False,
            "summary": {"error": "measure mode requires a target file "
                               "(or --recover/--prune-sinks)"},
            "failures": [{"reason": "measure mode requires a target file"}],
        })
    if getattr(args, "recover", None):
        result = _recover(_P(args.recover), project_root=args.project_root)
        result.update({"tool": "coverage", "ok": True})
        return _emit(result)
    if getattr(args, "prune_sinks", None):
        result = _prune(args.project_root,
                        retention_days=getattr(args, "retain_days", 7))
        result.update({"tool": "coverage", "ok": True})
        return _emit(result)
    try:
        result = run_coverage(
            file_path=args.file,
            project_root=args.project_root,
            min_percent=args.min_percent,
            timeout_s=args.timeout,
            timing_store=TimingStore(args.project_root, args.file,
                                     "coverage", None),
            tests_glob=args.tests,
            evidence_out=getattr(args, "evidence_out", None),
        )
    except FileNotFoundError as e:
        result = {
            "tool": "coverage",
            "ok": False,
            "summary": {"file": args.file, "error": str(e)},
            "failures": [{"reason": str(e)}],
        }
    return _emit(result)


def cmd_canonical(args: argparse.Namespace) -> int:
    import json as _json

    from pathlib import Path as _P

    from .canonical.orchestrator import CanonicalError, run_canonical
    from .canonical.recipe import verify_recipe

    recipe_path = _P(args.recipe)
    try:
        recipe = _json.loads(recipe_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return _emit({"tool": "canonical", "ok": False,
                      "summary": {"error": f"recipe load failed: {e}"},
                      "failures": [{"reason": f"recipe load failed: {e}"}]})
    ok, reason = verify_recipe(recipe)
    if not ok:
        return _emit({"tool": "canonical", "ok": False,
                      "summary": {"error": f"recipe invalid: {reason}"},
                      "failures": [{"reason": f"recipe invalid: {reason}"}]})
    roots = [_P(r) for r in args.roots]
    targets = sorted({str(p) for root in roots
                      for p in root.rglob("*.gd") if p.is_file()})
    if not targets:
        return _emit({"tool": "canonical", "ok": False,
                      "summary": {"error": "no .gd targets under roots"},
                      "failures": [{"reason": "no .gd targets"}]})
    probe_keys = (args.acceptance_probe_keys or None)
    if probe_keys:
        probe_keys = [k.strip() for k in probe_keys.split(",") if k.strip()]
    baseline_failures = None
    bf_path = getattr(args, "baseline_failures", None)
    if bf_path:
        try:
            doc = _json.loads(_P(bf_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return _emit({"tool": "canonical", "ok": False,
                          "summary": {"error":
                                      f"baseline-failures load failed: {e}"},
                          "failures": [{"reason":
                                        f"baseline-failures load failed: "
                                        f"{e}"}]})
        tests = doc.get("tests") if isinstance(doc, dict) else doc
        if not isinstance(tests, list) \
                or not all(isinstance(t, str) for t in tests):
            return _emit({"tool": "canonical", "ok": False,
                          "summary": {"error":
                                      "baseline-failures must be a JSON "
                                      "array of test names (or {tests: []})"},
                          "failures": [{"reason": "baseline-failures "
                                                  "malformed"}]})
        baseline_failures = set(tests)
    try:
        report = run_canonical(
            args.project_root, targets, recipe,
            probe_keys=probe_keys, timeout_s=args.timeout,
            evidence_out=getattr(args, "evidence_dir", None),
            scope_roots=[str(r) for r in roots],
            stability_baseline=getattr(args, "stability_baseline", None),
            shard_width=getattr(args, "shard_width", 6),
            acceptance_shards=getattr(args, "acceptance_shards", 4),
            shard_log_dir=getattr(args, "shard_log_dir", None),
            baseline_failures=baseline_failures)
    except CanonicalError as e:
        return _emit({"tool": "canonical", "ok": False,
                      "summary": {"error": str(e), "canonical_error": True},
                      "failures": [{"reason": f"canonical_error: {e}"}]})
    out_path = _P(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return _emit({"tool": "canonical", "ok": report["ok"],
                  "summary": report,
                  "failures": ([] if report["ok"] else
                               [{"reason": "union invalid or arms invalid"}])})


def _diagnose_gd_targets(roots: list[Path]) -> list[str]:
    targets = sorted({str(p) for root in roots
                      for p in root.rglob("*.gd") if p.is_file()})
    return targets


def _diagnose_summary(report: dict) -> dict:
    return {
        "report_version": report["report_version"],
        "instrumentation": report["instrumentation"],
        "features": [
            {"key": f["key"],
             "baseline_wall_s": f["baseline"].get("wall_s"),
             "instrumented_wall_s": f["instrumented"].get("wall_s"),
             "instrumented_timed_out": f["instrumented"].get("timed_out"),
             "wait_commands": f["wait_commands"],
             "attribution": f["attribution"]}
            for f in report["features"]],
    }


def cmd_canonical_diagnose(args: argparse.Namespace) -> int:
    """§SPEC-048 diagnostic-only acceptance 计时模式。"""
    import json as _json

    from pathlib import Path as _P

    from .canonical.orchestrator import CanonicalError, run_canonical_diagnose
    from .canonical.recipe import verify_recipe

    recipe_path = _P(args.recipe)
    try:
        recipe = _json.loads(recipe_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return _emit({"tool": "canonical-diagnose", "ok": False,
                      "summary": {"error": f"recipe load failed: {e}"},
                      "failures": [{"reason": f"recipe load failed: {e}"}]})
    ok, reason = verify_recipe(recipe)
    if not ok:
        return _emit({"tool": "canonical-diagnose", "ok": False,
                      "summary": {"error": f"recipe invalid: {reason}"},
                      "failures": [{"reason": f"recipe invalid: {reason}"}]})
    targets = _diagnose_gd_targets([_P(r) for r in args.roots])
    if not targets:
        return _emit({"tool": "canonical-diagnose", "ok": False,
                      "summary": {"error": "no .gd targets under roots"},
                      "failures": [{"reason": "no .gd targets"}]})
    keys = [k.strip() for k in args.features.split(",") if k.strip()]
    try:
        report = run_canonical_diagnose(
            args.project_root, targets, recipe, keys,
            timeout_s=args.timeout, progress_out=args.out)
    except CanonicalError as e:
        return _emit({"tool": "canonical-diagnose", "ok": False,
                      "summary": {"error": str(e)},
                      "failures": [{"reason": str(e)}]})
    _P(args.out).write_text(
        _json.dumps(report, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    return _emit({"tool": "canonical-diagnose", "ok": True,
                  "summary": _diagnose_summary(report), "failures": []})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gqt", description="godot-qa-toolkit")
    sub = parser.add_subparsers(dest="command", required=True)

    can = sub.add_parser("canonical", help="dual-arm batch coverage orchestration (SEE-1546 WP3 PR-C)")
    can.add_argument("roots", nargs="+",
                     help="target .gd roots (recursive glob); arms run the "
                          "full gutconfig face + acceptance suite over them")
    can.add_argument("--project-root", required=True,
                     help="Godot project root (addons/gut + .gutconfig.json)")
    can.add_argument("--recipe", required=True,
                     help="canonical-recipe/1 JSON (from `gqt recipe --out`)")
    can.add_argument("--out", required=True,
                     help="canonical-report/1 output JSON path")
    can.add_argument("--baseline-failures", default=None,
                     help="JSON file with pre-existing (un-instrumented) "
                          "failing test names ([...] or {\"tests\": [...]}) "
                          "— new failures gate the arm (§SPEC-060a)")
    can.add_argument("--acceptance-probe-keys", nargs="?",
                     const="aidomain_integration", default=None,
                     help="W4 cost probe keys (default: aidomain_integration)")
    can.add_argument("--timeout", type=int, default=180,
                     help="GUT timeout seconds per arm (default 180)")
    can.add_argument("--stability-baseline", default=None,
                     help="prior canonical report path: fids whose covered "
                          "lines differ are routed to the unstable lane "
                          "(no score, §SPEC-056)")
    can.add_argument("--shard-width", type=int, default=6,
                     help="gut_full parallel shard count (1 = sequential, "
                          "§SPEC-052)")
    can.add_argument("--acceptance-shards", type=int, default=4,
                     help="acceptance parallel shard count")
    can.add_argument("--shard-log-dir", default=None,
                     help="retain per-shard full logs and raw sink copies "
                          "(§SPEC-056)")
    can.add_argument("--evidence-dir", default=None,
                     help="optional coverage-evidence/2 output dir for "
                          "per-arm bundles")
    can.set_defaults(func=cmd_canonical)

    ac = sub.add_parser("accept", help="Gherkin acceptance pipeline (SEE-1426 P2)")
    acsub = ac.add_subparsers(dest="accept_command", required=True)

    acc = acsub.add_parser("compile", help=".feature -> versioned IR + generated GUT tests")
    acc.add_argument("feature", nargs="?", default=None,
                     help="path to the .feature file (omit when --keys "
                          "selects the private-suite set, §SPEC-035)")
    acc.add_argument("--project-root", required=True,
                     help="target Godot project root (writes acceptance/ir + acceptance/generated)")
    acc.add_argument("--write", action="store_true",
                     help="opt-in: write ir/generated into the project (contract §6 "
                          "whitelist dirs; omitted = dry compile, summary only)")
    acc.add_argument("--no-clean", action="store_true",
                     help="keep existing generated artifacts instead of clean-then-generate")
    acc.add_argument("--keys", default=None,
                     help="§SPEC-035: comma-separated feature keys — "
                          "compile the selected set into a private suite "
                          "dir (dir = selection set); pairs with --out-dir, "
                          "or creates a fresh .gqt-run-<uuid> dir")
    acc.add_argument("--out-dir", default=None,
                     help="private suite dir to write into instead of "
                          "acceptance/ir + acceptance/generated")
    acc.set_defaults(func=cmd_accept)

    acr = acsub.add_parser("run", help="replay-check + headless-run the generated suite")
    acr.add_argument("directory", nargs="?", default=None,
                     help="alternative project root (default: --project-root)")
    acr.add_argument("--project-root", required=True,
                     help="target Godot project root containing addons/gut/")
    acr.add_argument("--key", default=None,
                     help="feature key the suite was compiled under "
                          "(required without --suite-dir/--acceptance-probe-keys)")
    acr.add_argument("--suite-dir", default=None,
                     help="§SPEC-035 private suite dir — run the whole dir "
                          "(dir = selection set) with acceptance-suite/1 "
                          "reconciliation")
    acr.add_argument("--acceptance-probe-keys", nargs="?",
                     const="aidomain_integration", default=None,
                     help="W4 cost probe: compile+run these comma-separated "
                          "keys via the private-dir pipeline and emit a "
                          "performance.acceptance_probe record (default: "
                          "aidomain_integration)")
    acr.add_argument("--timeout", type=int, default=120,
                     help="GUT timeout seconds (default 120)")
    _add_godot_bin_arg(acr)
    acr.set_defaults(func=cmd_accept)

    acm = acsub.add_parser("mutate", help="full-matrix contract mutation over the compiled IR")
    acm.add_argument("directory", nargs="?", default=None,
                     help="alternative project root (default: --project-root)")
    acm.add_argument("--project-root", required=True,
                     help="target Godot project root containing addons/gut/")
    acm.add_argument("--key", required=True,
                     help="feature key the suite was compiled under")
    acm.add_argument("--timeout", type=int, default=120,
                     help="GUT timeout seconds per mutant round (default 120)")
    acm.add_argument("--worker", action="store_true",
                     help="opt-in true resident-worker channel: the generated "
                          "accept_worker.gd artifact serves one godot process "
                          "for the whole matrix via stdin NDJSON + ir_file "
                          "transfer, with per-mutant hard discipline "
                          "(release+rebuild+smoke probe, failure = run_error) "
                          "and a per-round verdict timeout (SEE-1426 §SPEC-14)")
    _add_godot_bin_arg(acm)
    acm.set_defaults(func=cmd_accept)

    a = sub.add_parser("arch", help="Pure Core architecture guard over .gd files (SEE-1426 P1)")
    a.add_argument("paths", nargs="+", help=".gd files or directories to scan")
    a.set_defaults(func=cmd_arch)

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
    _add_godot_bin_arg(m)
    m.set_defaults(func=cmd_mutation)

    r = sub.add_parser(
        "risk",
        help="exploratory CRAP-Line shadow over selected files (diagnostic-only, SEE-1546 M2)",
    )
    r.add_argument(
        "files",
        nargs="+",
        help="candidate .gd files (eligible ones are measured; lambda/inner-class/"
        "var-def files route to the measurement-gap lanes)",
    )
    r.add_argument("--project-root", required=True, help="target Godot project root")
    r.add_argument(
        "--core-tests",
        default="res://tests/unit/",
        help="GUT dir for the core recipe (default res://tests/unit/)",
    )
    r.add_argument(
        "--full-tests",
        default=None,
        help="GUT dir for the full recipe (default: res://tests/ via .gutconfig dirs)",
    )
    r.add_argument(
        "--report",
        default="shadow-report.json",
        help="where to write the shadow report JSON (default ./shadow-report.json)",
    )
    r.add_argument(
        "--performance-file",
        default=None,
        help="optional JSON with measured complexity/coverage head-vs-master medians",
    )
    r.add_argument(
        "--skip-measure",
        action="store_true",
        help="static-only dry run: no GUT rounds executed",
    )
    _add_godot_bin_arg(r)
    r.set_defaults(func=cmd_risk)

    cd = sub.add_parser(
        "canonical-diagnose",
        help="diagnostic-only per-feature acceptance timing under flags "
             "instrumentation (§SPEC-048)",
    )
    cd.add_argument("roots", nargs="+",
                    help="directories whose .gd files are instrumented")
    cd.add_argument("--project-root", required=True,
                    help="target Godot project root")
    cd.add_argument("--recipe", required=True,
                    help="canonical recipe json (verify + feature lookup)")
    cd.add_argument("--features", required=True,
                    help="comma-separated acceptance feature keys")
    cd.add_argument("--out", required=True,
                    help="diagnostic report json path (progress written "
                         "after each feature)")
    cd.add_argument("--timeout", type=int, default=480,
                    help="per-leg GUT timeout seconds (default 480)")
    _add_godot_bin_arg(cd)
    cd.set_defaults(func=cmd_canonical_diagnose)

    p = sub.add_parser(
        "parity",
        help="full per-function CC parity over the neutral registry (SEE-1546 WP1b, diagnostic-only)",
    )
    p.add_argument(
        "paths",
        nargs="+",
        help="directories or .gd files to scan (same roots as the complexity gate)",
    )
    p.add_argument("--project-root", required=True, help="project root for relative fids")
    p.add_argument(
        "--report",
        default="parity.json",
        help="where to write the parity report JSON (default ./parity.json)",
    )
    p.add_argument(
        "--registry-out",
        default=None,
        help="optionally write the multi-file registry snapshot JSON",
    )
    p.set_defaults(func=cmd_parity)

    rg = sub.add_parser("registry", help="registry snapshot operations (SEE-1546 WP1b)")
    rgsub = rg.add_subparsers(dest="registry_command", required=True)
    rd = rgsub.add_parser("diff", help="diff two registry snapshots (func-registry-diff/1)")
    rd.add_argument("old", help="old snapshot JSON path")
    rd.add_argument("new", help="new snapshot JSON path")
    rd.add_argument("--out", default=None, help="optionally write the diff JSON")
    rd.set_defaults(func=cmd_registry)

    cr = sub.add_parser(
        "recipe",
        help="build the canonical full recipe (canonical-recipe/1, read-only, SEE-1546 WP3)",
    )
    cr.add_argument("--project-root", required=True, help="target Godot project root")
    cr.add_argument("--out", default="recipe.json",
                    help="where to write the recipe JSON (default ./recipe.json)")
    cr.add_argument("--exclude", action="append", default=[],
                    help="exclusion as scope:entry:reason (repeatable; reason required)")
    cr.set_defaults(func=cmd_recipe)

    v = sub.add_parser("coverage", help="line coverage of a single .gd file via GUT")
    v.add_argument("file", nargs="?", default=None,
                   help="the .gd file to measure (required unless --recover/--prune-sinks)")
    v.add_argument("--project-root", required=True, help="project root containing addons/gut/")
    v.add_argument("--min-percent", type=float, default=80.0,
                   help="min coverage percent (default 80)")
    v.add_argument("--timeout", type=int, default=120, help="GUT timeout seconds (default 120)")
    v.add_argument("--tests", default=None,
                   help="scoped GUT dir or single .gd path for per-suite attribution, "
                        "e.g. res://tests/unit/time/ (SEE-1367 §SPEC-017); omitted = full res://tests/")
    v.add_argument("--evidence-out", default=None,
                   help="§SPEC-025/026: write coverage-evidence/2 bundles + run records "
                        "here; omit for the legacy stdout-only contract (zero drift)")
    v.add_argument("--recover", default=None,
                   help="§SPEC-025: recover started-but-unfinished runs from this "
                        "evidence dir without re-running GUT")
    v.add_argument("--prune-sinks", action="store_true",
                   help="§SPEC-025/W1: delete qa-coverage-hits-* sinks older than "
                        "--retain-days (default 7) under user:// and project root")
    v.add_argument("--retain-days", type=int, default=7,
                   help="retention period for --prune-sinks (default 7)")
    _add_godot_bin_arg(v)
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
