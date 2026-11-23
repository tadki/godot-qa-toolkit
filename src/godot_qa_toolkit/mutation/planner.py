"""SEE-1420 — mutation 执行面规划器（P1 差分 / P3 寻址 / P2 覆盖过滤）。

三模式互斥落定（自 runner.py 迁出——<800 行 + 复杂度门纪律）：
- 卡尺寻址（select_ids/select_lines，§SPEC-1420-11）：强制新执行，绕过差分
  与覆盖过滤——公平的重跑通道；
- worker 注入（retry_ids）：父层已规划，只执行注入面，不写 manifest；
- 串行自规划：D4 差分矩阵 → P2 覆盖前置过滤 → D6 budget-on-retry。
"""

from __future__ import annotations


from .coverlink import (COVERAGE_PREFILTER_MISS_STATES,
                        MAP_PATH_UNRESOLVED, apply_coverage_prefilter,
                        coverage_prefilter_summary)
from .diffengine import budget_on_retry, plan_diff, untouched_ranges
from .identity import certificate_valid
from .reporting import _dry_run_result, _mutant_record
from .resume2 import ResumeStoreV2
from .stagetiming import StageClock

__all__ = ["dry_run_estimate", "plan_execution"]

def dry_run_estimate(file_label, mutants, budget, scoped, total_sites,
                      project_root, tests_glob, binary_kind, resume2_store,
                      mutate_all, header_hash, scope_hashes,
                      attention: str | None = None,
                      identity: dict | None = None):
    """SPEC-009 清点模式（零 GUT/零落盘）+ §SPEC-1420-18 差分计数预估
    （manifest 在场时按 D4 矩阵估 retry 面，只读）。"""
    est_store = resume2_store if resume2_store is not None else \
        ResumeStoreV2(project_root, file_label, tests_glob,
                      binary_kind=binary_kind, identity=identity)
    old = est_store.load_manifest()
    if old is not None and not mutate_all:
        est_plan = plan_diff(header_hash, scope_hashes, old, mutants,
                             est_store.load_entries())
        retry = len(est_plan.executed)
        estimate = {"diff_available": True, "retry_estimate": retry,
                    "executed_estimate": min(retry, budget)}
    else:
        estimate = {"diff_available": False, "retry_estimate": total_sites,
                    "executed_estimate": min(total_sites, budget)}
    if attention is not None:
        estimate = {**estimate, "attention": attention}
    return _dry_run_result(file_label, mutants, budget, scoped,
                           total_sites=total_sites, estimate=estimate)


def plan_execution(path, project_root, mutants, header_hash, scope_hashes,
                    budget, tests_glob, binary_kind, resume2_store,
                    mutate_all, retry_ids, select_ids, select_lines,
                    reuse_coverage, attention, mutant_slice, original_src,
                    identity: dict | None = None, clock=None,
                    tests_scripts: "list[str] | None" = None,
                    map_path: "str | None | object" = MAP_PATH_UNRESOLVED):
    """执行面规划（P1 差分 / P3 寻址 / P2 覆盖过滤三模式互斥落定）。

    返回 (prior_entries, reused, untouched, disclosures, executed_list,
    uncovered_records)。worker 注入路径（retry_ids）不自建 plan、不写
    manifest（父层单点写）；卡尺寻址（select_*）强制新执行绕过差分。
    tests_scripts = 本次 tests_glob 承载的测试脚本集（投影层需要）；
    map_path = selection 已解析映射路径（A06；None = 不可得则投影层下沉
    不过滤）。
    """
    store2 = resume2_store if resume2_store is not None else ResumeStoreV2(
        project_root, str(path), tests_glob, binary_kind=binary_kind,
        identity=identity)
    disclosures: dict = {}
    if attention is not None:
        disclosures["attention"] = attention
    if select_ids is not None or select_lines is not None:
        return _plan_selection(store2, mutants, select_ids, select_lines,
                               disclosures)
    if retry_ids is not None:
        return _plan_worker(mutants, retry_ids, disclosures)
    return _plan_serial(store2, path, project_root, mutants, header_hash,
                        scope_hashes, budget, tests_glob, binary_kind,
                        mutate_all, reuse_coverage, mutant_slice,
                        original_src, disclosures, identity=identity,
                        clock=clock, tests_scripts=tests_scripts,
                        map_path=map_path)


def selection_preview(mutants, select_ids, select_lines):
    """SEE-1623 M5：卡尺选择的纯判定面（真实执行与 dry-run 预览共用）。

    与 _plan_selection 同一谓词与同一错误文本（单一失败出处）：返回
    (executed_list, disclosures)；零命中/stale id 抛同文 ValueError。
    """
    disclosures: dict = {}
    if select_ids is not None:
        wanted = set(select_ids)
        executed_list = [m for m in mutants if m.stable_id in wanted]
        missing = wanted - {m.stable_id for m in executed_list}
        if missing:
            raise ValueError(
                "selection matched no mutation sites (stale id after source "
                f"change, or out-of-range line?): {sorted(missing)[:5]}")
        return executed_list, disclosures
    lines = {int(l) for l in (select_lines or [])}
    executed_list = [m for m in mutants if m.line in lines]
    hit = sorted({m.line for m in executed_list})
    disclosures["lines_selection"] = {
        "requested": sorted(lines),
        "requested_count": len(lines),
        "hit": hit,
        "hit_count": len(hit),
        "skipped_no_site": sorted(lines - set(hit)),
    }
    if not executed_list:
        raise ValueError(
            "selection matched no mutation sites on the requested lines "
            f"(no-site lines: {sorted(lines)[:10]}"
            f"{'…' if len(lines) > 10 else ''})")
    return executed_list, disclosures


