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
    return {
        "tool": "mutation",
        "ok": True,
        "summary": summary,
        "failures": [],
    }


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
                "kill_bail_scripts"):
        if key in info:
            summary[key] = info[key]
    _emit_killed_certificates(summary, results)
    _emit_suspected_orphans(summary, results, info)


def _emit_killed_certificates(summary: dict, results: list[dict]) -> None:
    certs = [{"id": r["id"], "killed_by": r["killed_by"],
              **({"kill_bail": r["kill_bail_info"]}
                 if r.get("kill_bail_info") else {})}
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


def classify_worker_leg(m, leg, baseline_failures, baseline_dirty,
                        ownership_fn, project_root, original_src) -> dict:
    verdict = _classify_worker_leg_impl(m, leg, baseline_failures,
                                        baseline_dirty, ownership_fn,
                                        project_root, original_src)
    # SEE-1606 §SPEC-P3：worker 腿早停信息随 verdict 披露（marker 契约与
    # 冷通道一致——宿主在首个新增失败处中断后续脚本）。
    if getattr(leg, "bail_info", None):
        verdict = {**verdict, "kill_bail_info": dict(leg.bail_info)}
    return verdict


def _classify_worker_leg_impl(m, leg, baseline_failures, baseline_dirty,
                              ownership_fn, project_root, original_src) -> dict:
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
    new_failures = leg.failing_tests - baseline_failures
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
