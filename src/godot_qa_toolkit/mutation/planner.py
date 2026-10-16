"""SEE-1420 — mutation 执行面规划器（P1 差分 / P3 寻址 / P2 覆盖过滤）。

三模式互斥落定（自 runner.py 迁出——<800 行 + 复杂度门纪律）：
- 卡尺寻址（select_ids/select_lines，§SPEC-1420-11）：强制新执行，绕过差分
  与覆盖过滤——公平的重跑通道；
- worker 注入（retry_ids）：父层已规划，只执行注入面，不写 manifest；
- 串行自规划：D4 差分矩阵 → P2 覆盖前置过滤 → D6 budget-on-retry。
"""

from __future__ import annotations

import sys

from .coverlink import load_instrumentable_lines, load_reusable_coverage
from .diffengine import budget_on_retry, plan_diff, untouched_ranges
from .reporting import _dry_run_result, _mutant_record
from .resume2 import ResumeStoreV2

__all__ = ["dry_run_estimate", "plan_execution"]

def dry_run_estimate(file_label, mutants, budget, scoped, total_sites,
                      project_root, tests_glob, binary_kind, resume2_store,
                      mutate_all, header_hash, scope_hashes,
                      attention: str | None = None):
    """SPEC-009 清点模式（零 GUT/零落盘）+ §SPEC-1420-18 差分计数预估
    （manifest 在场时按 D4 矩阵估 retry 面，只读）。"""
    est_store = resume2_store if resume2_store is not None else \
        ResumeStoreV2(project_root, file_label, tests_glob,
                      binary_kind=binary_kind)
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
                    reuse_coverage, attention, mutant_slice, original_src):
    """执行面规划（P1 差分 / P3 寻址 / P2 覆盖过滤三模式互斥落定）。

    返回 (prior_entries, reused, untouched, disclosures, executed_list,
    uncovered_records)。worker 注入路径（retry_ids）不自建 plan、不写
    manifest（父层单点写）；卡尺寻址（select_*）强制新执行绕过差分。
    """
    store2 = resume2_store if resume2_store is not None else ResumeStoreV2(
        project_root, str(path), tests_glob, binary_kind=binary_kind)
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
                        original_src, disclosures)


def _plan_selection(store2, mutants, select_ids, select_lines, disclosures):
    """§SPEC-1420-11（D7 卡尺）：显式寻址 = 公平的重跑通道——不查缓存。"""
    if select_ids is not None:
        wanted = set(select_ids)
        executed_list = [m for m in mutants if m.stable_id in wanted]
        missing = wanted - {m.stable_id for m in executed_list}
    else:
        lines = set(select_lines)
        executed_list = [m for m in mutants if m.line in lines]
        missing = lines - {m.line for m in executed_list}
    if missing:
        raise ValueError(
            "selection matched no mutation sites (stale id after source "
            f"change, or out-of-range line?): {sorted(missing)[:5]}")
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
                 reuse_coverage, mutant_slice, original_src, disclosures):
    """串行自规划：D4 差分矩阵 → P2 覆盖前置过滤 → D6 budget-on-retry。"""
    store2.disclose_v1_superseded()
    old_manifest = store2.load_manifest()
    prior_entries = store2.load_entries()
    plan = plan_diff(header_hash, scope_hashes, old_manifest, mutants,
                     prior_entries, mutate_all=mutate_all)
    uncovered_mutants, retry_pool = _apply_coverage_prefilter(
        plan, path, project_root, tests_glob, binary_kind, reuse_coverage,
        disclosures)
    executed_list, untouched = budget_on_retry(retry_pool, budget)
    reused = plan.reused
    if disclosures.get("coverage_prefilter") not in ("store_miss", "instrumentable_face_missing") \
            and reuse_coverage:
        disclosures["coverage_prefilter"] = {
            "uncovered": len(uncovered_mutants),
            "covered_executed": len(executed_list),
        }
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
                              binary_kind, reuse_coverage, disclosures):
    """§SPEC-1420-12（L3/D7 P2）：coverage 前置过滤（budget 之前）——预算只
    花在 covered 位点上；uncovered 全部显式标记。store miss = 告警不过滤。

    返回 (uncovered, retry_pool)。D3 修复：过滤锚 = covered ∧ instrumentable
    ——落在不可插桩行（成员初始化器/参数默认值/内部类体等探针面外）上的位
    点**留在执行面**（Revy 实机 item.gd 11/60 假阴性实证）；可插桩集读不到
    （旧 schema/缺失）= 不过滤（宁多跑不假阴性）。
    """
    if not reuse_coverage:
        return [], plan.executed
    cov_lines, cov_reason = load_reusable_coverage(
        project_root, str(path), tests_glob, binary_kind)
    if cov_lines is None:
        print(f"mutation: coverage store miss ({cov_reason}) — "
              f"running unfiltered", file=sys.stderr)
        disclosures["coverage_prefilter"] = "store_miss"
        return [], plan.executed
    inst_lines, inst_reason = load_instrumentable_lines(
        project_root, str(path), tests_glob, binary_kind)
    if inst_lines is None:
        print(f"mutation: instrumentable face unavailable ({inst_reason}) — "
              f"running unfiltered", file=sys.stderr)
        disclosures["coverage_prefilter"] = "instrumentable_face_missing"
        return [], plan.executed
    uncovered = [m for m in plan.executed
                 if m.line not in cov_lines and m.line in inst_lines]
    pool = [m for m in plan.executed
            if m.line in cov_lines or m.line not in inst_lines]
    return uncovered, pool


