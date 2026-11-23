"""SEE-1619 §SPEC-010 — acceptance 性能实测的纯函数面（分批前台模式的
单轮合并与汇总）。真实 Godot 执行在 tests/integration/
run_see1619_acceptance_perf.py 的驱动层；本模块不拉起 Godot。"""
from __future__ import annotations

import json
import statistics
from pathlib import Path


def _pair_rounds(rounds: list[dict]) -> list[dict]:
    """交错语义：每个 B 轮与其后第一个 shared（A）轮配对。纯函数。"""
    pairs: list[dict] = []
    pending_b: dict | None = None
    for r in rounds:
        if r.get("mode") == "per_feature":
            pending_b = r
        elif r.get("mode") == "shared" and pending_b is not None:
            wa = r.get("acceptance_wall_s")
            wb = pending_b.get("acceptance_wall_s")
            pairs.append({"b_round": pending_b.get("round"),
                          "a_round": r.get("round"),
                          "b_wall_s": wb, "a_wall_s": wa,
                          "ratio_b_over_a": (round(wb / wa, 3)
                                             if wa and wb else None),
                          "b_valid": pending_b.get("valid"),
                          "a_valid": r.get("valid")})
            pending_b = None
    return pairs


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
    pairs = _pair_rounds(rounds)
    ratios = [p["ratio_b_over_a"] for p in pairs
              if p["ratio_b_over_a"] is not None]
    all_valid = all(r.get("valid") for r in rounds) and bool(rounds)
    reps = rounds[0].get("repeats") if rounds else None
    complete = len(a_walls) >= 3 and len(ratios) >= 3
    return {
        "keys": keys,
        "shards_a": shards_a,
        "reps": reps,
        "fanout_shared": shards_a * (reps or 0),
        "fanout_per_feature": keys * (reps or 0),
        "aa_noise_band": noise,
        "ab_pairs": pairs,
        "median_ratio_b_over_a": statistics.median(ratios)
        if len(ratios) >= 3 else None,
        "all_rounds_valid": all_valid,
        "rounds_complete": complete,
        "verdict": ("MEASURED" if all_valid and complete
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
        raise ValueError(f"duplicate round: {name}")
    doc.setdefault("doc", "see1619-acceptance-perf/1")
    doc.setdefault("rounds", []).append(round_record)
    doc["summary"] = compute_perf_summary(doc["rounds"], shards_a, keys)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc
