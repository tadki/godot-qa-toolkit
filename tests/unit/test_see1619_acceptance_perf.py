"""SEE-1619 §SPEC-010（C3-R2）— 性能实测报告汇总的合同测试。

分批前台模式的纯函数面：单轮合并（resume）语义、交错配对（B→后继 A）、
噪声带/倍率/判据汇总。真实 Godot 执行由 KOL 真副本实跑承载（§SPEC-010
判据命令），本文件不拉起 Godot。
"""
from __future__ import annotations

import json

import pytest

from godot_qa_toolkit.measurement.acceptance_perf import (
    compute_perf_summary,
    merge_round_into_report,
)


def _round(name: str, mode: str, wall: float, valid: bool = True,
           repeats: int = 3) -> dict:
    return {"round": name, "mode": mode,
            "acceptance_wall_s": wall, "valid": valid,
            "repeats": repeats, "shards": (4 if mode == "shared" else 44)}


# ---------- 单轮合并（resume） ----------

def test_merge_round_appends_and_recomputes() -> None:
    doc = {"doc": "see1619-acceptance-perf/1",
           "rounds": [_round("A1", "shared", 29.0)],
           "summary": compute_perf_summary(
               [_round("A1", "shared", 29.0)], 4, 44)}
    merge_round_into_report(doc, _round("B1", "per_feature", 190.0), 4, 44)
    assert [r["round"] for r in doc["rounds"]] == ["A1", "B1"]
    assert doc["summary"]["rounds_complete"] is False
    assert doc["summary"]["verdict"] == "INCONCLUSIVE"
    assert doc["summary"]["ab_pairs"] == [], \
        "resume 中：B 的配对待其后继 A 落盘时完成"
    merge_round_into_report(doc, _round("A2", "shared", 30.0), 4, 44)
    assert doc["summary"]["ab_pairs"][0]["ratio_b_over_a"] == pytest.approx(
        190.0 / 30.0, abs=1e-3), "后继 A 落盘时配对完成"
    assert [r["round"] for r in doc["rounds"]] == ["A1", "B1", "A2"]
    # §SPEC-011 hardener：配对/样本不足时判据必须保持 None/False——
    # median 倍率需 ≥3 对、rounds_complete 需 ≥3 A 样本且 ≥3 对。
    assert doc["summary"]["median_ratio_b_over_a"] is None, \
        "配对 <3 时 median 倍率不得给出"
    assert doc["summary"]["rounds_complete"] is False, \
        "1 A 墙钟 + 1 对不构成完备"


def test_pair_consumes_pending_b_once() -> None:
    """§SPEC-011 hardener：一个 B 只与其后第一个 shared A 配对一次——
    pending B 消费后必须清空，后继 A 不得复用同一 B 重复配对。"""
    rounds = [_round("A1", "shared", 29.0),
              _round("B1", "per_feature", 180.0),
              _round("A2", "shared", 30.0),
              _round("A3", "shared", 31.0)]
    s = compute_perf_summary(rounds, 4, 44)
    assert [p["b_round"] for p in s["ab_pairs"]] == ["B1"]
    assert s["ab_pairs"][0]["a_round"] == "A2"
    assert s["verdict"] == "INCONCLUSIVE", "仅 1 对不构成完备对比"


def test_merge_round_rejects_duplicate_round_name() -> None:
    doc = {"rounds": [_round("A1", "shared", 29.0)]}
    with pytest.raises(ValueError, match="duplicate round"):
        merge_round_into_report(doc, _round("A1", "shared", 30.0), 4, 44)


def test_merge_round_persists_report_file(tmp_path) -> None:
    rp = tmp_path / "perf.json"
    doc = {"rounds": [], "summary": {}}
    rp.write_text(json.dumps(doc), encoding="utf-8")
    merge_round_into_report(doc, _round("A1", "shared", 29.0), 4, 44,
                            report_path=rp)
    on_disk = json.loads(rp.read_text(encoding="utf-8"))
    assert len(on_disk["rounds"]) == 1


# ---------- 汇总：配对 / 噪声带 / 判据 ----------

def _full_rounds() -> list[dict]:
    # 与驱动器一致的插入序：A/A 块 → 交错 (B, A)×3
    rounds = [_round(f"A{i}", "shared", 29.0 + i) for i in (1, 2, 3)]
    for i in (1, 2, 3):
        rounds.append(_round(f"B{i}", "per_feature", 180.0 + 10 * i))
        rounds.append(_round(f"A10{i}", "shared", 30.0 + i))
    return rounds


def test_summary_pairs_b_with_next_shared_round() -> None:
    s = compute_perf_summary(_full_rounds(), 4, 44)
    assert len(s["ab_pairs"]) == 3
    # 交错配对：每个 B 与其后第一个 shared A 配对
    assert s["ab_pairs"][0]["b_round"] == "B1"
    assert s["ab_pairs"][0]["a_round"] == "A101"


def test_summary_noise_band_over_all_shared_samples() -> None:
    s = compute_perf_summary(_full_rounds(), 4, 44)
    nb = s["aa_noise_band"]
    assert len(nb["samples"]) == 6, "A/A 与交错 A 全部计入噪声带"
    assert nb["min_s"] == 30.0 and nb["max_s"] == 33.0
    assert nb["spread_pct"] == pytest.approx(
        (33.0 - 30.0) / 31.5 * 100, abs=0.1)


def test_summary_measured_only_when_complete_and_valid() -> None:
    rounds = _full_rounds()
    s = compute_perf_summary(rounds, 4, 44)
    assert s["verdict"] == "MEASURED" and s["rounds_complete"] is True
    bad = rounds + [_round("B9", "per_feature", 1.0, valid=False)]
    s2 = compute_perf_summary(bad, 4, 44)
    assert s2["verdict"] == "INCONCLUSIVE", "任一轮 invalid 不得出 MEASURED"


def test_summary_process_accounting() -> None:
    s = compute_perf_summary(_full_rounds(), 4, 44)
    assert s["fanout_shared"] == 12, "4 组 ×3 reps"
    assert s["fanout_per_feature"] == 132, "44 单例 ×3 reps"
