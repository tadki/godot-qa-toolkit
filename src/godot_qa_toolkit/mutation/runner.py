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
from .worker_channel import (RouteDriftError, build_worker_disclosure,
                             degraded_worker_disclosure, identity_for_target,
                             resolve_exec_scheme,
                             run_worker_legs, worker_enabled)
# 统一 mutant 明细记录形态（SEE-1416 扩列）、结果塑形契约与统一报告构建
# （SEE-1550 tidy 迁入）在 reporting.py（<800 行纪律拆分先例）；调用方留在
# runner，by-value import 经本命名空间解析——既有 monkeypatch seam 零变化。
from .reporting import (
    _attention_note, _classify_mutant_run, _mutant_record,
    _no_sites_result, _record_invalid_mutants, _require_baseline_snapshot,
    _route_drift_result, _run_error_result,
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
    _BaselineDirtyFailure,
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
from . import killbail
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

def _run_gut_on_project(project_root: str, timeout_s: int = 600,
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
    timeout_s: int = 600, dry_run: bool = False,
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
    break_on_baseline_failure: bool = False,
    kill_bail: bool | None = None,
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
        select_mode=select_mode, selection_meta=selection_meta,
        break_on_baseline_failure=break_on_baseline_failure,
        kill_bail=kill_bail)
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
                mutation_warning, dry_run=False) -> tuple:
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
    # SEE-1604 D2：dry-run 抑制映射续新（allow_renew=False）——清点操作
    # 绝不拉起全链 GUT；陈旧/自锁映射降级未圈定清点。
    with clock.stage("test_scan"):
        tests_glob, selection_meta = _resolve_auto_glob(
            tests_glob, str(path), project_root, selection_meta,
            select_mode=select_mode, allow_renew=not dry_run)
    original_src = path.read_text(encoding="utf-8")
    kind = binary_kind or detect_binary_kind()
    target_res_early = _res_path_of(str(path), str(project_root))
    # SEE-1550 §SPEC-020：exec_scheme 按目标路由入身份包（worker 显式启用且
    # 路由判 worker 才 worker-reset/1；A9 默认冷）。
    # SEE-1606 §SPEC-P1：父层 identity 已带权威 exec_scheme 时（planning
    # 注入），本地重解析只做比对——不一致具名 route_drift（baseline 前
    # fail-loud，禁止静默覆盖 file_key 面）；父层无 scheme（串行首解析）
    # 本次解析即权威，行为不变。
    with clock.stage("route_scan"):
        try:
            identity, _route, exec_scheme = identity_for_target(
                identity, project_root, target_res_early, worker_channel,
                check=True)
        except RouteDriftError as e:
            return _route_drift_result(str(path), e), {}
    if identity is None:
        identity = identity_pack(project_root, kind, exec_scheme=exec_scheme)
    try:
        raw_mutants = _collect_mutations(original_src, str(path))
    except Exception as e:
        return _setup_error_result(str(path), e), {}
    # P1 manifest 标定（定案 D1/D2/D3 + L6）：scope 提取 → no-mutate 过滤
    # → stable id。total_sites 按过滤后位点数计（无位点 scope 不进 manifest）。
    # SEE-1615 §SPEC-PERF2：等价预剪枝（env 默认 OFF；prune_out 逐点披露
    # 经 ctx → _core_plan disclosures → summary equivalent_pruned）
    eq_prune_out: dict = {}
    header_hash, spans, mutants, scope_hashes = build_manifest(
        original_src, str(path), raw_mutants, prune_out=eq_prune_out)
    ctx = {
        "path": path, "tests_glob": tests_glob, "selection_meta": selection_meta,
        "original_src": original_src, "kind": kind, "identity": identity,
        "header_hash": header_hash, "spans": spans, "mutants": mutants,
        "scope_hashes": scope_hashes, "total_sites": len(mutants),
        "attention": _attention_note(mutation_warning, len(mutants),
                                     str(path)),
        "equivalent_pruned": eq_prune_out.get("equivalent_pruned"),
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
        # SEE-1610 §SPEC-IMPL：组内 allowlist 透传（resolve 侧已组装）。
        "tests_allow": sel.get("tests_allow"),
    }
    if sel.get("tests_glob"):
        tests_glob = sel["tests_glob"]
    return selection_meta, tests_glob

def _godot_binary_precondition(project_root: str, godot_ok: bool,
                               godot_evidence: str) -> dict:
    """§SPEC-DERIVED-A F1：godot_binary 前置块（只读探测）。

    ok 仅表解析/存在性——WSL 下 bash wrapper 是合法启动方式，**不得**
    因「非原生 ELF」判不可用；binary kind（detect_binary_kind 同源）与
    godot_launch_issue 静态诊断只如实落 evidence，供 dry-run 读者区分
    原生二进制与 wrapper。"""
    from .inject import detect_binary_kind, godot_launch_issue

    _, issue = godot_launch_issue(str(project_root))
    kind = detect_binary_kind()
    evidence = (f"{godot_evidence} | kind={kind} | launch_issue="
                + (issue if issue else "none"))
    return {"ok": godot_ok, "evidence": evidence}


def _core_dry_run(path, mutants, budget, scoped, total_sites, project_root,
                  tests_glob, binary_kind, resume2_store, mutate_all,
                  header_hash, scope_hashes, attention, identity,
                  selection_meta, select_ids, select_lines, select_mode,
                  timeout_s: int = 600) -> dict:
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
    # SEE-1610 §SPEC-PLAN Phase 0：选面披露落定后重建同构 plan（V7 同构）。
    # SEE-1606 §SPEC-DERIVED-A：三静态输入独立成 derived 键（显式 timeout/
    # wiring hash/显式 tests glob；缺席不造）+ 只读静态前置块。
    from .identity import boot_wiring_hash
    from .reporting import _execution_plan, _plan_meta
    from .shadow import _IMPORT_ARTIFACTS
    explicit_glob = tests_glob if (selection_meta or {}).get(
        "source") == "explicit_tests" else None
    godot_dir = Path(project_root) / ".godot"
    import_missing = [n for n in _IMPORT_ARTIFACTS
                      if not (godot_dir / n).exists()]
    gut_cmdln = Path(project_root) / "addons" / "gut" / "gut_cmdln.gd"
    try:
        from .inject import godot_command
        godot_bin = godot_command()
        godot_ok, godot_evidence = Path(godot_bin).is_file(), str(godot_bin)
    except Exception as e:  # noqa: BLE001 — 解析失败具名落块（只读探测）
        godot_ok, godot_evidence = False, f"{type(e).__name__}: {e}"
    static_pc = {
        "import_cache": {
            "ok": not import_missing,
            "evidence": (f"{godot_dir} missing={import_missing}"
                         if import_missing else str(godot_dir))},
        "gut_cmdln": {"ok": gut_cmdln.is_file(), "evidence": str(gut_cmdln)},
        "godot_binary": _godot_binary_precondition(project_root, godot_ok,
                                                   godot_evidence),
    }
    derived_extra = {"budget": budget, "explicit_timeout_s": timeout_s,
                     "boot_wiring_hash": boot_wiring_hash(str(project_root)),
                     "explicit_tests_glob": explicit_glob}
    estimate["summary"]["execution_plan"] = _execution_plan(
        estimate["summary"], extra=derived_extra)
    # SEE-1606 §SPEC-P2：dry-run 计划预览元数据——映射状态/证明存在性
    # 只读探测（零续新、零 import、零 census；不计算 import_proof）；
    # validate_plan lint 仅供消费侧，不作运行门（此处不调用）。
    from .truth_select import map_preview_state
    from .prewarm_proof import _proof_paths
    _meta = _plan_meta(
        estimate["summary"]["execution_plan"], estimate["summary"],
        extra=derived_extra,
        map_state=map_preview_state(str(project_root), str(path)),
        import_proof_present=(
            _proof_paths(str(project_root))[1].is_file()),
        static_preconditions=static_pc)
    estimate["summary"]["execution_plan_meta"] = _meta
    return estimate

