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
import shutil
import signal  # noqa: F401  # 测试经 mrunner.signal monkeypatch signal.signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..source_io import anchor_to_project_root

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

from .inject import (forget_temp, godot_command,
                     detect_binary_kind,
                     godot_project_path as _unified_godot_project_path,
                     make_user_dir, mutant_env, register_temp, spawn_godot)
from . import beacon
from . import shadow
from . import shadowpool
from .shadow import SlotProvisionError
from .identity import (COLD_SCHEME, INJECT_SCHEME,
                       WORKER_SCHEME, identity_pack, killer_certificate,
                       suspected_orphan)
from .channel_router import CHANNEL_WORKER, route_target
from .stagetiming import StageClock, reconcile, self_rss_peak_mb
from .worker_channel import (build_worker_disclosure,
                             degraded_worker_disclosure, identity_for_target,
                             resolve_exec_scheme,
                             run_worker_legs, worker_enabled)
# 统一 mutant 明细记录形态（SEE-1416 扩列）、结果塑形契约与统一报告构建
# （SEE-1550 tidy 迁入）在 reporting.py（<800 行纪律拆分先例）；调用方留在
# runner，by-value import 经本命名空间解析——既有 monkeypatch seam 零变化。
from .reporting import (
    _attention_note, _classify_mutant_run, _mutant_record,
    _no_sites_result, _record_invalid_mutants, _require_baseline_snapshot,
    _run_error_result,
    _setup_error_result,
    _mutation_report,
    _valid_mutants,
    classify_worker_leg, line_text_of,)
from .godot_gate import GlobalGodotGate
from .diffengine import apply_timeout_streaks, make_entry, streak_for
from .coverlink import selection_map_path
from .planner import dry_run_estimate, plan_execution
from .progress import make_reporter
from .resume2 import ResumeStoreV2
from .scopemanifest import build_manifest
from ..timing import (explicit_timing_meta, project_relative,
                      resolve_budget, suggested_action, suggested_action_kind)


# GUT 执行域判定原语在 gutrun.py（SEE-1420 <800 行纪律拆分）；re-export
# 保持既有 import / monkeypatch 名称空间稳定。

# SEE-1594 BTIDY：glob/util/loop 支撑簇物理迁移至 runhelp（零行为拆分）；
# 再导出保持既有 runner.* 引用面与 monkeypatch seam。
from .runhelp import (  # noqa: F401
    _attach_killed_certificate,
    _auto_scan_tests_glob,
    _auto_scan_tests_glob_meta,
    _BaselineLaunchFailure,
    _caliper_lines,
    _exec_finalize,
    _exec_prep_results,
    _loop_slot,
    _negotiate_budget,
    _orphan_disclosure,
    _rebuild_reused_records,
    _record_timing_sample,
    _res_path_of,
    _resolve_auto_glob,
    _sm_classify,
    _sm_inject,
    _tally,
    adaptive_timeout_for,
)
from .gutrun import (  # noqa: F401,E402
    GUT_SCRIPT_RES_PATH,
    _GUT_RUN_EVIDENCE,
    _looks_like_godot_launch_failure,
    _parse_failing_tests,
)


def _godot_project_path(project_root: str) -> str:
    """--path 语义随 detect_binary_kind() 同源（§SPEC-1525-002）：委托
    inject.godot_project_path 统一原语——win64 通道 /mnt/ 转 Windows 形式
    （行为与 af728f5 逐字节一致）、linux 通道原生不转换，转换子进程行为
    与 coverage 共用一份，勿在此模块再分叉。"""
    return _unified_godot_project_path(project_root)


def _gut_extra_args(tests_glob: str | None) -> list[str]:
    """tests_glob → GUT 附加参数。

    -gtest 接受的是「脚本完整路径列表」，但会在装满所有收集脚本后仍全量跑
    （GUT 9.6 实测：单脚本 -gtest 展开 251 scripts/2718 tests）——不能作
    文件级子集。文件路径改走 -gselect=脚本名（GUT 按 -select_script 子集
    过滤收集结果，实测精确 1 script），目录仍走 -gdir（SEE-1316 实测）。"""
    gdir = tests_glob if tests_glob else "res://tests/"
    if gdir.endswith(".gd"):
        script_stem = Path(gdir).stem
        return [
            "-gdir=res://tests/",
            f"-gselect={script_stem}",
        ]
    return [f"-gdir={gdir}"]


