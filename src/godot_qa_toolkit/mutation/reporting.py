"""SEE-1416 mutation 报告投影层：summary 自足明细 + 记录扩列辅助。

从 runner 拆出以守住单文件 < 800 行纪律（owner-order §6.1）；runner 域内
re-export 保持既有 import / monkeypatch 名称空间稳定。SEE-1550 tidy 再迁入
统一报告构建（_mutation_report）、机制披露（_apply_mechanism_disclosures）
与 verdict 分类（_classify_mutant_run）——被 patch 的调用方留在 runner，
下移函数经 runner 命名空间 by-value 再导出（monkeypatch seam 零变化）。
"""

from __future__ import annotations

import sys

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ops import Mutant

from ..timing import explicit_timing_meta
from .ops import _split_invalid_mutants
from .gutrun import _looks_like_godot_launch_failure, _parse_failing_tests
from .identity import INJECT_SCHEME

# summary.survivors 精简投影键（§SPEC-1416-1）——切片 .summary 也自足：
# 不带 operator/line_text（failures 明细里已有全量）。§SPEC-1420-04 增 id。
SURVIVOR_DETAIL_KEYS = ("file", "line", "column", "kind", "original",
                        "mutated", "verdict", "reason", "id")
SURVIVORS_CAP = 5


def _setup_error_result(file: str, err) -> dict:
    return {
        "tool": "mutation",
        "ok": False,
        "summary": {"file": file, "mutants": 0, "error": str(err)},
        "failures": [{"reason": f"mutation setup failed: {err}"}],
    }


def _no_sites_result(file: str, scoped: bool | None = None,
                     zero_ref: bool = False) -> dict:
    summary = {"file": file, "mutants": 0, "killed": 0, "survived": 0,
               "timeout": 0, "kill_rate": 0.0, "note": "no mutation sites",
               "scoped": bool(scoped)}
    if zero_ref:
        # SEE-1589 A-L2：映射明确判零测试且位点为零——同样是一次短路，
        # 与 `zero_ref_short_circuit_result` 同键，文档/实测口径一致。
        summary["zero_ref_short_circuit"] = True
        summary["short_circuit_reason"] = "no_mutation_sites"
    return {"tool": "mutation", "ok": True, "summary": summary,
            "failures": []}


def _route_drift_result(file: str, err) -> dict:
    """SEE-1606 §SPEC-P1：route 漂移具名中止契约（fail-loud，零 baseline）。

    summary.route_drift=True + 权威/实跑 scheme 双披露——下游重解析不得
    静默覆盖 file_key 面，也不得以漂移后的通道继续执行。"""
    return {
        "tool": "mutation",
        "ok": False,
        "summary": {
            "file": file, "run_error": True, "route_drift": True,
            "error": str(err),
            "authoritative_scheme": getattr(err, "expected", None),
            "resolved_scheme": getattr(err, "actual", None),
            "drift_site": getattr(err, "where", None)},
        "failures": [{"reason": str(err)}],
    }


def _run_error_result(file: str, error: str, failure_reason: str,
                      hint: str | None = None,
                      action_kind: str | None = None) -> dict:
    """统一 run_error 中止契约（contract.md §4.3 中止形态）。

    hint（SEE-1356 L1）：baseline 超时中止附 suggested_action，机器可执行
    的补救指令直接可读。
    """
    summary = {"file": file, "run_error": True, "error": error}
    if hint:
        summary["suggested_action"] = hint
    if action_kind:
        summary["suggested_action_kind"] = action_kind
    return {
        "tool": "mutation",
        "ok": False,
        "summary": summary,
        "failures": [{"reason": failure_reason}],
    }


def _baseline_skip_unsupported_result(file: str, names: list[str],
                                      face_kind: str) -> dict:
    """§SPEC-P2-R1：基线脏 ∧ 冷通道面无法物理跳过 → 具名拒绝契约。

    结构性限制的最小可执行方案：不退回「红测试跑了但不算」，改指可跳过
    的面（默认 line/fn 的物化选择目录、卡尺）或显式开关。"""
    failures = sorted(names)
    return {
        "tool": "mutation",
        "ok": False,
        "summary": {
            "file": file,
            "run_error": True,
            "error": (f"baseline has {len(failures)} failing test(s) and "
                      f"this face ({face_kind}) cannot physically skip "
                      "them on the cold legacy runner"),
            "baseline_skip_unsupported": True,
            "baseline_dirty": True,
            "baseline_failures": len(failures),
            "baseline_failure_names": failures,
            "suggested_action": ("rerun with the default line/fn selection "
                                 "faces, a --mutation/--lines caliper, or "
                                 "set --break-on-baseline-failure"),
        },
        "failures": [{
            "reason": ("cold legacy runner cannot physically skip baseline "
                       f"red tests on face={face_kind}: "
                       f"{failures[:3]}")}],
    }


def _break_on_baseline_result(file: str, names: list[str]) -> dict:
    """SEE-1606 P2：`--break-on-baseline-failure` 的基线脏中止契约。

    与 baseline 超时/启动失败同档（run_error 中止形态），但带具名基线
    失败清单供机器消费；此路径零 mutant GUT。
    """
    failures = sorted(names)
    return {
        "tool": "mutation",
        "ok": False,
        "summary": {
            "file": file,
            "run_error": True,
            "error": (f"baseline has {len(failures)} failing test(s) and "
                      "--break-on-baseline-failure is set"),
            "break_on_baseline_failure": True,
            "baseline_dirty": True,
            "baseline_failures": len(failures),
            "baseline_failure_names": failures,
        },
        "failures": [{
            "reason": ("baseline failures present with "
                       f"--break-on-baseline-failure: {failures[:3]}")}],
    }


def _dry_run_result(file: str, mutants: list["Mutant"], budget: int,
                    scoped: bool | None = None,
                    total_sites: int | None = None,
                    estimate: dict | None = None) -> dict:
    """SPEC-009：清点模式——零 GUT 调用，mutant 清单即产出。

    §SPEC-1420-01：budget 截断时同步披露 total_sites（截断前全量位点数）。
    §SPEC-1420-18：estimate = 差分计数预估（retry_estimate/executed_estimate/
    diff_available）。
    """
    kinds: dict[str, int] = {}
    for m in mutants[:budget]:
        kinds[m.kind] = kinds.get(m.kind, 0) + 1
    summary = {"file": file, "mutants": len(mutants[:budget]),
               "dry_run": True, "kinds": kinds, "scoped": bool(scoped)}
    if total_sites is not None and total_sites > budget:
        summary["total_sites"] = total_sites
    if estimate:
        summary.update(estimate)
    # SEE-1610 §SPEC-PLAN Phase 0：dry-run 同构计划（零 GUT/零续新已由
    # 本路径构造保证；budget 参数不在 summary，经 extra 进组）。
    summary["execution_plan"] = _execution_plan(summary, extra={"budget":
                                                                budget})
    return {
        "tool": "mutation",
        "ok": True,
        "summary": summary,
        "failures": [],
    }


def _execution_plan(summary: dict, extra: "dict | None" = None) -> dict:
    """SEE-1610 §SPEC-PLAN Phase 0：只读执行计划聚合。

    把报告内**已算出**的值按 selection/route/budget/diff/preconditions
    五组重排为 Agent 可读计划——纯读 dict 键过滤（键按固定序收集，缺席
    不造），零副作用、零 GUT/import/map 触达（V1）；同输入同 JSON（V3）。
    extra：调用面想在 plan 里可见但未进 summary 的参数（如 dry-run 的
    budget）。"""
    src = dict(summary)
    if extra:
        src.update(extra)
    groups = {
        "selection": ("selection_mode", "scoped", "test_selection",
                      "lines_selection", "selection_disclosure"),
        "route": ("exec_scheme", "inject_scheme", "slot_mode", "freshness",
                  "worker_channel"),
        "budget": ("budget", "adaptive_timeout_s", "timing_source"),
        "diff": ("baseline_reused", "resumed", "diff_available",
                 "retry_estimate", "executed_estimate"),
        "preconditions": ("baseline_dirty", "baseline_failures",
                          "baseline_failure_names", "baseline_skip",
                          "kill_bail", "kill_bail_face", "kill_bail_scripts",
                          "test_filter", "timeout_streak_suspended"),
    }
    return {group: {k: src[k] for k in keys if k in src}
            for group, keys in groups.items()}