def _plan_selection(store2, mutants, select_ids, select_lines, disclosures):
    """§SPEC-1420-11（D7 卡尺）：显式寻址 = 公平的重跑通道——不查缓存。

    SEE-1587：`--lines` 与请求行求交并披露 `requested / hit /
    skipped_no_site`（请求行中无变异位点的显式列出）；全部无位点才显式
    失败。`--mutation` 单 id 契约不变（无命中 = 显式错误）。"""
    executed_list, sel_disc = selection_preview(mutants, select_ids,
                                                select_lines)
    disclosures.update(sel_disc)
    prior_entries = store2.load_entries()  # streak 连续性（仅读）
    disclosures["executed"] = len(executed_list)
    disclosures["selected"] = True
    return (prior_entries, {}, [], disclosures, executed_list, [])


def _plan_worker(mutants, retry_ids, disclosures):
    by_id = {m.stable_id: m for m in mutants}
    missing = [i for i in retry_ids if i not in by_id]
    if missing:
        raise ValueError(
            f"retry_ids not found in current scan (source drift?): "
            f"{missing[:5]}")
    executed_list = [by_id[i] for i in retry_ids]
    disclosures["executed"] = len(executed_list)
    return ({}, {}, [], disclosures, executed_list, [])


def _plan_serial(store2, path, project_root, mutants, header_hash,
                 scope_hashes, budget, tests_glob, binary_kind, mutate_all,
                 reuse_coverage, mutant_slice, original_src, disclosures,
                 identity: dict | None = None, clock=None,
                 tests_scripts: "list[str] | None" = None,
                 map_path: "str | None | object" = MAP_PATH_UNRESOLVED):
    """串行自规划：D4 差分矩阵（killed 证书门）→ P2 覆盖前置过滤 →
    D6 budget-on-retry。"""
    old_manifest = store2.load_manifest()
    prior_entries = store2.load_entries()
    # killed 证书复用门（SEE-1550 §SPEC-007）：killer 改/删/依赖变 → 仅该
    # entry 重跑；证书缺失 → 保守重跑。校验是定向的（只碰 killer 闭包）。
    plan = plan_diff(header_hash, scope_hashes, old_manifest, mutants,
                     prior_entries, mutate_all=mutate_all,
                     cert_validator=lambda kb: certificate_valid(kb,
                                                                 project_root))
    clock = clock or StageClock()
    with clock.stage("prefilter"):
        uncovered_mutants, retry_pool = _apply_coverage_prefilter(
            plan, path, project_root, tests_glob, binary_kind,
            reuse_coverage, disclosures, identity=identity,
            tests_scripts=tests_scripts, map_path=map_path)
    executed_list, untouched = budget_on_retry(retry_pool, budget)
    reused = plan.reused
    if disclosures.get("coverage_prefilter") not in COVERAGE_PREFILTER_MISS_STATES \
            and reuse_coverage:
        src = disclosures.get("coverage_prefilter")
        # A08：summary.coverage_prefilter 唯一 writer（coverlink）
        disclosures["coverage_prefilter"] = coverage_prefilter_summary(
            src if isinstance(src, str) else None,
            len(uncovered_mutants), len(executed_list))
    if plan.diff_active or mutate_all:
        # O3 对齐：差分在场与 --mutate-all 两径披露同一键集（retry_total /
        # executed / untouched_ranges / diff_skipped）——mutate_all 全量面
        # （executed == retry_total，diff_skipped 空）也必须可见。
        disclosures.update({
            "retry_total": len(plan.executed),
            "executed": len(executed_list),
            "untouched_ranges": untouched_ranges(untouched),
            "diff_skipped": sorted(reused),
        })
    if mutant_slice is not None:
        executed_list = list(executed_list[slice(*mutant_slice)])
    uncovered_records = [
        _mutant_record(m, "uncovered",
                       f"line {m.line} not covered by tests (reused coverage "
                       f"prefilter; site never executed)", src=original_src)
        for m in uncovered_mutants]
    return (prior_entries, reused, untouched, disclosures, executed_list,
            uncovered_records)


def _apply_coverage_prefilter(plan, path, project_root, tests_glob,
                              binary_kind, reuse_coverage, disclosures,
                              identity: dict | None = None,
                              tests_scripts: "list[str] | None" = None,
                              map_path: "str | None | object" = MAP_PATH_UNRESOLVED):
    """§SPEC-1420-12（L3/D7 P2）+ SEE-1550 L1 + SEE-1587 + SEE-1594 S2：
    coverage 前置过滤（budget 之前）——预算只花在 covered 位点上；
    uncovered 显式标记。

    数据源统一经 coverlink.resolve_coverage_for_prefilter：/2 投影 store
    （绑定映射内容与执行证据）→ 行级主真值现场投影（scripts = 本次
    tests_glob 承载的测试脚本集；map_path = selection 已解析映射路径，
    A06）——两层全失才回退不过滤，绝不假过滤。source 进
    disclosures["coverage_prefilter"] 披露。
    D3 锚：不可插桩行（成员初始化器/参数默认值/内部类体等探针面外）上的
    位点**留在执行面**；可插桩集读不到 = 不过滤（宁多跑不假阴性）。
    """
    # A07（SEE-1594 S3）：应用逻辑单一 helper 在 coverlink，本函数只保留
    # 形参适配；D3 锚/miss 披露口径与 executor 路径同源。
    return apply_coverage_prefilter(
        plan.executed, str(path), project_root, tests_glob, tests_scripts,
        binary_kind, identity, reuse_coverage, disclosures, map_path)