def _run_gut_on_project(project_root: str, timeout_s: int = 60,
                        tests_glob: str | None = None) -> tuple[int, str]:
    """在项目里 headless 跑 GUT；返回 (exit_code, 输出尾部)。

    -s 用 res:// 路径（绝对路径会让 godot 拒绝加载）；槽位闸/env/入口脚本
    经模块级 _ACTIVE_* 传递（3 参 fake 契约不变）；§SPEC-015 导入缓存缺失
    fail-fast。tail = 运行流 + stderr 尾部拼接（SPEC-017：注入失败与 GUT
    运行流互斥出现，拼接不互相挤出）。"""
    shadow.require_import_cache(project_root)
    gut_fs = os.path.join(project_root, "addons", "gut", "gut_cmdln.gd")
    if not os.path.isfile(gut_fs):
        raise FileNotFoundError(f"GUT runner not found: {gut_fs}")
    extra = _gut_extra_args(tests_glob)
    env = dict(os.environ)
    if _ACTIVE_ENV:
        env.update(_ACTIVE_ENV)
    with GlobalGodotGate(_ACTIVE_GATE_ROOT or project_root):
        r = spawn_godot(
            [godot_command(), "--headless", "--path",
             _godot_project_path(project_root),
             "-s", _ACTIVE_SCRIPT, *extra, "-gexit"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=project_root,
            env=env,
        )
    tail = (r.stdout[-200000:] if r.stdout else "") + "\n--- gqt: stderr ---\n" + r.stderr[-10000:]
    return r.returncode, tail


# 子进程环境/入口脚本的活动状态（mutant 循环内动态切换，见 _run_single_mutant）
_ACTIVE_ENV: dict | None = None
_ACTIVE_SCRIPT: str = GUT_SCRIPT_RES_PATH
# 槽位闸锚（SEE-1550）：mutant 轮指向真实仓根（闸必须跨 worker 全局计数，
# 不能锚到各 worker 各自的影子槽）；baseline 轮为 None → 回落 project_root。
_ACTIVE_GATE_ROOT: str | None = None


# 自适应 timeout 倍数：per-mutant 上限 = baseline 实测耗时 × 该倍数。
_TIMEOUT_SCALE = 2.0





def run_mutation(file_path: str, project_root: str, budget: int = 50,
    timeout_s: int = 60, dry_run: bool = False,
    tests_glob: str | None = None,
    precomputed_baseline: dict | None = None,
    mutant_slice: tuple[int, int] | None = None, timing_store=None,
    binary_kind: str | None = None, mutation_warning: int | None = None,
    timeout_factor: float | None = None, mutate_all: bool = False,
    retry_ids: list[str] | None = None, resume2_store=None,
    select_ids: list[str] | None = None, select_lines: list[int] | None = None,
    reuse_coverage: bool = True, progress: dict | None = None,
    identity: dict | None = None, worker_channel: bool | None = None,
    select_mode: str = "full", selection_meta: dict | None = None,
) -> dict:
    """单文件 mutation 入口（§SPEC-202 单位级测量面挂载点）。

    dry-run / 零位点 / setup 错误契约保持既有 summary 形态（零键增量）；
    完整报告路径在 summary 挂 stage_timings（8 阶段全键）+ rss_peak_mb
    （self VmHWM）——total 在包装层补记（覆盖核心体全程墙钟）。
    """
    t0 = time.monotonic()
    result = _run_mutation_core(
        file_path, project_root, budget=budget, timeout_s=timeout_s,
        dry_run=dry_run, tests_glob=tests_glob,
        precomputed_baseline=precomputed_baseline,
        mutant_slice=mutant_slice, timing_store=timing_store,
        binary_kind=binary_kind,
        mutation_warning=mutation_warning, timeout_factor=timeout_factor,
        mutate_all=mutate_all, retry_ids=retry_ids,
        resume2_store=resume2_store, select_ids=select_ids,
        select_lines=select_lines, reuse_coverage=reuse_coverage,
        progress=progress, identity=identity, worker_channel=worker_channel,
        select_mode=select_mode, selection_meta=selection_meta)
    s = result.get("summary") if isinstance(result, dict) else None
    if isinstance(s, dict):
        # SEE-1589 A：有效选面唯一判别点（串行/卡尺/显式 --tests 同源）。
        from .testscan import resolve_selection_mode
        s.setdefault("selection_mode", resolve_selection_mode(
            select_mode, selection_meta or {}, select_ids, select_lines))
    if isinstance(s, dict) and "stage_timings" in s:
        wall_s = time.monotonic() - t0
        s["stage_timings"]["total"] = round(wall_s, 3)
        s["stage_reconciliation"] = reconcile(s["stage_timings"], wall_s)
        s["rss_peak_mb"] = self_rss_peak_mb()
    return result


def _core_setup(file_path, project_root, tests_glob, binary_kind,
                worker_channel, identity, selection_meta, select_mode, clock,
                mutation_warning) -> tuple:
    """core 前置：目标锚定、选面解析、源码/身份/路由、收集与 manifest 标定。
    返回 (error_result_or_None, ctx)；error 在场时 ctx 为空（目标缺失 /
    收集异常 → error 契约）。"""
    path = Path(anchor_to_project_root(file_path, project_root))
    if not path.is_file():
        raise FileNotFoundError(
            f"mutation target not found: {file_path} (anchored: {path})")
    selection_meta = dict(selection_meta or {})
    # SEE-1583 §SPEC-012：串行/卡尺路径与编排路径同源遵守 select_mode
    # （SEE-1587 默认 line）——不可用映射 fail-loud，无静默全脸路径。
    # SEE-1587：显式 --tests 也经同一守门函数，披露「未使用行级选面」。
    with clock.stage("test_scan"):
        tests_glob, selection_meta = _resolve_auto_glob(
            tests_glob, str(path), project_root, selection_meta,
            select_mode=select_mode)
    original_src = path.read_text(encoding="utf-8")
    kind = binary_kind or detect_binary_kind()
    target_res_early = _res_path_of(str(path), str(project_root))
    # SEE-1550 §SPEC-020：exec_scheme 按目标路由入身份包（worker 显式启用且
    # 路由判 worker 才 worker-reset/1；A9 默认冷）。parent 传入的 identity
    # 用同源解析覆盖 scheme——父子两侧 file_key 一致。
    with clock.stage("route_scan"):
        identity, _route, exec_scheme = identity_for_target(
            identity, project_root, target_res_early, worker_channel)
    if identity is None:
        identity = identity_pack(project_root, kind, exec_scheme=exec_scheme)
    try:
        raw_mutants = _collect_mutations(original_src, str(path))
    except Exception as e:
        return _setup_error_result(str(path), e), {}
    # P1 manifest 标定（定案 D1/D2/D3 + L6）：scope 提取 → no-mutate 过滤
    # → stable id。total_sites 按过滤后位点数计（无位点 scope 不进 manifest）。
    header_hash, spans, mutants, scope_hashes = build_manifest(
        original_src, str(path), raw_mutants)
    ctx = {
        "path": path, "tests_glob": tests_glob, "selection_meta": selection_meta,
        "original_src": original_src, "kind": kind, "identity": identity,
        "header_hash": header_hash, "spans": spans, "mutants": mutants,
        "scope_hashes": scope_hashes, "total_sites": len(mutants),
        "attention": _attention_note(mutation_warning, len(mutants),
                                     str(path)),
    }
    return None, ctx


def _core_caliper(project_root, path, mutants, select_ids, select_lines,
                  select_mode, selection_meta, tests_glob) -> tuple:
    """SEE-1587 卡尺行级测试面：`--mutation`/`--lines` 默认（line 模式）消费
    选中位点所在行的测试集合并集物化为单一 glob；无映射测试的选中位点照常
    执行（公平重跑通道）并披露 no_mapped_tests。显式 --tests 用户接管时不
    参与。返回 (selection_meta, tests_glob)。"""
    if not ((select_ids or select_lines)
            and str(select_mode).lower() == "line"
            and selection_meta.get("source") != "explicit_tests"):
        return selection_meta, tests_glob
    caliper_lines = _caliper_lines(mutants, select_ids, select_lines)
    if not caliper_lines:
        return selection_meta, tests_glob
    from .truth_select import resolve_line_group_selection
    file_sel = {"source": "runtime_mapping", "zero_ref": False,
                "tests": selection_meta.get("tests") or [],
                "tests_glob": tests_glob,
                "mapping": selection_meta.get("mapping")}
    sel = resolve_line_group_selection(
        project_root, str(path), caliper_lines,
        file_selection=file_sel)
    selection_meta = {
        **selection_meta,
        "source": sel.get("source"),
        "reason": ("caliper_line_mapping"
                   if sel.get("source") == "line_mapping"
                   else "caliper_line_has_no_mapped_tests"),
        "tests": sel.get("tests") or [],
        "lines": sel.get("lines") or sorted(caliper_lines),
        "no_mapped_tests": sel.get("no_mapped_tests") or [],
    }
    if sel.get("tests_glob"):
        tests_glob = sel["tests_glob"]
    return selection_meta, tests_glob


def _core_dry_run(path, mutants, budget, scoped, total_sites, project_root,
                  tests_glob, binary_kind, resume2_store, mutate_all,
                  header_hash, scope_hashes, attention, identity,
                  selection_meta, select_ids, select_lines, select_mode) -> dict:
    """dry_run（SPEC-009）估算与选面/求交披露（与完整执行路径同一判别点）。"""
    estimate = dry_run_estimate(
        str(path), mutants, budget, scoped, total_sites,
        project_root, tests_glob, binary_kind, resume2_store,
        mutate_all, header_hash, scope_hashes, attention=attention,
        identity=identity)
    from .testscan import resolve_selection_mode, selection_disclosure
    estimate["summary"].update(selection_disclosure(selection_meta))
    estimate["summary"].setdefault(
        "selection_mode",
        resolve_selection_mode(select_mode, selection_meta or {},
                               select_ids, select_lines))
    # 卡尺 `--lines` 的求交披露在 dry-run 也与完整路径同源（requested/
    # hit/skipped_no_site），避免只在实际执行轮才可见。
    if select_lines is not None:
        lines = {int(v) for v in select_lines}
        dry_hit = sorted({m.line for m in mutants if m.line in lines})
        estimate["summary"]["lines_selection"] = {
            "requested": sorted(lines),
            "requested_count": len(lines),
            "hit": dry_hit,
            "hit_count": len(dry_hit),
            "skipped_no_site": sorted(lines - set(dry_hit)),
        }
    return estimate


def _run_mutation_core(file_path: str,
    project_root: str,
    budget: int = 50,
    timeout_s: int = 60,
    dry_run: bool = False,
    tests_glob: str | None = None,
    precomputed_baseline: dict | None = None,
    mutant_slice: tuple[int, int] | None = None,
    timing_store=None,
    binary_kind: str | None = None,
    mutation_warning: int | None = None,
    timeout_factor: float | None = None,
    mutate_all: bool = False,
    retry_ids: list[str] | None = None,
    resume2_store=None,
    select_ids: list[str] | None = None,
    select_lines: list[int] | None = None,
    reuse_coverage: bool = True,
    progress: dict | None = None,
    identity: dict | None = None,
    worker_channel: bool | None = None,
    select_mode: str = "full",
    selection_meta: dict | None = None,
) -> dict:
    """对单个 .gd 文件做变异测试并出统一 JSON 契约（语义总账见各分解函数；
    kill 判定 = 相对 baseline 的新增失败集，非 exit code）。

    budget 控制 mutant 上限；dry_run 只清点不跑 GUT；precomputed_baseline
    为共享快照锚；retry_ids = 父层差分规划后的 worker 注入面（不自建
    plan、不写 manifest 索引层）；mutate_all 显式全量；reuse_coverage
    默认自动启用；注入机制 = 磁盘影子注入（disk-farm/1）。"""
    _require_baseline_snapshot(mutant_slice, retry_ids, precomputed_baseline)
    clock = StageClock()
    setup_error, c = _core_setup(
        file_path, project_root, tests_glob, binary_kind, worker_channel,
        identity, selection_meta, select_mode, clock, mutation_warning)
    if setup_error is not None:
        return setup_error
    path, original_src = c["path"], c["original_src"]
    tests_glob, selection_meta = c["tests_glob"], c["selection_meta"]
    identity = c["identity"]
    scoped = tests_glob is not None
    backup = original_src  # 永不改写原文件之外的任何东西（kill rate 证据隔离）
    # SEE-1587：卡尺行级测试面（--mutation/--lines 默认 line 模式）。
    selection_meta, tests_glob = _core_caliper(
        project_root, path, mutants=c["mutants"], select_ids=select_ids,
        select_lines=select_lines, select_mode=select_mode,
        selection_meta=selection_meta, tests_glob=tests_glob)
    scoped = tests_glob is not None
    if dry_run:
        return _core_dry_run(
            path, c["mutants"], budget, scoped, c["total_sites"],
            project_root, tests_glob, binary_kind, resume2_store, mutate_all,
            c["header_hash"], c["scope_hashes"], c["attention"], identity,
            selection_meta, select_ids, select_lines, select_mode)
    if not c["mutants"]:
        return _no_sites_result(str(path), scoped,
                                zero_ref=bool(selection_meta.get("zero_ref")))
    # P1 差分（定案 D4）：串行路径自建 plan；worker 注入路径只执行
    # retry_ids；显式寻址（--mutation/--lines 卡尺）强制新执行，绕过差分。
    store2 = resume2_store if resume2_store is not None else ResumeStoreV2(
        project_root, str(path), tests_glob, binary_kind=binary_kind,
        identity=identity)
    prior_entries, reused, untouched, disclosures, executed_list, \
        results_uncovered = plan_execution(
            path, project_root, c["mutants"], c["header_hash"],
            c["scope_hashes"], budget, tests_glob, binary_kind, resume2_store,
            mutate_all, retry_ids, select_ids, select_lines, reuse_coverage,
            c["attention"], mutant_slice, original_src, identity=identity,
            clock=clock,
            tests_scripts=selection_meta.get("tests") or None,
            map_path=selection_map_path(selection_meta))
    # budget 吃光且无可报告复用——零 mutant 不得触发 baseline GUT（硬化
    # 反例实证：白付一轮 108s baseline）。
    if not executed_list and not reused:
        return _no_sites_result(str(path), scoped)
    from .testscan import selection_disclosure
    # §SPEC-613：自动选测披露（弱选测时 weak=true 随 summary 透出）。
    disclosures.update(selection_disclosure(selection_meta))
    if c["total_sites"] > budget:
        disclosures["total_sites"] = c["total_sites"]
        # O2：此处 executed 仍为计划面预估——_execute_planned 在 invalid
        # 预检后以 actually_executed 覆写（实际执行语义的唯一出处）。
        disclosures.setdefault("executed", len(executed_list))
    return _execute_planned(
        path, project_root, original_src, backup, c["mutants"], executed_list,
        reused, results_uncovered, prior_entries, c["scope_hashes"],
        disclosures, budget, tests_glob, timing_store, timeout_s,
        precomputed_baseline, store2, timeout_factor, c["header_hash"],
        retry_ids, scoped, c["total_sites"], progress=progress,
        identity=identity, targeted_only=bool(select_ids or select_lines),
        worker_channel=worker_channel, clock=clock)










def _execute_planned(path, project_root, original_src, backup, mutants,
                     executed_list, reused, results_uncovered, prior_entries,
                     scope_hashes, disclosures, budget, tests_glob,
                     timing_store, timeout_s, precomputed_baseline, store2,
                     timeout_factor, header_hash, retry_ids, scoped,
                     total_sites, progress=None, identity=None,
                     targeted_only=False, worker_channel=None,
                     clock: StageClock | None = None):
    """执行规划面 → D5 streak 落定 → 统一 JSON 报告（run_mutation 的执行尾）。"""
    clock = clock or StageClock()
    if not executed_list and not reused:
        return _no_sites_result(str(path), scoped)
    if total_sites > budget:
        disclosures["total_sites"] = total_sites
        disclosures.setdefault("executed", len(executed_list))
    results, invalid_count = _exec_prep_results(
        path, project_root, original_src, mutants, executed_list, reused,
        results_uncovered, scope_hashes, disclosures, budget, store2,
        header_hash, retry_ids, total_sites)
    effective_timeout, timing_meta = _negotiate_budget(
        timing_store, timeout_s, precomputed_baseline)
    # SEE-1525 进度面：分母 = 本 worker 真实 GUT 执行面（invalid 预检剔除后）；
    # 并行分片由父层注入全局分母（progress["total"]，含 invalid 位点）。
    reporter = make_reporter(project_root, str(path), progress,
                             len(executed_list) - invalid_count,
                             invalid_count)
    # 磁盘影子注入（SEE-1550 M0）：目标文件写入全部发生在影子槽内，真实仓
    # 目标文件零写入。
    outcome = _run_mutant_loop_with_aborts(
        path, project_root, original_src, backup, executed_list,
        tests_glob, effective_timeout, precomputed_baseline,
        store2, prior_entries, scope_hashes, timeout_factor,
        reporter=reporter, identity=identity, targeted_only=targeted_only,
        worker_channel=worker_channel, clock=clock)
    if isinstance(outcome, dict):
        return outcome  # baseline 中止契约（timeout / launch failure）
    raw_pairs, baseline_reused, baseline_elapsed, slot_info = outcome
    return _exec_finalize(path, results, raw_pairs, prior_entries,
                          scope_hashes, store2, timing_store, baseline_reused,
                          baseline_elapsed, timing_meta, reused, invalid_count,
                          budget, scoped, disclosures, slot_info,
                          original_src, clock)


















# per-mutant 自适应上限（baseline 耗时决定，_run_mutant_loop 内写入；统一
# 报告构建已迁 reporting.py，经参数传入读取时刻同值）。
_adaptive_timeout = 0






















def _run_single_mutant(m: Mutant, path, project_root: str, original_src: str,
                       backup: str, tests_glob: str | None, seq: int,
                       baseline_failures: set[str], baseline_dirty: bool,
                       slot=None, auto_name: str = "",
                       ownership_fn=None, target_rel: str = "",
                       target_res: str = "") -> dict:
    """磁盘影子注入 → 跑 GUT → 分类；真实仓目标文件全程零写入（SEE-1550）。

    - 注入：每 mutant 一次 replace，无恢复步，写失败上抛转 run_error。
    - 四级 beacon：任何一级失败都不得产出 killed/survived。"""
    global _ACTIVE_ENV, _ACTIVE_SCRIPT
    injected, cfg_path, user_dir = _sm_inject(
        m, slot, target_rel, target_res, original_src, project_root, path,
        auto_name)
    if cfg_path is None:
        return injected  # 注入失败 run_error 契约
    _ACTIVE_ENV, _ = mutant_env(str(cfg_path), user_dir)
    _ACTIVE_SCRIPT = beacon.beacon_script_res(slot.root)
    try:
        try:
            rc, tail = _run_gut_on_project(
                str(slot.root), timeout_s=_adaptive_timeout,
                tests_glob=tests_glob)
            verdict = _sm_classify(m, rc, tail, baseline_failures,
                                   baseline_dirty, original_src, ownership_fn,
                                   project_root)
        except subprocess.TimeoutExpired:
            verdict = _mutant_record(m, "timeout",
                                     f"GUT timed out after {m.kind} {m.original}→{m.mutated}",
                                     src=original_src)
    finally:
        _ACTIVE_ENV = None
        _ACTIVE_SCRIPT = GUT_SCRIPT_RES_PATH
        _ACTIVE_GATE_ROOT = None
        Path(cfg_path).unlink(missing_ok=True)
        forget_temp(cfg_path)
        if user_dir:
            shutil.rmtree(user_dir, ignore_errors=True)
    return verdict


def _run_baseline(project_root: str, tests_glob: str | None, timeout_s: int
                  ) -> tuple[int, str, float]:
    """跑 baseline 并计时；TimeoutExpired 由调用方按中止契约处理。"""
    from godot_qa_toolkit.mutation import runner as _r

    t0 = time.monotonic()
    rc, tail = _r._run_gut_on_project(project_root, timeout_s=timeout_s,
                                      tests_glob=tests_glob)
    return rc, tail, time.monotonic() - t0


def _run_cold_legs(path, project_root, original_src, backup, mutants,
                   tests_glob, baseline_failures, baseline_dirty, slot,
                   auto_name, target_rel, target_res, ownership_fn, reporter,
                   store2, prior_entries, scopes):
    """冷通道（M0）逐 mutant 执行循环（默认路径，行为与既有逐位不变）。"""
    raw_pairs: list[tuple] = []
    for seq, m in enumerate(mutants):
        t_mutant = time.monotonic()
        verdict = _run_single_mutant(
            m, path, project_root, original_src, backup, tests_glob, seq,
            baseline_failures, baseline_dirty, slot=slot,
            auto_name=auto_name, ownership_fn=ownership_fn,
            target_rel=target_rel, target_res=target_res)
        raw_pairs.append((m, verdict))
        # SEE-1525 进度面唯一计数点：人眼进度行 + 机器可读分片。
        if reporter is not None:
            reporter.record(m, verdict, time.monotonic() - t_mutant)
        # 中断安全增量落盘：乐观 streak（毒窗口收尾修正）。
        # SEE-1405 发现三毒条目防护由 resume2.record_entry 承接
        # （启动失败类 run_error 拒收——verdict 零信息量）。
        if store2 is not None:
            prior = prior_entries.get(m.stable_id)
            streak, v2_verdict = streak_for(prior, verdict["verdict"])
            store2.record_entry(make_entry(m, verdict, v2_verdict, streak,
                                           scopes or {}, time.time()))
    return raw_pairs


def _loop_baseline(workdir, path, project_root, tests_glob, timeout_s,
                   precomputed_baseline, timeout_factor) -> tuple:
    """workdir 备份 + baseline 实测/复用 + 自适应 timeout 锚 + 启动失败守卫。
    baseline 永远实测（判定语义锚）；快照 elapsed 只喂预算锚。"""
    backup_path = os.path.join(workdir, "original.gd")
    shutil.copy2(str(path), backup_path)
    if precomputed_baseline is not None:
        baseline_rc = int(precomputed_baseline["rc"])
        baseline_tail = str(precomputed_baseline["tail"])
        baseline_elapsed = float(precomputed_baseline["elapsed"])
        baseline_reused = True
    else:
        # Revy QA retest 实证：baseline 必然花最久——timeout 漏网按 run_error
        # JSON 契约归一（与 per-mutant 循环同款处理）。
        baseline_rc, baseline_tail, baseline_elapsed = _run_baseline(
            project_root, tests_glob, timeout_s)
        baseline_reused = False
    # SPEC-009 自适应 timeout：per-mutant 上限 = baseline 耗时 × F（只放大）。
    global _adaptive_timeout
    _adaptive_timeout = adaptive_timeout_for(baseline_elapsed, timeout_s,
                                             timeout_factor)
    if baseline_rc != 0 and _looks_like_godot_launch_failure(baseline_tail):
        raise _BaselineLaunchFailure(baseline_tail)
    baseline_failures = _parse_failing_tests(baseline_tail)
    return baseline_failures, bool(baseline_failures), baseline_reused, \
        baseline_elapsed


def _loop_legs(path, project_root, original_src, backup, mutants, tests_glob,
               baseline_failures, baseline_dirty, slot, auto_name, target_rel,
               target_res, kind, idpack, exec_scheme_resolved, worker_channel,
               reporter, store2, prior_entries, scopes, clock, slot_info,
               t_slot) -> list:
    """beacon 播种 → worker/冷腿执行 → 槽归还断言与 slot_sync 计时。"""
    ownership_holder: list = []

    def _ownership():
        if not ownership_holder:
            from .truth_select import ownership_for
            ownership_holder.append(
                ownership_for(str(project_root), str(path)))
        return ownership_holder[0]

    use_worker = exec_scheme_resolved == WORKER_SCHEME
    timings_out: dict = {}
    try:
        beacon.seed_beacon_script(slot.root)
        if use_worker:
            from .worker_channel import execute_worker_branch
            with clock.stage("per_mutant"):
                raw_pairs, fatal_events = execute_worker_branch(
                    path, project_root, original_src, mutants, tests_glob,
                    baseline_failures, baseline_dirty, slot, auto_name,
                    target_rel, target_res, kind, reporter, store2,
                    prior_entries, scopes, _ownership,
                    timings_out=timings_out)
            clock.add("worker_boot",
                      timings_out.get("worker_boot_s", 0.0))
            if fatal_events:
                slot_info["worker_fatal_events"] = fatal_events
                slot_info["worker_channel"] = degraded_worker_disclosure(
                    slot_info.get("worker_channel") or {}, fatal_events)
        else:
            with clock.stage("per_mutant"):
                raw_pairs = _run_cold_legs(
                    path, project_root, original_src, backup, mutants,
                    tests_glob, baseline_failures, baseline_dirty, slot,
                    auto_name, target_rel, target_res, _ownership, reporter,
                    store2, prior_entries, scopes)
    finally:
        release_problems = shadowpool.release_slot(slot)
        # §SPEC-202 slot_sync = 槽获取→归还断言全程；槽位固定成本 = 其与
        # per_mutant 之差（§SPEC-210 LOW-1 口径修正）。
        clock.add("slot_sync", time.monotonic() - t_slot)
        if release_problems:
            slot_info["release_problems"] = release_problems
    return raw_pairs


def _run_mutant_loop(
    path, project_root: str, original_src: str, backup: str,
    mutants: list[Mutant], tests_glob: str | None, timeout_s: int,
    precomputed_baseline: dict | None = None,
    store2=None, prior_entries: dict | None = None, scopes: dict | None = None,
    timeout_factor: float | None = None, reporter=None,
    identity: dict | None = None, targeted_only: bool = False,
    worker_channel=None, clock: StageClock | None = None,
) -> tuple[list, bool, float, dict]:
    """逐 mutant 跑 GUT 并分类判定；返回 (raw_pairs, baseline_reused,
    baseline_elapsed, slot_info)。verdict 记录不在此处进 results——D5 streak
    是窗口级判定，由调用方 apply_timeout_streaks 落定后统一扩展。"""
    with tempfile.TemporaryDirectory() as workdir:
        (baseline_failures, baseline_dirty, baseline_reused,
         baseline_elapsed) = _loop_baseline(workdir, path, project_root,
                                            tests_glob, timeout_s,
                                            precomputed_baseline,
                                            timeout_factor)
        clock = clock or StageClock()
        (slot, idpack, auto_name, target_rel, target_res, scheme,
         slot_info, t_slot) = _loop_slot(
            project_root, path, detect_binary_kind(), identity, tests_glob,
            worker_channel, targeted_only, clock)
        raw_pairs = _loop_legs(
            path, project_root, original_src, backup, mutants, tests_glob,
            baseline_failures, baseline_dirty, slot, auto_name, target_rel,
            target_res, detect_binary_kind(), idpack, scheme, worker_channel,
            reporter, store2, prior_entries, scopes, clock, slot_info, t_slot)
        slot_info["inject_scheme"] = idpack.get("inject_scheme", INJECT_SCHEME)
        slot_info["slot_mode"] = slot.mode
        slot_info["freshness"] = list(slot.freshness)
        if any(v["verdict"] == "survived" for _, v in raw_pairs):
            slot_info["suspected_orphan"] = _orphan_disclosure(
                project_root, target_res)
    return raw_pairs, baseline_reused, baseline_elapsed, slot_info


def _run_mutant_loop_with_aborts(path, project_root, original_src, backup,
                                 executed_list, tests_glob, timeout_s,
                                 precomputed_baseline=None,
                                 store2=None, prior_entries=None,
                                 scopes=None,
                                 timeout_factor: float | None = None,
                                 reporter=None, identity=None,
                                 targeted_only: bool = False,
                                 worker_channel=None,
                                 clock: StageClock | None = None):
    """baseline 中止形态转统一 JSON（timeout / godot 启动失败）；否则返回 raw。"""
    try:
        return _run_mutant_loop(
            path, project_root, original_src, backup,
            _valid_mutants(original_src, executed_list), tests_glob, timeout_s,
            precomputed_baseline, store2, prior_entries, scopes,
            timeout_factor, reporter=reporter, identity=identity,
            targeted_only=targeted_only, worker_channel=worker_channel,
            clock=clock,
        )
    except subprocess.TimeoutExpired:
        # SEE-1356 L1 (§SPEC-L1-03)：预算超时附机器可执行的补救指令。
        return _run_error_result(
            str(path),
            f"baseline GUT timed out after {timeout_s}s — mutation data untrustworthy",
            f"baseline GUT timed out after {timeout_s}s (project's baseline run exceeds "
            f"the timeout — raise --timeout or check why tests are this slow)",
            hint=suggested_action(timeout_s),
            action_kind=suggested_action_kind(timeout_s),
        )
    except _BaselineLaunchFailure as e:
        return _run_error_result(
            str(path),
            "godot launch failed on baseline — mutation data untrustworthy",
            f"godot launch failed on baseline run: {e.tail.strip()[:200]}",
        )
    except SlotProvisionError as e:
        # 影子槽制备失败（.gitignore fail-loud / 磁盘不足）——数据不可信，
        # 中止契约转统一 JSON（与 baseline 启动失败同档）。
        return _run_error_result(
            str(path),
            "shadow slot provisioning failed — mutation data untrustworthy",
            f"shadow slot provisioning failed: {e}",
        )