# SEE-1606 §SPEC-P2：字段分区——derived 仅由静态输入决定（plan_id 唯一
# 来源）；observed = 映射/环境实测事实；runtime = 执行期状态。五组内每个
# 已知字段都必须在此分类（validate_plan 对未分类字段 lint）。
_PLAN_FIELD_PARTITION = {
    # derived
    "selection_mode": "derived", "scoped": "derived",
    "lines_selection": "derived", "exec_scheme": "derived",
    "inject_scheme": "derived", "budget": "derived",
    "kill_bail": "derived", "kill_bail_face": "derived",
    "test_filter": "derived",
    # observed
    "test_selection": "observed", "selection_disclosure": "observed",
    "baseline_reused": "observed", "resumed": "observed",
    "diff_available": "observed", "retry_estimate": "observed",
    "executed_estimate": "observed", "baseline_dirty": "observed",
    "baseline_failures": "observed", "baseline_failure_names": "observed",
    "baseline_skip": "observed",
    # runtime
    "slot_mode": "runtime", "freshness": "runtime",
    "worker_channel": "runtime", "adaptive_timeout_s": "runtime",
    "timing_source": "runtime", "kill_bail_scripts": "runtime",
    "timeout_streak_suspended": "runtime",
}
_PLAN_FAILURE_PATHS = (
    "route_drift", "selection_map_unusable", "no_mapped_tests",
    "zero_ref_short_circuit", "baseline_timeout", "baseline_launch_failed",
    "baseline_skip_unsupported_face", "slot_provision_failed",
    "no_mutation_sites")
_MAP_RENEW_COST = "auto map-update (full-chain GUT renewal)"
_PROOF_COST = "godot --headless --import (single warm-up)"


def _plan_partitions(plan: dict) -> "tuple[dict, list[str]]":
    """五组计划 → (三分区视图, 未分类字段名单)（纯读重排）。"""
    out: dict[str, dict] = {"derived": {}, "observed": {}, "runtime": {}}
    unclassified: list[str] = []
    for group, fields in plan.items():
        for k, v in fields.items():
            part = _PLAN_FIELD_PARTITION.get(k)
            if part is not None:
                out[part].setdefault(group, {})[k] = v
            else:
                unclassified.append(f"{group}.{k}")
    return out, unclassified


def _plan_id(derived_view: dict) -> str:
    """plan_id：仅 hash derived 分区（observed/runtime 不参与）。"""
    import hashlib
    import json as _json
    payload = _json.dumps(derived_view, sort_keys=True,
                          ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _map_state_block(map_state: "dict | None",
                     import_proof_present: "bool | None") -> dict:
    """缺/旧 map 与证明缺失 → would_renew + 具名缺失 + 成本（只读披露）。"""
    missing: list[str] = []
    costs: list[str] = []
    would_renew = None
    if map_state is not None:
        would_renew = (not map_state.get("present")) \
            or (map_state.get("fresh") is False)
        if not map_state.get("present"):
            missing.append("attribution_map")
        elif map_state.get("fresh") is False:
            missing.append("attribution_map_freshness")
        if would_renew:
            costs.append(_MAP_RENEW_COST)
    if import_proof_present is False:
        missing.append("prewarm_proof")
        costs.append(_PROOF_COST)
    return {"would_renew": would_renew, "missing": missing, "cost": costs,
            "reason": (map_state or {}).get("reason")}


def _plan_reasons(plan: dict, src: dict) -> list:
    """理由回答：route 披露优先，选面披露补充（都缺席则空列表）。"""
    route = plan.get("route") or {}
    reasons: list = list((route.get("worker_channel") or {})
                         .get("route_reasons") or [])
    sel = src.get("test_selection") or src.get("selection_disclosure") or {}
    if sel.get("reason"):
        reasons.append(sel["reason"])
    return reasons


def _plan_meta(plan: dict, src: dict, extra: "dict | None" = None,
               map_state: "dict | None" = None,
               import_proof_present: "bool | None" = None) -> dict:
    """SEE-1606 §SPEC-P2：dry-run 计划预览元数据（只增键不改既有键）。

    - partitions：字段按 derived/observed/runtime 分区；
    - plan_id：仅 hash derived（同 derived 输入逐字节稳定，observed 变化
      不动 id）；
    - map_state：缺/旧 map 仅 would_renew+具名缺失+成本披露（探测只读，
      零续新）；
    - answers：通道/理由/失败路径/复用面四类机器问题；
    - assumptions：计划假设与 handler（handler=None 供 validate_plan
      lint——不作运行门）。"""
    partitions, unclassified = _plan_partitions(plan)
    if extra:
        for k, v in extra.items():
            part = _PLAN_FIELD_PARTITION.get(k)
            if part is not None:
                partitions[part].setdefault("budget", {})[k] = v
            else:
                unclassified.append(f"budget.{k}")
    block = _map_state_block(map_state, import_proof_present)
    route = plan.get("route") or {}
    return {
        "plan_id": _plan_id(partitions["derived"]),
        "partitions": partitions,
        "unclassified_fields": sorted(set(unclassified)),
        "map_state": block,
        "answers": {
            "channel": route.get("exec_scheme"),
            "reasons": _plan_reasons(plan, src),
            "failure_paths": list(_PLAN_FAILURE_PATHS),
            "reuse_surface": dict(plan.get("diff") or {}),
        },
        "assumptions": [
            {"name": "route_stable", "handler": "route_drift_gate"},
            {"name": "selection_usable",
             "handler": None if block["would_renew"]
             else "selection_disclosure"},
        ],
    }


def validate_plan(meta: dict) -> list[str]:
    """SEE-1606 §SPEC-P2：计划静态完整性 lint（仅供 Agent/测试消费）。

    无 handler assumption / 未分类字段 → 具名问题；**不作运行门**——
    运行路径不调用本函数，lint 失败不改变任何结果契约。"""
    problems: list[str] = []
    for a in meta.get("assumptions") or []:
        if not a.get("handler"):
            problems.append(f"assumption_without_handler:{a.get('name')}")
    for f in meta.get("unclassified_fields") or []:
        problems.append(f"unclassified_field:{f}")
    return problems


def line_text_of(src: str, line: int) -> str:
    """变异行原文；行号越界返回空串（不吞错误——记录仍可判读）。"""
    lines = src.splitlines()
    return lines[line - 1] if 0 < line <= len(lines) else ""


def survivors_projection(failures: list[dict]) -> tuple[list[dict], bool]:
    """非 killed 明细的 top ≤5 精简投影；返回 (survivors, truncated)。"""
    survivors = [{k: f[k] for k in SURVIVOR_DETAIL_KEYS}
                 for f in failures[:SURVIVORS_CAP]]
    return survivors, len(failures) > SURVIVORS_CAP


def _mutant_record(m: "Mutant", verdict: str, reason: str,
                   src: str | None = None) -> dict:
    """统一 mutant 明细记录形态（契约 §4.3 failures 项）。

    SEE-1416：扩列 column/operator/line_text——Mutant 的定位面全量透出，
    agent 不必打开源文件对照。所有 verdict（含 killed / 静态预检 invalid）
    同构升级；resume 复用重建走同一函数，保证新旧记录形态逐字段一致。
    §SPEC-1420-04：增 id 列（D3 寻址提示；未标定 scope 的 mutant 为空串）。
    """
    record = {"file": m.file, "line": m.line, "column": m.column,
              "kind": m.kind, "operator": m.operator,
              "original": m.original, "mutated": m.mutated,
              "verdict": verdict, "reason": reason,
              "id": getattr(m, "stable_id", "") or ""}
    if src is None:
        return record
    return {**record, "line_text": line_text_of(src, m.line)}


def _valid_mutants(src: str, mutants: list) -> list:
    return _split_invalid_mutants(src, mutants)[0]


def _record_invalid_mutants(src: str, mutants: list,
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
                                      f"{m.kind} {m.original}→{m.mutated}",
                                      src=src))
    return results, len(invalid)


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
    bare = _parse_failing_tests(tail)
    # §SPEC-P2-R1-D2：同名脚本限定豁免（runner 每轮写入的脚本限定红集合；
    # 空 = 无脚本标题 → 回退裸名差集语义）。
    lazy = None
    try:
        from . import runner as _runner_mod
        lazy = getattr(_runner_mod, "_BASELINE_RED_MAP", None)
    except Exception:
        lazy = None
    red_map = lazy if lazy else None
    if red_map:
        from .gutrun import parse_failures_with_scripts
        qualified = parse_failures_with_scripts(tail)
        if qualified:
            # D2-R1：Run Summary 段（脚本上下文丢失，记作 ("*", n)）是运行
            # 流同名失败的重复回显——不计入新失败，否则同名基线红被误判为
            # mutant 新失败（QA 假 kill 根因）。
            new_names = {n for _s, n in qualified
                         if _s != "*" and (_s, n) not in red_map}
        else:
            new_names = bare - baseline_failures
    else:
        new_names = bare - baseline_failures
    new_failures = new_names
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


