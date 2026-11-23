"""SEE-1619 §SPEC-010（C3-R2）— 性能实测报告汇总的合同测试。

分批前台模式的纯函数面：单轮合并（resume）语义、交错配对（B→后继 A）、
噪声带/倍率/判据汇总。真实 Godot 执行由 KOL 真副本实跑承载（§SPEC-010
判据命令），本文件不拉起 Godot。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from godot_qa_toolkit.measurement.gherkin_perf import (
    compute_perf_summary,
    merge_round_into_report,
)


def _round(name: str, mode: str, wall: float, valid: bool = True,
           repeats: int = 3) -> dict:
    return {"round": name, "mode": mode,
            "gherkin_wall_s": wall, "valid": valid,
            "repeats": repeats, "shards": (4 if mode == "shared" else 44)}


# ---------- 单轮合并（resume） ----------

def test_merge_round_appends_and_recomputes() -> None:
    doc = {"doc": "see1619-gherkin-perf/1",
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


def test_summary_noise_band_from_independent_aa_baseline() -> None:
    """§SPEC-029：噪声带只取首个 B 之前的独立 A/A 基线——交错 A 轮
    不得稀释/替换基线；基线样本与交错对数量必须显式披露。"""
    s = compute_perf_summary(_full_rounds(), 4, 44)
    nb = s["aa_noise_band"]
    assert len(nb["samples"]) == 3, "仅独立 A/A 基线计入噪声带"
    assert nb["samples"] == [30.0, 31.0, 32.0]
    assert nb["min_s"] == 30.0 and nb["max_s"] == 32.0
    assert nb["spread_pct"] == pytest.approx(
        (32.0 - 30.0) / 31.0 * 100, abs=0.1)
    assert s["baseline_samples"] == 3
    assert s["interleaved_shared_rounds"] == 3
    assert s["interleaved_pairs"] == 3


def test_interleaved_only_report_not_complete() -> None:
    """§SPEC-029 HIGH：仅 B1,A1,B2,A2,B3,A3（无独立 A/A 基线）不得
    complete/MEASURED——不得把交错 A 充作 A/A 噪声依据。"""
    rounds = []
    for i in (1, 2, 3):
        rounds.append(_round(f"B{i}", "per_feature", 200.0))
        rounds.append(_round(f"A{i}", "shared", 30.0))
    s = compute_perf_summary(rounds, 4, 44)
    assert s["baseline_samples"] == 0
    assert s["interleaved_pairs"] == 3
    assert s["rounds_complete"] is False, "缺独立 A/A 基线必须 incomplete"
    assert s["verdict"] == "INCONCLUSIVE"


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


# ---------- §SPEC-026：速度门 / 孤立 B / 具名重复轮 ----------

def test_regression_b_faster_must_not_pass() -> None:
    """§SPEC-026：B/A=0.333、噪声 0 的反例必须非 PASS——速度门方向为
    median B/A ≥ 1 + 噪声带。"""
    rounds = [_round(f"A{i}", "shared", 30.0) for i in (1, 2, 3)]
    for i in (1, 2, 3):
        rounds.append(_round(f"B{i}", "per_feature", 10.0))
        rounds.append(_round(f"A10{i}", "shared", 30.0))
    s = compute_perf_summary(rounds, 4, 44)
    assert s["verdict"] == "INCONCLUSIVE", "回归方向不得判 MEASURED"
    assert s["speed_gate"]["ok"] is False
    assert s["speed_gate"]["threshold_b_over_a"] == 1.0, "噪声 0 时阈值=1"
    assert round(s["speed_gate"]["median_ratio_b_over_a"], 3) == 0.333


def test_improvement_within_noise_not_measured() -> None:
    """改善低于/等于 A/A 噪声带必须非 PASS。"""
    rounds = [_round("A1", "shared", 30.0), _round("A2", "shared", 30.5),
              _round("A3", "shared", 31.0)]
    for i in (1, 2, 3):
        rounds.append(_round(f"B{i}", "per_feature", 31.0))
        rounds.append(_round(f"A10{i}", "shared", 30.5))
    s = compute_perf_summary(rounds, 4, 44)
    assert s["verdict"] == "INCONCLUSIVE", "噪声内改善不得判 MEASURED"
    assert s["speed_gate"]["ok"] is False


def test_speed_gate_equality_is_not_pass() -> None:
    """§SPEC-026 边界：median B/A 恰等于阈值（覆盖 threshold rounding）
    必须非 PASS——门为严格大于，改善恰等于噪声带不算改善。"""
    rounds = [_round("A1", "shared", 38.0), _round("A2", "shared", 40.0),
              _round("A3", "shared", 42.0)]
    for i in (1, 2, 3):
        rounds.append(_round(f"B{i}", "per_feature", 44.0))
        rounds.append(_round(f"A10{i}", "shared", 40.0))
    s = compute_perf_summary(rounds, 4, 44)
    assert s["speed_gate"]["threshold_b_over_a"] == 1.1, \
        "噪声带 10% 时阈值=1.1（rounding 边界）"
    assert s["speed_gate"]["median_ratio_b_over_a"] == 1.1, \
        "median 倍率恰等于阈值"
    assert s["speed_gate"]["ok"] is False, "等值必须非 PASS"
    assert s["verdict"] == "INCONCLUSIVE"


def test_speed_gate_passes_for_existing_4x_shape() -> None:
    """既有 4.064×（噪声 ~3.75%）形态必须仍判 MEASURED。"""
    s = compute_perf_summary(_full_rounds(), 4, 44)
    assert s["speed_gate"]["ok"] is True
    assert s["verdict"] == "MEASURED"


def test_orphan_b_breaks_completeness_and_is_disclosed() -> None:
    """孤立 B（无后继 shared A）不得静默丢弃：rounds_complete=False 且
    证据具名可见。"""
    rounds = _full_rounds()
    rounds.append(_round("B9", "per_feature", 200.0))
    s = compute_perf_summary(rounds, 4, 44)
    assert s["rounds_complete"] is False
    assert s["verdict"] == "INCONCLUSIVE"
    assert s["orphan_b_rounds"] == ["B9"], "孤立 B 必须具名披露"


def test_consecutive_bs_each_pair_own_next_a() -> None:
    """连续两个 B：各与其后第一个 shared A 配对（FIFO 不覆盖——旧实现
    B2 静默顶掉 B1），无尾部孤立时 orphan 面为空。"""
    rounds = [_round(f"A{i}", "shared", 30.0 + i) for i in (1, 2, 3)]
    rounds += [_round("B1", "per_feature", 180.0),
               _round("B2", "per_feature", 190.0),
               _round("A101", "shared", 31.0),
               _round("A102", "shared", 31.5),
               _round("A103", "shared", 32.0)]
    s = compute_perf_summary(rounds, 4, 44)
    assert [(p["b_round"], p["a_round"]) for p in s["ab_pairs"]] == [
        ("B1", "A101"), ("B2", "A102")], "各 B 与其后第一个 A 配对，不覆盖"
    assert s["orphan_b_rounds"] == [], "连续 B 均有后继 A，无孤立"
    assert s["verdict"] == "INCONCLUSIVE", "仅 2 对不构成完备对比"


def test_duplicate_round_raises_named_error(tmp_path) -> None:
    """重复 batch round 具名拒绝：专用异常类型（仍为 ValueError 子类）。"""
    from godot_qa_toolkit.measurement.gherkin_perf import (
        DuplicateRoundError,
    )
    doc = {"rounds": [_round("A1", "shared", 29.0)]}
    with pytest.raises(DuplicateRoundError, match="duplicate round"):
        merge_round_into_report(doc, _round("A1", "shared", 30.0), 4, 44)


def test_driver_refuses_duplicate_round_with_exit_2(
        tmp_path, monkeypatch, capsys):
    """分批驱动器：重复轮必须具名拒绝 exit 2，不得裸 ValueError 崩溃。"""
    import importlib.util
    script = Path(__file__).resolve().parents[1] / "integration" / \
        "run_see1619_gherkin_perf.py"
    spec = importlib.util.spec_from_file_location("perf_driver", script)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    report = tmp_path / "perf.json"
    report.write_text(json.dumps(
        {"doc": "see1619-gherkin-perf/1",
         "rounds": [_round("A1", "shared", 29.0)]}), encoding="utf-8")
    args = argparse.Namespace(gqt="unused", godot="unused", timeout=60,
                              shards_a=4, roots=None, mode="A",
                              round_index=1, report=report)
    monkeypatch.setattr(driver, "_gherkin_wall",
                        lambda *a, **k: {"valid": True, "rc": 0,
                                         "gherkin_wall_s": 30.0,
                                         "shards": 4, "repeats": 3})
    rc = driver.run_single_round(args, "A", 1, tmp_path, 44,
                                 tmp_path / "recipe.json", report)
    assert rc == 2, "重复轮必须具名拒绝 exit 2"
    assert "duplicate round" in capsys.readouterr().err


# ---------- §SPEC-028：batch 出口必须服从合并后的 summary 门 ----------

def _load_driver():
    import importlib.util
    script = Path(__file__).resolve().parents[1] / "integration" / \
        "run_see1619_gherkin_perf.py"
    spec = importlib.util.spec_from_file_location("perf_driver_s", script)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    return driver


def _batch_args(tmp_path, report):
    return argparse.Namespace(gqt="unused", godot="unused", timeout=60,
                              shards_a=4, roots=None, mode="A",
                              round_index=1, report=report)


def _seed_report(report: Path, rounds: list[dict]) -> None:
    report.write_text(json.dumps(
        {"doc": "see1619-gherkin-perf/1", "rounds": rounds}),
        encoding="utf-8")


def _eight_round_prefix(b_wall: float) -> list[dict]:
    """追加一轮 shared 即凑满 9 轮（3 A/A + 3 对）的前缀。"""
    return [_round("A1", "shared", 30.0), _round("A2", "shared", 30.0),
            _round("A3", "shared", 30.0),
            _round("B1", "per_feature", b_wall),
            _round("A101", "shared", 30.0),
            _round("B2", "per_feature", b_wall),
            _round("A102", "shared", 30.0),
            _round("B3", "per_feature", b_wall)]


def _append_round(driver, monkeypatch, tmp_path, report, wall, rtype="A",
                  ridx=103):
    monkeypatch.setattr(driver, "_gherkin_wall",
                        lambda *a, **k: {"valid": True, "rc": 0,
                                         "gherkin_wall_s": wall,
                                         "shards": 4, "repeats": 3})
    return driver.run_single_round(
        _batch_args(tmp_path, report), rtype, ridx, tmp_path, 44,
        tmp_path / "recipe.json", report)


def test_batch_complete_regressed_report_exits_nonzero(
        tmp_path, monkeypatch, capsys):
    """§SPEC-028 HIGH：9 轮全 valid 但 B/A=0.333（speed_gate 失败）的
    完整报告，batch 必须 exit≠0——round valid 不等于报告合格。"""
    driver = _load_driver()
    report = tmp_path / "perf.json"
    _seed_report(report, _eight_round_prefix(b_wall=10.0))
    rc = _append_round(driver, monkeypatch, tmp_path, report, wall=30.0)
    doc = json.loads(report.read_text(encoding="utf-8"))
    assert doc["summary"]["rounds_complete"] is True
    assert doc["summary"]["verdict"] == "INCONCLUSIVE"
    assert rc == 1, "完整但不合格的报告不得 exit 0"
    assert "INCONCLUSIVE report" in capsys.readouterr().err


def test_batch_complete_improved_report_exits_zero(tmp_path, monkeypatch):
    """改善超噪声的完整报告 batch 必须 exit 0（不回归）。"""
    driver = _load_driver()
    report = tmp_path / "perf.json"
    _seed_report(report, _eight_round_prefix(b_wall=200.0))
    rc = _append_round(driver, monkeypatch, tmp_path, report, wall=30.0)
    doc = json.loads(report.read_text(encoding="utf-8"))
    assert doc["summary"]["verdict"] == "MEASURED"
    assert rc == 0


def test_batch_intermediate_round_resumable_with_explicit_note(
        tmp_path, monkeypatch, capsys):
    """合法中间轮：报告不完整时 exit 0 且必须显式提示可 resume——收尾
    语义（最终以全量模式重读 verdict）写明，不得假绿。"""
    driver = _load_driver()
    report = tmp_path / "perf.json"
    _seed_report(report, [])
    rc = _append_round(driver, monkeypatch, tmp_path, report, wall=30.0,
                       rtype="A", ridx=1)
    err = capsys.readouterr().err
    assert rc == 0, "中间轮必须可 resume"
    assert "report incomplete" in err and "resume" in err, \
        "不完整报告必须显式提示 resume 语义"


def test_batch_orphan_b_disclosed_and_resumable(tmp_path, monkeypatch,
                                                capsys):
    """孤立 B：显式披露待配对，仍可 resume（后继 shared A 可配对），
    但不得静默——提示必须点名。"""
    driver = _load_driver()
    report = tmp_path / "perf.json"
    _seed_report(report, [_round("A1", "shared", 30.0),
                          _round("A2", "shared", 30.0),
                          _round("A3", "shared", 30.0),
                          _round("B1", "per_feature", 200.0),
                          _round("A101", "shared", 30.0)])
    rc = _append_round(driver, monkeypatch, tmp_path, report, wall=200.0,
                       rtype="B", ridx=2)
    err = capsys.readouterr().err
    doc = json.loads(report.read_text(encoding="utf-8"))
    assert doc["summary"]["orphan_b_rounds"] == ["B2"]
    assert rc == 0, "孤立 B 可被后继 A 修复，中间态可 resume"
    assert "unpaired B" in err and "B2" in err, "孤立 B 必须显式披露"


def test_batch_interleaved_only_report_terminal_exit_1(
        tmp_path, monkeypatch, capsys):
    """§SPEC-029：交错-only 报告在 batch 中必须 exit≠0——基线无法通过
    末尾追加修复（基线块固定于报告开头），属终态失败而非可 resume。"""
    driver = _load_driver()
    report = tmp_path / "perf.json"
    _seed_report(report, [
        _round("B1", "per_feature", 200.0), _round("A1", "shared", 30.0),
        _round("B2", "per_feature", 200.0), _round("A2", "shared", 30.0)])
    rc = _append_round(driver, monkeypatch, tmp_path, report, wall=200.0,
                       rtype="B", ridx=3)
    doc = json.loads(report.read_text(encoding="utf-8"))
    assert doc["summary"]["baseline_samples"] == 0
    assert doc["summary"]["rounds_complete"] is False
    assert rc == 1, "交错-only 报告不得 exit 0"
    assert "baseline" in capsys.readouterr().err


def test_batch_pure_aa_intermediate_still_resumable(tmp_path, monkeypatch,
                                                    capsys):
    """纯 A/A 阶段（尚无 B）仍可 resume：追加 A 轮可补足基线。"""
    driver = _load_driver()
    report = tmp_path / "perf.json"
    _seed_report(report, [_round("A1", "shared", 30.0),
                          _round("A2", "shared", 30.0)])
    rc = _append_round(driver, monkeypatch, tmp_path, report, wall=30.0,
                       rtype="A", ridx=3)
    err = capsys.readouterr().err
    assert rc == 0, "纯 A/A 中间轮必须可 resume"
    assert "report incomplete" in err
