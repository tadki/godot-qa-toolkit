"""Mutation runner: source-level GDScript mutants + kill/survive/timeout verdicts.

SEE-1268 自研（gdtoolkit lark AST 定位 → 源码 span 替换 → 跑 GUT 看该 mutant
是否被杀死）。kill rate 永不进验收证据（owner-order L5 条文）。

P0' 判定原则落点：每个 mutant 输出机器可判的 killed/survived/timeout 布尔 +
结构化 failures；exit 0=全部杀死 1=有存活/超时。

SEE-1312 增强（plan-debate 定案）：
- 变异点扩展：三元表达式（cond 取反 + 两臂交换）、守卫取反（含去重）、
  算子第二梯队（and⇄or / in→not in / is 取反）、UOI token 级删 not（排除 not in）、
  常量 Token 级精确匹配（原 atom 节点在 lark 折叠下永不出现，已死代码——改直查）。
- 定位机制升级为 span 替换（gather_metadata 提供 start_pos/end_pos），
  取代脆弱的 line/column 单点替换。
- 成本控制：--dry-run（只清点不跑 GUT）、--tests 受影响测试子集、
  自适应 timeout（baseline 实测耗时 × k）。
- 有效性分类：invalid_mutant 语法预检、suspect 标签（baseline 脏 ∧ 差集空）。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 变异算子机制在 ops.py（runner <800 行约束拆分）；re-export 保持既有
# import / monkeypatch 名称空间稳定。
from .ops import (  # noqa: F401
    Mutant,
    _apply_arm_swap,
    _apply_mutation,
    _apply_mutation_fallback,
    _collect_mutations,
    _cond_span,
    _line_offsets,
    _span_mutants_for,
    _span_token_ops,
    _split_invalid_mutants,
    _subtree_has_mutable_node,
    _ternary_mutants,
    _token_mutants_for,
    _token_pos,
    _tree_depth_walk,
    mk_guard_not,
)

from .inject import cleanup_temp_files, forget_temp, host_script_res, mutant_env, seed_host_script, wslpath_win


# GUT 的命令行入口必须以 res:// 形式传给 `godot -s`：Godot 对绝对路径的 -s
# 一律报 "Attempt to open script ... File not found"（Revy QA FAIL 实证），
# 文件真实存在也一样——res:// 是唯一可靠形式。
GUT_SCRIPT_RES_PATH = "res://addons/gut/gut_cmdln.gd"

# godot 进程级失败的特征（与"测试跑过但失败"截然不同——绝不能计入 kill）。
_GODOT_LAUNCH_ERROR_MARKERS = (
    "attempt to open script",
    "file not found",
    "failed loading resource",
    "error: failed to load script",
)


def _looks_like_godot_launch_failure(output: str) -> bool:
    low = output.lower()
    return any(marker in low for marker in _GODOT_LAUNCH_ERROR_MARKERS)


def _godot_project_path(project_root: str) -> str:
    """win64 godot 的 --path 语义：/mnt/... 形式的 WSL 绝对路径会被拒
    （'Invalid project path'，硬ener 实机实证——0.1s 静默 rc≠0、零测试执行）。

    相对路径由 godot 自行解析（CWD 契约：调用方必须已 cd 到项目根）；
    绝对路径走 wslpath 转 Windows 形式（不可用时原样返回）。
    """
    if os.path.isabs(project_root):
        if project_root.startswith("/mnt/"):
            return wslpath_win(project_root) or project_root
        return project_root
    return project_root


def _run_gut_on_project(project_root: str, timeout_s: int = 60,
                        tests_glob: str | None = None) -> tuple[int, str]:
    """在项目里 headless 跑 GUT；返回 (exit_code, 输出尾部)。

    -s 用 res:// 路径（绝对路径会让 godot 拒绝加载）；脚本存在性仍由调用方
    校验（文件系统检查），加载失败由 _looks_like_godot_launch_failure 识别。
    tests_glob（SPEC-009）：受影响测试子集（如 res://tests/save/），替代全量
    -gdir=res://tests/ 以压缩单轮成本。
    SEE-1321 SPEC-006/007：子进程 env 注入（XDG_DATA_HOME / GQT_MUTATION_CFG）
    与入口脚本切换经模块级 _ACTIVE_ENV/_ACTIVE_SCRIPT 传递——保持函数签名
    向后兼容（既有令牌测试以 3 参 fake 替换本函数）。
    """
    gut_fs = os.path.join(project_root, "addons", "gut", "gut_cmdln.gd")
    if not os.path.isfile(gut_fs):
        raise FileNotFoundError(f"GUT runner not found: {gut_fs}")
    gdir = tests_glob if tests_glob else "res://tests/"
    # -gtest 接受的是「脚本完整路径列表」，但会在装满所有收集脚本后仍全量跑
    # （GUT 9.6 实测：单脚本 -gtest 展开 251 scripts/2718 tests）——不能作
    # 文件级子集。文件路径改走 -gselect=脚本名（GUT 按 -select_script 子集
    # 过滤收集结果，实测精确 1 script），目录仍走 -gdir（SEE-1316 实测）。
    if gdir.endswith(".gd"):
        script_stem = Path(gdir).stem
        extra = [
            "-gdir=res://tests/",
            f"-gselect={script_stem}",
        ]
    else:
        extra = [f"-gdir={gdir}"]
    env = dict(os.environ)
    if _ACTIVE_ENV:
        env.update(_ACTIVE_ENV)
    r = subprocess.run(
        ["godot", "--headless", "--path", _godot_project_path(project_root),
         "-s", _ACTIVE_SCRIPT, *extra, "-gexit"],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        cwd=project_root,
        env=env,
    )
    # 失败测试名解析需要完整运行流（'-*', '* test_x'、[Failed] 行）——2000
    # 字符的窗口在长断言输出下会把失败行挤出窗外导致 kill 判定失真（
    # SEE-1321 实测 pricing_resolver or→and 29 项新失败被吞成 survived）。
    tail = r.stdout[-200000:] if r.stdout else r.stderr[-10000:]
    return r.returncode, tail


# 子进程环境/入口脚本的活动状态（mutant 循环内动态切换，见 _run_single_mutant）
_ACTIVE_ENV: dict | None = None
_ACTIVE_SCRIPT: str = GUT_SCRIPT_RES_PATH


def _parse_failing_tests(gut_output: str) -> set[str]:
    """从 GUT 输出解析失败测试名集合。

    双格式兼容（硬ener 实机实证）：
    - 运行流：'* test_xxx' 开启一个测试，其后 [Failed] 行归属它；
    - Run Summary 段（GUT 4.6 win64 实测）：'- test_xxx' 后跟 [Failed] 行，
      但输出被 stdout[-2000:] 截断后往往只剩该段——单靠运行流正则会漏掉
      全部失败（save_manager 实机 66/66 假 survived 的根因）。
    另辅以 junit XML（res://tests/reports/junit_report_*.xml）兜底——格式
    变化最稳的机器判据。
    用测试名集合做 kill 判定——exit code 只能说明"有没有失败"，无法区分
    pre-existing 失败与 mutant 引入的新失败（SEE-1268 Revy QA 实证）。
    """
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    failing: set[str] = set()
    current = None
    for raw in gut_output.splitlines():
        line = ansi.sub("", raw).replace("\r", "")
        m = re.match(r"^[*-]\s+(test_\S+)", line)
        if m:
            current = m.group(1)
            continue
        if "[Failed]" in line and current:
            failing.add(current)
    return failing


# 自适应 timeout 倍数：per-mutant 上限 = baseline 实测耗时 × 该倍数。
_TIMEOUT_SCALE = 2.0


def _mutant_record(m: Mutant, verdict: str, reason: str) -> dict:
    """统一 mutant 明细记录形态（契约 §4.3 failures 项）。"""
    return {"file": m.file, "line": m.line, "kind": m.kind,
            "original": m.original, "mutated": m.mutated,
            "verdict": verdict, "reason": reason}


def _run_error_result(file: str, error: str, failure_reason: str) -> dict:
    """统一 run_error 中止契约（contract.md §4.3 中止形态）。"""
    return {
        "tool": "mutation",
        "ok": False,
        "summary": {"file": file, "run_error": True, "error": error},
        "failures": [{"reason": failure_reason}],
    }


def _valid_mutants(src: str, mutants: list[Mutant]) -> list[Mutant]:
    return _split_invalid_mutants(src, mutants)[0]


def _record_invalid_mutants(src: str, mutants: list[Mutant],
                            mutant_slice: tuple[int, int] | None = None
                            ) -> tuple[list[dict], int]:
    """预检并记录 invalid_mutant 明细；返回 (results, invalid_count)。

    mutant_slice 时只预检分片区间（per-mutant 并发 worker 各管各片区，
    合并层求和不得重复计数）。
    """
    results: list[dict] = []
    scoped = mutants
    if mutant_slice is not None:
        start, stop = mutant_slice
        scoped = mutants[start:stop]
    _, invalid = _split_invalid_mutants(src, scoped)
    for m in invalid:
        results.append(_mutant_record(m, "invalid_mutant",
                                      f"mutated source fails to parse (operator bug): "
                                      f"{m.kind} {m.original}→{m.mutated}"))
    return results, len(invalid)


def run_mutation(
    file_path: str,
    project_root: str,
    budget: int = 50,
    timeout_s: int = 60,
    dry_run: bool = False,
    tests_glob: str | None = None,
    precomputed_baseline: dict | None = None,
    mutant_slice: tuple[int, int] | None = None,
) -> dict:
    """对单个 .gd 文件做变异测试并出统一 JSON 契约。

    budget 控制 mutant 上限（mutation 风暴防御——100 变异 × GUT 全套 = 时间爆炸）。
    kill 判定 = 失败测试名集合相对 baseline 的差集（mutant 引入的新失败），
    而非 exit code——pre-existing failure 不算 killed。
    dry_run（SPEC-009）：只清点变异点不跑 GUT——CI 快闸与成本评估入口。
    tests_glob（SPEC-009）：受影响测试子集，passed to _run_gut_on_project。
    precomputed_baseline（SEE-1321 SPEC-008）：同次运行内共享的 baseline
    快照 {rc, tail, elapsed}——单进程冷启动产物，自适应 timeout 锚点。
    mutant_slice（SEE-1321 SPEC-003 修订）：per-mutant 并发 worker 的分片，
    只跑 [start, stop) 的 mutants；必须携带共享 baseline（worker 各自跑
    baseline 会 N 倍放大启动税且并发期耗时污染 timeout 锚）。
    返回 {"tool":"mutation", "ok", "summary", "failures"}；ok=True 表示
    所有 mutant 均被测试杀死。
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"mutation target not found: {file_path}")

    if mutant_slice is not None and precomputed_baseline is None:
        raise ValueError(
            "mutant_slice workers require a precomputed baseline snapshot — "
            "each worker running its own baseline would multiply the startup "
            f"tax N-fold and pollute the adaptive timeout anchor: {mutant_slice}")

    if tests_glob is None:
        tests_glob = _auto_scan_tests_glob(str(path), project_root)
    scoped = tests_glob is not None

    original_src = path.read_text(encoding="utf-8")
    backup = original_src  # 永不改写原文件之外的任何东西（kill rate 证据隔离）

    try:
        mutants = _collect_mutations(original_src, str(path))
    except Exception as e:
        return _setup_error_result(str(path), e)

    if dry_run:
        return _dry_run_result(str(path), mutants, budget, scoped)

    if not mutants:
        return _no_sites_result(str(path), scoped)

    mutants = mutants[:budget]
    # budget 截断后可能为空（budget=0 / 极小预算）——零 mutant 不得触发
    # baseline GUT（硬ener 反例实证：白付一轮 108s baseline）。
    if not mutants:
        return _no_sites_result(str(path), scoped)
    results, invalid_count = _record_invalid_mutants(original_src, mutants,
                                                     mutant_slice)

    # 缺陷1 兜底恢复守卫（SEE-1319 与 SEE-1321 内存注入共存）：内存注入路径
    # 磁盘目标文件本就不写变异；守卫保留以覆盖 interrupt/未知异常路径幂等恢复。
    with _interrupt_guard(path, backup):
        outcome = _run_mutant_loop_with_aborts(
            path, project_root, original_src, backup, mutants, tests_glob,
            timeout_s, results, precomputed_baseline, mutant_slice)
    if isinstance(outcome, dict):
        return outcome  # baseline 中止契约（timeout / launch failure）

    counts, baseline_reused = outcome

    return _mutation_report(str(path), *counts, invalid_count, budget,
                            results, scoped, baseline_reused)


def make_sigterm_restore_handler(file_path: str, backup: str):
    """SIGTERM/SIGHUP 硬杀不保证执行 finally——handler 内先恢复再终止。

    exit code 128+sign（POSIX shell 约定），用 os._exit 跳过任何可能再次
    抛异常的清理路径，保证恢复写盘是最后动作。
    """
    def _handler(signum, frame):
        try:
            Path(file_path).write_text(backup, encoding="utf-8")
        except OSError as e:
            # 恢复失败必须显式暴露——静默吞掉会把变异体遗留进共享分支
            print(f"mutation SIGTERM restore failed for {file_path}: {e}",
                  file=sys.stderr)
        # SPEC-015：os._exit 跳过 finally 与 atexit——临时文件（cfg JSON +
        # 宿主 .gd）必须在此显式清理，否则中断后工作树残留（QA MEDIUM 实测
        # 12 个 untracked 文件）。
        cleanup_temp_files()
        os._exit(128 + signum)
    return _handler


class _interrupt_guard:
    """缺陷1 兜底恢复守卫：任何异常/硬杀路径均恢复原文件。

    per-mutant finally 覆盖不到 SIGTERM/SIGINT 硬杀与循环外未知异常（Revy QA
    TaskStop 实证遗留变异体进 git diff）。SIGTERM/SIGHUP 走 handler 主动恢复
    （硬杀不保证 finally），其余异常走 context manager finally 兜底——两路合
    一后原样上抛，不吞中断语义。信号安装失败（非主线程/受限环境）降级为仅
    finally 兜底。
    """

    def __init__(self, path: Path, backup: str):
        self._path = path
        self._backup = backup
        self._handlers: dict = {}

    def __enter__(self):
        restore_handler = make_sigterm_restore_handler(str(self._path), self._backup)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                self._handlers[sig] = signal.signal(sig, restore_handler)
            except (ValueError, OSError):
                pass  # 非主线程/受限环境：守卫降级为 finally 兜底
        return self

    def __exit__(self, exc_type, exc, tb):
        for sig, prev in self._handlers.items():
            try:
                signal.signal(sig, prev)
            except (ValueError, OSError):
                pass
        # 幂等恢复——正常路径与 per-mutant finally 重复写同内容无副作用。
        try:
            self._path.write_text(self._backup, encoding="utf-8")
        except OSError as e:
            print(f"mutation restore failed for {self._path}: {e}", file=sys.stderr)
        return False


def _auto_scan_tests_glob(file_path: str, project_root: str) -> str | None:
    """SPEC-001：目标 → 扫描 tests/ 引用子集；零命中退回全量（scoped=False）。"""
    try:
        from .testscan import derive_tests_glob
        return derive_tests_glob(_res_path_of(file_path, project_root),
                                 Path(project_root) / "tests")
    except Exception as e:
        print(f"auto scan failed, falling back to full suite: {e}",
              file=sys.stderr)
        return None


def _res_path_of(file_path: str, project_root: str) -> str:
    real = Path(file_path).resolve().as_posix()
    root = Path(project_root).resolve().as_posix()
    rel = real[len(root):].lstrip("/") if real.startswith(root) \
        else Path(file_path).name
    return "res://" + rel


def _setup_error_result(file: str, err) -> dict:
    return {
        "tool": "mutation",
        "ok": False,
        "summary": {"file": file, "mutants": 0, "error": str(err)},
        "failures": [{"reason": f"mutation setup failed: {err}"}],
    }


def _no_sites_result(file: str, scoped: bool | None = None) -> dict:
    return {
        "tool": "mutation",
        "ok": True,
        "summary": {"file": file, "mutants": 0, "killed": 0, "survived": 0,
                    "timeout": 0, "kill_rate": 0.0, "note": "no mutation sites",
                    "scoped": bool(scoped)},
        "failures": [],
    }


def _run_mutant_loop_with_aborts(path, project_root, original_src, backup,
                                 mutants, tests_glob, timeout_s, results,
                                 precomputed_baseline=None,
                                 mutant_slice=None):
    """baseline 中止形态转统一 JSON（timeout / godot 启动失败）；否则返回计数。"""
    try:
        return _run_mutant_loop(
            path, project_root, original_src, backup,
            _valid_mutants(original_src, mutants), tests_glob, timeout_s,
            results, precomputed_baseline, mutant_slice,
        )
    except subprocess.TimeoutExpired:
        return _run_error_result(
            str(path),
            f"baseline GUT timed out after {timeout_s}s — mutation data untrustworthy",
            f"baseline GUT timed out after {timeout_s}s (project's baseline run exceeds "
            f"the timeout — raise --timeout or check why tests are this slow)",
        )
    except _BaselineLaunchFailure as e:
        return _run_error_result(
            str(path),
            "godot launch failed on baseline — mutation data untrustworthy",
            f"godot launch failed on baseline run: {e.tail.strip()[:200]}",
        )


def _mutation_report(file: str, killed: int, survived: int, timeout_count: int,
                     run_error_count: int, suspect_count: int, invalid_count: int,
                     budget: int, results: list[dict], scoped: bool = False,
                     baseline_reused: bool = False) -> dict:
    total = killed + survived + timeout_count + run_error_count + invalid_count + suspect_count
    kill_rate = killed / total if total else 0.0
    failures = [r for r in results if r["verdict"] != "killed"]
    return {
        "tool": "mutation",
        # suspect = 「无法证明被杀死」——语义上与 survived 同挡 gate ok
        # （硬ener 反例实证：suspect>0 时 ok=True 是自欺）。
        "ok": survived == 0 and timeout_count == 0 and run_error_count == 0
              and suspect_count == 0,
        "summary": {
            "file": file,
            "mutants": total,
            "killed": killed,
            "survived": survived,
            "suspect": suspect_count,
            "timeout": timeout_count,
            "run_errors": run_error_count,
            "invalid_mutants": invalid_count,
            "kill_rate": round(kill_rate, 4),
            "budget": budget,
            "adaptive_timeout_s": _adaptive_timeout,
            "scoped": scoped,
            "baseline_reused": baseline_reused,
        },
        "failures": failures,
    }


# per-mutant 自适应上限（baseline 耗时决定，_run_mutant_loop 内写入）
_adaptive_timeout = 0


def _dry_run_result(file: str, mutants: list[Mutant], budget: int,
                    scoped: bool | None = None) -> dict:
    """SPEC-009：清点模式——零 GUT 调用，mutant 清单即产出。"""
    kinds: dict[str, int] = {}
    for m in mutants[:budget]:
        kinds[m.kind] = kinds.get(m.kind, 0) + 1
    return {
        "tool": "mutation",
        "ok": True,
        "summary": {"file": file, "mutants": len(mutants[:budget]),
                    "dry_run": True, "kinds": kinds, "scoped": bool(scoped)},
        "failures": [],
    }


def _run_baseline(project_root: str, tests_glob: str | None, timeout_s: int
                  ) -> tuple[int, str, float]:
    """跑 baseline 并计时；TimeoutExpired 由调用方按中止契约处理。"""
    t0 = time.monotonic()
    rc, tail = _run_gut_on_project(project_root, timeout_s=timeout_s, tests_glob=tests_glob)
    return rc, tail, time.monotonic() - t0


def _run_mutant_loop(
    path, project_root: str, original_src: str, backup: str,
    mutants: list[Mutant], tests_glob: str | None, timeout_s: int,
    results: list[dict], precomputed_baseline: dict | None = None,
    mutant_slice: tuple[int, int] | None = None,
) -> tuple[tuple[int, int, int, int, int], bool]:
    """逐 mutant 跑 GUT 并分类判定；返回 ((killed, survived, timeout, run_error, suspect), baseline_reused)。

    mutant_slice（SPEC-003 修订）：per-mutant 并发 worker 分片，只跑
    [start, stop) 区间（下界含、上界不含）；分片工作线程复用共享 baseline，
    不自跑 baseline。
    """
    global _adaptive_timeout
    killed = survived = timeout_count = run_error_count = suspect_count = 0

    with tempfile.TemporaryDirectory() as workdir:
        backup_path = os.path.join(workdir, "original.gd")
        shutil.copy2(str(path), backup_path)

        # Baseline：原文件的 GUT 结果（失败测试名集合）。pre-existing 失败
        # 不属于任何 mutant——kill 判定只看相对 baseline 的【新增】失败。
        # SEE-1321 SPEC-008：同次运行内可复用上游单进程冷启动产出的 baseline
        # 快照（同一份代码状态）；无快照时亲自跑。快照 elapsed 也是自适应
        # timeout 锚（SPEC-005：并发前锚定，worker 不采并发期耗时）。
        if precomputed_baseline is not None:
            baseline_rc = int(precomputed_baseline["rc"])
            baseline_tail = str(precomputed_baseline["tail"])
            baseline_elapsed = float(precomputed_baseline["elapsed"])
            baseline_reused = True
        else:
            # Revy QA retest 实证：baseline 必然花最久（实测项目 ~108s），默认
            # --timeout 60 下 TimeoutExpired 漏网成原始 traceback（无统一 JSON）——
            # 与 per-mutant 循环的 timeout 同款处理：归 run_error JSON 契约。
            baseline_rc, baseline_tail, baseline_elapsed = _run_baseline(
                project_root, tests_glob, timeout_s)
            baseline_reused = False
        # SPEC-009 自适应 timeout：per-mutant 上限 = baseline 耗时 × k（不降
        # 低于调用方显式 timeout——只放大，不收紧）。
        _adaptive_timeout = max(timeout_s, int(baseline_elapsed * _TIMEOUT_SCALE) + 1)

        if baseline_rc != 0 and _looks_like_godot_launch_failure(baseline_tail):
            raise _BaselineLaunchFailure(baseline_tail)

        baseline_failures = _parse_failing_tests(baseline_tail)
        baseline_dirty = bool(baseline_failures)

        host = seed_host_script(str(Path(project_root)))
        try:
            start, stop = (mutant_slice if mutant_slice is not None
                           else (0, len(mutants)))
            for seq, m in enumerate(mutants):
                if not start <= seq < stop:
                    continue
                verdict = _run_single_mutant(m, path, project_root, original_src,
                                             backup, tests_glob, seq,
                                             baseline_failures, baseline_dirty)
                results.append(verdict)
        finally:
            host.unlink(missing_ok=True)
            forget_temp(host)

    tally = {"killed": 0, "survived": 0, "timeout": 0, "run_error": 0, "suspect": 0}
    for r in results:
        if r["verdict"] in tally:
            tally[r["verdict"]] += 1
    return ((tally["killed"], tally["survived"], tally["timeout"],
             tally["run_error"], tally["suspect"]), baseline_reused)