def _report_base_summary(file: str, killed: int, survived: int,
                         timeout_count: int, run_error_count: int,
                         suspect_count: int, invalid_count: int, budget: int,
                         adaptive_timeout_s: int, scoped: bool,
                         baseline_reused: bool) -> tuple:
    """基础 summary 组装（total 含 invalid_count 参数项——原口径）。"""
    total = killed + survived + timeout_count + run_error_count \
        + invalid_count + suspect_count
    summary = {
        "file": file,
        "mutants": total,
        "killed": killed,
        "survived": survived,
        "suspect": suspect_count,
        "timeout": timeout_count,
        "run_errors": run_error_count,
        "invalid_mutants": 0,
        "kill_rate": round(killed / total, 4) if total else 0.0,
        "budget": budget,
        "adaptive_timeout_s": adaptive_timeout_s,
        "scoped": scoped,
        "baseline_reused": baseline_reused,
    }
    return summary, total


def _report_conditional_keys(summary: dict, stage_timings, resumed, pinned_count,
                             disclosures, timing_meta) -> None:
    """条件披露键（§SPEC-202 / SEE-1400 / §SPEC-1420-08/01/07 / SEE-1356）。"""
    if stage_timings is not None:
        summary["stage_timings"] = stage_timings
    if resumed is not None:
        summary["resumed"] = resumed
    if pinned_count:
        summary["timeout_pinned"] = pinned_count
    if disclosures:
        summary.update(disclosures)
    summary.update(timing_meta or explicit_timing_meta())


