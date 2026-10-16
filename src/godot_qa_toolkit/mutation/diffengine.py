"""SEE-1420 Phase 1 — 差分引擎：跳过规则矩阵（D4）+ timeout_pinned（D5）
+ budget-on-retry（D6）。

跳过规则（D4，照抄 clj sites-to-retry，契约化于 contract.md §4.3）：
- 未改 scope 且上次 killed → 跳过（复用）
- 未改 scope 且上次 survived → **永远重跑**（survived 永不缓存信任——红线）
- 未改 scope 且上次 timeout / timeout_pinned / run_error → 重跑
  （timeout 重跑是 streak 递增的唯一通道；pinned 重跑防解除路径失效变永久
  毒条目；run_error 属基础设施存疑，宁跑不脏）
- 未改 scope 且上次 suspect / invalid_mutant → 复用（重跑无信息增益；
  suspect 复用沿 v1 --resume 既有语义）
- scope hash 变更（或新 scope）→ 该 scope 全跑；header hash 变更 → 全文件全跑
- module 速断：全部 hash 同且无 retry 档位点 → 零 mutant 执行（baseline 仍
  实测——判定语义锚，§四.6 B 场景 GUT=1 baseline）

id 语义（D3）：id 仅寻址提示。复用前必须过两道校验，任一失败 = 告警 +
拒绝复用 + 降级重跑，绝不静默按 id 取条目：
1. 全签名五元组 (line, column, kind, original, mutated) 与当前 mutant 一致；
2. 条目 scope_hash 与当前 scope hash 一致（防"同签名异语义"的域内漂移）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

# 复用档 / 重跑档——矩阵的唯一出处（tests/unit/test_diff_matrix_see1420.py 逐格断言）。
SKIP_VERDICTS = frozenset({"killed", "suspect", "invalid_mutant"})
RETRY_VERDICTS = frozenset({"survived", "timeout", "timeout_pinned", "run_error"})

# D5：streak ≥ N 转 timeout_pinned；毒 run 守卫阈值（单 run timeout 占比
# ≥50% 且 ≥5 条 → 本 run 不落 streak）。
D5_TIMEOUT_PINNED_N = 2
D5_POISON_MIN_COUNT = 5
D5_POISON_RATIO = 0.5

FULL_SIGNATURE_KEYS = ("line", "column", "kind", "original", "mutated")


@dataclass
class DiffPlan:
    executed: list = field(default_factory=list)   # 待执行 mutants（扫描序）
    reused: dict = field(default_factory=dict)     # stable_id → prior entry
    triggers: dict = field(default_factory=dict)   # stable_id → 判定理由
    header_changed: bool = False
    diff_active: bool = False                      # 旧 manifest 在场 ∧ 未 --mutate-all


def signature_matches(entry: dict, mutant) -> bool:
    return all(entry.get(k) == getattr(mutant, k) for k in FULL_SIGNATURE_KEYS)


def plan_diff(header_hash: str, scopes: dict, old_manifest: dict | None,
              mutants: list, entries: dict, mutate_all: bool = False) -> DiffPlan:
    """D4 矩阵：逐位点判 skip/retry。mutants 须已标 stable_id/scope_id。"""
    if mutate_all or old_manifest is None:
        plan = DiffPlan(executed=list(mutants), diff_active=False)
        reason = "mutate_all" if mutate_all else "no_prior_manifest"
        for m in mutants:
            plan.triggers[m.stable_id] = reason
        return plan

    plan = DiffPlan(diff_active=True)
    plan.header_changed = old_manifest.get("header_hash") != header_hash
    old_scopes = old_manifest.get("scopes") or {}
    for m in mutants:
        sid = m.stable_id
        if plan.header_changed:
            plan.executed.append(m)
            plan.triggers[sid] = "header_changed"
            continue
        if old_scopes.get(m.scope_id) != scopes.get(m.scope_id):
            plan.executed.append(m)
            plan.triggers[sid] = ("scope_new" if m.scope_id not in old_scopes
                                  else "scope_changed")
            continue
        prior = entries.get(sid)
        if prior is None:
            plan.executed.append(m)
            plan.triggers[sid] = "new_site"
            continue
        if not signature_matches(prior, m):
            print(f"diff: id {sid} full-signature drift "
                  f"(stored {prior.get('line')}:{prior.get('column')} "
                  f"{prior.get('kind')} vs current {m.line}:{m.column} "
                  f"{m.kind}) — refusing to reuse by id, degrading to rerun",
                  file=sys.stderr)
            plan.executed.append(m)
            plan.triggers[sid] = "signature_drift"
            continue
        if prior.get("scope_hash") != scopes.get(m.scope_id):
            plan.executed.append(m)
            plan.triggers[sid] = "scope_hash_drift"
            continue
        verdict = prior.get("verdict")
        if verdict in SKIP_VERDICTS:
            plan.reused[sid] = prior
            plan.triggers[sid] = f"reused:{verdict}"
        elif verdict in RETRY_VERDICTS:
            plan.executed.append(m)
            plan.triggers[sid] = f"retry:{verdict}"
        else:
            # 未知 verdict（旧 schema 残留）——按重跑处理，绝不复用
            plan.executed.append(m)
            plan.triggers[sid] = f"retry:unknown_verdict:{verdict}"
    return plan


def budget_on_retry(executed: list, budget: int) -> tuple[list, list]:
    """D6：budget 按差分后 retry 数计；截断保序（拒轮转——seq 寻址与差分
    对照的固定基准不可破坏）。返回 (final_executed, untouched)。"""
    if budget <= 0:
        # 负/零预算 = 零执行面（与 v1 mutants[:budget<=0] 的 no_sites 守卫同向）
        return [], list(executed)
    if len(executed) <= budget:
        return executed, []
    return executed[:budget], executed[budget:]


def untouched_ranges(untouched: list) -> list[list[int]]:
    """未触达位点的行号区间（连续行合并）——D6 强制披露的 untouched 面。"""
    if not untouched:
        return []
    lines = sorted(m.line for m in untouched)
    ranges: list[list[int]] = [[lines[0], lines[0]]]
    for ln in lines[1:]:
        if ln <= ranges[-1][1] + 1:
            ranges[-1][1] = ln
        else:
            ranges.append([ln, ln])
    return ranges


def prior_timeout_streak(prior: dict | None) -> int:
    """D5：只有 timeout/timeout_pinned 链条上的 streak 延续；其余归零。"""
    if prior and prior.get("verdict") in ("timeout", "timeout_pinned"):
        return int(prior.get("timeout_streak") or 0)
    return 0


def streak_for(prior: dict | None, verdict: str,
               poison: bool = False) -> tuple[int, str]:
    """单位点 streak 判定（循环内乐观落盘与窗口收尾修正共用一套公式）。

    返回 (streak, verdict_out)：timeout 非毒窗口 streak+1，≥N 转
    timeout_pinned；非超时一律解除清零；毒窗口 timeout 保留原 streak。
    """
    base = prior_timeout_streak(prior)
    if verdict != "timeout":
        return 0, verdict
    streak = base if poison else base + 1
    return streak, ("timeout_pinned" if streak >= D5_TIMEOUT_PINNED_N
                    else "timeout")


def make_entry(m, record: dict, verdict: str, streak: int,
               scopes: dict, now: float) -> dict:
    """v2 per-mutant 条目（全签名五元组 + scope 锚 + streak）。"""
    return {
        "id": m.stable_id,
        "scope_id": m.scope_id,
        "scope_hash": scopes.get(m.scope_id),
        "line": m.line,
        "column": m.column,
        "kind": m.kind,
        "original": m.original,
        "mutated": m.mutated,
        "verdict": verdict,
        "reason": record.get("reason", ""),
        "timeout_streak": streak,
        "executed_at": now,
    }


@dataclass
class StreakOutcome:
    records: list = field(default_factory=list)   # 终态 verdict 记录（含 pinned 转化）
    entries: list = field(default_factory=list)   # 待落盘 v2 条目
    pinned_ids: list = field(default_factory=list)
    suspended: bool = False


def apply_timeout_streaks(raw_pairs: list, prior_entries: dict,
                          scopes: dict, now: float) -> StreakOutcome:
    """D5 落定：streak 计数 + pinned 转化 + 毒 run 守卫。

    raw_pairs: [(mutant, record)]——record 为 _mutant_record 同构形态，verdict
    为本轮原始判定。毒 run 守卫按本调用窗口（串行=整 run；并发=worker 分片，
    契约披露此口径）计算：timeout ≥5 且占比 ≥50% → 本窗口 timeout 不落
    streak（保留原 streak），报告披露 timeout streak suspended。
    """
    timeout_count = sum(1 for _, r in raw_pairs if r["verdict"] == "timeout")
    executed_n = len(raw_pairs)
    poison = (timeout_count >= D5_POISON_MIN_COUNT and executed_n > 0
              and timeout_count / executed_n >= D5_POISON_RATIO)
    outcome = StreakOutcome(suspended=poison)
    for m, record in raw_pairs:
        prior = prior_entries.get(m.stable_id)
        streak, verdict = streak_for(prior, record["verdict"], poison=poison)
        if verdict == "timeout_pinned":
            record = {**record, "reason": (
                f"{record.get('reason', '')}; timeout_pinned after "
                f"{streak} consecutive timeouts (streak >= "
                f"{D5_TIMEOUT_PINNED_N}) — degraded from timeout")}
            outcome.pinned_ids.append(m.stable_id)
        final = {**record, "verdict": verdict}
        outcome.records.append(final)
        outcome.entries.append(make_entry(m, final, verdict, streak, scopes, now))
    return outcome