def _core_caliper_and_early(c: dict, project_root: str, path: Path, *,
                            select_ids, select_lines, select_mode, dry_run,
                            budget, timeout_s: int, binary_kind,
                            resume2_store, mutate_all, identity) -> tuple:
    """卡尺选面 + dry-run / 零位点两个早退出口。返回 (early_or_None,
    tests_glob, selection_meta)。"""
    tests_glob = c["tests_glob"]
    selection_meta = c["selection_meta"]
    # SEE-1587：卡尺行级测试面（--mutation/--lines 默认 line 模式）。
    selection_meta, tests_glob = _core_caliper(
        project_root, path, mutants=c["mutants"], select_ids=select_ids,
        select_lines=select_lines, select_mode=select_mode,
        selection_meta=selection_meta, tests_glob=tests_glob)
    scoped = tests_glob is not None
    if dry_run:
        return (_core_dry_run(
            path, c["mutants"], budget, scoped, c["total_sites"],
            project_root, tests_glob, binary_kind, resume2_store, mutate_all,
            c["header_hash"], c["scope_hashes"], c["attention"], identity,
            selection_meta, select_ids, select_lines, select_mode,
            timeout_s=timeout_s), tests_glob, selection_meta)
    if not c["mutants"]:
        return (_no_sites_result(str(path), scoped,
                                 zero_ref=bool(selection_meta.get("zero_ref"))),
                tests_glob, selection_meta)
    return None, tests_glob, selection_meta


def _core_plan(c: dict, path: Path, project_root: str, original_src: str, *,
               tests_glob, budget, binary_kind, resume2_store, mutate_all,
               retry_ids, select_ids, select_lines, reuse_coverage,
               mutant_slice, identity, clock, selection_meta) -> "tuple | None":
    """P1 差分（定案 D4）：串行路径自建 plan；worker 注入路径只执行
    retry_ids；显式寻址（--mutation/--lines 卡尺）强制新执行，绕过差分。
    返回 (store2, prior_entries, reused, untouched, disclosures,
    executed_list, results_uncovered)；零执行且零复用 → None（零 GUT）。"""
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
        return None
    from .testscan import selection_disclosure
    # §SPEC-613：自动选测披露（弱选测时 weak=true 随 summary 透出）。
    disclosures.update(selection_disclosure(selection_meta))
    # SEE-1615 §SPEC-PERF2：等价剪枝披露（enabled=false 时不落键——既有
    # 消费方零扰动）
    eq_disc = c.get("equivalent_pruned")
    if eq_disc and eq_disc.get("enabled"):
        disclosures["equivalent_pruned"] = eq_disc
    if c["total_sites"] > budget:
        disclosures["total_sites"] = c["total_sites"]
        # O2：此处 executed 仍为计划面预估——_execute_planned 在 invalid
        # 预检后以 actually_executed 覆写（实际执行语义的唯一出处）。
        disclosures.setdefault("executed", len(executed_list))
    return (store2, prior_entries, reused, untouched, disclosures,
            executed_list, results_uncovered)


def _run_mutation_core(file_path: str, project_root: str, budget: int = 50,
    timeout_s: int = 600, dry_run: bool = False,
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
    break_on_baseline_failure: bool = False,
    kill_bail: bool | None = None) -> dict:
    """对单个 .gd 文件做变异测试并出统一 JSON 契约。分解见 _core_setup/
    _core_caliper_and_early/_core_plan + _execute_planned。"""
    _require_baseline_snapshot(mutant_slice, retry_ids, precomputed_baseline)
    clock = StageClock()
    setup_error, c = _core_setup(
        file_path, project_root, tests_glob, binary_kind, worker_channel,
        identity, selection_meta, select_mode, clock, mutation_warning,
        dry_run=dry_run)
    if setup_error is not None:
        return setup_error
    path, original_src, identity = c["path"], c["original_src"], c["identity"]
    early, tests_glob, selection_meta = _core_caliper_and_early(
        c, project_root, path, select_ids=select_ids,
        select_lines=select_lines, select_mode=select_mode, dry_run=dry_run,
        budget=budget, timeout_s=timeout_s, binary_kind=binary_kind,
        resume2_store=resume2_store, mutate_all=mutate_all,
        identity=identity)
    if early is not None:
        return early
    # SEE-1610 §SPEC-IMPL：test 级筛选开关（默认开启，flag/env 可关断）。
    # 仅当 chunk 携带组内映射（tests_allow）且开关开启时下发宿主；
    # fn/file/full 选面与无映射组天然不含 tests_allow → 行为不变。
    global _ACTIVE_ALLOW_TESTS
    _ACTIVE_ALLOW_TESTS = None
    _ta = (selection_meta or {}).get("tests_allow") or {}
    if _ta and killbail.resolve_test_filter():
        _ACTIVE_ALLOW_TESTS = (
            [[str(i).partition("::")[0], str(i).partition("::")[2]]
             for i in (_ta.get("identities") or [])]
            + [[str(s2), "*"] for s2 in (_ta.get("script_lane") or [])])
    global _ACTIVE_ALLOW_SCOPE
    _ACTIVE_ALLOW_SCOPE = str(_ta.get("scope") or "line") if _ta else None
    scoped = tests_glob is not None
    plan = _core_plan(
        c, path, project_root, original_src, tests_glob=tests_glob,
        budget=budget, binary_kind=binary_kind, resume2_store=resume2_store,
        mutate_all=mutate_all, retry_ids=retry_ids, select_ids=select_ids,
        select_lines=select_lines, reuse_coverage=reuse_coverage,
        mutant_slice=mutant_slice, identity=identity, clock=clock,
        selection_meta=selection_meta)
    if plan is None:
        return _no_sites_result(str(path), scoped)
    (store2, prior_entries, reused, untouched, disclosures, executed_list,
     results_uncovered) = plan
    return _execute_planned(
        path, project_root, original_src, original_src, c["mutants"],
        executed_list, reused, results_uncovered, prior_entries,
        c["scope_hashes"], disclosures, budget, tests_glob, timing_store,
        timeout_s, precomputed_baseline, store2, timeout_factor,
        c["header_hash"], retry_ids, scoped, c["total_sites"],
        progress=progress, identity=identity,
        targeted_only=bool(select_ids or select_lines),
        worker_channel=worker_channel, clock=clock,
        break_on_baseline_failure=break_on_baseline_failure,
        kill_bail=kill_bail)