def _mutation_report(file: str, killed: int, survived: int, timeout_count: int,
                     run_error_count: int, suspect_count: int, invalid_count: int,
                     budget: int, results: list[dict], scoped: bool = False,
                     baseline_reused: bool = False,
                     timing_meta: dict | None = None,
                     resumed: int | None = None,
                     disclosures: dict | None = None,
                     pinned_count: int = 0,
                     slot_info: dict | None = None,
                     original_src: str | None = None,
                     adaptive_timeout_s: int = 0,
                     stage_timings: dict | None = None) -> dict:
    """统一 JSON 报告（单文件路径唯一出处；ok 门 = 无未证明杀死档）。"""
    summary, _total = _report_base_summary(
        file, killed, survived, timeout_count, run_error_count, suspect_count,
        invalid_count, budget, adaptive_timeout_s, scoped, baseline_reused)
    failures = [r for r in results if r["verdict"] != "killed"]
    # SPEC-017：runtime invalid 与静态预检 invalid 合并计数——param 传入值
    # 与扫描会双计数，改为扫描唯一口径。
    summary["invalid_mutants"] = sum(
        1 for r in results if r["verdict"] == "invalid_mutant")
    _apply_mechanism_disclosures(summary, results, slot_info, original_src)
    # §SPEC-202 只加不改：完整报告路径恒携带 8 阶段全键（缺省阶段 0.0）；
    # dry-run / 零位点 / setup 错误契约不经本函数，形态零变化。
    _report_conditional_keys(summary, stage_timings, resumed, pinned_count,
                             disclosures, timing_meta)
    # SEE-1416 §SPEC-1416-1：summary 自足——非 killed verdict 的 top 明细
    # 投影进场（键恒在场，`| jq '.summary'` 切片也不失明；零命中为空数组）。
    summary["survivors"], truncated = survivors_projection(failures)
    if truncated:
        summary["survivors_truncated"] = True
    # SEE-1610 §SPEC-PLAN Phase 0：只读执行计划聚合（本 dict 全键纯读重排，
    # 零副作用零触达；既有键零变化，只新增 execution_plan）。
    summary["execution_plan"] = _execution_plan(summary)
    return {
        "tool": "mutation",
        # suspect = 「无法证明被杀死」——语义上与 survived 同挡 gate ok
        # （硬化 反例实证：suspect>0 时 ok=True 是自欺）。
        "ok": survived == 0 and timeout_count == 0 and run_error_count == 0
              and suspect_count == 0,
        "summary": summary,
        "failures": failures,
    }

def _apply_mechanism_disclosures(summary: dict, results: list[dict],
                                 slot_info: dict | None,
                                 original_src: str | None) -> None:
    """SEE-1550 stdout 只加不改披露面：机制标签、槽位/新鲜度、killed
    证书、疑似孤儿诊断（unreachable 不进 verdict——只披露）。"""
    info = slot_info or {}
    summary["inject_scheme"] = info.get("inject_scheme", INJECT_SCHEME)
    if info.get("exec_scheme"):
        summary["exec_scheme"] = info["exec_scheme"]
    if info.get("slot_mode"):
        summary["slot_mode"] = info["slot_mode"]
    if info.get("freshness"):
        summary["freshness"] = info["freshness"]
    # SEE-1589 B §SPEC-002：机器可读具名 reason（禁止无解释静默冷通道/
    # 静默 farm 重建）。freshness 字符串列表保留为人类可读兼容面。
    if info.get("worker_channel"):
        summary["worker_channel"] = info["worker_channel"]
    if info.get("farm"):
        summary["farm"] = info["farm"]
    if info.get("worker_fatal_events"):
        summary["worker_fatal_events"] = info["worker_fatal_events"]
    # SEE-1606 P2：基线失败登记披露（默认继续跑时的机器可读面）。
    for key in ("baseline_dirty", "baseline_failures",
                "baseline_failure_names", "break_on_baseline_failure",
                "kill_bail", "kill_bail_face", "kill_bail_disabled_by",
                "kill_bail_scripts", "baseline_skip",
                "test_filter"):
        if key in info:
            summary[key] = info[key]
    _emit_killed_certificates(summary, results)
    _emit_suspected_orphans(summary, results, info)
    _emit_test_face_aggregate(summary, results)


