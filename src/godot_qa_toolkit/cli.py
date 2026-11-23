"""Unified CLI for godot-qa-toolkit (SEE-1268 M1; SEE-1583 hierarchy).

Every subcommand emits the unified JSON contract:
    {"tool": ..., "ok": bool, "summary": {...}, "failures": [...]}

Exit code is 0 on ok, 1 on any failure — the machine verdict consumers rely on.
SEE-1583 groups the surface into four top-level entries — `gherkin`,
`mutation`, `measure` (all measurement/scoring/diagnostics faces) and
`doctor`. Old top-level entries are gone without aliases or deprecation
shims: unknown names exit 2 via argparse. SEE-1624 D-n3：旧 acceptance
前缀 flags 全量改 `--gherkin-*`，旧 CLI 别名不恢复，旧 flag exit 2。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .doctor import cmd_doctor
# complexity/coverage/mutation are imported lazily inside their cmd_* handlers:
# they pull in gdtoolkit, so a gdtoolkit-less environment (e.g. a minimal CI
# runner) must still start the CLI (§SPEC-114, SEE-1367).


def _godot_bin_ready(project_root: Path,
                     cmd: str | None = None) -> tuple[str, bool, str]:
    """§SPEC-075/076/078：解析 godot 启动命令并做同源启动校验（存在性+
    执行位+exec-format+shebang 解释器）——校验条件与子进程实际拉起条
    件一致。核心实现为 mutation.inject.godot_launch_issue（§SPEC-078 M3
    同源：measurement 与 mutation/measure coverage/gherkin 共用同一校验函数）。

    - 相对路径按子进程 cwd（= project_root）解析后校验（消除 CLI cwd
      与子进程 cwd 的漂移窗口，R32-M 形态 1）；
    - 裸名走继承 PATH 查找（与 subprocess 语义一致，§SPEC-077）；
    - 「有执行位但内容不可执行」及「#! 头解释器不可用」→ 拒绝
      （R32-M 形态 2 + §SPEC-078 M1——OS exec 会抛裸 traceback）。
    返回 (cmd, ok, reason)；reason 仅供诊断消息。cmd 参数供测试注入。"""
    from .mutation.inject import godot_command, godot_launch_issue

    if cmd is None:
        cmd = godot_command()
    _, reason = godot_launch_issue(str(project_root), cmd)
    return cmd, not reason, reason


def _emit_godot_unavailable(tool: str, cmd: str, reason: str = "") -> int:
    detail = f": {reason}" if reason else ""
    return _emit({"tool": tool, "ok": False,
                  "summary": {"error": f"godot binary not found or not "
                                       f"executable: {cmd}{detail} "
                                       f"(set GQT_GODOT_BIN or --godot-bin)"},
                  "failures": [{"reason": "godot binary unavailable"}]})


def _emit(result: dict) -> int:
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 1


def _lark_unexpected_input() -> "tuple[type, ...]":
    """lark 解析异常族（UnexpectedCharacters/UnexpectedToken 的公共基类）。

    延迟导入保持 §SPEC-114 最小 CI 面（gdtoolkit/lark 缺席环境不因本
    except 分支收集失败）。"""
    from lark.exceptions import UnexpectedInput
    return (UnexpectedInput,)


def _apply_godot_bin_or_contract(tool: str, args: argparse.Namespace) -> int | None:
    """§SPEC-1525-002：--godot-bin 命令级覆盖注入（> GQT_GODOT_BIN > PATH），
    显式配置错误 → 统一 JSON 错误契约（ok:false，与既有 BinaryConfigError
    出口同形态）。返回 None 表示继续派发；返回 int 为已完成的错误响应。

    修复轮 F-1/F-2（hardener 上报）：注入后立即 refresh 重判——(a) 显式 env
    非法路径在此早验证并转契约，先于 gherkin/measure coverage 的 GUT/重放等任何其他
    校验（不得被掩盖、不得裸 traceback，与 mutation 同形态）；(b) 同进程切换
    二进制后 binary kind 必须重钉，--path 形态与 timing 键成分取覆盖后真实
    类型（与 mutation 既有 refresh 语义对齐）。"""
    from .mutation.inject import BinaryConfigError, apply_godot_bin_override, \
        detect_binary_kind, resolve_env_godot_bin_relative

    try:
        # §SPEC-078 M2：flag/env 显式相对路径统一在入口按子进程 cwd
        # （project_root）解析校验——project-only 有效配置不得按 CLI
        # cwd 误拒；解析后的绝对路径写回 env 供后续链与子进程共用。
        # 无 project_root 的直调方（F-1/F-2 单元契约）保持原语义。
        project_root = getattr(args, "project_root", None)
        base_dir = str(project_root) if project_root is not None else None
        apply_godot_bin_override(getattr(args, "godot_bin", None),
                                 base_dir=base_dir)
        if base_dir is not None:
            resolve_env_godot_bin_relative(base_dir)
        detect_binary_kind(refresh=True)
    except BinaryConfigError as e:
        return _emit({
            "tool": tool,
            "ok": False,
            "summary": {"error": str(e)},
            "failures": [{"reason": str(e)}],
        })
    return None


def _resolve_project_root_or_contract(tool: str, args: argparse.Namespace,
                                      *, target: str | None = None) -> int | None:
    """§SPEC-019：统一项目根解析（显式优先 → 目标/CWD 向上发现）。

    成功时把解析结果写回 `args.project_root`（后续链路沿用字符串形态），
    失败时返回已发布的 JSON 错误码（fail-loud）。"""
    from .projectroot import ProjectRootError, resolve_project_root

    try:
        root = resolve_project_root(getattr(args, "project_root", None),
                                    target=target)
    except ProjectRootError as e:
        return _emit({"tool": tool, "ok": False,
                      "summary": {"error": str(e)},
                      "failures": [{"reason": str(e)}]})
    args.project_root = str(root)
    return None


def _bootstrap_import_or_contract(tool: str, args: argparse.Namespace) -> int | None:
    """零配置：新鲜检出缺 `.godot` 时自动导入一次（失败 fail-loud）。"""
    from .projectroot import ImportBootstrapError, ensure_import_cache

    try:
        ensure_import_cache(args.project_root,
                            godot_bin=getattr(args, "godot_bin", None))
    except ImportBootstrapError as e:
        return _emit({"tool": tool, "ok": False,
                      "summary": {"error": str(e)},
                      "failures": [{"reason": str(e)}]})
    return None


# `<file>#<lines>`：行级地址写法（file 必须以 .gd 结尾，spec 为单行/区间/多段）。
_FILE_LINES_RE = re.compile(r"^(?P<file>.+\.gd)#(?P<spec>[0-9][0-9,\-]*)$")


def _git_diff_targets(root: str) -> list[str]:
    """无位置参数时按 git diff 推导目标（§SPEC-020）。

    基准：HEAD（工作区相对 HEAD 的已跟踪改动，含暂存+未暂存）并上未跟踪的
    .gd 文件。非 Git 仓库 / git 失败 / 无改动 → ValueError（fail-loud）。"""
    def _git(*argv: str) -> subprocess.CompletedProcess:
        # 不递归子模块：KOL 等仓的 vendor 子模块常是未初始化/断链 gitlink，
        # 递归会 fatal: not a git repository 并让整轮 diff 失败。
        return subprocess.run(
            ["git", "-C", root, "-c", "submodule.recurse=false", *argv],
            capture_output=True, text=True)

    top = _git("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise ValueError(
            f"not a git repository under {root}; no-arg mutation requires git "
            f"diff (pass files explicitly)")
    changed = _git("diff", "--name-only", "--diff-filter=ACMR",
                   "--relative", "--ignore-submodules=all", "HEAD")
    if changed.returncode != 0:
        raise ValueError(
            f"git diff HEAD failed under {root}: {changed.stderr.strip()[:240]}")
    untracked = _git("ls-files", "--others", "--exclude-standard")
    names = [ln.strip() for ln in
             (changed.stdout + "\n" + untracked.stdout).splitlines() if ln.strip()]

    def _in_scope(name: str) -> bool:
        parts = name.split("/")
        # 排除隐藏目录（.qa-cache/.gqt-attr-select/.godot 等工具产物）与
        # vendored addons/——它们不是被测项目源码，纳入会污染默认目标集。
        if any(p.startswith(".") for p in parts):
            return False
        return not (parts and parts[0] == "addons")

    files: list[str] = []
    for name in sorted(dict.fromkeys(names)):
        if (name.endswith(".gd") and _in_scope(name)
                and (Path(root) / name).is_file() and name not in files):
            files.append(name)
    if not files:
        raise ValueError(
            "no changed .gd files from git diff HEAD (nothing to mutate); "
            "pass files explicitly")
    return files


def _normalize_mutation_targets(args: argparse.Namespace) -> int | None:
    """把 positionals 归一为单一 `<file>#<lines>` 卡尺或 git diff 目标集。

    - 形如 `<file>.gd#10-20,30` 的位置参数 → 单文件行级卡尺（与 `--lines` 冲突
      fail-loud；不可与普通文件混用）。
    - 无位置参数 → git diff 推导。
    返回 int 表示已发布错误，None 表示 `args.files` 已就绪。"""
    raw = list(getattr(args, "files", []) or [])
    addressed: list[tuple[str, str]] = []
    plain: list[str] = []
    for token in raw:
        m = _FILE_LINES_RE.match(token)
        (addressed.append((m.group("file"), m.group("spec")))
         if m else plain.append(token))
    if addressed:
        if len(addressed) > 1 or plain:
            msg = ("<file>#<lines> addresses a single file and cannot be mixed "
                   "with other targets")
            return _emit({"tool": "mutation", "ok": False,
                          "summary": {"error": msg},
                          "failures": [{"reason": msg}]})
        file_path, spec = addressed[0]
        if getattr(args, "lines", None) and args.lines != spec:
            msg = ("conflicting line selection: <file>#<lines> and --lines differ")
            return _emit({"tool": "mutation", "ok": False,
                          "summary": {"error": msg},
                          "failures": [{"reason": msg}]})
        args.lines = spec
        args.files = [file_path]
        return None
    if plain:
        args.files = plain
        return None
    try:
        args.files = _git_diff_targets(args.project_root)
    except ValueError as e:
        return _emit({"tool": "mutation", "ok": False,
                      "summary": {"error": str(e)},
                      "failures": [{"reason": str(e)}]})
    return None


def _add_godot_bin_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--godot-bin", default=None,
                        help="per-command Godot binary override; priority: "
                             "this flag > GQT_GODOT_BIN env > PATH (SEE-1525)")


def _gherkin_compile(args: argparse.Namespace) -> int:
    from pathlib import Path as _P

    from .gherkin.compile import compile_feature
    from .gherkin.suite import (SuiteError, compile_keys_to_suite,
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
                "tool": "gherkin", "ok": False,
                "summary": {"error": str(e), "suite_error": True},
                "failures": [{"reason": f"suite_error: {e}"}],
            })
        return _emit({
            "tool": "gherkin", "ok": True,
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
            "tool": "gherkin", "ok": False,
            "summary": {"error": "gherkin compile needs <feature> or --keys"},
            "failures": [{"reason": "gherkin compile needs <feature> or "
                                    "--keys"}],
        })
    suite, error = compile_feature(
        args.feature, args.project_root,
        write=args.write, clean=not args.no_clean, out_dir=out_dir)
    if error is not None:
        return _emit({
            "tool": "gherkin", "ok": False,
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
                f"{base}/generated/gherkin_worker.gd",
                f"{base}/generated/{suite.feature_key}.manifest.json",
            ]
        else:
            wrote = [
                f"gherkin/ir/{suite.feature_key}.json",
                f"gherkin/generated/test_{suite.feature_key}.gd",
                "gherkin/generated/gherkin_worker.gd",
                f"gherkin/generated/{suite.feature_key}.manifest.json",
            ]
    return _emit({
        "tool": "gherkin", "ok": True,
        "summary": {
            "feature": suite.ir["feature"],
            "feature_key": suite.feature_key,
            "cases": len(suite.ir["cases"]),
            "ir_hash": suite.manifest["ir_hash"],
            "wrote": wrote,
        },
        "failures": [],
    })


def cmd_gherkin(args: argparse.Namespace) -> int:
    from .gherkin.run import GherkinRunError, run_suite
    from .gherkin.suite import SuiteError, run_gherkin_probe, run_suite_dir

    if args.gherkin_command == "compile":
        return _gherkin_compile(args)
    # run / mutate
    early = _apply_godot_bin_or_contract("gherkin", args)
    if early is not None:
        return early
    # §SPEC-079 B：启动校验不在入口静态拦截（无 godot CI 形态的 mocked
    # 用例不得在 mock 生效前被打回），真实拉起点的 spawn_godot 统一把
    # 内核级启动失败转 BinaryConfigError，由下方领域 JSON 出口收口。
    from .mutation.inject import BinaryConfigError
    probe_keys = getattr(args, "gherkin_probe_keys", None)
    suite_dir_arg = getattr(args, "suite_dir", None)
    if args.gherkin_command == "run" and (probe_keys or suite_dir_arg):
        if probe_keys and suite_dir_arg:
            return _emit({
                "tool": "gherkin", "ok": False,
                "summary": {"error": "--gherkin-probe-keys compiles its "
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
                        "tool": "gherkin", "ok": False,
                        "summary": {"error": "no keys parsed"},
                        "failures": [{"reason": "no keys parsed"}],
                    })
                probe = run_gherkin_probe(args.project_root, keys,
                                          timeout_s=args.timeout)
                return _emit({"tool": "gherkin", "ok": probe["suite_ok"],
                              "summary": probe, "failures": []})
            record = run_suite_dir(args.project_root, suite_dir_arg,
                                   timeout_s=args.timeout)
            return _emit({"tool": "gherkin", "ok": record["ok"],
                          "summary": record, "failures": []})
        except BinaryConfigError as e:
            return _emit({
                "tool": "gherkin", "ok": False,
                "summary": {"error": str(e)},
                "failures": [{"reason": str(e)}],
            })
        except GherkinRunError as e:
            return _emit({
                "tool": "gherkin", "ok": False,
                "summary": {"error": str(e), "run_error": True},
                "failures": [{"reason": f"run_error: {e}"}],
            })
        except SuiteError as e:
            return _emit({
                "tool": "gherkin", "ok": False,
                "summary": {"error": str(e), "suite_error": True},
                "failures": [{"reason": f"suite_error: {e}"}],
            })
    if not args.key:
        return _emit({
            "tool": "gherkin", "ok": False,
            "summary": {"error": "--key is required without "
                                 "--suite-dir/--gherkin-probe-keys"},
            "failures": [{"reason": "--key is required"}],
        })
    try:
        if args.gherkin_command == "mutate":
            from .gherkin.mutate_runner import run_mutate
            return _emit(run_mutate(
                args.directory if args.directory else args.project_root,
                args.key, timeout_s=args.timeout, use_worker=args.worker))
        return _emit(run_suite(args.directory if args.directory
                               else args.project_root, args.key,
                               timeout_s=args.timeout))
    except BinaryConfigError as e:
        return _emit({
            "tool": "gherkin", "ok": False,
            "summary": {"error": str(e)},
            "failures": [{"reason": str(e)}],
        })
    except GherkinRunError as e:
        return _emit({
            "tool": "gherkin", "ok": False,
            "summary": {"error": str(e), "run_error": True},
            "failures": [{"reason": f"run_error: {e}"}],
        })


def cmd_measure_arch(args: argparse.Namespace) -> int:
    # gdtoolkit 依赖延迟导入（同 complexity/mutation——§SPEC-114 最小 CI 面）。
    from .arch.gate import run_arch_gate

    return _emit(run_arch_gate(args.paths))


def cmd_measure_complexity(args: argparse.Namespace) -> int:
    from .complexity.gate import GateConfig, run_gate

    # When --max is lowered below --warn, warn collapses with it: an explicit
    # strict max means "everything above max fails", nothing merely warns.
    warn = min(args.warn, args.max)
    config = GateConfig(warn_complexity=warn, max_complexity=args.max)
    return _emit(run_gate(args.paths, config))


def _mutation_target_hint(args: argparse.Namespace) -> "str | None":
    raw_files = list(getattr(args, "files", []) or [])
    if not raw_files:
        return None
    m = _FILE_LINES_RE.match(raw_files[0])
    return m.group("file") if m else raw_files[0]


def _apply_mutation_map_flags(args: argparse.Namespace, truth_mod) -> None:
    """映射相关 env 单点注入（§SPEC-ARGMERGE 三态 + 兼容别名）。

    `--attribution-map auto|off|<path>` 三态：auto（默认，env 语义不变）、
    off（关闭自动生成/续新，无可用映射 fail-loud）、<path>（显式产物只读
    透传）。`--no-attribution-auto` 保留为 off 的兼容别名；env
    GQT_ATTRIBUTION_MAP_MODE 语义不变，CLI 显式值覆盖 env。"""
    import os as _os
    map_arg = getattr(args, "attribution_map", None)
    if map_arg is not None:
        if map_arg == "auto":
            _os.environ.pop(truth_mod.ENV_MAP, None)
            _os.environ.pop(truth_mod.ENV_MODE, None)
        elif map_arg == "off":
            _os.environ.pop(truth_mod.ENV_MAP, None)
            _os.environ[truth_mod.ENV_MODE] = "off"
        else:
            _os.environ[truth_mod.ENV_MAP] = str(map_arg)
            _os.environ.pop(truth_mod.ENV_MODE, None)
    # 兼容别名仅在主旋钮未显式给定时生效（--attribution-map 为新主旋钮）
    if getattr(args, "attribution_auto", True) is False \
            and map_arg is None:
        _os.environ.pop(truth_mod.ENV_MAP, None)
        _os.environ[truth_mod.ENV_MODE] = "off"


def _p1_prepare_gate(args: argparse.Namespace, plan: dict) -> "int | None":
    """§SPEC-PREREQ-MATRIX：P1 prepare-blockers 默认停（一次列全 +
    修复命令/成本）；--auto-prepare 续跑；--select full 不需要 map；
    目标不存在时交由下游原错误契约。SEE-1623 M1：显式 `--tests` 用户
    接管测试面——映射前置对显式面不适用（此前 prepare_required 阻断，
    显式面接管语义名存实亡）。"""
    from .source_io import anchor_to_project_root as _anchor
    p1 = [p for p in plan["problems"]
          if p.get("level") == "P1" and p.get("severity") == "warning"]
    if not p1:
        return None
    if getattr(args, "dry_run", False) or getattr(args, "auto_prepare",
                                                  False):
        return None
    if getattr(args, "select_mode", "line") == "full":
        return None
    if getattr(args, "tests", None):
        return None
    files = list(getattr(args, "files", []) or [])
    if not all(Path(_anchor(f, args.project_root)).is_file() for f in files):
        return None
    # SEE-1623 M6：显式 --attribution-map 只读、绝不重建——修复提示不得
    # 与该契约相反（此前一律建议 --auto-prepare，Harti 障碍4）。
    _map_arg = getattr(args, "attribution_map", None)
    if _map_arg and str(_map_arg) not in ("auto", "off"):
        _notice = ("preconditions missing — the explicit attribution map "
                   f"{_map_arg} is read-only and will never be rebuilt: "
                   "fix or replace the map file, or omit --attribution-map "
                   "to use the auto-managed map")
    else:
        _notice = ("preconditions missing — fix the listed items or rerun "
                   "with --auto-prepare to build them automatically")
    # SEE-1623 M7：信封归一——plan 只在 summary（顶层 error/plan 键撤销），
    # failures 恒在场（§SPEC-1312 flat 契约：{tool, ok, summary, failures}）。
    return _emit({"tool": "mutation", "ok": False,
                  "summary": {"error": "prepare_required", "plan": plan,
                              "problems": p1,
                              "notice": _notice},
                  "failures": [{"reason": f"{p['code']}: prepare blocker "
                                f"({p.get('level')})"} for p in p1]})


def _plan_gate_mutation(args: argparse.Namespace) -> "dict | None":
    """§SPEC-PLANFIRST：默认 run 的 plan 段（静态穷查 ≤10s）。

    返回早退 payload（plan_blocked / dry-run plan-only）；None = 继续。
    """
    from .mutation.plancheck import static_plan

    plan = static_plan(args.project_root,
                       args.files[0] if args.files else None, args.tests)
    errors = [p for p in plan["problems"] if p["severity"] == "error"]
    print(f"mutation: plan {len(plan['problems'])} problem(s) "
          f"({len(errors)} error) in {plan['elapsed_s']}s",
          file=sys.stderr)
    if errors:
        # SEE-1623 M7：信封归一（plan 只在 summary，顶层键撤销）。
        return _emit({"tool": "mutation", "ok": False,
                      "summary": {"error": "plan_blocked", "plan": plan},
                      "failures": [{"reason": f"{p['code']}: "
                                    f"{json.dumps(p['evidence'],
                                                 ensure_ascii=False)}"}
                                   for p in errors]})
    # §SPEC-PREREQ-MATRIX：P1 prepare-blockers（map 缺失/stale、import
    # 缓存缺失）默认停并一次列全问题 + 修复命令与成本；--auto-prepare
    # 时自动补齐续跑；--select full 不需要 map 直接继续
    p1_early = _p1_prepare_gate(args, plan)
    if p1_early is not None:
        return p1_early
    if getattr(args, "dry_run", False) \
            and not getattr(args, "attribution_map", None) \
            and getattr(args, "select_mode", "line") != "full" and any(
                p["code"] == "attribution_map_missing_or_stale"
                for p in plan["problems"]):
        # SEE-1623 M7：信封归一（plan 只在 summary，failures 恒在场）。
        return _emit({"tool": "mutation", "ok": True,
                      "summary": {"plan": plan,
                                  "selection_mode": "plan_only",
                                  "static_preconditions": {
                                      "baseline_health":
                                          _plan_only_baseline_health(args)},
                                  "notice": "no usable attribution map — "
                                            "dry-run returned the static "
                                            "plan only; run without "
                                            "--dry-run to prepare the map, "
                                            "or pass --select full to "
                                            "preview sites now"},
                      "failures": []})
    return None


def _plan_only_baseline_health(args: argparse.Namespace) -> dict:
    """SEE-1623 M3：--check-baseline 在 plan-only 早退形态同样执行
    （一次未插桩基线 GUT；未请求 → not_checked，与 _core_dry_run 同源）。"""
    from .mutation.runner import _baseline_health_precondition
    return _baseline_health_precondition(
        str(args.project_root), int(getattr(args, "timeout", 600) or 600))


def _mutation_numeric_arg_error(args: argparse.Namespace) -> str | None:
    """SEE-1623 M4：数值参数下限前置校验（§5 用法错误 exit 2）。

    budget/jobs/timeout 下限 1（此前 0/负数静默假绿或瞬间超时）；
    `--lines` 形态在解析层校验（此前非法形态泄露 Python int() 转换
    异常）。返回 None = 合法。"""
    if getattr(args, "budget", None) is not None and args.budget < 1:
        return f"--budget must be >= 1 (got {args.budget})"
    if getattr(args, "jobs", None) is not None and args.jobs < 1:
        return f"--jobs must be >= 1 (got {args.jobs})"
    if getattr(args, "timeout", None) is not None and args.timeout < 1:
        return f"--timeout must be >= 1 (got {args.timeout})"
    spec = getattr(args, "lines", None)
    if spec:
        from .mutation.truth_lines import parse_lines_spec
        try:
            parse_lines_spec(spec)
        except ValueError as e:
            return f"--lines {spec!r}: {e}"
    return None


def _measure_timeout_arg_error(args: argparse.Namespace) -> str | None:
    """SEE-1623 M4：measure 面 --timeout 下限（Refacty L9：负值被接受，
    两臂瞬间超时）。"""
    if getattr(args, "timeout", None) is not None and args.timeout < 1:
        return f"--timeout must be >= 1 (got {args.timeout})"
    return None


def _usage_exit(message: str) -> int:
    """§5 用法/装载错误档：stderr 可读提示，stdout 无 JSON，exit 2。"""
    print(f"gqt: usage error — {message}", file=sys.stderr)
    return 2


def _prepare_mapping_face(args: argparse.Namespace, _truth) -> "int | None":
    """默认映射面的运行前准备链（非 dry-run 且 full-face / 显式 --tests
    接管面以外）：单文件目标存在性校验 → 自动导入 → ensure_map。

    A20（SEE-1594 S5）：显式 `--select full` 是全脸审计——不需要映射，不得
    被无关映射的生成/校验失败阻断（需要 prefilter 的 line/fn/file 默认路径
    仍自动生成/复用新鲜映射；不可用 fail-loud）。
    SEE-1623 M1：显式 `--tests` 同理——用户接管测试面，映射非必需，不触发
    ensure_map（prepare_required/映射构建都不该拦显式面）。

    返回 rc 非 None 即终止（调用方原样返回）。"""
    if getattr(args, "dry_run", False):
        return None
    full_face = getattr(args, "select_mode", "line") == "full"
    if full_face or getattr(args, "tests", None):
        return None
    # SEE-1623 M2：单文件目标存在性校验前于映射构建/prepare 链
    # （BUG-6：不存在的目标此前先报无关 acceptance/.gutconfig 错误）。
    # 多文件收集失败契约（SEE-1321 LOW-6：缺失文件以 per-file error 进
    # results）不在此拦——处理仍在 dispatch 层。
    from .mutation.truth_select import MutationSelectionError
    from .source_io import anchor_to_project_root as _anchor_t
    _files = getattr(args, "files", None) or []
    _t = _files[0] if len(_files) == 1 else None
    if _t is not None:
        _anchored = Path(_anchor_t(_t, args.project_root))
        if not _anchored.is_file():
            _msg = (f"mutation target not found: {_t} (anchored: "
                    f"{_anchored})")
            return _emit({"tool": "mutation", "ok": False,
                          "summary": {"files": args.files,
                                      "error": _msg},
                          "failures": [{"reason": _msg}]})
    # 零配置：新鲜检出缺 `.godot` 时先自动导入一次，再建映射（fail-loud）。
    rc = _bootstrap_import_or_contract("mutation", args)
    if rc is not None:
        return rc
    # SEE-1581（Owner 定稿）：默认自动生成/复用新鲜映射；不可用 fail-loud
    try:
        _truth.ensure_map(str(Path(args.project_root)),
                          [str(f) for f in args.files])
    except MutationSelectionError as e:
        return _selection_error_payload(args, e)
    return None


def cmd_mutation(args: argparse.Namespace) -> int:
    from .mutation.inject import detect_binary_kind

    # SEE-1623 M4：数值参数校验前置（budget 0/负数此前静默假绿、负
    # timeout 瞬间超时、--lines 非法形态泄露 Python 异常）——dry-run 与
    # 真实运行同样即拒（§SPEC-002 非法参数 dry-run 即拒同源达成）。
    _num_err = _mutation_numeric_arg_error(args)
    if _num_err is not None:
        return _usage_exit(_num_err)
    # SEE-1623 M3：--check-baseline 单点 env 提前到 plan 门之前——此前
    # 只在 serial 路径设置，dry-run plan-only 早退形态从未执行检查
    # （Harti BUG-6：承诺的基线检查零发生）。
    if getattr(args, "check_baseline", False):
        os.environ["GQT_MUTATION_CHECK_BASELINE"] = "1"

    # §SPEC-1525-002 + 修复轮 F-1/F-2：覆盖注入与二进制早验证/重判统一在
    # _apply_godot_bin_or_contract（三子命令同源同形态）——此处取已重钉的
    # 判定结果即可（平台分支与 key 二进制成分共用判定）。
    # 错误优先级保持既有：显式 godot-bin 非法先于项目根校验报出。
    early = _apply_godot_bin_or_contract("mutation", args)
    if early is not None:
        return early

    # §SPEC-019：再把显式/自动发现的项目根落定；目标锚定与 git diff 发现
    # 都以它为准（target 供发现时向上找 project.godot）。
    rc = _resolve_project_root_or_contract(
        "mutation", args, target=_mutation_target_hint(args))
    if rc is not None:
        return rc
    rc = _normalize_mutation_targets(args)
    if rc is not None:
        return rc
    # §SPEC-079 B：入口不做静态启动拦截（mocked 用例/--dry-run 离线路径
    # 均不受影响）；真实拉起点 spawn_godot 的 BinaryConfigError 由下方
    # 领域 JSON 出口收口。
    from .mutation.inject import BinaryConfigError
    from .mutation.truth_select import MutationSelectionError
    from .mutation import truth_select as _truth
    _truth.reset_ledger()
    _apply_mutation_map_flags(args, _truth)
    # §SPEC-PLANFIRST：默认 run 的第一步 = plan（静态穷查 ≤10s，先于
    # import/映射构建等任何贵操作）——error 级问题即停并列问题；dry-run
    # 永不要求 map：无/旧映射时成功返回 plan 全量（problems[] 含 warning）
    # 与成本预估，不进入需要映射的选面解析。
    _plan_early = _plan_gate_mutation(args)
    if _plan_early is not None:
        return _plan_early
    rc = _prepare_mapping_face(args, _truth)
    if rc is not None:
        return rc
    binary_kind = detect_binary_kind()
    try:
        result = _dispatch_mutation(args, binary_kind)
    except MutationSelectionError as e:
        return _selection_error_payload(args, e)
    except BinaryConfigError as e:
        return _emit({"tool": "mutation", "ok": False,
                      "summary": {"error": str(e)},
                      "failures": [{"reason": str(e)}]})
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
    led = _truth.ledger()
    if led["events"]:
        result.setdefault("summary", {})["attribution"] = led
    return _emit(result)


def _selection_error_payload(args: argparse.Namespace, e) -> int:
    """选面不可用 → 领域 JSON；呈现根因优先（启动/预导入守卫与 master 同源）。"""
    reason = str(e)
    # 顺序与 master 一致：预导入守卫先于启动校验（缺 godot 时用户最先该
    # 看到的也是"先 --import"这一 master O-1 口径）
    try:
        from .mutation.shadow import require_import_cache
        require_import_cache(args.project_root)
    except Exception as ge:  # noqa: BLE001 — 预导入守卫原因优先
        reason = str(ge)
    else:
        _cmd, _ok, _launch = _godot_bin_ready(Path(args.project_root))
        if not _ok and _launch:
            reason = f"godot binary unusable: {_launch}"
    return _emit({"tool": "mutation", "ok": False,
                  "summary": {"error": reason,
                              "selection_error": True,
                              "selection_reason": getattr(e, "reason", None)},
                  "failures": [{"reason": reason}]})


def _kill_bail_flag(args: argparse.Namespace) -> bool | None:
    """SEE-1606 P3（§SPEC-P3-T1 零行为拆分）：无 `--no-kill-bail` 时交由
    env `GQT_MUTATION_KILL_BAIL` 决定（None）；显式关断为 False（env 逃生
    门仍可叠加）。取值语义与拆分前逐位一致。"""
    return None if not getattr(args, "no_kill_bail", False) else False


def _baseline_break_flag(args: argparse.Namespace) -> bool:
    """SEE-1623 M3：`--fail-on-red-baseline` 与 break-on-baseline-failure
    通道合流（红基线快速失败）——编排与串行两条路径的单一出处。"""
    return (getattr(args, "break_on_baseline_failure", False)
            or getattr(args, "fail_on_red_baseline", False))


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
    # SEE-1587：--lines 规格在此早解析（非法规格 = 参数错误契约，先于任何
    # GUT/预热）；解析结果随串行/编排路径下发。
    _ = _parsed_select_lines(args)
    # SEE-1413：--jobs 缺省时单文件同样走并发切片路径（默认并行）；显式
    # --jobs 1 在 executor 内强制串行。串行快路径仅留给显式串行请求。
    multi_concurrency = (len(files) > 1 or args.jobs is None or args.jobs > 1)
    if selecting:
        multi_concurrency = False
    # SEE-1587：行级（默认）/fn 分组需要每文件多 chunk（per-group glob）——
    # 除非显式 --tests/--jobs 1 的卡尺语义，一律走编排路径。
    if getattr(args, "select_mode", "line") in ("line", "fn") and not selecting \
            and not args.tests:
        multi_concurrency = True
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
        tests_glob=args.tests, jobs=args.jobs,
        binary_kind=binary_kind, timing_store=timing_store,
        mutation_warning=args.mutation_warning,
        timeout_factor=args.timeout_factor,
        mutate_all=args.mutate_all,
        reuse_coverage=_reuse_coverage_flag(args),
        worker_channel=True if getattr(args, "worker_channel", False) else None,
        select_mode=getattr(args, "select_mode", "line"),
        break_on_baseline_failure=_baseline_break_flag(args),
        kill_bail=_kill_bail_flag(args))
    if len(files) == 1:
        # SEE-1589 A-M3：单文件对外恒为 flat CLI 契约（--jobs 1 不得套用
        # 多文件 results[] 信封）；信封仅限多文件编排层。
        envelope = result
        result = envelope["results"][0]
        result["summary"].setdefault("jobs", envelope["jobs"])
    return result


def _parsed_select_lines(args: argparse.Namespace) -> list[int] | None:
    """`--lines` 规格 → 去重升序行号列表（None = 未指定）。

    非法规格抛 ValueError（cmd_mutation 转参数错误契约 ok=false）。"""
    raw = getattr(args, "lines", None)
    if raw is None:
        return None
    from .mutation.truth_select import parse_lines_spec
    return parse_lines_spec(raw)


def _run_mutation_serial(args: argparse.Namespace, binary_kind: str) -> dict:
    from .mutation.runner import run_mutation
    from .timing import TimingStore

    # SEE-1356 L1：SPEC-009 dry-run 跳过 timing store 读写。
    timing_store = None
    if getattr(args, "check_baseline", False):
        os.environ["GQT_MUTATION_CHECK_BASELINE"] = "1"
    if getattr(args, "fail_on_red_baseline", False):
        os.environ["GQT_MEASUREMENT_FAIL_ON_RED_BASELINE"] = "1"
    if getattr(args, "intolerant_tests", None):
        os.environ["GQT_MEASUREMENT_INTOLERANT_TESTS"] = \
            str(args.intolerant_tests)
    if getattr(args, "auto_prepare", False):
        os.environ["GQT_MUTATION_AUTO_PREPARE"] = "1"
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
        binary_kind=binary_kind,
        mutation_warning=args.mutation_warning,
        timeout_factor=args.timeout_factor,
        mutate_all=args.mutate_all,
        select_ids=([args.mutation] if getattr(args, "mutation", None)
                    else None),
        select_lines=_parsed_select_lines(args),
        reuse_coverage=_reuse_coverage_flag(args),
        worker_channel=True if getattr(args, "worker_channel", False) else None,
        # §SPEC-012：串行/卡尺与编排路径同源遵守 --select（SEE-1587 默认
        # line），不可静默退化全脸（HIGH-1 修复点）。
        select_mode=getattr(args, "select_mode", "line"),
        break_on_baseline_failure=_baseline_break_flag(args),
        kill_bail=_kill_bail_flag(args),
    )
    result["summary"].setdefault("jobs", 1)
    return result


def _reuse_coverage_flag(args: argparse.Namespace) -> bool:
    """SEE-1550 L1：coverage prefilter 默认自动启用；--no-reuse-coverage
    显式关断。旧 --reuse-coverage 旗标已随 A13（SEE-1594 S5）物理退役
    （不再注册，传参即 argparse exit 2）——prefilter 无旗标即默认开。"""
    return not getattr(args, "no_reuse_coverage", False)


def _load_performance_input(args: argparse.Namespace) -> tuple:
    """SEE-1623 H6：坏 JSON / 不可读 performance 文件转结构化信封——原始
    JSONDecodeError/OSError traceback 即工具崩溃形态（§5 exit 1 判定失败；
    stdout 恒有 JSON）。

    返回 (error_payload, performance_input)：error_payload 非 None 即调用方
    `_emit` 后终止；未请求 performance 文件时两者皆 None。"""
    if not args.performance_file:
        return None, None
    try:
        return None, json.loads(
            Path(args.performance_file).read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError) as e:
        return ({
            "tool": "measure.risk",
            "ok": False,
            "summary": {
                "error": (f"performance file not usable: "
                          f"{args.performance_file}"),
                "reason": "performance_file_unparseable",
                "reason_detail": f"{type(e).__name__}: {e}",
                "suggested_action": (
                    "pass a JSON file written by a prior measure run "
                    "(performance block); check the path is readable"),
            },
            "failures": [{"reason": "performance_file_unparseable: "
                                    f"{type(e).__name__}: {e}"}],
        }), None


def cmd_measure_risk(args: argparse.Namespace) -> int:
    from .risk.shadow import run_shadow_from_paths

    early = _apply_godot_bin_or_contract("measure.risk", args)
    if early is not None:
        return early
    rc = _resolve_project_root_or_contract(
        "measure.risk", args, target=(args.files[0] if args.files else None))
    if rc is not None:
        return rc
    # §SPEC-019/021：与 mutation/coverage 同口径——相对路径锚定项目根，
    # 不再按调用方 CWD 解析（同一写法在不同 CWD 下结果一致）。
    root = Path(args.project_root)
    paths = [(Path(p) if Path(p).is_absolute() else root / p)
             for p in args.files]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        return _emit(
            {
                "tool": "measure.risk",
                "ok": False,
                "summary": {"error": f"target files not found: {missing}"},
                "failures": [{"reason": f"target files not found: {missing}"}],
            }
        )
    _perf_err, performance_input = _load_performance_input(args)
    if _perf_err is not None:
        return _emit(_perf_err)
    # SEE-1605 §SPEC-001：首触 auto 生成须一次覆盖全部请求文件的首层目录——
    # 车道内逐文件 ensure_map 只绑定当前文件目录，跨目录文件会退化为逐车道
    # 增量（virgin 上即 QA 实测的静默落空）。预温生成失败 = 本轮测量面不可
    # 用，具名中止（台账已留痕），不带病进入车道循环。
    if not args.skip_measure:
        from .mutation.truth_select import MutationSelectionError, ensure_map
        try:
            ensure_map(args.project_root, [str(p) for p in paths])
        except MutationSelectionError as e:
            # §SPEC-009：具名原因同步走 stderr 披露，不得以空 stderr 仅留
            # 机器信封（stdout JSON 契约不变）。
            msg = (f"attribution map auto-generation failed: {e}; "
                   "aborting before measurement lanes")
            print(msg, file=sys.stderr)
            return _emit({"tool": "measure.risk", "ok": False,
                          "summary": {"error": msg},
                          "failures": [{"reason": msg}]})
    report = run_shadow_from_paths(
        paths=paths,
        project_root=args.project_root,
        core_tests=args.core_tests,
        full_tests=args.full_tests,
        skip_measure=args.skip_measure,
        performance_input=performance_input,
    )
    report_path = Path(args.report)
    try:
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    except OSError as e:
        # §SPEC-021：可选 JSON 文件报告写入失败必须非零披露（不静默/不裸
        # traceback）；stdout 契约不变，仅把失败转成统一信封。
        msg = f"report write failed: {report_path}: {e}"
        return _emit({"tool": "measure.risk", "ok": False,
                      "summary": {"error": msg},
                      "failures": [{"reason": msg}]})
    # SEE-1605 §SPEC-002：auto 生成/重建失败时禁止假 `ok:true`——报告仍
    # 落盘供诊断，但 CLI 信封具名失败并退出非零。台账是生成是否成功的唯一
    # 出处（显式 map/mode=off/warmed 复用不产生 generate/rebuild 事件，
    # skip-measure 不消费映射，均不受影响）。
    if not args.skip_measure:
        from .mutation.truth_select import ledger
        gen_error = next(
            (e.get("error") or "attribution map auto-generation failed"
             for e in ledger()["events"]
             if e.get("action") in ("generate", "rebuild")
             and not e.get("ok", False)),
            None)
        if gen_error is not None:
            # §SPEC-009：同上——运行后台账翻转出口的 stderr 同步披露。
            msg = (f"attribution map auto-generation failed: {gen_error}; "
                   f"coverage lanes are run_error — see {report_path}")
            print(msg, file=sys.stderr)
            return _emit({"tool": "measure.risk", "ok": False,
                          "summary": {"error": msg,
                                      "report": str(report_path)},
                          "failures": [{"reason": msg}]})
    scored = report["ranking"]["top"]
    return _emit(
        {
            "tool": "measure.risk",
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


def cmd_measure_parity(args: argparse.Namespace) -> int:
    from .complexity.parity import parity_paths
    from .registry.snapshot import save_snapshot

    rc = _resolve_project_root_or_contract(
        "measure.parity", args, target=(args.paths[0] if args.paths else None))
    if rc is not None:
        return rc
    paths = [Path(p) for p in args.paths]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        return _emit(
            {
                "tool": "measure.parity",
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


def cmd_measure_registry_diff(args: argparse.Namespace) -> int:
    from .registry.snapshot import diff_registries, load_snapshot, save_snapshot

    try:
        old = load_snapshot(args.old)
        new = load_snapshot(args.new)
    except (OSError, ValueError) as e:
        # json.JSONDecodeError is a ValueError subclass: unparseable or
        # missing snapshots fail loud with the unified contract (ok=false)
        return _emit(
            {
                "tool": "measure.registry-diff",
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


def cmd_measure_suite(args: argparse.Namespace) -> int:
    from .measurement.recipe import build_recipe

    rc = _resolve_project_root_or_contract("measure.suite", args)
    if rc is not None:
        return rc
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
                "tool": "measure.suite",
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
            "tool": "measure.suite",
            "ok": False,
            "summary": {"error": error},
            "failures": [{"reason": error}],
        })
    out_path = Path(args.out)
    out_path.write_text(
        json.dumps(recipe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return _emit({
        "tool": "measure.suite",
        "ok": True,
        "summary": {
            "recipe_version": recipe["recipe_version"],
            "recipe_hash": recipe["recipe_hash"],
            "out": str(out_path),
            "gut_dirs": len(recipe["gut_face"]["dirs"]),
            "gherkin_features": recipe["gherkin_face"]["count"],
            "exclusions": len(recipe["exclusions"]),
        },
        "failures": [],
    })


def cmd_measure_coverage(args: argparse.Namespace) -> int:
    from .coverage.runner import run_coverage

    # §SPEC-1525-001/002：godot-bin 覆盖先行——映射构建/增量仍需要 Godot
    # （新鲜投影不启动进程时可无执行成本，但构建阶段必须能解析二进制）。
    early = _apply_godot_bin_or_contract("measure.coverage", args)
    if early is not None:
        return early
    rc = _resolve_project_root_or_contract("measure.coverage", args,
                                           target=getattr(args, "file", None))
    if rc is not None:
        return rc
    from .coverage.evidence import prune_sinks as _prune
    from pathlib import Path as _P

    if not getattr(args, "prune_sinks", None) and not args.file:
        return _emit({
            "tool": "measure.coverage",
            "ok": False,
            "summary": {"error": "measure mode requires a target file "
                               "(or --prune-sinks)"},
            "failures": [{"reason": "measure mode requires a target file"}],
        })
    if getattr(args, "prune_sinks", None):
        # --prune-sinks 仅清理旧文件，绝不产生覆盖答案（§SPEC-027 A16）。
        result = _prune(args.project_root,
                        retention_days=getattr(args, "retain_days", 7))
        result.update({"tool": "measure.coverage", "ok": True})
        return _emit(result)
    from .mutation.inject import BinaryConfigError
    # 缺 godot 时不预导入（新鲜投影不需要）；映射缺失/失效时构建阶段会
    # fail-loud（BinaryConfigError 由下方领域 JSON 出口收口）。
    _gcmd, _gok, _greason = _godot_bin_ready(_P(args.project_root))
    if _gok:
        rc = _bootstrap_import_or_contract("measure.coverage", args)
        if rc is not None:
            return rc
    try:
        result = run_coverage(
            file_path=args.file,
            project_root=args.project_root,
            min_percent=args.min_percent,
            timeout_s=getattr(args, "timeout", None),
            tests_glob=args.tests,
            evidence_out=getattr(args, "evidence_out", None),
        )
    except BinaryConfigError as e:
        return _emit({"tool": "measure.coverage", "ok": False,
                      "summary": {"error": str(e)},
                      "failures": [{"reason": str(e)}]})
    except FileNotFoundError as e:
        result = {
            "tool": "measure.coverage",
            "ok": False,
            "summary": {"file": args.file, "error": str(e)},
            "failures": [{"reason": str(e)}],
        }
    except _lark_unexpected_input() as e:
        # SEE-1623 H6：非 .gd 目标 / 畸形 .gd 的解析异常转结构化信封——
        # stdout 恒有 JSON、零 traceback 泄漏（§5：判定侧输入畸形 =
        # 判定失败 exit 1，不是用法错误 exit 2）。
        return _emit({
            "tool": "measure.coverage",
            "ok": False,
            "summary": {
                "file": str(args.file),
                "error": "coverage target is not parseable GDScript",
                "reason": "target_not_parseable_gdscript",
                "reason_detail": str(e),
                "suggested_action": (
                    "pass a .gd GDScript file as the coverage target; "
                    "if the target is a .gd file, fix the syntax error "
                    "at the cited line/column (parse detail in "
                    "reason_detail)"),
            },
            "failures": [{"reason":
                          f"target_not_parseable_gdscript: {str(e)[:200]}"}],
        })
    return _emit(result)


def _attribution_args(args: argparse.Namespace
                      ) -> tuple[bool, str | None, bool]:
    """§SPEC-083：`--test-attribution` 开关与其产物路径的默认推导。

    显式 `--test-attribution-out` 优先；否则仅在开关开启时落到
    `<--out>.attribution.json`（默认关闭路径不产生任何路径）。
    `--attribution-causal`（R2）依附归属开关：开关未开时忽略（不静默
    改变默认路径行为）。"""
    enabled = bool(getattr(args, "test_attribution", False))
    explicit = getattr(args, "test_attribution_out", None)
    out = explicit or (f"{args.out}.attribution.json" if enabled else None)
    causal = bool(getattr(args, "attribution_causal", False)) and enabled
    return enabled, out, causal


def cmd_measure_map_update(args: argparse.Namespace) -> int:
    import json as _json
    from pathlib import Path as _P

    from .measurement.increment import IncrementError, run_attribution_increment

    rc = _resolve_project_root_or_contract(
        "measure.map-update", args,
        target=(args.roots[0] if getattr(args, "roots", None) else None))
    if rc is not None:
        return rc
    rc = _apply_godot_bin_or_contract("measure.map-update", args)
    if rc is not None:
        return rc
    try:
        recipe = _json.loads(_P(args.recipe).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return _emit({"tool": "measure.map-update", "ok": False,
                      "summary": {"error": f"recipe load failed: {e}"},
                      "failures": [{"reason": f"recipe load failed: {e}"}]})
    roots = [_P(r) for r in args.roots]
    targets = sorted({str(p) for root in roots
                      for p in root.rglob("*.gd") if p.is_file()})
    try:
        report = run_attribution_increment(
            args.project_root, targets, recipe, args.baseline,
            [str(c) for c in args.changed], args.out,
            timeout_s=args.timeout,
            attribution_causal=bool(args.attribution_causal),
            calibration_every=args.calibration_every)
    except IncrementError as e:
        return _emit({"tool": "measure.map-update", "ok": False,
                      "summary": {"error": str(e), "increment_error": True},
                      "failures": [{"reason": f"increment_error: {e}"}]})
    return _emit({"tool": "measure.map-update", "ok": True,
                  "summary": report, "failures": []})


def _attribution_usable(attribution_out, run_started: float,
                        project_root: str | None = None) -> bool:
    """行映射可用性（A22 中央校验）：schema/臂执行证据/目标文件绑定/
    recipe 全部一致才算可用；event_counts_stale 的增量产物一律不可用。

    union 不可认证 ≠ 产物不可用——陈旧产物不得冒充本轮可用证据。
    """
    if not attribution_out:
        return False
    import json as _json
    from pathlib import Path as _P

    try:
        from .measurement.recipe import build_recipe, recipe_hash as _raw_hash
        from .mutation.truth_select import arms_valid
        map_file = _P(attribution_out)
        doc = _json.loads(map_file.read_text(encoding="utf-8"))
        # 中央校验 1：臂执行完整性（probes/bundles/pairs）
        if not arms_valid(doc.get("arms_state") or {}):
            return False
        # 中央校验 2：A22——增量合并产物显式标记 event_counts_stale 的
        # （事件计数未重测）不得冒充新鲜可用证据
        if doc.get("increment", {}).get("event_counts_stale"):
            return False
        # 中央校验 3：recipe 一致（当前树可复算同一 pristine 哈希）。
        # project_root 由调用侧显式传入（--attribution-out/--out 可指到
        # 仓库外，路径算术猜根会把校验错归因为 recipe 漂移）。
        if doc.get("recipe_hash_pristine"):
            root = _P(project_root) if project_root \
                else _P(attribution_out).parent.parent
            recipe, error = build_recipe(root)
            if error is not None:
                return False
            if _raw_hash({k: v for k, v in recipe.items()
                          if k != "recipe_hash"}) != \
                    doc.get("recipe_hash_pristine"):
                return False
        # 中央校验 4：本轮时间锚（映射文件不早于本轮开始——替代原 mtime
        # 单点判新，仍保留防止上轮产物冒充）
        return map_file.stat().st_mtime >= run_started - 1.0
    except (OSError, ValueError):
        return False


def _acceptance_paused_single_arm(report: dict) -> bool:
    """SEE-1623 H4-A（Owner 2026-11-23 裁决 A）：暂停期间单臂可用判定。

    acceptance_arm 暂停披露在场 且 gut_full 臂 state=valid（臂门控已覆盖
    new_failures/超时/run_error/空测试面等 invalid 原因）→ 单臂可用。
    恢复双臂运行时无该披露 → 恒 False（回到 union 认证语义）。"""
    if not isinstance(report.get("gherkin_arm"), str):
        return False
    arms = report.get("arms") or []
    gut = next((a for a in arms
                if isinstance(a, dict) and a.get("arm") == "gut_full"), None)
    return bool(gut and gut.get("state") == "valid")


def _measure_run_summary(report: dict, attribution_out,
                         attribution_usable: bool) -> tuple[dict, bool]:
    """measure run 摘要 + 退出判定：区分映射可用与 union 未认证。

    SEE-1623 H4-A（Owner 裁决 A）：acceptance 暂停期间普通 run（无
    --test-attribution，即 attribution_out 缺省）按单臂可用语义——
    gut_full 有效且通过 → ok=true / exit 0、certification=uncertified、
    exit_semantics=single_arm_usable_acceptance_paused（保留
    acceptance_arm 暂停披露）；gut_full 无效/失败仍 failed / exit 1。
    归属路径（--test-attribution）与恢复双臂路径语义零变化。"""
    union_certified = bool(report.get("ok"))
    single_arm_usable = (attribution_out is None and not union_certified
                         and not attribution_usable
                         and _acceptance_paused_single_arm(report))
    if union_certified:
        certification, exit_semantics, ok = (
            "certified", "certified", True)
    elif single_arm_usable:
        certification, exit_semantics, ok = (
            "uncertified", "single_arm_usable_acceptance_paused", True)
    elif attribution_usable:
        certification, exit_semantics, ok = (
            "uncertified", "artifacts_usable_union_uncertified", True)
    else:
        certification, exit_semantics, ok = ("unusable", "failed", False)
    summary = dict(report)
    summary.update({
        "certification": certification,
        "union_certified": union_certified,
        "attribution_usable": attribution_usable,
        "exit_semantics": exit_semantics,
    })
    if attribution_out:
        summary["attribution_map"] = str(attribution_out)
    return summary, ok


def _load_baseline_failures(bf_path) -> tuple:
    """§SPEC-064b：`--baseline-failures` 装载与形态校验（自 cmd_measure_run
    原样搬移）。返回 (error_payload, pairs)：error_payload 非 None 即调用方
    `_emit` 后终止；未请求时两者皆 None。"""
    if not bf_path:
        return None, None
    try:
        doc = json.loads(Path(bf_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return ({"tool": "measure.run", "ok": False,
                 "summary": {"error": f"baseline-failures load failed: {e}"},
                 "failures": [{"reason":
                               f"baseline-failures load failed: {e}"}]},
                None)
    pairs = doc.get("pairs") if isinstance(doc, dict) else doc
    if not isinstance(pairs, list) \
            or not all(isinstance(x, list) and len(x) == 2
                       and all(isinstance(y, str) for y in x)
                       for x in pairs):
        return ({"tool": "measure.run", "ok": False,
                 "summary": {"error":
                             "baseline-failures must be a JSON "
                             "array of [script, test] pairs (or "
                             "{\"pairs\": [...]}) — bare names are "
                             "identity-ambiguous (§SPEC-064b)"},
                 "failures": [{"reason": "baseline-failures "
                                         "malformed"}]},
                None)
    return None, {(x[0], x[1]) for x in pairs}


def _load_intolerant_config(it_path) -> tuple:
    """`--intolerant-tests` 装载与形态校验（自 cmd_measure_run 原样搬移）。
    返回 (error_payload, config)：error_payload 非 None 即调用方 `_emit`
    后终止；未请求时两者皆 None。"""
    if not it_path:
        return None, None
    try:
        it_doc = json.loads(Path(it_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return ({"tool": "measure.run", "ok": False,
                 "summary": {"error":
                             f"intolerant-tests load failed: {e}"},
                 "failures": [{"reason":
                               f"intolerant-tests load failed: "
                               f"{e}"}]},
                None)
    if not isinstance(it_doc, dict):
        return ({"tool": "measure.run", "ok": False,
                 "summary": {"error":
                             "intolerant-tests must be an object "
                             "with optional tests/coverage_"
                             "limitations arrays"},
                 "failures": [{"reason": "intolerant-tests "
                                         "malformed"}]},
                None)
    return None, it_doc


def cmd_measure_run(args: argparse.Namespace) -> int:
    import json as _json
    import time as _time

    from pathlib import Path as _P

    from .measurement.orchestrator import MeasurementError, run_measurement
    from .measurement.recipe import verify_recipe

    run_started = _time.time()
    # SEE-1623 M4：负 timeout 此前被接受（两臂瞬间超时）——用法层即拒。
    _num_err = _measure_timeout_arg_error(args)
    if _num_err is not None:
        return _usage_exit(_num_err)
    rc = _apply_godot_bin_or_contract("measure.run", args)
    if rc is not None:
        return rc

    recipe_path = _P(args.recipe)
    try:
        recipe = _json.loads(recipe_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return _emit({"tool": "measure.run", "ok": False,
                      "summary": {"error": f"recipe load failed: {e}"},
                      "failures": [{"reason": f"recipe load failed: {e}"}]})
    ok, reason = verify_recipe(recipe)
    if not ok:
        return _emit({"tool": "measure.run", "ok": False,
                      "summary": {"error": f"recipe invalid: {reason}"},
                      "failures": [{"reason": f"recipe invalid: {reason}"}]})
    # §SPEC-019：recipe 校验（输入契约）先于项目根解析，保持既有错误优先级。
    rc = _resolve_project_root_or_contract(
        "measure.run", args, target=(args.roots[0] if args.roots else None))
    if rc is not None:
        return rc
    roots = [_P(r) for r in args.roots]
    targets = _gd_targets_under_roots(roots)
    if not targets:
        return _emit({"tool": "measure.run", "ok": False,
                      "summary": {"error": "no .gd targets under roots"},
                      "failures": [{"reason": "no .gd targets"}]})
    probe_keys = (args.gherkin_probe_keys or None)
    if probe_keys:
        probe_keys = [k.strip() for k in probe_keys.split(",") if k.strip()]
    _bf_err, baseline_failures = _load_baseline_failures(
        getattr(args, "baseline_failures", None))
    if _bf_err is not None:
        return _emit(_bf_err)
    _it_err, intolerant_config = _load_intolerant_config(
        getattr(args, "intolerant_tests", None))
    if _it_err is not None:
        return _emit(_it_err)
    # §SPEC-075/076：拉起任何 Godot 子进程前校验解析结果——不可用即领域错误
    godot_cmd, godot_ok, godot_reason = _godot_bin_ready(_P(args.project_root))
    if not godot_ok:
        return _emit_godot_unavailable("measure.run", godot_cmd, godot_reason)
    rc = _bootstrap_import_or_contract("measure.run", args)
    if rc is not None:
        return rc
    test_attribution, attribution_out, attribution_causal = (
        _attribution_args(args))
    try:
        report = run_measurement(
            args.project_root, targets, recipe,
            probe_keys=probe_keys, timeout_s=args.timeout,
            evidence_out=getattr(args, "evidence_dir", None),
            scope_roots=[str(r) for r in roots],
            stability_baseline=getattr(args, "stability_baseline", None),
            shard_width=getattr(args, "shard_width", 6),
            gherkin_shards=getattr(args, "gherkin_shards", 4),
            shard_log_dir=getattr(args, "shard_log_dir", None),
            baseline_failures=baseline_failures,
            intolerant_config=intolerant_config,
            gherkin_repeats=getattr(args, "gherkin_repeats", 3),
            test_attribution=test_attribution,
            attribution_out=attribution_out,
            attribution_causal=attribution_causal,
            arms_parallel=bool(getattr(args, "arms_parallel", False)),
            gherkin_adaptive=bool(
                getattr(args, "gherkin_adaptive", False)))
    except MeasurementError as e:
        return _emit({"tool": "measure.run", "ok": False,
                      "summary": {"error": str(e), "measurement_error": True},
                      "failures": [{"reason": f"measurement_error: {e}"}]})
    out_path = _P(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    attribution_usable = _attribution_usable(
        attribution_out, run_started, project_root=args.project_root)
    summary, ok = _measure_run_summary(report, attribution_out,
                                       attribution_usable)
    return _emit({"tool": "measure.run", "ok": ok, "summary": summary,
                  "failures": ([] if ok else
                               [{"reason": "union invalid or arms invalid"}])})


def _gd_targets_under_roots(roots: list[Path]) -> list[str]:
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


def _diagnose_gate(report: dict) -> tuple[bool, list[dict]]:
    """§SPEC-070 FR-5：diagnose 不再恒 ok=true——任一 feature 插桩腿
    超时即为失败并具名披露。"""
    timed_out = [f["key"] for f in report.get("features", [])
                 if (f.get("instrumented") or {}).get("timed_out")]
    failures = [{"reason": f"feature instrumented leg timed out: {k}"}
                for k in timed_out]
    return (not timed_out), failures


def cmd_measure_diagnose(args: argparse.Namespace) -> int:
    """§SPEC-048 diagnostic-only gherkin 计时模式。"""
    import json as _json

    from pathlib import Path as _P

    from .measurement.diagnose import MeasurementError, run_measurement_diagnose
    from .measurement.recipe import verify_recipe

    rc = _resolve_project_root_or_contract(
        "measure.diagnose", args,
        target=(args.roots[0] if getattr(args, "roots", None) else None))
    if rc is not None:
        return rc

    rc = _apply_godot_bin_or_contract("measure.diagnose", args)
    if rc is not None:
        return rc

    # §SPEC-075/076：拉起任何 Godot 子进程前校验解析结果——不可用即领域错误
    godot_cmd, godot_ok, godot_reason = _godot_bin_ready(_P(args.project_root))
    if not godot_ok:
        return _emit_godot_unavailable("measure.diagnose", godot_cmd,
                                       godot_reason)

    recipe_path = _P(args.recipe)
    try:
        recipe = _json.loads(recipe_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return _emit({"tool": "measure.diagnose", "ok": False,
                      "summary": {"error": f"recipe load failed: {e}"},
                      "failures": [{"reason": f"recipe load failed: {e}"}]})
    ok, reason = verify_recipe(recipe)
    if not ok:
        return _emit({"tool": "measure.diagnose", "ok": False,
                      "summary": {"error": f"recipe invalid: {reason}"},
                      "failures": [{"reason": f"recipe invalid: {reason}"}]})
    targets = _gd_targets_under_roots([_P(r) for r in args.roots])
    if not targets:
        return _emit({"tool": "measure.diagnose", "ok": False,
                      "summary": {"error": "no .gd targets under roots"},
                      "failures": [{"reason": "no .gd targets"}]})
    keys = [k.strip() for k in args.features.split(",") if k.strip()]
    try:
        report = run_measurement_diagnose(
            args.project_root, targets, recipe, keys,
            timeout_s=args.timeout, progress_out=args.out)
    except MeasurementError as e:
        return _emit({"tool": "measure.diagnose", "ok": False,
                      "summary": {"error": str(e)},
                      "failures": [{"reason": str(e)}]})
    _P(args.out).write_text(
        _json.dumps(report, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    ok, failures = _diagnose_gate(report)
    return _emit({"tool": "measure.diagnose", "ok": ok,
                  "summary": _diagnose_summary(report), "failures": failures})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gqt", description="godot-qa-toolkit")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- gherkin（原 accept；SEE-1583 名称回归领域标准，管线不变）---
    gh = sub.add_parser("gherkin", help="Gherkin pipeline (SEE-1426 P2)")
    ghsub = gh.add_subparsers(dest="gherkin_command", required=True)

    acc = ghsub.add_parser("compile", help=".feature -> versioned IR + generated GUT tests")
    acc.add_argument("feature", nargs="?", default=None,
                     help="path to the .feature file (omit when --keys "
                          "selects the private-suite set, §SPEC-035)")
    acc.add_argument("--project-root", required=True,
                     help="target Godot project root (writes gherkin/ir + gherkin/generated)")
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
                          "gherkin/ir + gherkin/generated")
    acc.set_defaults(func=cmd_gherkin)

    acr = ghsub.add_parser("run", help="replay-check + headless-run the generated suite")
    acr.add_argument("directory", nargs="?", default=None,
                     help="alternative project root (default: --project-root)")
    acr.add_argument("--project-root", required=True,
                     help="target Godot project root containing addons/gut/")
    acr.add_argument("--key", default=None,
                     help="feature key the suite was compiled under "
                          "(required without --suite-dir/--gherkin-probe-keys)")
    acr.add_argument("--suite-dir", default=None,
                     help="§SPEC-035 private suite dir — run the whole dir "
                          "(dir = selection set) with gherkin-suite/1 "
                          "reconciliation")
    acr.add_argument("--gherkin-probe-keys", nargs="?",
                     const="aidomain_integration", default=None,
                     help="W4 cost probe: compile+run these comma-separated "
                          "keys via the private-dir pipeline and emit a "
                          "performance.gherkin_probe record (default: "
                          "aidomain_integration)")
    acr.add_argument("--timeout", type=int, default=120,
                     help="GUT timeout seconds (default 120)")
    _add_godot_bin_arg(acr)
    acr.set_defaults(func=cmd_gherkin)

    acm = ghsub.add_parser("mutate", help="full-matrix contract mutation over the compiled IR")
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
                          "gherkin_worker.gd artifact serves one godot process "
                          "for the whole matrix via stdin NDJSON + ir_file "
                          "transfer, with per-mutant hard discipline "
                          "(release+rebuild+smoke probe, failure = run_error) "
                          "and a per-round verdict timeout (SEE-1426 §SPEC-14)")
    _add_godot_bin_arg(acm)
    acm.set_defaults(func=cmd_gherkin)

    # --- mutation（名称与语义准确，保持一级）---
    m = sub.add_parser("mutation", help="mutation test one or more .gd files")
    m.add_argument("files", nargs="*",
                   help="the .gd file(s) to mutate, or '<file>.gd#<lines>' "
                        "line-level address; omitted = derive targets from "
                        "git diff HEAD (tracked changes + untracked .gd)")
    m.add_argument("--project-root", default=None,
                   help="project root containing addons/gut/; omitted = "
                        "discover the nearest project.godot walking up from "
                        "the target (or CWD)")
    m.add_argument("--budget", type=int, default=50, help="max mutants (default 50)")
    # SEE-1606 P1（Owner 2026-11-19 已决）：默认值 = BUDGET_CAP_S。旧 60s
    # 短于 autoload 项目的文件级并集面（KOL 159 脚本实测 ~77s），必然触发
    # baseline 快照超时→串行降级；600s 给全量面 4-8 倍余量。
    m.add_argument("--timeout", type=int, default=600,
                   help="GUT timeout seconds (default 600)")
    m.add_argument("--dry-run", action="store_true",
                   help="list mutation sites without running GUT (SPEC-009)")
    m.add_argument("--check-baseline", action="store_true",
                   help="SEE-1615 §SPEC-PREFLIGHT: dry-run runs one "
                        "uninstrumented baseline GUT (read-only, no map "
                        "write) and names failing tests in "
                        "static_preconditions.baseline_health; default "
                        "not_checked")
    m.add_argument("--auto-prepare", action="store_true",
                   help="SEE-1615 §SPEC-PREREQ-MATRIX: when the plan "
                        "reports P1 prepare-blockers (missing/stale "
                        "attribution map, missing import cache), "
                        "automatically build them and continue; without "
                        "this flag the run stops with the problem list and "
                        "estimated cost")
    m.add_argument("--intolerant-tests", default=None,
                   help="SEE-1615 §SPEC-PLANFIRST: JSON declaration path "
                        "for structurally instrumentation-intolerant "
                        "tests (source-contract assertions); the "
                        "baseline-preflight interruption report generates "
                        "this draft. Also readable from "
                        "GQT_MEASUREMENT_INTOLERANT_TESTS")
    m.add_argument("--fail-on-red-baseline", action="store_true",
                   help="SEE-1615 §SPEC-PREFLIGHT x PLANFIRST: fail fast "
                        "when the uninstrumented baseline is red (default "
                        "OFF: red tests are excluded by name, mapping "
                        "marked partial, run continues)")
    m.add_argument("--tests", default=None,
                   help="scoped GUT dir override, e.g. res://tests/save/ "
                        "(SPEC-009); omitted = SEE-1581 runtime truth-map "
                        "selection — a fresh mapping picks the touching test "
                        "set (zero-ref is a terminal short-circuit, no "
                        "fallback), an unusable mapping fails the run")
    m.add_argument("--jobs", type=int, default=None,
                   help="worker pool size for per-file AND per-mutant slicing "
                        "(a single file is sliced per-mutant too); default = "
                        "max(2, cpu//2); --jobs 1 forces serial (SEE-1413)")
    m.add_argument("--select", dest="select_mode", default="line",
                   choices=["line", "fn", "file", "full"],
                   help="SEE-1587 test-selection granularity: line (default, "
                        "mutants grouped by their line's test-set signature; "
                        "line facts are the primary truth), fn (explicit "
                        "calibration/fallback view, grouped by enclosing "
                        "function), file (explicit calibration/fallback "
                        "view, per-file set), full (the only explicit "
                        "full-face audit; bypasses attribution)")
    m.add_argument("--attribution-map", default=None,
                   help="SEE-1581 + §SPEC-ARGMERGE three-state: "
                        "'auto' (default) = auto-managed "
                        ".qa-cache/attribution-map.json with generation/"
                        "incremental refresh; 'off' = disable auto map "
                        "(fail loud with no usable map); <path> = explicit "
                        "attribution artifact (test-attribution/6, "
                        "line-primary) used read-only as the runtime truth "
                        "for test selection (never renewed)")
    m.add_argument("--no-attribution-auto", dest="attribution_auto",
                   action="store_false",
                   help="[deprecated alias of --attribution-map off] "
                        "disable automatic generation/incremental refresh of "
                        "the attribution map; with no usable map present the "
                        "run fails loud instead of falling back (build one "
                        "first via `gqt measure map-update` or an explicit "
                        "`gqt measure run --test-attribution`)")
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
    # SEE-1606 P2（对齐 Stryker break-on-initial-test-failure，默认 false）。
    m.add_argument("--break-on-baseline-failure", action="store_true",
                   help="abort when the baseline run has failing tests, "
                        "instead of continuing (default: continue; failing "
                        "baseline tests are excluded from kill determination "
                        "and disclosed in summary.baseline_failures/names)")
    # SEE-1606 P3（对齐 Stryker disableBail，默认 false = kill 即退默认开启）。
    m.add_argument("--no-kill-bail", action="store_true",
                   help="disable script-granularity kill bail: with bail on "
                        "(default) each mutant's selection face runs script "
                        "by script in one engine process and stops at the "
                        "first script whose failures are NEW vs baseline; "
                        "env GQT_MUTATION_KILL_BAIL=0 also disables")
    m.add_argument("--mutation", default=None, metavar="ID",
                   help="single-site rerun: '<file>#<scope_id>#<n>' (SEE-1420 "
                        "P3 caliper) — forces fresh execution of that one "
                        "site (1 baseline + 1 mutant), bypassing diff reuse; "
                        "single-file only")
    m.add_argument("--lines", type=str, default=None, metavar="SPEC",
                   help="caliper rerun of all mutation sites on the requested "
                        "source lines (SEE-1587): single line '42', range "
                        "'10-20', or multi-segment '10-20,30,35-40'; "
                        "intersected with actual mutation sites "
                        "(requested/hit/skipped_no_site disclosed; no site at "
                        "all fails loud); the selected sites run under the "
                        "union of their lines' test sets (discloses "
                        "no_mapped_tests); single-file only")
    m.add_argument("--no-reuse-coverage", action="store_true",
                   help="disable the coverage prefilter (SEE-1550 L1 / "
                        "SEE-1594 S5: prefilter is ON by default and "
                        "consumes the line-level mapping single-truth chain "
                        "(/2 derived cache bound to the map content and "
                        "execution evidence, then live projection); "
                        "uncovered sites never enter the ok gate)")
    m.add_argument("--worker-channel", action="store_true",
                   help="force the resident worker channel (SEE-1550 "
                        "§SPEC-020 worker-reset/1): eligible targets run in "
                        "a persistent GUT-direct-drive worker under "
                        "equivalence gates (parity/state-drift/canary "
                        "disqualification with automatic cold degrade); "
                        "worker is DEFAULT-ON — static routing sends "
                        "autoload/GUT-domain/scene-held targets to the cold "
                        "M0 channel; env GQT_MUTATION_WORKER=0 opts out "
                        "(cold) and is honored on every execution path "
                        "(serial, auto-serial, parallel sharding)")
    _add_godot_bin_arg(m)
    m.set_defaults(func=cmd_mutation)

    # --- measure（SEE-1583：测量/评分/诊断功能面收敛）---
    ms = sub.add_parser("measure", help="measurement, scoring and diagnostics (SEE-1583)")
    mssub = ms.add_subparsers(dest="measure_command", required=True)

    cr = mssub.add_parser(
        "suite",
        help="build the measurement suite definition (measurement-suite/2, "
             "read-only, SEE-1546 WP3)",
    )
    cr.add_argument("--project-root", default=None,
                    help="target Godot project root; omitted = discover from CWD")
    cr.add_argument("--out", default="recipe.json",
                    help="where to write the recipe JSON (default ./recipe.json)")
    cr.add_argument("--exclude", action="append", default=[],
                    help="exclusion as scope:entry:reason (repeatable; reason required)")
    cr.set_defaults(func=cmd_measure_suite)

    can = mssub.add_parser("run", help="dual-arm batch coverage orchestration (SEE-1546 WP3 PR-C)")
    can.add_argument("roots", nargs="+",
                     help="target .gd roots (recursive glob); arms run the "
                          "full gutconfig face + gherkin suite over them")
    can.add_argument("--project-root", default=None,
                     help="Godot project root (addons/gut + .gutconfig.json); "
                          "omitted = discover from roots/CWD")
    can.add_argument("--recipe", required=True,
                     help="measurement-suite/2 JSON (from `gqt measure suite --out`)")
    can.add_argument("--out", required=True,
                     help="measurement-report/2 output JSON path")
    can.add_argument("--baseline-failures", default=None,
                     help="JSON file with pre-existing (un-instrumented) "
                          "failing (script, test) pairs ([[..],..] or "
                          "{\"pairs\": [[..],..]}) — new failing pairs gate "
                          "the arm; bare names are identity-ambiguous and "
                          "rejected (§SPEC-060a/064b)")
    can.add_argument("--intolerant-tests", default=None,
                     help="JSON file declaring instrumentation-intolerant "
                          "tests ({\"tests\": [{\"name\", \"reason\"}]}) and "
                          "coverage_limitations ([{\"file\", \"reason\"}]) — "
                          "declared tests bypass failure gating by name and "
                          "are disclosed as coverage blind spots; limitation "
                          "files name why unstable lanes cannot be "
                          "deterministic (§SPEC-061b/c)")
    can.add_argument("--gherkin-probe-keys", nargs="?",
                     const="aidomain_integration", default=None,
                     help="W4 cost probe keys (default: aidomain_integration)")
    can.add_argument("--timeout", type=int, default=180,
                     help="GUT timeout seconds per arm (default 180)")
    can.add_argument("--stability-baseline", default=None,
                     help="prior measurement report path: fids whose covered "
                          "lines differ are routed to the unstable lane "
                          "(no score, §SPEC-056)")
    can.add_argument("--shard-width", type=int, default=6,
                     help="gut_full parallel shard count (1 = sequential, "
                          "§SPEC-052)")
    can.add_argument("--gherkin-repeats", type=int, default=3,
                     help="gherkin arm runs each shard group N times and "
                          "takes the line-set union; convergence round is "
                          "disclosed in the report (§SPEC-062C)")
    can.add_argument("--gherkin-shards", type=int, default=4,
                     help="gherkin parallel shard count")
    can.add_argument("--shard-log-dir", default=None,
                     help="retain per-shard full logs and raw sink copies "
                          "(§SPEC-056)")
    can.add_argument("--evidence-dir", default=None,
                     help="optional coverage-evidence/2 output dir for "
                          "per-arm bundles")
    can.add_argument("--test-attribution", action="store_true",
                     help="EXPERIMENTAL (SEE-1581): attribute probe hits to "
                          "the GUT test that was running, emitting a "
                          "test->file mapping artifact; default off, the "
                          "default path is byte-identical")
    can.add_argument("--test-attribution-out", default=None,
                     help="attribution artifact path (default: "
                          "<--out>.attribution.json)")
    can.add_argument("--attribution-causal", action="store_true",
                     help="EXPERIMENTAL (SEE-1581 R2): also wrap "
                          "call_deferred/create_timer creation sites in "
                          "instrumented files so their callbacks are "
                          "attributed to the test that created them; "
                          "requires --test-attribution")
    can.add_argument("--gherkin-adaptive", action="store_true",
                     help="EXPERIMENTAL (SEE-1581 P1): run gherkin reps "
                          "round-by-round and stop early once the line-set "
                          "union stabilises (bound stays --gherkin-"
                          "repeats); default off")
    can.add_argument("--arms-parallel", action="store_true",
                     help="EXPERIMENTAL (SEE-1581 P2): run gut_full and "
                          "gherkin arms concurrently (gherkin is "
                          "instrumented inside its shard shadows); default "
                          "off — timing baseline must be redefined when "
                          "enabled")
    _add_godot_bin_arg(can)
    can.set_defaults(func=cmd_measure_run)

    cd = mssub.add_parser(
        "diagnose",
        help="diagnostic-only per-feature gherkin timing under flags "
             "instrumentation (§SPEC-048)",
    )
    cd.add_argument("roots", nargs="+",
                    help="directories whose .gd files are instrumented")
    cd.add_argument("--project-root", default=None,
                    help="target Godot project root; omitted = discover from roots/CWD")
    cd.add_argument("--recipe", required=True,
                    help="measurement suite json (verify + feature lookup)")
    cd.add_argument("--features", required=True,
                    help="comma-separated gherkin feature keys")
    cd.add_argument("--out", required=True,
                    help="diagnostic report json path (progress written "
                         "after each feature)")
    cd.add_argument("--timeout", type=int, default=480,
                    help="per-leg GUT timeout seconds (default 480)")
    _add_godot_bin_arg(cd)
    cd.set_defaults(func=cmd_measure_diagnose)

    inc = mssub.add_parser(
        "map-update",
        help="EXPERIMENTAL (SEE-1581 方向①): incremental rebuild of the "
             "test-attribution mapping from a baseline artifact")
    inc.add_argument("roots", nargs="+",
                     help="target .gd roots — must be the same universe as "
                          "the baseline artifact")
    inc.add_argument("--project-root", default=None,
                     help="Godot project root; omitted = discover from roots/CWD")
    inc.add_argument("--recipe", required=True,
                     help="measurement-suite/2 JSON (same content as the "
                          "baseline run)")
    inc.add_argument("--baseline", required=True,
                     help="prior attribution artifact "
                          "(test-attribution/6, line-primary)")
    inc.add_argument("--changed", action="append", default=[],
                     help="changed/new/removed file path (repeatable)")
    inc.add_argument("--out", required=True,
                     help="incremented attribution artifact output path")
    inc.add_argument("--timeout", type=int, default=180)
    inc.add_argument("--calibration-every", type=int, default=5,
                     help="force a full-face rebuild after N increments")
    inc.add_argument("--attribution-causal", action="store_true")
    _add_godot_bin_arg(inc)
    inc.set_defaults(func=cmd_measure_map_update)

    v = mssub.add_parser(
        "coverage",
        help="line coverage of a single .gd file (line-level mapping projection, "
             "no direct GUT)")
    v.add_argument("file", nargs="?", default=None,
                   help="the .gd file to measure (required unless --prune-sinks)")
    v.add_argument("--project-root", default=None,
                   help="project root containing addons/gut/; omitted = "
                        "discover from the target file or CWD")
    v.add_argument("--min-percent", type=float, default=80.0,
                   help="min coverage percent (default 80)")
    v.add_argument("--timeout", type=int, default=120,
                   help="GUT timeout seconds for the map build / incremental "
                        "refresh phases (default 120); a fresh projection runs "
                        "no process and spends no GDP budget")
    v.add_argument("--tests", default=None,
                   help="restrict the mapped selection to test scripts under this "
                        "res:// dir/file (e.g. res://tests/unit/); omitted = the "
                        "full mapped test face")
    v.add_argument("--evidence-out", default=None,
                   help="write a coverage-evidence/2 bundle generated from the "
                        "line map (hits_lines from projection; probes_fired from "
                        "the map arms_state — no second GUT run)")
    v.add_argument("--prune-sinks", action="store_true",
                   help="delete leftover legacy qa-coverage-hits-* sink files "
                        "older than --retain-days (cleanup only; never a coverage "
                        "source)")
    v.add_argument("--retain-days", type=int, default=7,
                   help="retention period for --prune-sinks (default 7)")
    _add_godot_bin_arg(v)
    v.set_defaults(func=cmd_measure_coverage)

    c = mssub.add_parser("complexity", help="complexity gate over .gd files")
    c.add_argument("paths", nargs="+", help=".gd files or directories to scan")
    c.add_argument("--warn", type=int, default=10, help="warn threshold (default 10)")
    c.add_argument("--max", type=int, default=15, help="fail threshold (default 15)")
    c.set_defaults(func=cmd_measure_complexity)

    a = mssub.add_parser("arch", help="Pure Core architecture guard over .gd files (SEE-1426 P1)")
    a.add_argument("paths", nargs="+", help=".gd files or directories to scan")
    a.set_defaults(func=cmd_measure_arch)

    p = mssub.add_parser(
        "parity",
        help="full per-function CC parity over the neutral registry (SEE-1546 WP1b, diagnostic-only)",
    )
    p.add_argument(
        "paths",
        nargs="+",
        help="directories or .gd files to scan (same roots as the complexity gate)",
    )
    p.add_argument("--project-root", default=None,
                   help="project root for relative fids; omitted = discover from paths/CWD")
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
    p.set_defaults(func=cmd_measure_parity)

    rd = mssub.add_parser("registry-diff", help="diff two registry snapshots (func-registry-diff/1)")
    rd.add_argument("old", help="old snapshot JSON path")
    rd.add_argument("new", help="new snapshot JSON path")
    rd.add_argument("--out", default=None, help="optionally write the diff JSON")
    rd.set_defaults(func=cmd_measure_registry_diff)

    r = mssub.add_parser(
        "risk",
        help="exploratory CRAP-Line shadow over selected files (diagnostic-only, SEE-1546 M2)",
    )
    r.add_argument(
        "files",
        nargs="+",
        help="candidate .gd files (eligible ones are measured; lambda/inner-class/"
        "var-def files route to the measurement-gap lanes)",
    )
    r.add_argument("--project-root", default=None,
                   help="target Godot project root; omitted = discover from "
                        "the candidate files or CWD")
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
    r.set_defaults(func=cmd_measure_risk)

    # --- doctor（保持一级，契约不变）---
    d = sub.add_parser("doctor", help="self-check editable install vs current worktree gitlink")
    d.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