def _execute_planned(path, project_root, original_src, backup, mutants,
                     executed_list, reused, results_uncovered, prior_entries,
                     scope_hashes, disclosures, budget, tests_glob,
                     timing_store, timeout_s, precomputed_baseline, store2,
                     timeout_factor, header_hash, retry_ids, scoped,
                     total_sites, progress=None, identity=None,
                     targeted_only=False, worker_channel=None,
                     clock: StageClock | None = None,
                     break_on_baseline_failure: bool = False,
                     kill_bail: bool | None = None):
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
        worker_channel=worker_channel, clock=clock,
        break_on_baseline_failure=break_on_baseline_failure,
        kill_bail=kill_bail)
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
# SEE-1615 §SPEC-HANG：腿 spawn 超时下限（秒）——缺失/0 兜底，防立即超时。
_LEG_TIMEOUT_FLOOR = 120

# §SPEC-P2-R1-D2：本轮 baseline 红测试的脚本限定身份集 {(script, name)}，
# 由 _loop_baseline 每轮重置；供（1）宿主红测跳过配对（2）同名列 kill 判
# 定的保守豁免。空集 = 基线流无脚本标题可解析 → 回退裸名语义。
_BASELINE_RED_MAP: set[tuple[str, str]] = set()

# SEE-1610 §SPEC-IMPL：本 run 的 test 级 allowlist（selection_meta.tests_allow
# 解析产物：[script, name] / [script, "*"]）。None = 本腿不启用过滤（开关
# 关断 / 非 line 选面 / 组内无映射）。per-run 进程级上下文，与
# _BASELINE_RED_MAP 同款（chunk 进程各自在 run_mutation 入口重置）。
_ACTIVE_ALLOW_TESTS: list | None = None
# 闭包形态（"line"|"file"；None=未启用）。file = 归因有损文件的整文件面。
_ACTIVE_ALLOW_SCOPE: str | None = None