def _emit_test_face_aggregate(summary: dict, results: list[dict]) -> None:
    """SEE-1610 D2：test_face 全 verdict 档机器聚合（与执行计数一致）。

    对账口径：kept+removed 只累加 marker 在场腿；marker_missing 腿单列
    （宿主未及打点，如 timeout/注入失败），by_verdict 给各档腿数。"""
    legs = [r for r in results if r.get("test_face")]
    if not legs:
        return
    agg = {"applied_legs": len(legs), "kept": 0, "removed": 0,
           "whole_script_fallback": 0, "marker_missing_legs": 0,
           "overlap_red": 0, "by_verdict": {}}
    for r in legs:
        tf = r["test_face"]
        if tf.get("marker_missing"):
            agg["marker_missing_legs"] += 1
        else:
            agg["kept"] += int(tf.get("kept") or 0)
            agg["removed"] += int(tf.get("removed") or 0)
            agg["whole_script_fallback"] += int(
                tf.get("whole_script_fallback") or 0)
            agg["overlap_red"] += int(tf.get("overlap_red") or 0)
        v = r.get("verdict", "")
        agg["by_verdict"][v] = agg["by_verdict"].get(v, 0) + 1
    summary["test_face"] = agg


def _emit_killed_certificates(summary: dict, results: list[dict]) -> None:
    certs = [{"id": r["id"], "killed_by": r["killed_by"],
              **({"kill_bail": r["kill_bail_info"]}
                 if r.get("kill_bail_info") else {}),
              **({"test_face": r["test_face"]}
                 if r.get("test_face") else {})}
             for r in results
             if r.get("verdict") == "killed" and r.get("killed_by")]
    if certs:
        summary["killed_by_certificates"] = certs


def _emit_suspected_orphans(summary: dict, results: list[dict],
                            info: dict) -> None:
    if info.get("suspected_orphan") and any(
            r.get("verdict") == "survived" for r in results):
        for r in results:
            if r.get("verdict") == "survived":
                r["suspected_orphan"] = True
        summary["suspected_orphans"] = [r["id"] for r in results
                                        if r.get("verdict") == "survived"]


def _leg_killed_record(m, new_failures, ownership_fn, project_root,
                       original_src) -> dict:
    """killed 记录 + killer 证书签发（与冷通道相同签发面）。"""
    from .identity import killer_certificate

    record = _mutant_record(
        m, "killed",
        f"new failures after {m.kind} {m.original}→{m.mutated}: "
        f"{sorted(new_failures)[:3]}", src=original_src)
    if ownership_fn is not None:
        owner = ownership_fn()
        if owner is not None:
            cert = killer_certificate(
                new_failures, owner, project_root,
                ownership_source=getattr(owner, "source",
                                         "runtime_mapping"))
            if cert is not None:
                record["killed_by"] = cert
    return record


def _script_aware_new(failing_names, failing_scripts, baseline_names,
                      baseline_red_map) -> set:
    """§SPEC-P2-R1-D2：裸名差集的脚本限定精化。

    - baseline_red_map 非 None：逐 (script, name) 精确豁免；同名脚本不在
      基线红集合 → 该失败是真新失败（绿脚本同名 killer 照杀）。
    - map 为 None（旧路径/无脚本标题）或腿无脚本标题 → 回退裸名差集。"""
    if baseline_red_map is None or not failing_scripts:
        return failing_names - baseline_names
    base_map = baseline_red_map or set()
    new = set()
    for name in failing_names:
        # D2-R1：通配 ("*", name) 条目是 Run Summary 的重复回显，不授予
        # 豁免——只有「脚本归属 + 名」精确命中基线红的失败才是既有失败；
        # 其它（含同名绿脚本 killer 的失败）都是真新失败 → killed。
        covered = any((scr, name) in base_map
                      for scr, n in failing_scripts
                      if n == name and scr != "*")
        if not covered:
            new.add(name)
    return new


