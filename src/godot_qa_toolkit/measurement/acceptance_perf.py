"""SEE-1619 §SPEC-010 — acceptance 性能实测的纯函数面（分批前台模式的
单轮合并与汇总）。真实 Godot 执行在 tests/integration/
run_see1619_acceptance_perf.py 的驱动层；本模块不拉起 Godot。"""
from __future__ import annotations

import json
import statistics
from pathlib import Path


class DuplicateRoundError(ValueError):
    """重复 batch round：具名拒绝（重跑同轮不得静默覆盖样本）。"""


def _pair_rounds(rounds: list[dict]) -> tuple[list[dict], list[dict]]:
    """交错语义：每个 B 轮与其后第一个 shared（A）轮配对一次（FIFO——
    连续多个 B 时取最旧 pending，未配对者原样返回，证据不静默丢弃）。
    纯函数。"""
    pairs: list[dict] = []
    pending: list[dict] = []
    for r in rounds:
        if r.get("mode") == "per_feature":
            pending.append(r)
        elif r.get("mode") == "shared" and pending:
            b = pending.pop(0)
            wa = r.get("acceptance_wall_s")
            wb = b.get("acceptance_wall_s")
            pairs.append({"b_round": b.get("round"),
                          "a_round": r.get("round"),
                          "b_wall_s": wb, "a_wall_s": wa,
                          "ratio_b_over_a": (round(wb / wa, 3)
                                             if wa and wb else None),
                          "b_valid": b.get("valid"),
                          "a_valid": r.get("valid")})
    return pairs, pending


def compute_perf_summary(rounds: list[dict], shards_a: int,
                         keys: int) -> dict:
    """纯函数：由逐轮记录汇总噪声带/倍率/进程账/判据。分批前台模式下
    每轮追加后重算，测量口径与单次全量模式一致。"""
    a_walls = [r.get("acceptance_wall_s") for r in rounds
               if r.get("mode") == "shared" and r.get("acceptance_wall_s")]
    noise = {}
    if a_walls:
        med = statistics.median(a_walls)
        spread = max(a_walls) - min(a_walls)
        noise = {"samples": a_walls, "median_s": round(med, 3),
                 "min_s": min(a_walls), "max_s": max(a_walls),
                 "spread_s": round(spread, 3),
                 "spread_pct": round(spread / med * 100, 2)
                 if med else None}
    pairs, orphan_b = _pair_rounds(rounds)
    ratios = [p["ratio_b_over_a"] for p in pairs
              if p["ratio_b_over_a"] is not None]
    all_valid = all(r.get("valid") for r in rounds) and bool(rounds)
    reps = rounds[0].get("repeats") if rounds else None
    complete = len(a_walls) >= 3 and len(ratios) >= 3
    # §SPEC-026：孤立 B = 证据缺口，rounds_complete 必须为 False 且具名
    # 披露（旧实现连续 B 覆盖、尾部 B 静默丢弃）。
    rounds_complete = complete and not orphan_b
    med_ratio = (statistics.median(ratios) if len(ratios) >= 3 else None)
    # §SPEC-026：速度门——median B/A 改善必须**严格大于** A/A 噪声带
    # （阈值 = 1 + spread_pct/100）；等于阈值（改善恰等于噪声带）与
    # 回归方向（B/A<1）一律 INCONCLUSIVE，B 慢于基线反例不得 exit 0。
    speed_gate = None
    if med_ratio is not None and noise.get("spread_pct") is not None:
        threshold = round(1 + noise["spread_pct"] / 100, 4)
        speed_gate = {"median_ratio_b_over_a": round(med_ratio, 4),
                      "threshold_b_over_a": threshold,
                      "ok": bool(med_ratio > threshold)}
    return {
        "keys": keys,
        "shards_a": shards_a,
        "reps": reps,
        "fanout_shared": shards_a * (reps or 0),
        "fanout_per_feature": keys * (reps or 0),
        "aa_noise_band": noise,
        "ab_pairs": pairs,
        "orphan_b_rounds": [b.get("round") for b in orphan_b],
        "median_ratio_b_over_a": med_ratio,
        "speed_gate": speed_gate,
        "all_rounds_valid": all_valid,
        "rounds_complete": rounds_complete,
        "verdict": ("MEASURED" if all_valid and rounds_complete
                    and speed_gate is not None and speed_gate["ok"]
                    else "INCONCLUSIVE"),
    }


def merge_round_into_report(doc: dict, round_record: dict, shards_a: int,
                            keys: int,
                            report_path: Path | None = None) -> dict:
    """分批前台模式：单轮记录合并入既有报告 doc（resume 语义），逐轮
    追加后重算汇总；round 名重复即拒绝（重跑同轮不得静默覆盖样本）。
    report_path 给定时落盘。"""
    name = round_record.get("round")
    if any(r.get("round") == name for r in doc.get("rounds", [])):
        raise DuplicateRoundError(f"duplicate round: {name}")
    doc.setdefault("doc", "see1619-acceptance-perf/1")
    doc.setdefault("rounds", []).append(round_record)
    doc["summary"] = compute_perf_summary(doc["rounds"], shards_a, keys)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc
