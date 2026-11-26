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
        # SEE-1623 H1：文件形态回落的根目录扫描同样不递归（tests 在子目录
        # 时收集 0 个脚本 → 全腿无测试流）；-gselect 仍按名单过滤。
        return [
            "-gdir=res://tests/",
            f"-gselect={script_stem}",
            "-ginclude_subdirs",
        ]
    if tests_glob is None:
        # SEE-1623 H1：full 审计面（tests_glob=None）的缺省根扫描必须递归
        # ——GUT 9.6 add_directory「Does not look in sub directories」，
        # 标准布局（tests/unit/ 等）下 0 脚本 → L4 门全腿 run_error。
        # 显式目录 glob（上方 return）语义逐字不变。
        return ["-gdir=res://tests/", "-ginclude_subdirs"]
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
    jobs: int | None = None,
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
        kill_bail=kill_bail,
        jobs=jobs
    )
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
        # §SPEC-032/D1：run 阶段不再自己续新 map（prepare 后再漂移 →
        # 既有具名失败、零 mutant）——renew 固定 False
        tests_glob, selection_meta = _resolve_auto_glob(
            tests_glob, str(path), project_root, selection_meta,
            select_mode=select_mode, allow_renew=False)
    global _FACE_SCRIPT_BY_BASENAME
    _FACE_SCRIPT_BY_BASENAME = {
        Path(t).name: t for t in (selection_meta.get("tests") or [])}
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

_ENV_CHECK_BASELINE = "GQT_MUTATION_CHECK_BASELINE"


def _expensive_operations() -> list:
    """§SPEC-PLANFIRST：plan 必须列出的贵操作与成本口径（静态清单，
    成本为经验默认而非实测——实测面见 stage_timings/预检 wall_s）。"""
    return [
        {"op": "baseline_gut", "when": "map build path",
         "cost": "uninstrumented full-face GUT (minutes-scale)",
         "skippable": False,
         "reuse": "preflight result reused for arms (P4, no second run)"},
        {"op": "attribution_map_build",
         "when": "map missing/stale (auto mode)",
         "cost": "instrumented gut_full arm + acceptance GUT (minutes)",
         "skippable": True, "reuse": "fresh map; partial map on red/"
         "timeout baseline (PLANFIRST)"},
        {"op": "mutation_legs", "when": "always",
         "cost": "per-mutant GUT legs (budget x timeout_s)",
         "skippable": False, "reuse": "resume v2 store (diff/resume)"},
    ]


def _check_baseline_requested() -> bool:
    """§SPEC-PREFLIGHT P3：--check-baseline（CLI 设 env 单点）。"""
    return os.environ.get(_ENV_CHECK_BASELINE, "").strip() \
        not in ("", "0", "false")


def _baseline_health_precondition(project_root: str,
                                  timeout_s: int) -> dict:
    """§SPEC-PREFLIGHT P3：dry-run 计划的基线健康前置。

    默认 not_checked + 原因（不假装全绿）；--check-baseline 只读跑一次
    未插桩基线 GUT（不写 map、零副作用面=一次 GUT 进程）——红即具名
    （count/top-N），绿 green，获取失败 unknown + 原因。"""
    if not _check_baseline_requested():
        return {"status": "not_checked",
                "reason": "check_baseline_not_requested"}
    try:
        from ..measurement.orchestrator import (
            _acquire_lazy_baseline, _gutconfig_face_args)
        from ..measurement.recipe import build_recipe
        recipe, err = build_recipe(Path(project_root))
        if err is not None:
            return {"status": "unknown",
                    "reason": f"recipe unavailable: {err}"}
        pairs, meta = _acquire_lazy_baseline(
            Path(project_root), _gutconfig_face_args(recipe), timeout_s)
    except Exception as e:  # noqa: BLE001 — 探测失败具名落块（不假装绿）
        return {"status": "unknown", "reason": f"{type(e).__name__}: {e}"}
    if pairs:
        top = sorted(pairs)[:10]
        return {"status": "red", "count": len(pairs),
                "top": [f"{sr}::{t}" for sr, t in top],
                "face": meta.get("face"), "wall_s": meta.get("wall_s")}
    return {"status": "green", "count": 0, "face": meta.get("face"),
            "wall_s": meta.get("wall_s")}


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


def _dry_run_lines_disclosure(select_lines, mutants) -> "dict | None":
    """卡尺 `--lines` 的求交披露在 dry-run 也与完整路径同源（requested/
    hit/skipped_no_site），避免只在实际执行轮才可见（自 _core_dry_run
    原样搬移）。未请求 `--lines` 返回 None。"""
    if select_lines is None:
        return None
    lines = {int(v) for v in select_lines}
    dry_hit = sorted({m.line for m in mutants if m.line in lines})
    return {
        "requested": sorted(lines),
        "requested_count": len(lines),
        "hit": dry_hit,
        "hit_count": len(dry_hit),
        "skipped_no_site": sorted(lines - set(dry_hit)),
    }


def _dry_run_selected_sites(path, mutants, select_ids, select_lines) -> tuple:
    """SEE-1623 M5：dry-run 预览与真实执行同构（自 _core_dry_run 原样搬移）
    ——显式寻址（--mutation/--lines）先于估算应用（此前计数用全量位点，
    预览失真且掩盖数值校验）；全无位点抛与真实运行同文的显式错误（不再
    exit 0 假成功）。

    返回 (error_result, mutants, total_sites)：error_result 非 None 即调用
    方原样返回终止；未显式寻址时 total_sites 为 None（调用方保持原值）。"""
    if select_ids is None and select_lines is None:
        return None, mutants, None
    from .planner import selection_preview
    from .reporting import _setup_error_result
    try:
        selected, _sel_disc = selection_preview(mutants, select_ids,
                                                select_lines)
    except ValueError as e:
        return _setup_error_result(str(path), e), mutants, None
    return None, selected, len(selected)


def _core_dry_run(path, mutants, budget, scoped, total_sites, project_root,
                  tests_glob, binary_kind, resume2_store, mutate_all,
                  header_hash, scope_hashes, attention, identity,
                  selection_meta, select_ids, select_lines, select_mode,
                  timeout_s: int = 600,
                  jobs: int | None = None,
                  clock: "StageClock | None" = None,
                  t0: "float | None" = None) -> dict:
    """dry_run（SPEC-009）估算与选面/求交披露（与完整执行路径同一判别点）。"""
    # §SPEC-PLANFIELD-A：clock/t0 由 _run_mutation_core 传入（含
    # _core_setup 的 test_scan 段——重度解析在 dry-run 早退路径同样计入）；
    # 直调（测试）缺省自建。
    if clock is None:
        clock = StageClock()
    if t0 is None:
        t0 = time.monotonic()
    _sel_err, mutants, _sel_total = _dry_run_selected_sites(
        path, mutants, select_ids, select_lines)
    if _sel_err is not None:
        return _sel_err
    if _sel_total is not None:
        total_sites = _sel_total
    with clock.stage("planning"):
        estimate = dry_run_estimate(
            str(path), mutants, budget, scoped, total_sites,
            project_root, tests_glob, binary_kind, resume2_store,
            mutate_all, header_hash, scope_hashes, attention=attention,
            identity=identity)
    from .testscan import resolve_selection_mode, selection_disclosure
    with clock.stage("route_scan"):
        estimate["summary"].update(selection_disclosure(selection_meta))
    estimate["summary"].setdefault(
        "selection_mode",
        resolve_selection_mode(select_mode, selection_meta or {},
                               select_ids, select_lines))
    # 卡尺 `--lines` 的求交披露在 dry-run 也与完整路径同源（requested/
    # hit/skipped_no_site），避免只在实际执行轮才可见。
    _lines_disc = _dry_run_lines_disclosure(select_lines, mutants)
    if _lines_disc is not None:
        estimate["summary"]["lines_selection"] = _lines_disc
    # SEE-1610 §SPEC-PLAN Phase 0：选面披露落定后重建同构 plan（V7 同构）。
    # SEE-1606 §SPEC-DERIVED-A：三静态输入独立成 derived 键（显式 timeout/
    # wiring hash/显式 tests glob；缺席不造）+ 只读静态前置块。
    from .executor import effective_jobs
    from .identity import boot_wiring_hash
    from .reporting import _execution_plan, _plan_meta
    from .shadow import _IMPORT_ARTIFACTS
    explicit_glob = tests_glob if (selection_meta or {}).get(
        "source") == "explicit_tests" else None
    jobs_eff = effective_jobs(jobs)
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
        "baseline_health": _baseline_health_precondition(
            project_root, int(timeout_s or 600)),
    }
    derived_extra = {"budget": budget, "explicit_timeout_s": timeout_s,
                     "boot_wiring_hash": boot_wiring_hash(str(project_root)),
                     "explicit_tests_glob": explicit_glob,
                     "jobs": jobs_eff}
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
        static_preconditions=static_pc,
        expensive_operations=_expensive_operations())
    estimate["summary"]["execution_plan_meta"] = _meta
    # §SPEC-PLANFIELD-A：dry-run 同款全键阶段分解（planning/route_scan
    # 真实测量，其余阶段未发生为 0.0；total=墙钟基准，形态与实跑一致）
    # §SPEC-PLANFIRST：dry-run = plan 全部内容——静态问题清单（永不阻断，
    # severity 分级由消费方判；error 级在默认 run 入口阻断）
    from .plancheck import static_plan
    estimate["summary"]["plan"] = static_plan(
        str(project_root), str(path), tests_glob)
    estimate["summary"]["stage_timings"] = clock.disclosure()
    estimate["summary"]["stage_timings"]["total"] = round(
        time.monotonic() - t0, 3)
    return estimate

def _core_caliper_and_early(c: dict, project_root: str, path: Path, *,
                            select_ids, select_lines, select_mode, dry_run,
                            budget, timeout_s: int, binary_kind,
                            resume2_store, mutate_all, identity,
                            jobs: int | None = None,
                            clock: "StageClock | None" = None,
                            t0: "float | None" = None) -> tuple:
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
            timeout_s=timeout_s, jobs=jobs, clock=clock,
            t0=t0), tests_glob, selection_meta)
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
    kill_bail: bool | None = None,
    jobs: int | None = None) -> dict:
    """对单个 .gd 文件做变异测试并出统一 JSON 契约。分解见 _core_setup/
    _core_caliper_and_early/_core_plan + _execute_planned。"""
    _require_baseline_snapshot(mutant_slice, retry_ids, precomputed_baseline)
    t0 = time.monotonic()
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
        identity=identity, jobs=jobs, clock=clock, t0=t0)
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
# §SPEC-032/D13：行选面 wrapper basename → 原脚本 res:// 完整路径
# （选择目录物化保名；同名歧义由选择目录具名拒绝——see1581）
_FACE_SCRIPT_BY_BASENAME: dict[str, str] = {}

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

def _synthesized_map_baseline(path, project_root, tests_glob):
    """SEE-1631 §SPEC-020：判定基线单一真源——map 源时由 verdict 表合成
    既有形状的快照（下游解析/判定零改动）；非 map 源/显式 off 返回 None
    （冷启动照旧）。

    §SPEC-021：prepare 阶段（CLI）已自动续新；此处为库内直调兜底——
    合成前再校验一致性，漂移即具名 fail-loud（不静默退化，续新由调用方
    的 prepare 链承担）。

    C3-R10 退回2：prepare 之后的 prewarm re-import 会把引擎侧车
    （.translation 等，在内容指纹内）重新生成，同一条命令的 run 期合成
    于是判 map 过期、失败一次。内容漂移类 BaselineSourceError 在本命令
    内补一次 refresh（具名披露）再重试合成一次；仍漂移才 fail-loud。"""
    from .truth_select import mode as _map_mode
    from .verdict_source import (
        BaselineSourceError, baseline_source, map_baseline_snapshots,
    )

    if baseline_source() != "map" or _map_mode() == "off":
        return None
    try:
        syn, _fail = map_baseline_snapshots(
            [str(path)], project_root, tests_glob)
    except BaselineSourceError as e:
        if "production content drift" not in str(e):
            raise BaselineSourceError(f"loop baseline: {e}") from e
        # 本命令内 prewarm re-import 重新生成的侧车：命令内补续新一次
        from .map_contract import refresh as _refresh

        print("mutation: run-time content drift (engine re-import "
              "regenerated sidecars) — refreshing in-command "
              "(C3-R10 退回2, disclosed)", file=sys.stderr)
        _refresh(project_root, [str(path)])
        syn, _fail = map_baseline_snapshots(
            [str(path)], project_root, tests_glob)
    except BaselineSourceError as e:
        raise BaselineSourceError(
            f"loop baseline: {e}") from e
    snap = syn.get(tests_glob) or syn.get(None)
    if snap is None:
        raise BaselineSourceError(
            "loop baseline: map produced no baseline snapshot "
            f"for glob {tests_glob!r}")
    print("mutation: verdict baseline sourced from attribution "
          "map (§SPEC-020; cold start skipped)", file=sys.stderr)
    return snap


def _assert_map_reds_match_real(precomputed_baseline, qualified) -> None:
    """C3-R10 退回4：map 快照基线的实跑红集与 map 红集一致性门——cold /
    --tests 等无 parity 腿的入口，map 说红而实跑绿（或反之）在此具名
    fail-loud（零 mutant），与 worker parity 门同判据。非 map 快照放行。"""
    if precomputed_baseline is None \
            or precomputed_baseline.get("red_pairs") is None:
        return
    from .verdict_source import BaselineSourceError as _BSE

    map_reds = {name for _scr, name
                in (precomputed_baseline.get("red_pairs") or [])}
    real_reds = {name for _scr, name in qualified}
    if real_reds != map_reds:
        raise _BSE(
            "baseline_map_mismatch: attribution-map verdict baseline "
            "diverged from real execution (loop baseline) — "
            f"batch invalidated: map={sorted(map_reds)[:4]} "
            f"real={sorted(real_reds)[:4]}")


def _worker_branch_baseline(baseline_failures, baseline_dirty,
                            red_pairs) -> tuple:
    """C3-R10 退回4：chunk 路径 baseline_failures 恒空（map 红集经
    executor 填入 _BASELINE_RED_MAP，ProcessPool 子进程全局不复用）——
    map 红集非空时视同基线脏并填充失败集/红对，让 worker parity 门拿到
    真实期望集（负对照：map 谎报红 → 整批作废）。"""
    from .verdict_source import baseline_source as _bs

    wf_baseline_failures = baseline_failures
    wf_dirty = baseline_dirty
    wf_red_pairs = red_pairs
    if _bs() == "map" and _BASELINE_RED_MAP:
        if not wf_baseline_failures:
            wf_baseline_failures = {n for _s, n in _BASELINE_RED_MAP}
        if not wf_red_pairs:
            wf_red_pairs = sorted(_BASELINE_RED_MAP)
        wf_dirty = True
    return wf_baseline_failures, wf_dirty, wf_red_pairs


def _loop_baseline(workdir, path, project_root, tests_glob, timeout_s,
                   precomputed_baseline, timeout_factor,
                   break_on_baseline_failure: bool = False) -> tuple:
    """workdir 备份 + baseline 实测/复用 + 自适应 timeout 锚 + 启动失败守卫。
    baseline 永远实测（判定语义锚）；快照 elapsed 只喂预算锚。"""
    backup_path = os.path.join(workdir, "original.gd")
    shutil.copy2(str(path), backup_path)
    if precomputed_baseline is None:
        precomputed_baseline = _synthesized_map_baseline(
            path, project_root, tests_glob)
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
    _assert_map_reds_match_real(precomputed_baseline, qualified)
    # SEE-1631 §SPEC-022（Owner 终裁）：map 新鲜且与当前树一致时，跑
    # mutant 之前不执行任何未插桩全脸基线/验证运行——判定基线 = map
    # verdict 表（树一致性已在合成前 fail-loud 门承担）。残余通道内不
    # 一致由既有 parity 门 / re-canary / 失配整批作废守护兜底。
    # C3-R10 退回4：合成基线在函数内部生成时回传给调用方——否则
    # _map_parity_gate 拿到的 precomputed_baseline 仍是 None，--select
    # full 入口的"map 与实跑分歧整批作废"负对照失效。
    return baseline_failures, bool(baseline_failures), baseline_reused, \
        baseline_elapsed, precomputed_baseline

def _loop_legs(path, project_root, original_src, backup, mutants, tests_glob,
               baseline_failures, baseline_dirty, slot, auto_name, target_rel,
               target_res, kind, idpack, exec_scheme_resolved, worker_channel,
               reporter, store2, prior_entries, scopes, clock, slot_info,
               t_slot, kill_bail=False, direct_scripts=None,
               red_pairs=None, baseline_snapshot: dict | None = None) -> list:
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
            wf_baseline_failures, wf_dirty, wf_red_pairs = \
                _worker_branch_baseline(baseline_failures, baseline_dirty,
                                        red_pairs)
            with clock.stage("per_mutant"):
                raw_pairs, fatal_events = execute_worker_branch(
                    path, project_root, original_src, mutants, tests_glob,
                    wf_baseline_failures, wf_dirty, slot, auto_name,
                    target_rel, target_res, kind, reporter, store2,
                    prior_entries, scopes, _ownership,
                    timings_out=timings_out, kill_bail=kill_bail,
                    tests_scripts=direct_scripts, red_pairs=wf_red_pairs,
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

def _map_parity_gate(slot_info: dict, precomputed_baseline) -> None:
    """SEE-1631 §SPEC-020：map 源判定基线下 parity 门失配 = "map 说绿
    而执行红（或反之）"被实跑证伪——整批作废、具名 fail-loud（不得
    降级继续产出批量假 kill）。冷源（legacy）维持原降级语义。"""
    if not baseline_snapshot_from_map(precomputed_baseline):
        return
    for ev in slot_info.get("worker_fatal_events") or []:
        if str(ev).startswith("mode parity gate"):
            from .verdict_source import BaselineSourceError

            raise BaselineSourceError(
                "baseline_map_mismatch: attribution-map verdict "
                "baseline diverged from real execution "
                f"(parity gate) — batch invalidated: {ev}")


def _mutant_slot_disclosures(slot_info: dict, idpack: dict, slot,
                             baseline_dirty: bool, baseline_failures,
                             break_on_baseline_failure: bool,
                             bail_effective, bail_face, kill_bail,
                             bail_scripts) -> None:
    """slot_info 判定与披露字段登记（键序与既有产物形状逐项一致）。"""
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


def _map_reds_dirty(red_map) -> bool:
    """C3-R10 退回4：chunk/worker 路径的 baseline_failures 恒空（map 红
    集经 executor 填入 _BASELINE_RED_MAP）——map 红集非空时视同基线脏，
    启用 skip 与 parity 门（负对照：map 谎报红 → parity 腿全脸实测绿 →
    失配整批作废）。"""
    from .verdict_source import baseline_source as _bs

    return _bs() == "map" and bool(red_map)


class _SkipDecision:
    """skip/parity 门判定结果（C3-R10 退回4：dirty_eff 单点判定）。"""

    def __init__(self, dirty, worker_host, bail_scripts, skip_applied):
        self.dirty = dirty
        self.worker_host = worker_host
        self.bail_scripts = bail_scripts
        self.skip_applied = skip_applied


def _skip_decision(baseline_failures, red_map, dirty_eff, bail_scripts,
                   identity):
    """§SPEC-P2-R1：冷通道无法物理跳过红测试的面（显式目录/全脸/缺
    gut.gd）+ 基线脏 → 具名拒绝；worker 通道（可控宿主）不受限。"""
    worker_host = (dirty_eff and (identity or {}).get("exec_scheme")
                   == WORKER_SCHEME)
    if dirty_eff and bail_scripts is None and not worker_host:
        raise _BaselineSkipUnsupportedFace(
            sorted(baseline_failures) or sorted(red_map), "worker")
    skip_applied = dirty_eff and (bail_scripts is not None or worker_host)
    return _SkipDecision(dirty_eff, worker_host, bail_scripts, skip_applied)


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
         baseline_elapsed, precomputed_baseline) = _loop_baseline(
            workdir, path, project_root,
            tests_glob, timeout_s,
            precomputed_baseline,
            timeout_factor,
            break_on_baseline_failure)
        clock = clock or StageClock()
        # SEE-1606 §SPEC-P3：kill 即退有效开关在此解析（env/flag 单点），
        # 直驱脚本清单按面判定一次并贯穿 cold/worker 两条腿路径。
        bail_effective = killbail.resolve_kill_bail(kill_bail)
        dirty_eff = (bool(baseline_failures)
                     or _map_reds_dirty(_BASELINE_RED_MAP))
        bail_scripts, bail_face, bail_gut_ok = _cold_direct_decision(
            bail_effective, str(project_root), tests_glob, dirty=dirty_eff)
        # SEE-1606 §SPEC-PERF1：历史杀手优先排序（仅置换清单，verdict 语义
        # 不变；缺失历史确定性原序；env 可关断）。
        bail_scripts = _ordered_bail_scripts(
            project_root, path, bail_scripts)
        # §SPEC-P2-R1：冷通道无法物理跳过红测试的面（显式目录/全脸/缺
        # gut.gd）+ 基线脏 → 具名拒绝；worker 通道（可控宿主）不受限。
        # §SPEC-P1：通道判读消费权威 scheme（不再重解析——preflight 门已
        # 保证 setup 权威与实跑一致）。
        skip = _skip_decision(baseline_failures, _BASELINE_RED_MAP,
                              dirty_eff, bail_scripts, identity)
        (slot, idpack, auto_name, target_rel, target_res, scheme,
         slot_info, t_slot) = _loop_slot(
            project_root, path, detect_binary_kind(), identity, tests_glob,
            worker_channel, targeted_only, clock)
        red_pairs = sorted(_BASELINE_RED_MAP) if skip.skip_applied else []
        raw_pairs = _loop_legs(
            path, project_root, original_src, backup, mutants, tests_glob,
            baseline_failures, baseline_dirty, slot, auto_name, target_rel,
            target_res, detect_binary_kind(), idpack, scheme, worker_channel,
            reporter, store2, prior_entries, scopes, clock, slot_info, t_slot,
            bail_effective, bail_scripts, red_pairs,
            baseline_snapshot=precomputed_baseline)
        _map_parity_gate(slot_info, precomputed_baseline)
        _mutant_slot_disclosures(
            slot_info, idpack, slot, baseline_dirty, baseline_failures,
            break_on_baseline_failure, bail_effective, bail_face, kill_bail,
            bail_scripts)
        if skip.dirty:
            slot_info["baseline_skip"] = _baseline_skip_slot_info(
                slot_info, dirty=skip.dirty, skip_applied=skip.skip_applied,
                worker_host=skip.worker_host, bail_face=bail_face,
                bail_scripts=bail_scripts,
                requested=len(baseline_failures))
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


def _baseline_skip_disclosure(*, dirty: bool, skip_applied: bool,
                              worker_host: bool, bail_face: str,
                              requested: int,
                              effective_face: str | None = None,
                              degrade_reason: str | None = None,
                              cold_can_skip: bool = True) -> dict:
    """SEE-1623 M3：baseline_skip 披露自洽（单一出处）。

    计划面 = worker 直驱（宿主可物理跳红测）或冷通道 bail 面；有效执行面
    = 实际跑腿的面（worker 直驱因 parity 失配等降级冷通道后为冷面）。
    `applied` 只在有效执行面确实能物理跳过红测时为真；降级时显式给出
    planned_face / degrade_reason——不得出现「face=worker_direct_drive
    且 applied=true 而实际执行面为 cold」的矛盾（QA 观察 2）。"""
    planned = ("worker_direct_drive" if (dirty and worker_host)
               else bail_face)
    effective = effective_face or planned
    applied = (bool(skip_applied) if effective == planned
               else bool(skip_applied) and bool(cold_can_skip))
    out = {"applied": applied, "face": effective,
           "requested": int(requested)}
    if effective != planned:
        out["planned_face"] = planned
        out["degraded"] = True
        out["degrade_reason"] = degrade_reason or "worker_degraded_cold"
    return out


def baseline_snapshot_from_map(precomputed_baseline: dict | None) -> bool:
    """SEE-1631 §SPEC-020：当前判定基线是否来自 attribution map。"""
    return bool(precomputed_baseline
                and precomputed_baseline.get("source") == "map")


def _worker_degraded_state(slot_info: dict) -> tuple[bool, str | None]:
    """worker 直驱是否已降级冷通道（parity 失配 / 会话致命 / canary 否证）
    → (degraded, reason)；未降级 → (False, None)。"""
    wc = slot_info.get("worker_channel") or {}
    decision = str(wc.get("decision") or "")
    events = slot_info.get("worker_fatal_events") or []
    if events or decision.startswith("worker_degraded"):
        reason = wc.get("reason") or (events[0] if events else None)
        return True, str(reason or "worker_degraded_cold")
    return False, None


def _baseline_skip_slot_info(slot_info: dict, *, dirty: bool,
                              skip_applied: bool, worker_host: bool,
                              bail_face: str, bail_scripts,
                              requested: int) -> dict:
    """SEE-1623 M3：baseline_skip 披露构建（抽函数保复杂度棘轮）。

    worker 直驱降级冷通道后，披露反映**有效执行面**（冷面 + degraded +
    原因），计划面另存 planned_face。"""
    degraded, degrade_reason = _worker_degraded_state(slot_info)
    return _baseline_skip_disclosure(
        dirty=dirty, skip_applied=skip_applied, worker_host=worker_host,
        bail_face=bail_face, requested=requested,
        effective_face=(bail_face if degraded else None),
        degrade_reason=degrade_reason,
        cold_can_skip=bail_scripts is not None)


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
