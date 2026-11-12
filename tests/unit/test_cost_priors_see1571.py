"""SEE-1571 §SPEC-502：E6-5 成本感知分片转正（先验驱动串/并行自动仲裁）。

钉死行为（蓝本 = SEE-1568 批次二留档，决策公式与四键先验逐位复原）：
- 默认生效；无先验 = 并行缺省（KOL 量级不误降级）；
- 决策公式对留档决策线逐位吻合（fixture r2/r3 串行、KOL r2 并行）；
- 先验文件落位项目 `.qa-cache`（单写者）；跨 run 合并 = 0 值保留旧先验
  （消除串/并行逐轮翻转）；
- 决策线 stderr 披露格式逐字符钉死。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import cost_priors, executor
import godot_qa_toolkit.mutation.runner as runner_mod

GD_WIDE = (
    "func f(a, b):\n"
    "\tif a > 0:\n"
    "\t\treturn a + b\n"
    "\treturn a - b and a * b\n"
)

# 批次二留档先验（qa-data-b2/e6 stderr 决策线逐值转录）
FX_R1_PRIORS = {"prewarm_s": 3.69, "baseline_s": 1.388,
                "per_mutant_s": 1.5392, "slot_fixed_s": 0.098}
FX_R3_PRIORS = {"prewarm_s": 3.69, "baseline_s": 0.0,
                "per_mutant_s": 0.9688000000000001, "slot_fixed_s": 0.038}
KOL_R2_PRIORS = {"prewarm_s": 5.003, "baseline_s": 7.638,
                 "per_mutant_s": 9.396411764705883,
                 "slot_fixed_s": 1.728}


def _write_priors(proj: str, priors: dict) -> None:
    path = Path(proj) / ".qa-cache" / "gqt-mutation-cost-priors.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(priors), encoding="utf-8")


def _read_priors(proj: str) -> dict:
    path = Path(proj) / ".qa-cache" / "gqt-mutation-cost-priors.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write_project(tmp_path: Path, source: str) -> tuple[str, str]:
    proj = tmp_path / "proj"
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "tests" / "unit" / "test_x.gd").write_text(
        'preload("res://calc.gd")\n')
    target = proj / "calc.gd"
    target.write_text(source)
    return str(proj), str(target)


class TestDecisionModel:
    def test_fixture_r2_serial_bit_exact(self):
        v = cost_priors.evaluate(FX_R1_PRIORS, total=5, jobs=2)
        assert v is not None
        serial, s_est, p_est = v
        assert serial is True
        assert round(s_est, 2) == 9.08
        assert round(p_est, 2) == 9.89

    def test_fixture_r3_serial_with_zero_baseline_fallback(self):
        """baseline 先验为 0（串行轮毒化态）：回退 per_mutant，判串行不翻转。"""
        v = cost_priors.evaluate(FX_R3_PRIORS, total=5, jobs=2)
        assert v is not None
        serial, s_est, p_est = v
        assert serial is True
        assert round(s_est, 2) == 5.81
        assert round(p_est, 2) == 7.64

    def test_kol_r2_parallel_bit_exact(self):
        """KOL 量级：先验判并行（167.38 vs 100.66）——不误降级。"""
        v = cost_priors.evaluate(KOL_R2_PRIORS, total=17, jobs=2)
        assert v is not None
        serial, s_est, p_est = v
        assert serial is False
        assert round(s_est, 2) == 167.38
        assert round(p_est, 2) == 100.66

    def test_all_zero_priors_unusable_defaults_parallel(self):
        assert cost_priors.evaluate(
            {k: 0.0 for k in cost_priors.PRIOR_KEYS}, total=5, jobs=2) is None

    def test_missing_priors_defaults_parallel_no_disclosure(
            self, tmp_path, capsys):
        proj, _ = _write_project(tmp_path, GD_WIDE)
        assert cost_priors.prefers_serial(proj, total=5, jobs=2) is False
        assert "cost-aware" not in capsys.readouterr().err


class TestDisclosureLine:
    def test_decision_line_pins_batch2_format(self, tmp_path, capsys):
        proj, _ = _write_project(tmp_path, GD_WIDE)
        _write_priors(proj, FX_R1_PRIORS)
        assert cost_priors.prefers_serial(proj, total=5, jobs=2) is True
        err = capsys.readouterr().err
        priors_json = json.dumps({k: FX_R1_PRIORS[k]
                                  for k in cost_priors.PRIOR_KEYS})
        assert err == (f"cost-aware: total=5 jobs=2 serial_est=9.08s "
                       f"parallel_est=9.89s -> serial (priors: "
                       f"{priors_json})\n")

    def test_parallel_decision_line(self, tmp_path, capsys):
        proj, _ = _write_project(tmp_path, GD_WIDE)
        _write_priors(proj, KOL_R2_PRIORS)
        assert cost_priors.prefers_serial(proj, total=17, jobs=2) is False
        assert "-> parallel" in capsys.readouterr().err


class TestPriorFileContract:
    def test_record_write_side_bit_exact_batch2(self, tmp_path):
        """wrapper 写侧四键归一对留档先验逐位复原（stage÷executed、÷jobs）。"""
        proj, _ = _write_project(tmp_path, GD_WIDE)
        cost_priors.record(proj, {"prewarm": 3.69, "baseline": 1.388,
                                  "per_mutant": 7.696, "slot_sync": 0.196},
                           jobs=2, executed=5)
        assert _read_priors(proj) == FX_R1_PRIORS

    def test_record_zero_keeps_old_anti_flapping(self, tmp_path):
        """串行轮写 0（prewarm/baseline 测不到）：0 值保留旧先验，不翻转。"""
        proj, _ = _write_project(tmp_path, GD_WIDE)
        _write_priors(proj, FX_R1_PRIORS)
        cost_priors.record(proj, {"prewarm": 0.0, "baseline": 0.0,
                                  "per_mutant": 4.844, "slot_sync": 0.076},
                           jobs=2, executed=5)
        merged = _read_priors(proj)
        assert merged["prewarm_s"] == 3.69
        assert merged["baseline_s"] == 1.388
        assert merged["per_mutant_s"] == pytest.approx(0.9688)
        assert merged["slot_fixed_s"] == pytest.approx(0.038)

    def test_record_skips_zero_executed(self, tmp_path):
        proj, _ = _write_project(tmp_path, GD_WIDE)
        cost_priors.record(proj, {"prewarm": 1.0}, jobs=2, executed=0)
        assert not (Path(proj) / ".qa-cache" /
                    "gqt-mutation-cost-priors.json").exists()

    def test_record_creates_qa_cache_dir(self, tmp_path):
        proj, _ = _write_project(tmp_path, GD_WIDE)
        cost_priors.record(proj, {"prewarm": 1.0, "baseline": 2.0,
                                  "per_mutant": 3.0, "slot_sync": 4.0},
                           jobs=2, executed=1)
        assert _read_priors(proj)["prewarm_s"] == 1.0

    def test_load_corrupt_warns_and_treats_missing(
            self, tmp_path, capsys):
        proj, _ = _write_project(tmp_path, GD_WIDE)
        path = Path(proj) / ".qa-cache" / "gqt-mutation-cost-priors.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert cost_priors.load(proj) is None
        assert cost_priors.prefers_serial(proj, total=5, jobs=2) is False
        assert "cost priors unreadable" in capsys.readouterr().err

    def test_no_flapping_across_serial_rounds(self, tmp_path):
        """连续串行轮：合并保旧值，判定逐轮稳定（消除逐轮翻转）。"""
        proj, _ = _write_project(tmp_path, GD_WIDE)
        _write_priors(proj, FX_R1_PRIORS)
        decisions = []
        for _ in range(3):
            serial = cost_priors.prefers_serial(proj, total=5, jobs=2)
            decisions.append(serial)
            cost_priors.record(proj, {"prewarm": 0.0, "baseline": 0.0,
                                      "per_mutant": 4.844, "slot_sync": 0.076},
                               jobs=2, executed=5)
        assert decisions == [True, True, True]
        merged = _read_priors(proj)
        assert merged["prewarm_s"] == 3.69 and merged["baseline_s"] == 1.388


class TestExecutorWiring:
    def _sealed(self, monkeypatch):
        monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass [passed]"))

    def test_small_target_auto_serial_with_priors(
            self, tmp_path, monkeypatch, capsys):
        """有先验的小目标（total > jobs）：自动退串行，prewarm 之前仲裁。"""
        proj, target = _write_project(tmp_path, GD_WIDE)
        _write_priors(proj, FX_R1_PRIORS)
        monkeypatch.setattr(executor.os, "cpu_count", lambda: 4)
        self._sealed(monkeypatch)
        warm = {"n": 0}
        monkeypatch.setattr(
            executor, "prewarm_import",
            lambda root: warm.__setitem__("n", warm["n"] + 1))
        d = executor.run_mutation_files([target], proj, budget=5,
                                        timeout_s=60, dry_run=False,
                                        jobs=None)
        assert warm["n"] == 0, "成本仲裁退串行必须保持在 prewarm 之前"
        assert d["jobs"] == 1
        assert d["results"][0]["summary"]["jobs"] == 1
        assert "cost-aware:" in capsys.readouterr().err

    def test_no_prior_single_file_stays_parallel(
            self, tmp_path, monkeypatch, capsys):
        """无先验：并行缺省（分片路径，无决策线）。"""
        proj, target = _write_project(tmp_path, GD_WIDE)
        monkeypatch.setattr(executor.os, "cpu_count", lambda: 4)
        monkeypatch.setattr(
            executor, "_run_baseline_snapshot",
            lambda root, g, t: {"rc": 0, "tail": "", "elapsed": 5.0})
        self._sealed(monkeypatch)
        d = executor.run_mutation_files([target], proj, budget=50,
                                        timeout_s=60, dry_run=False,
                                        jobs=None)
        assert d["jobs"] > 1
        assert "cost-aware:" not in capsys.readouterr().err

    def test_serial_round_writes_priors_without_poison(
            self, tmp_path, monkeypatch, capsys):
        """仲裁退串行轮收尾写先验：prewarm/baseline 为 0 不毒化既有值。"""
        proj, target = _write_project(tmp_path, GD_WIDE)
        _write_priors(proj, FX_R1_PRIORS)
        monkeypatch.setattr(executor.os, "cpu_count", lambda: 4)
        self._sealed(monkeypatch)
        executor.run_mutation_files([target], proj, budget=5,
                                    timeout_s=60, dry_run=False, jobs=None)
        merged = _read_priors(proj)
        assert merged["prewarm_s"] == 3.69 and merged["baseline_s"] == 1.388

    def test_explicit_jobs_one_skips_arbitration(
            self, tmp_path, monkeypatch, capsys):
        """显式 --jobs 1（CLI 串行面）无仲裁必要：不读先验不出决策线。"""
        proj, target = _write_project(tmp_path, GD_WIDE)
        _write_priors(proj, FX_R1_PRIORS)
        self._sealed(monkeypatch)
        d = executor.run_mutation_files([target], proj, budget=50,
                                        dry_run=False, jobs=1)
        assert d["jobs"] == 1
        assert "cost-aware:" not in capsys.readouterr().err