def _run_single_mutant(m: Mutant, path, project_root: str, original_src: str,
                       backup: str, tests_glob: str | None, seq: int,
                       baseline_failures: set[str], baseline_dirty: bool,
                       slot=None, auto_name: str = "",
                       ownership_fn=None, target_rel: str = "",
                       target_res: str = "",
                       direct_scripts: list[str] | None = None,
                       bail_enabled: bool = True,
                       red_pairs: list[tuple[str, str]] | None = None) -> dict:
    """磁盘影子注入 → 跑 GUT → 分类；真实仓目标文件全程零写入（SEE-1550）。

    - 注入：每 mutant 一次 replace，无恢复步，写失败上抛转 run_error。
    - 四级 beacon：任何一级失败都不得产出 killed/survived。
    - direct_scripts 在场（§SPEC-P3 路线 B）= cold 一次性直驱宿主：进程数
      不变，宿主内按脚本顺序执行并新增失败早停。"""
    global _ACTIVE_ENV, _ACTIVE_SCRIPT
    injected, cfg_path, user_dir = _sm_inject(
        m, slot, target_rel, target_res, original_src, project_root, path,
        auto_name, direct_scripts=direct_scripts,
        baseline_failures=baseline_failures, bail_enabled=bail_enabled,
        red_pairs=red_pairs)
    if cfg_path is None:
        # D2：注入失败腿（宿主未跑）在过滤启用时同样携带 test_face 兜底键。
        if _ACTIVE_ALLOW_TESTS:
            injected = {**injected, "test_face": {
                "requested": None, "kept": None, "removed": None,
                "whole_script_fallback": None, "marker_missing": True}}
        return injected  # 注入失败 run_error 契约
    _ACTIVE_ENV, _ = mutant_env(str(cfg_path), user_dir)
    if direct_scripts is not None:
        from . import directdrive
        _ACTIVE_SCRIPT = directdrive.host_script_res(slot.root)
    else:
        _ACTIVE_SCRIPT = beacon.beacon_script_res(slot.root)
    try:
        try:
            # SEE-1615 §SPEC-HANG：仅当 _adaptive_timeout 缺失/≤0（未跑
            # baseline 的调用面）时用有界兜底 120s——正常自适应值（含显式
            # 60s）保持既有语义不抬高。
            _leg_timeout = int(_adaptive_timeout or 0)
            rc, tail = _run_gut_on_project(
                str(slot.root),
                timeout_s=(_leg_timeout if _leg_timeout > 0
                           else _LEG_TIMEOUT_FLOOR),
                tests_glob=tests_glob)
            verdict = _sm_classify(m, rc, tail, baseline_failures,
                                   baseline_dirty, original_src, ownership_fn,
                                   project_root)
        except subprocess.TimeoutExpired:
            verdict = _mutant_record(m, "timeout",
                                     f"GUT timed out after {m.kind} {m.original}→{m.mutated}",
                                     src=original_src)
            if _ACTIVE_ALLOW_TESTS:
                verdict["test_face"] = {
                    "requested": None, "kept": None, "removed": None,
                    "whole_script_fallback": None, "marker_missing": True}
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
                   store2, prior_entries, scopes, kill_bail=False,
                   direct_scripts=None, red_pairs=None):
    """冷通道（M0）逐 mutant 执行循环（默认路径，行为与既有逐位不变）。"""
    raw_pairs: list[tuple] = []
    for seq, m in enumerate(mutants):
        t_mutant = time.monotonic()
        verdict = _run_single_mutant(
            m, path, project_root, original_src, backup, tests_glob, seq,
            baseline_failures, baseline_dirty, slot=slot,
            auto_name=auto_name, ownership_fn=ownership_fn,
            target_rel=target_rel, target_res=target_res,
            direct_scripts=direct_scripts, bail_enabled=kill_bail,
            red_pairs=red_pairs)
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
                   precomputed_baseline, timeout_factor,
                   break_on_baseline_failure: bool = False) -> tuple:
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
    # §SPEC-P2-R1-D2：红测试身份 = 脚本归属 + 测试名（裸名跨脚本删除是
    # D2 缺陷根因）。脚本限定集供跳过与同名列 kill 判定使用。
    global _BASELINE_RED_MAP
    from .gutrun import parse_failures_with_scripts
    qualified = parse_failures_with_scripts(baseline_tail)
    _BASELINE_RED_MAP = {(scr, name) for scr, name in qualified
                         if scr and scr != "*"}
    # SEE-1606 P2：开关关闭（默认）时继续跑并只做披露；打开时 fail-loud，
    # 零 mutant GUT（对齐 Stryker break-on-initial-test-failure=true）。
    if break_on_baseline_failure and baseline_failures:
        raise _BaselineDirtyFailure(baseline_failures)
    return baseline_failures, bool(baseline_failures), baseline_reused, \
        baseline_elapsed