def classify_worker_leg(m, leg, baseline_failures, baseline_dirty,
                        ownership_fn, project_root, original_src,
                        baseline_red_map=None) -> dict:
    verdict = _classify_worker_leg_impl(m, leg, baseline_failures,
                                        baseline_dirty, ownership_fn,
                                        project_root, original_src,
                                        baseline_red_map=baseline_red_map)
    # SEE-1606 §SPEC-P3：worker 腿早停信息随 verdict 披露（marker 契约与
    # 冷通道一致——宿主在首个新增失败处中断后续脚本）。
    if getattr(leg, "bail_info", None):
        verdict = {**verdict, "kill_bail_info": dict(leg.bail_info)}
    # SEE-1610 §SPEC-JSON：worker 腿 test_face 披露（与冷通道同键）。
    info = getattr(leg, "allow_info", None)
    if info:
        verdict = {**verdict, "test_face": {
            "requested": int(info.get("requested") or 0),
            "kept": int(info.get("kept") or 0),
            "removed": int(info.get("removed") or 0),
            "whole_script_fallback": int(info.get("whole") or 0),
            "overlap_red": int(info.get("overlap_red") or 0)}}
    else:
        from godot_qa_toolkit.mutation import runner as _r
        if getattr(_r, "_ACTIVE_ALLOW_TESTS", None):
            verdict = {**verdict, "test_face": {
                "requested": None, "kept": None, "removed": None,
                "whole_script_fallback": None, "marker_missing": True}}
    return verdict


def _classify_worker_leg_impl(m, leg, baseline_failures, baseline_dirty,
                              ownership_fn, project_root, original_src,
                              baseline_red_map=None) -> dict:
    """worker 腿 → verdict（与冷通道同一 kill 语义：失败名差集 + 同档分类）。

    worker 腿的 L1/L4 beacon 失败 → run_error（数据不可信，绝不 killed/
    survived）；killed 走与冷通道相同的 killer 证书签发面。D-DVP：目标
    编译失败 → invalid_mutant（与冷通道 beacon-invalid 同档同语义）。"""
    src = original_src
    tag = f"{m.kind} {m.original}→{m.mutated}"
    if getattr(leg, "target_compile_invalid", False):
        return _mutant_record(
            m, "invalid_mutant",
            "mutant fails to compile in the resident worker "
            "(target reload non-zero, can_instantiate=false) — "
            "cold-beacon-equivalent invalid", src=src)
    if not leg.hash_ok:
        return _mutant_record(
            m, "run_error", f"worker beacon: disk hash mismatch for {tag}",
            src=src)
    if not leg.gut_ran:
        return _mutant_record(
            m, "run_error", f"worker leg: no GUT run stream for {tag}",
            src=src)
    if leg.fail_count == 0:
        return _mutant_record(
            m, "survived", f"tests passed after {tag} (worker leg)", src=src)
    new_failures = _script_aware_new(leg.failing_tests,
                                     leg.failing_scripts,
                                     baseline_failures,
                                     baseline_red_map)
    if new_failures:
        return _leg_killed_record(m, new_failures, ownership_fn,
                                  project_root, src)
    if baseline_dirty:
        return _mutant_record(
            m, "suspect",
            f"dirty baseline ∧ empty diff after {tag} (worker leg)", src=src)
    return _mutant_record(
        m, "survived",
        f"only pre-existing failures after {tag} (worker leg)", src=src)

def _require_baseline_snapshot(mutant_slice, retry_ids,
                               precomputed_baseline) -> None:
    """worker 注入契约：分片/重试面必须携带共享 baseline 快照（各自跑
    baseline 会 N 倍放大启动税且污染 timeout 锚）。"""
    if ((mutant_slice is not None or retry_ids is not None)
            and precomputed_baseline is None):
        raise ValueError(
            "slice/retry_ids workers require a precomputed baseline snapshot — "
            "each worker running its own baseline would multiply the startup "
            f"tax N-fold and pollute the adaptive timeout anchor: {mutant_slice}")


def _attention_note(mutation_warning: int | None, total_sites: int,
                    file_label: str) -> str | None:
    """§SPEC-1420-01：按截断前全量位点数的告警（stderr + summary.attention）。"""
    if mutation_warning is None or total_sites <= mutation_warning:
        return None
    note = (f"{total_sites} mutation sites exceed "
            f"--mutation-warning {mutation_warning} — consider "
            f"splitting this file")
    print(f"mutation-warning: {note} in {file_label}", file=sys.stderr)
    return note