def _run_single_mutant(m: Mutant, path, project_root: str, original_src: str,
                       backup: str, tests_glob: str | None, seq: int,
                       baseline_failures: set[str], baseline_dirty: bool) -> dict:
    """内存变异注入 → 跑 GUT → 分类；磁盘目标文件全程保持原始内容（SPEC-007）。"""
    global _ACTIVE_ENV, _ACTIVE_SCRIPT
    mutated_src = _apply_mutation(original_src, m)
    env_extra, cfg_path = mutant_env(_res_path_of(str(path), str(Path(project_root))),
                                     mutated_src, str(Path(project_root)), seq)
    _ACTIVE_ENV = env_extra
    _ACTIVE_SCRIPT = host_script_res()
    try:
        try:
            rc, tail = _run_gut_on_project(
                project_root, timeout_s=_adaptive_timeout, tests_glob=tests_glob)
            if rc == 3 and "GQT_INJECT_FAIL" in tail:
                # 注入失败绝不能当 kill/survive——宿主没把变异送进引擎，
                # 跑的是原代码，任何判定都是假证据。
                return _mutant_record(m, "run_error",
                                      f"memory injection failed: {tail.strip()[:200]}")
            verdict = _classify_mutant_run(m, rc, tail, baseline_failures,
                                           baseline_dirty)
        except subprocess.TimeoutExpired:
            verdict = _mutant_record(m, "timeout",
                                     f"GUT timed out after {m.kind} {m.original}→{m.mutated}")
    finally:
        _ACTIVE_ENV = None
        _ACTIVE_SCRIPT = GUT_SCRIPT_RES_PATH
        Path(cfg_path).unlink(missing_ok=True)
        forget_temp(cfg_path)
    return verdict