def _loop_legs(path, project_root, original_src, backup, mutants, tests_glob,
               baseline_failures, baseline_dirty, slot, auto_name, target_rel,
               target_res, kind, idpack, exec_scheme_resolved, worker_channel,
               reporter, store2, prior_entries, scopes, clock, slot_info,
               t_slot, kill_bail=False, direct_scripts=None,
               red_pairs=None) -> list:
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
        if direct_scripts is not None:
            # §SPEC-P3 路线 B + D1-R1：cold 直驱宿主随 run 播种（per-PID，
            # 与 beacon 同款隔离）。播种不得区分 worker/冷分支——worker 分
            # 支的 cold canary/cold 腿同样可能走直驱宿主（D1-R1 未播种挂
            # 死路径）。
            from . import directdrive
            directdrive.seed_direct_host(slot.root)
        if use_worker:
            from .worker_channel import execute_worker_branch
            with clock.stage("per_mutant"):
                raw_pairs, fatal_events = execute_worker_branch(
                    path, project_root, original_src, mutants, tests_glob,
                    baseline_failures, baseline_dirty, slot, auto_name,
                    target_rel, target_res, kind, reporter, store2,
                    prior_entries, scopes, _ownership,
                    timings_out=timings_out, kill_bail=kill_bail,
                    tests_scripts=direct_scripts, red_pairs=red_pairs,
                    baseline_red_map=_BASELINE_RED_MAP,
                    allow_tests=_ACTIVE_ALLOW_TESTS,
                    expected_scheme=exec_scheme_resolved)
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
                    store2, prior_entries, scopes, kill_bail=kill_bail,
                    direct_scripts=direct_scripts, red_pairs=red_pairs)
    finally:
        release_problems = shadowpool.release_slot(slot)
        # §SPEC-202 slot_sync = 槽获取→归还断言全程；槽位固定成本 = 其与
        # per_mutant 之差（§SPEC-210 LOW-1 口径修正）。
        clock.add("slot_sync", time.monotonic() - t_slot)
        if release_problems:
            slot_info["release_problems"] = release_problems
    return raw_pairs

def _test_filter_disclosure(active_allow) -> dict:
    """SEE-1610 §SPEC-JSON：test_filter 摘要披露（开关 + 实际下发规模；
    逐腿 kept/removed/whole_script_fallback 在 verdict.test_face）。"""
    at = active_allow or []
    return {
        "enabled": killbail.resolve_test_filter(),
        "disabled_by": killbail.test_filter_disabled_by(None),
        "applied": bool(at),
        "allow_identities": sum(1 for p in at if p[1] != "*"),
        "script_lane_fallback": sum(1 for p in at if p[1] == "*"),
        # SEE-1610 D1：闭包形态（file = 归因有损文件整文件面回退）。
        "scope": _ACTIVE_ALLOW_SCOPE if at else None}

