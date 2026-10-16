"""Mutation runner: source-level GDScript mutants + kill/survive/timeout verdicts.

SEE-1268 自研（gdtoolkit lark AST 定位 → 源码 span 替换 → 跑 GUT 看该 mutant
是否被杀死）。kill rate 永不进验收证据（owner-order L5 条文）。

P0' 判定原则落点：每个 mutant 输出机器可判的 killed/survived/timeout 布尔 +
结构化 failures；exit 0=全部杀死 1=有存活/超时。

SEE-1312 增强：变异点扩展（三元/守卫/第二梯队算子/常量）、span 替换定位、
--dry-run/--tests/自适应 timeout 成本控制、invalid_mutant 预检与 suspect 标签。
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

from .inject import (cleanup_temp_files, forget_temp, godot_command, host_script_res,
                     _impl_fs_path, mutant_env, seed_host_script, wslpath_win)
# 统一 mutant 明细记录形态（SEE-1416 扩列）与结果塑形契约在 reporting.py
# （<800 行纪律拆分）；re-export 保持既有 import / monkeypatch 名称空间稳定。
from .reporting import (  # noqa: F401
    SURVIVOR_DETAIL_KEYS,
    SURVIVORS_CAP,
    _dry_run_result,
    _mutant_record,
    _no_sites_result,
    _record_invalid_mutants,
    _run_error_result,
    _setup_error_result,
    _valid_mutants,
    line_text_of,
    survivors_projection,
)
from .godot_gate import GlobalGodotGate
from .diffengine import apply_timeout_streaks, budget_on_retry, make_entry, \
    plan_diff, streak_for, untouched_ranges
from .resume2 import ResumeStoreV2
from .scopemanifest import build_manifest
from ..timing import (BUDGET_CAP_S, explicit_timing_meta, project_relative,
                      resolve_budget, suggested_action)


# GUT 执行域判定原语在 gutrun.py（SEE-1420 <800 行纪律拆分）；re-export
# 保持既有 import / monkeypatch 名称空间稳定。
from .gutrun import (  # noqa: F401,E402
    GUT_SCRIPT_RES_PATH,
    _GUT_RUN_EVIDENCE,
    _looks_like_godot_launch_failure,
    _parse_failing_tests,
)


def _godot_project_path(project_root: str) -> str:
    """win64 godot 的 --path 语义：/mnt/... 形式的 WSL 绝对路径会被拒
    （'Invalid project path'，硬化 实机实证——0.1s 静默 rc≠0、零测试执行）。

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
    # SEE-1400 §SPEC-003：跨进程槽位闸包裹真实 godot 子进程（等槽时间不计
    # timeout——timeout 语义只管测试进程本身）。签名与 3 参 fake 契约不变。
    with GlobalGodotGate(project_root):
        r = subprocess.run(
            [godot_command(), "--headless", "--path",
             _godot_project_path(project_root),
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
    # SPEC-017（§SPEC-016 实证）：注入宿主的 printerr 全落 stderr——GUT 运行
    # 流落 stdout，而 GQT_INJECT_FAIL 只落 stderr。旧逻辑 stdout 非空时丢弃
    # stderr，rc==3 注入失败在 stdout 里只有 73 字节引擎 banner，
    # "GQT_INJECT_FAIL in tail" 守卫永不命中 → 注入失败被吞成 survived
    # （12/17 假 survived）。修复：tail = 运行流 + stderr 尾部拼接，两者都
    # 进判定窗口；注入失败与 GUT 运行流互斥出现，拼接不会互相挤出。
    tail = (r.stdout[-200000:] if r.stdout else "") + "\n--- gqt: stderr ---\n" + r.stderr[-10000:]
    return r.returncode, tail


# 子进程环境/入口脚本的活动状态（mutant 循环内动态切换，见 _run_single_mutant）
_ACTIVE_ENV: dict | None = None
_ACTIVE_SCRIPT: str = GUT_SCRIPT_RES_PATH


# 自适应 timeout 倍数：per-mutant 上限 = baseline 实测耗时 × 该倍数。
_TIMEOUT_SCALE = 2.0


def adaptive_timeout_for(baseline_elapsed: float, timeout_s: int,
                         factor: float | None = None) -> int:
    """§SPEC-1420-02：adaptive = max(timeout_s, baseline×F+1)。

    factor None → 模块默认 _TIMEOUT_SCALE（2.0，L5 定案"默认不动"）；
    显式 factor 只放大不收紧（低于显式 timeout 的派生值被显式值覆盖）。
    """
    k = _TIMEOUT_SCALE if factor is None else float(factor)
    return max(timeout_s, int(baseline_elapsed * k) + 1)


def run_mutation(
    file_path: str,
    project_root: str,
    budget: int = 50,
    timeout_s: int = 60,
    dry_run: bool = False,
    tests_glob: str | None = None,
    precomputed_baseline: dict | None = None,
    mutant_slice: tuple[int, int] | None = None,
    timing_store=None,
    resume: bool = False,
    binary_kind: str | None = None,
    mutation_warning: int | None = None,
    timeout_factor: float | None = None,
    mutate_all: bool = False,
    retry_ids: list[str] | None = None,
    resume2_store=None,
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
    resume（SEE-1400 §SPEC-002）：v1 遗留开关。SEE-1420 起差分**默认开启**
    （manifest 在场即差分，定案 D4）；--mutate-all 显式退路；本 flag 仅保留
    summary.resumed 键在场性（无复用时为 0），语义已被差分吸收。
    mutate_all（§SPEC-1420-07）：--mutate-all 显式全量——忽略旧 manifest，
    全位点执行（仍落盘 v2）。
    retry_ids（并发 worker 注入面）：父层（executor）已做差分规划与预算
    截断，worker 只执行这些 stable_id；注入时 worker 不再自建 plan、
    不写 manifest 索引层（父层单点写，规避并发竞态）。
    mutation_warning（§SPEC-1420-01）：按截断前全量位点数计数的告警阈值
    （默认 50）——超阈值 stderr 告警 + summary.attention 建议拆分。
    timeout_factor（§SPEC-1420-02）：覆盖 _TIMEOUT_SCALE 的自适应倍数
    （默认 None=2.0 不动）；两者均不进任何 resume key（定案 D6 负面清单）。
    返回 {"tool":"mutation", "ok", "summary", "failures"}；ok=True 表示
    所有 mutant 均被测试杀死。
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"mutation target not found: {file_path}")

    if ((mutant_slice is not None or retry_ids is not None)
            and precomputed_baseline is None):
        raise ValueError(
            "slice/retry_ids workers require a precomputed baseline snapshot — "
            "each worker running its own baseline would multiply the startup "
            f"tax N-fold and pollute the adaptive timeout anchor: {mutant_slice}")

    if tests_glob is None:
        tests_glob = _auto_scan_tests_glob(str(path), project_root)
    scoped = tests_glob is not None

    original_src = path.read_text(encoding="utf-8")
    backup = original_src  # 永不改写原文件之外的任何东西（kill rate 证据隔离）

    try:
        raw_mutants = _collect_mutations(original_src, str(path))
    except Exception as e:
        return _setup_error_result(str(path), e)

    # P1 manifest 标定（定案 D1/D2/D3 + L6）：scope 提取 → no-mutate 过滤
    # → stable id。total_sites 按过滤后位点数计（无位点 scope 不进 manifest）。
    header_hash, spans, mutants, scope_hashes = build_manifest(
        original_src, str(path), raw_mutants)
    total_sites = len(mutants)
    attention: str | None = None
    if mutation_warning is not None and total_sites > mutation_warning:
        attention = (f"{total_sites} mutation sites exceed "
                     f"--mutation-warning {mutation_warning} — consider "
                     f"splitting this file")
        print(f"mutation-warning: {total_sites} mutation sites exceed "
              f"--mutation-warning {mutation_warning} in {path} — consider "
              f"splitting this file", file=sys.stderr)

    if dry_run:
        # SPEC-009：dry-run 跳过 timing store 读写（清点模式零 IO）。
        return _dry_run_result(str(path), mutants, budget, scoped,
                               total_sites=total_sites)

    if not mutants:
        return _no_sites_result(str(path), scoped)

    # P1 差分（定案 D4）：串行路径自建 plan；worker 注入路径只执行 retry_ids。
    store2 = resume2_store if resume2_store is not None else ResumeStoreV2(
        project_root, str(path), tests_glob, binary_kind=binary_kind)
    prior_entries: dict = {}
    reused: dict = {}
    untouched: list = []
    disclosures: dict = {}
    if attention is not None:
        disclosures["attention"] = attention
    if retry_ids is not None:
        by_id = {m.stable_id: m for m in mutants}
        missing = [i for i in retry_ids if i not in by_id]
        if missing:
            raise ValueError(
                f"retry_ids not found in current scan (source drift?): "
                f"{missing[:5]}")
        executed_list = [by_id[i] for i in retry_ids]
        disclosures["executed"] = len(executed_list)
    else:
        store2.disclose_v1_superseded()
        old_manifest = store2.load_manifest()
        prior_entries = store2.load_entries()
        plan = plan_diff(header_hash, scope_hashes, old_manifest, mutants,
                         prior_entries, mutate_all=mutate_all)
        executed_list, untouched = budget_on_retry(plan.executed, budget)
        reused = plan.reused
        if plan.diff_active or mutate_all:
            disclosures.update({
                "retry_total": len(plan.executed),
                "executed": len(executed_list),
                "untouched_ranges": untouched_ranges(untouched),
                "diff_skipped": sorted(reused),
            })
        if mutant_slice is not None:
            executed_list = list(executed_list[slice(*mutant_slice)])
    # budget 吃光且无可报告复用——零 mutant 不得触发 baseline GUT（硬化
    # 反例实证：白付一轮 108s baseline）。
    if not executed_list and not reused:
        return _no_sites_result(str(path), scoped)
    if total_sites > budget:
        disclosures["total_sites"] = total_sites
        disclosures.setdefault("executed", len(executed_list))

    # 复用记录重建（全签名已验）——串行路径在此落报告面；worker 路径的
    # reused 为空（父层合并后单点注入，避免分片重复计数）。
    results: list[dict] = []
    by_id_all = {m.stable_id: m for m in mutants}
    for sid, entry in reused.items():
        m = by_id_all.get(sid)
        if m is not None:
            results.append(_mutant_record(m, entry["verdict"],
                                          entry.get("reason", ""),
                                          src=original_src))
    # _record_invalid_mutants 返回新列表——与 reused 记录拼接而非覆盖。
    invalid_results, invalid_count = _record_invalid_mutants(
        original_src, executed_list, None)
    results = results + invalid_results
    executed_ids = {m.stable_id for m in executed_list}
    # P1：manifest 索引层先行写入（中断安全——索引在场 + 条目增量，下轮
    # 差分对已执行子集正确复用，未落盘位点按 new_site 重跑，保守正确）。
    # worker 注入路径不写（父层合并后单点写，规避并发竞态）。
    if retry_ids is None:
        store2.write_manifest(header_hash, scope_hashes,
                              ResumeStoreV2._sha1_text(original_src),
                              project_relative(project_root, str(path)))

    effective_timeout, timing_meta = _negotiate_budget(
        timing_store, timeout_s, precomputed_baseline)

    # 缺陷1 兜底恢复守卫（SEE-1319 与 SEE-1321 内存注入共存）：内存注入路径
    # 磁盘目标文件本就不写变异；守卫保留以覆盖 interrupt/未知异常路径幂等恢复。
    with _interrupt_guard(path, backup):
        outcome = _run_mutant_loop_with_aborts(
            path, project_root, original_src, backup, executed_list,
            tests_glob, effective_timeout, precomputed_baseline,
            store2, prior_entries, scope_hashes, timeout_factor)
    if isinstance(outcome, dict):
        return outcome  # baseline 中止契约（timeout / launch failure）

    raw_pairs, baseline_reused, baseline_elapsed = outcome
    resumed_count = len(reused)

    # D5 落定（权威口径）：streak/pinned 转化 + 毒 run 修正——循环内乐观
    # 落盘已保证中断安全，此处毒窗口时改写条目为未递增 streak。
    streaks = apply_timeout_streaks(raw_pairs, prior_entries, scope_hashes,
                                    time.time())
    results.extend(streaks.records)
    if streaks.suspended:
        for e in streaks.entries:
            store2.record_entry(e)
        print("mutation: timeout streak suspended: suspected infra run",
              file=sys.stderr)

    _record_timing_sample(timing_store, baseline_reused, baseline_elapsed,
                          timing_meta)

    return _mutation_report(str(path), *_tally(results), invalid_count,
                            budget, results, scoped, baseline_reused,
                            timing_meta,
                            resumed_count if (resumed_count or resume) else None,
                            disclosures=disclosures or None,
                            pinned_count=len(streaks.pinned_ids))


def _negotiate_budget(timing_store, timeout_s: int,
                      precomputed_baseline: dict | None
                      ) -> tuple[int, dict]:
    """SEE-1356 L1 (§SPEC-L1-01)：预算协商。缓存只喂预算锚——baseline 永远
    实测（判定语义）；worker 分片（precomputed_baseline 携带者）跳过
    store 读写（executor 单进程拥有协商与采样）。"""
    timing_meta = explicit_timing_meta()
    if timing_store is None or precomputed_baseline is not None:
        return timeout_s, timing_meta
    hist = timing_store.read()
    anchor = hist["last"] if hist else None
    effective_timeout, timing_meta["timing_source"] = resolve_budget(
        timeout_s, anchor)
    timing_meta["samples_n"] = hist["samples_n"] if hist else 0
    timing_meta["baseline_p90"] = hist["p90"] if hist else None
    return effective_timeout, timing_meta


def _record_timing_sample(timing_store, baseline_reused: bool,
                          baseline_elapsed: float, timing_meta: dict) -> None:
    """baseline 永远实测——只有亲自跑过 baseline 才采样（worker 不重复计数）。
    报告字段反映本轮采样后的 store 状态（samples_n/baseline_p90 为观察字段）。"""
    if timing_store is None or baseline_reused:
        return
    timing_store.record(baseline_elapsed)
    fresh = timing_store.read()
    if fresh:
        timing_meta["samples_n"] = fresh["samples_n"]
        timing_meta["baseline_p90"] = fresh["p90"]


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
    """SPEC-001：目标 → 扫描 tests/ 引用子集；零命中退回全量（scoped=False）。

    SEE-1405 §SPEC-004：project_root 显式传给 derive_tests_glob——内部
    换源 gdself 正向索引（间接消费闭包可见）；扫描失败诚实退全量。
    """
    try:
        from .testscan import derive_tests_glob
        return derive_tests_glob(_res_path_of(file_path, project_root),
                                 Path(project_root) / "tests",
                                 project_root=project_root)
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


def _tally(results: list[dict]) -> tuple[int, int, int, int, int]:
    """results（含复用/invalid/streak 终态记录）→ 五档计数。"""
    tally = {"killed": 0, "survived": 0, "timeout": 0, "run_error": 0,
             "suspect": 0}
    for r in results:
        if r["verdict"] in tally:
            tally[r["verdict"]] += 1
    return (tally["killed"], tally["survived"], tally["timeout"],
            tally["run_error"], tally["suspect"])


def _run_mutant_loop_with_aborts(path, project_root, original_src, backup,
                                 executed_list, tests_glob, timeout_s,
                                 precomputed_baseline=None,
                                 store2=None, prior_entries=None,
                                 scopes=None,
                                 timeout_factor: float | None = None):
    """baseline 中止形态转统一 JSON（timeout / godot 启动失败）；否则返回 raw。"""
    try:
        return _run_mutant_loop(
            path, project_root, original_src, backup,
            _valid_mutants(original_src, executed_list), tests_glob, timeout_s,
            precomputed_baseline, store2, prior_entries, scopes,
            timeout_factor,
        )
    except subprocess.TimeoutExpired:
        # SEE-1356 L1 (§SPEC-L1-03)：预算超时附机器可执行的补救指令。
        return _run_error_result(
            str(path),
            f"baseline GUT timed out after {timeout_s}s — mutation data untrustworthy",
            f"baseline GUT timed out after {timeout_s}s (project's baseline run exceeds "
            f"the timeout — raise --timeout or check why tests are this slow)",
            hint=suggested_action(BUDGET_CAP_S),
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
                     baseline_reused: bool = False,
                     timing_meta: dict | None = None,
                     resumed: int | None = None,
                     disclosures: dict | None = None,
                     pinned_count: int = 0) -> dict:
    total = killed + survived + timeout_count + run_error_count + invalid_count + suspect_count
    kill_rate = killed / total if total else 0.0
    failures = [r for r in results if r["verdict"] != "killed"]
    # SPEC-017：runtime invalid_mutant（impl 编译被拒）与静态预检 invalid 合并
    # 计数——同档同语义，failures 里已留明细。
    invalid_count += sum(1 for r in results if r["verdict"] == "invalid_mutant")
    summary = {
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
    }
    # SEE-1400 §SPEC-002：仅在 resume 跳过语义在场的 run 报 skipped 数——
    # 常规 run 的 summary 与升级前逐字节同构（既有消费方零感知）。
    if resumed is not None:
        summary["resumed"] = resumed
    # §SPEC-1420-08（D5）：timeout_pinned 独立档——永不并入 killed、不计入
    # ok 门禁失败档；报告持续可见并披露降级来源（failures 明细 reason）。
    if pinned_count:
        summary["timeout_pinned"] = pinned_count
    # §SPEC-1420-01/07：截断面 N/M 披露 + 差分披露（retry_total/executed/
    # untouched_ranges/diff_skipped）。
    if disclosures:
        summary.update(disclosures)
    # SEE-1356 L1 (§SPEC-L1-03)：报告字段 timing_source/samples_n/baseline_p90
    # ——timing store 未接入时缺省 explicit（显式 timeout 即最终预算）。
    summary.update(timing_meta or explicit_timing_meta())
    # SEE-1416 §SPEC-1416-1：summary 自足——非 killed verdict 的 top 明细
    # 投影进场（键恒在场，`| jq '.summary'` 切片也不失明；零命中为空数组）。
    summary["survivors"], truncated = survivors_projection(failures)
    if truncated:
        summary["survivors_truncated"] = True
    return {
        "tool": "mutation",
        # suspect = 「无法证明被杀死」——语义上与 survived 同挡 gate ok
        # （硬化 反例实证：suspect>0 时 ok=True 是自欺）。
        "ok": survived == 0 and timeout_count == 0 and run_error_count == 0
              and suspect_count == 0,
        "summary": summary,
        "failures": failures,
    }


# per-mutant 自适应上限（baseline 耗时决定，_run_mutant_loop 内写入）
_adaptive_timeout = 0


def _run_baseline(project_root: str, tests_glob: str | None, timeout_s: int
                  ) -> tuple[int, str, float]:
    """跑 baseline 并计时；TimeoutExpired 由调用方按中止契约处理。"""
    t0 = time.monotonic()
    rc, tail = _run_gut_on_project(project_root, timeout_s=timeout_s, tests_glob=tests_glob)
    return rc, tail, time.monotonic() - t0


def _run_mutant_loop(
    path, project_root: str, original_src: str, backup: str,
    mutants: list[Mutant], tests_glob: str | None, timeout_s: int,
    precomputed_baseline: dict | None = None,
    store2=None, prior_entries: dict | None = None, scopes: dict | None = None,
    timeout_factor: float | None = None,
) -> tuple[list, bool, float]:
    """逐 mutant 跑 GUT 并分类判定；返回 (raw_pairs, baseline_reused, baseline_elapsed)。

    mutants = 已过差分/预算的【执行面】（串行：budget_on_retry 产物；worker：
    父层注入的 retry_ids 子集）。verdict 记录不在此处进 results——D5 streak
    是窗口级判定，由调用方 apply_timeout_streaks 落定后统一扩展。
    条目增量落盘（中断安全）：每 verdict 立即以乐观 streak 写 v2 条目；
    毒 run 守卫在窗口收尾修正（apply_timeout_streaks + 调用方改写）。
    baseline 永远实测（判定语义锚）；快照 elapsed 只喂预算锚。
    timeout_factor（§SPEC-1420-02）：None = 模块默认 _TIMEOUT_SCALE。
    """
    global _adaptive_timeout
    raw_pairs: list[tuple] = []
    prior_entries = prior_entries or {}

    with tempfile.TemporaryDirectory() as workdir:
        backup_path = os.path.join(workdir, "original.gd")
        shutil.copy2(str(path), backup_path)

        # Baseline：原文件的 GUT 结果（失败测试名集合）。pre-existing 失败
        # 不属于任何 mutant——kill 判定只看相对 baseline 的【新增】失败。
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
        # SPEC-009 自适应 timeout：per-mutant 上限 = baseline 耗时 × F（§SPEC-
        # 1420-02：F 可由 --timeout-factor 覆盖；不降低于显式 timeout——只放大）。
        _adaptive_timeout = adaptive_timeout_for(baseline_elapsed, timeout_s,
                                                 timeout_factor)

        if baseline_rc != 0 and _looks_like_godot_launch_failure(baseline_tail):
            raise _BaselineLaunchFailure(baseline_tail)

        baseline_failures = _parse_failing_tests(baseline_tail)
        baseline_dirty = bool(baseline_failures)

        host = seed_host_script(str(Path(project_root)))
        try:
            for seq, m in enumerate(mutants):
                verdict = _run_single_mutant(m, path, project_root,
                                             original_src, backup, tests_glob,
                                             seq, baseline_failures,
                                             baseline_dirty)
                raw_pairs.append((m, verdict))
                # 中断安全增量落盘：乐观 streak（毒窗口收尾修正）。
                # SEE-1405 发现三毒条目防护由 resume2.record_entry 承接
                # （启动失败类 run_error 拒收——verdict 零信息量）。
                if store2 is not None:
                    prior = prior_entries.get(m.stable_id)
                    streak, v2_verdict = streak_for(prior,
                                                    verdict["verdict"])
                    store2.record_entry(make_entry(m, verdict, v2_verdict,
                                                   streak, scopes or {},
                                                   time.time()))
        finally:
            host.unlink(missing_ok=True)
            forget_temp(host)

    return raw_pairs, baseline_reused, baseline_elapsed


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
                # SPEC-017：注入失败细分两类——
                #   (a) impl 解析失败（"Parse Error" + "Invalid operands" 等
                #       编译错误）：变异体本身产不出可编译脚本 = 操作符 bug
                #       类 invalid_mutant（与 _split_invalid_mutants 的静态
                #       预检同类，但解析器拒绝的形态只有引擎能判）；
                #   (b) 其余注入失败（cfg 丢失/env 缺失/写入失败）：run_error
                #       ——基础设施问题，数据不可信。
                # 两者都非 survived——旧路径（stdout-only tail）看不见
                # GQT_INJECT_FAIL，把这类全部吞成 "only pre-existing failures"
                # 的假 survived（§SPEC-016 实证 7 个受累变异）。
                if "Parse Error" in tail or "Failed to load script" in tail:
                    return _mutant_record(
                        m, "invalid_mutant",
                        f"mutated source fails to compile in-project (parser "
                        f"rejection): {m.kind} {m.original}→{m.mutated}",
                        src=original_src)
                return _mutant_record(m, "run_error",
                                      f"memory injection failed: {tail.strip()[:200]}",
                                      src=original_src)
            # classify 保持 5 参签名（interrupt 令牌测试以同签名 fake 替换）；
            # line_text 统一在消费侧补——所有 verdict 记录同构（SEE-1416）。
            verdict = _classify_mutant_run(m, rc, tail, baseline_failures,
                                           baseline_dirty)
            verdict = {**verdict, "line_text": line_text_of(original_src, m.line)}
        except subprocess.TimeoutExpired:
            verdict = _mutant_record(m, "timeout",
                                     f"GUT timed out after {m.kind} {m.original}→{m.mutated}",
                                     src=original_src)
    finally:
        _ACTIVE_ENV = None
        _ACTIVE_SCRIPT = GUT_SCRIPT_RES_PATH
        # impl 磁盘文件正常轮由 Godot 自删；兜底补删覆盖子进程写盘后被中断 /
        # 编译失败未自删的窗口（登记表仍持有 → SIGTERM/atexit 也能清）。
        # 路径解析必须在 cfg unlink 之前（cfg 先删则恒读不到——LOW-1 死代码修复）
        impl_fs = _impl_fs_path(project_root, cfg_path)
        Path(cfg_path).unlink(missing_ok=True)
        forget_temp(cfg_path)
        if impl_fs is not None:
            Path(impl_fs).unlink(missing_ok=True)
            forget_temp(impl_fs)
    return verdict


class _BaselineLaunchFailure(Exception):
    """baseline run 命中 godot 启动失败——mutant 数据不可信，run_mutation 层转中止契约。"""

    def __init__(self, tail: str):
        self.tail = tail
        super().__init__(tail[:200])


# 启动失败类 run_error 的 reason 标记（与 _run_single_mutant /
# _classify_mutant_run 的产出措辞同源；毒条目防护只认这两类）。
_LAUNCH_FAILURE_REASON_MARKERS = (
    "memory injection failed:",
    "godot launch failed (tests did not run):",
)


def _is_launch_failure_record(verdict: dict) -> bool:
    """run_error ∧ 启动失败类 → resume store 拒写（毒条目防护）。"""
    return verdict.get("verdict") == "run_error" and any(
        marker in verdict.get("reason", "")
        for marker in _LAUNCH_FAILURE_REASON_MARKERS)


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
                              f"godot launch failed (tests did not run): {tail.strip()[:200]}",
                              src=None)
    if rc == 0:
        # 测试通过 = mutant 存活（测试没抓住它）
        return _mutant_record(m, "survived",
                              f"tests passed after {m.kind} {m.original}→{m.mutated}",
                              src=None)
    new_failures = _parse_failing_tests(tail) - baseline_failures
    if new_failures:
        return _mutant_record(m, "killed",
                              f"new failures after {m.kind} {m.original}→{m.mutated}: "
                              f"{sorted(new_failures)[:3]}",
                              src=None)
    # SPEC-010 suspect 标签：baseline 脏 ∧ 差集空——无法区分「测试没抓住 mutant」
    # 与「测试根本没跑到该区域」，保守标记（不计 killed 也不计 survived）。
    if baseline_dirty:
        return _mutant_record(m, "suspect",
                              f"dirty baseline ∧ empty diff after {m.kind} "
                              f"{m.original}→{m.mutated} — cannot distinguish "
                              f"survive from unexercised",
                              src=None)
    return _mutant_record(m, "survived",
                          f"only pre-existing failures after {m.kind} "
                          f"{m.original}→{m.mutated} — not a kill",
                          src=None)