class _BaselineLaunchFailure(Exception):
    """baseline run 命中 godot 启动失败——mutant 数据不可信，run_mutation 层转中止契约。"""

    def __init__(self, tail: str):
        self.tail = tail
        super().__init__(tail[:200])


def _classify_mutant_run(m: Mutant, rc: int, tail: str,
                         baseline_failures: set[str], baseline_dirty: bool) -> dict:
    """单 mutant run 结果分类（killed/survived/suspect/run_error）。

    测试失败时区分 pre-existing（基线就有）与 mutant 引入的新失败：只有新失败
    才算 killed（Revy QA 第二层假阳性实证：某些项目基线无头环境 rc=1 是常态，
    按 rc 判定则全部误杀）。
    """
    if rc != 0 and _looks_like_godot_launch_failure(tail):
        # godot 进程级失败（脚本没加载起来）≠ 测试抓到 mutant——Revy QA FAIL
        # 实证：绝对 -s 路径恒 rc=1 被误判 killed。记 run_error，宁可报错不报通过。
        return _mutant_record(m, "run_error",
                              f"godot launch failed (tests did not run): {tail.strip()[:200]}")
    if rc == 0:
        # 测试通过 = mutant 存活（测试没抓住它）
        return _mutant_record(m, "survived",
                              f"tests passed after {m.kind} {m.original}→{m.mutated}")
    new_failures = _parse_failing_tests(tail) - baseline_failures
    if new_failures:
        return _mutant_record(m, "killed",
                              f"new failures after {m.kind} {m.original}→{m.mutated}: "
                              f"{sorted(new_failures)[:3]}")
    # SPEC-010 suspect 标签：baseline 脏 ∧ 差集空——无法区分「测试没抓住 mutant」
    # 与「测试根本没跑到该区域」，保守标记（不计 killed 也不计 survived）。
    if baseline_dirty:
        return _mutant_record(m, "suspect",
                              f"dirty baseline ∧ empty diff after {m.kind} "
                              f"{m.original}→{m.mutated} — cannot distinguish "
                              f"survive from unexercised")
    return _mutant_record(m, "survived",
                          f"only pre-existing failures after {m.kind} "
                          f"{m.original}→{m.mutated} — not a kill")