def _run_mutant_loop(
    path, project_root: str, original_src: str, backup: str,
    mutants: list[Mutant], tests_glob: str | None, timeout_s: int,
    precomputed_baseline: dict | None = None,
    store2=None, prior_entries: dict | None = None, scopes: dict | None = None,
    timeout_factor: float | None = None, reporter=None,
    identity: dict | None = None, targeted_only: bool = False,
    worker_channel=None, clock: StageClock | None = None,
    break_on_baseline_failure: bool = False,
    kill_bail: bool | None = None,
) -> tuple[list, bool, float, dict]:
    """逐 mutant 跑 GUT 并分类判定；返回 (raw_pairs, baseline_reused,
    baseline_elapsed, slot_info)。verdict 记录不在此处进 results——D5 streak
    是窗口级判定，由调用方 apply_timeout_streaks 落定后统一扩展。"""
    with tempfile.TemporaryDirectory() as workdir:
        # SEE-1606 §SPEC-P1：baseline 前权威比对门——setup 权威 scheme 与
        # 实跑重解析漂移即具名失败（零 baseline 支付、零槽写入）。
        _preflight_route_check(identity, project_root, path, worker_channel)
        (baseline_failures, baseline_dirty, baseline_reused,
         baseline_elapsed) = _loop_baseline(workdir, path, project_root,
                                            tests_glob, timeout_s,
                                            precomputed_baseline,
                                            timeout_factor,
                                            break_on_baseline_failure)
        clock = clock or StageClock()
        # SEE-1606 §SPEC-P3：kill 即退有效开关在此解析（env/flag 单点），
        # 直驱脚本清单按面判定一次并贯穿 cold/worker 两条腿路径。
        bail_effective = killbail.resolve_kill_bail(kill_bail)
        dirty = bool(baseline_failures)
        bail_scripts, bail_face, bail_gut_ok = _cold_direct_decision(
            bail_effective, str(project_root), tests_glob, dirty=dirty)
        # SEE-1606 §SPEC-PERF1：历史杀手优先排序（仅置换清单，verdict 语义
        # 不变；缺失历史确定性原序；env 可关断）。
        bail_scripts = _ordered_bail_scripts(
            project_root, path, bail_scripts)
        # §SPEC-P2-R1：冷通道无法物理跳过红测试的面（显式目录/全脸/缺
        # gut.gd）+ 基线脏 → 具名拒绝；worker 通道（可控宿主）不受限。
        # §SPEC-P1：通道判读消费权威 scheme（不再重解析——preflight 门已
        # 保证 setup 权威与实跑一致）。
        worker_host = (dirty and (identity or {}).get("exec_scheme")
                       == WORKER_SCHEME)
        if dirty and bail_scripts is None and not worker_host:
            raise _BaselineSkipUnsupportedFace(
                sorted(baseline_failures), bail_face)
        skip_applied = dirty and (bail_scripts is not None or worker_host)
        (slot, idpack, auto_name, target_rel, target_res, scheme,
         slot_info, t_slot) = _loop_slot(
            project_root, path, detect_binary_kind(), identity, tests_glob,
            worker_channel, targeted_only, clock)
        red_pairs = sorted(_BASELINE_RED_MAP) if (dirty and skip_applied) \
            else []
        raw_pairs = _loop_legs(
            path, project_root, original_src, backup, mutants, tests_glob,
            baseline_failures, baseline_dirty, slot, auto_name, target_rel,
            target_res, detect_binary_kind(), idpack, scheme, worker_channel,
            reporter, store2, prior_entries, scopes, clock, slot_info, t_slot,
            bail_effective, bail_scripts, red_pairs)
        slot_info["inject_scheme"] = idpack.get("inject_scheme", INJECT_SCHEME)
        slot_info["slot_mode"] = slot.mode
        slot_info["freshness"] = list(slot.freshness)
        # SEE-1606 P2：基线失败登记披露（默认继续跑；失败测试不参与 kill
        # 判定——判定侧 `- baseline_failures` 差集保证）。
        slot_info["baseline_dirty"] = baseline_dirty
        slot_info["baseline_failures"] = len(baseline_failures)
        slot_info["baseline_failure_names"] = sorted(baseline_failures)
        slot_info["break_on_baseline_failure"] = bool(
            break_on_baseline_failure)
        slot_info["kill_bail"] = bail_effective
        slot_info["kill_bail_face"] = bail_face
        slot_info["kill_bail_disabled_by"] = killbail.disabled_by(kill_bail)
        slot_info["kill_bail_scripts"] = (
            len(bail_scripts) if bail_scripts is not None else None)
        # SEE-1610 §SPEC-JSON：test_filter 摘要披露（开关 + 实际下发规模；
        # 逐腿 kept/removed/whole_script_fallback 在 verdict.test_face）。
        slot_info["test_filter"] = _test_filter_disclosure(_ACTIVE_ALLOW_TESTS)
        if dirty:
            slot_info["baseline_skip"] = {
                "applied": bool(skip_applied), "face": bail_face,
                "requested": len(baseline_failures)}
        if any(v["verdict"] == "survived" for _, v in raw_pairs):
            slot_info["suspected_orphan"] = _orphan_disclosure(
                project_root, target_res)
    return raw_pairs, baseline_reused, baseline_elapsed, slot_info

def _ordered_bail_scripts(project_root, path, bail_scripts):
    """SEE-1606 §SPEC-PERF1：历史杀手优先（None / env 关断 → 原样返回）。"""
    if bail_scripts is None or not killbail.resolve_test_order():
        return bail_scripts
    return killbail.order_scripts_by_history(
        bail_scripts, killbail.kill_history(str(project_root), str(path)))


def _preflight_route_check(identity, project_root, path, worker_channel):
    """SEE-1606 §SPEC-P1：baseline 前权威比对门。

    setup/父层身份携带权威 exec_scheme 时，本地重解析只做漂移校验——
    不一致抛 RouteDriftError（where=pre_baseline），由 with_aborts 转具名
    JSON；此门保证漂移永不先付 baseline。identity 无权威 scheme（串行
    首解析前的 None 形态）时跳过——下游 _loop_slot 比对仍覆盖。"""
    expected = (identity or {}).get("exec_scheme") \
        if isinstance(identity, dict) else None
    if expected is None:
        return
    scheme, _route = resolve_exec_scheme(
        str(project_root), _res_path_of(str(path), str(project_root)),
        worker_channel)
    if scheme != expected:
        raise RouteDriftError(_res_path_of(str(path), str(project_root)),
                              str(expected), str(scheme), "pre_baseline")


class _BaselineSkipUnsupportedFace(Exception):
    """§SPEC-P2-R1：基线脏 ∧ 冷通道面无法物理跳过红测试 → 具名拒绝。"""

    def __init__(self, names: list[str], face_kind: str):
        self.names = sorted(names)
        self.face_kind = face_kind
        super().__init__(
            f"baseline has {len(self.names)} failing test(s); cold legacy "
            f"face cannot physically skip them (face={face_kind})")


def _cold_direct_decision(kb_effective: bool, project_root: str,
                          tests_glob: str | None, dirty: bool = False,
                          ) -> tuple[list[str] | None, str, bool]:
    """cold 直驱宿主启用判定（§SPEC-P3 路线 B + §SPEC-P2-R1 单点）。

    返回 (脚本清单|None, face 种类, gut_ok)。清单非 None = 启用直驱宿主；
    None = 既有 beacon+gut_cmdln 路径（face/gut 缺失具名落在
    kill_bail_face）。宿主启用条件：bail 开启，或基线脏（§SPEC-P2-R1
    红测试物理跳过必须在可控宿主内进行，gut_cmdln 无法按单测过滤）。"""
    from .worker_channel import gut_direct_drive_available

    scripts, face_kind = killbail.face_scripts(project_root, tests_glob)
    gut_ok = gut_direct_drive_available(project_root)
    if scripts is None or not gut_ok:
        return None, ("gut_addon_missing" if scripts is not None
                      else face_kind), gut_ok
    if not (kb_effective or dirty):
        return None, face_kind, gut_ok
    return scripts, face_kind, gut_ok


def _run_mutant_loop_with_aborts(path, project_root, original_src, backup,
                                 executed_list, tests_glob, timeout_s,
                                 precomputed_baseline=None,
                                 store2=None, prior_entries=None,
                                 scopes=None,
                                 timeout_factor: float | None = None,
                                 reporter=None, identity=None,
                                 targeted_only: bool = False,
                                 worker_channel=None,
                                 clock: StageClock | None = None,
                                 break_on_baseline_failure: bool = False,
                                 kill_bail: bool | None = None):
    """baseline 中止形态转统一 JSON（timeout / godot 启动失败）；否则返回 raw。"""
    try:
        return _run_mutant_loop(
            path, project_root, original_src, backup,
            _valid_mutants(original_src, executed_list), tests_glob, timeout_s,
            precomputed_baseline, store2, prior_entries, scopes,
            timeout_factor, reporter=reporter, identity=identity,
            targeted_only=targeted_only, worker_channel=worker_channel,
            clock=clock,
            break_on_baseline_failure=break_on_baseline_failure,
            kill_bail=kill_bail,
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
    except _BaselineDirtyFailure as e:
        # SEE-1606 P2：显式 --break-on-baseline-failure 下的基线脏中止。
        # 默认路径不走这里（继续跑 + 失败测试退出 kill 判定）。
        from .reporting import _break_on_baseline_result
        return _break_on_baseline_result(str(path), e.names)
    except _BaselineSkipUnsupportedFace as e:
        # §SPEC-P2-R1：基线脏 ∧ 冷通道面无法物理跳过 → 具名拒绝（结构性
        # 限制的最小可执行方案：拒绝，不退回「跑了但不算」）。
        from .reporting import _baseline_skip_unsupported_result
        return _baseline_skip_unsupported_result(str(path), e.names,
                                                 e.face_kind)
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
    except RouteDriftError as e:
        # SEE-1606 §SPEC-P1：route 漂移（pre_baseline 门 / _loop_slot /
        # worker 注入）→ 具名中止契约（零 baseline / 零错误 key 写入）。
        return _route_drift_result(str(path), e)
