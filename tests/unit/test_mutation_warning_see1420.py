"""§SPEC-1420-01：--mutation-warning 阈值（默认 50）+ 截断 N/M 强制披露。

定案 §一 L1（Owner 裁决 2026-10-16：阈值与 budget 对齐）：
- 按截断前全量位点数计数；超阈值 → stderr 告警 + summary.attention 建议拆分；
- 位点数 > budget 即强制披露 total_sites/executed（N/M）；
- --mutation-warning / --timeout-factor 绝不进 resume key（定案 D6 负面清单）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.resume2 import ResumeStoreV2, file_key

# 6 个变异点（与既有 resume 测试同源 fixture——budget=2 必然截断）。
TARGET_SRC = (
    "func add(a, b):\n"
    "\tif a < b:\n"
    "\t\treturn a + b\n"
    "\treturn b\n"
)


def _write_project(tmp_path, src=TARGET_SRC):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(src)
    return proj, target


def _fake_gut(calls):
    def fake(root, timeout_s=60, tests_glob=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (0, "baseline ok")
        return (0, "all passed")

    return fake


def _site_count():
    return len(mrunner._collect_mutations(TARGET_SRC, "calc.gd"))


class TestMutationWarningThreshold:
    def test_warning_fires_above_threshold(self, tmp_path, monkeypatch,
                                           capsys):
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _fake_gut(calls))
        r = mrunner.run_mutation(str(target), str(proj), budget=50,
                                 tests_glob="res://tests/",
                                 mutation_warning=2)
        assert _site_count() > 2
        assert "attention" in r["summary"]
        assert "splitting" in r["summary"]["attention"]
        err = capsys.readouterr().err
        assert "mutation-warning" in err or "mutation_warning" in err

    def test_no_warning_under_threshold(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _fake_gut(calls))
        r = mrunner.run_mutation(str(target), str(proj), budget=50,
                                 tests_glob="res://tests/",
                                 mutation_warning=50)
        assert "attention" not in r["summary"]

    def test_warning_counts_pre_truncation_total(self, tmp_path, monkeypatch):
        """budget 截断不掩盖告警——阈值按截断前全量位点数判定（§01 判据）。"""
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _fake_gut(calls))
        r = mrunner.run_mutation(str(target), str(proj), budget=2,
                                 tests_glob="res://tests/",
                                 mutation_warning=2)
        assert _site_count() == 6
        assert "attention" in r["summary"]

    def test_default_threshold_is_50(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _fake_gut(calls))
        # 6 位点 < 50（默认阈值）→ 无告警；阈值语义不依赖显式传参
        r = mrunner.run_mutation(str(target), str(proj), budget=50,
                                 tests_glob="res://tests/")
        assert "attention" not in r["summary"]


class TestTruncationDisclosure:
    def test_truncation_discloses_total_and_executed(self, tmp_path,
                                                     monkeypatch):
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _fake_gut(calls))
        r = mrunner.run_mutation(str(target), str(proj), budget=2,
                                 tests_glob="res://tests/")
        assert r["summary"]["total_sites"] == 6
        assert r["summary"]["executed"] == 2
        # N/M 披露是强制语义：执行轮次同样收窄（baseline + 2 mutant）
        assert calls["n"] == 3

    def test_no_disclosure_without_truncation(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}
        monkeypatch.setattr(mrunner, "_run_gut_on_project", _fake_gut(calls))
        r = mrunner.run_mutation(str(target), str(proj), budget=50,
                                 tests_glob="res://tests/")
        assert "total_sites" not in r["summary"]
        assert "executed" not in r["summary"]

    def test_dry_run_discloses_total_sites(self, tmp_path):
        proj, target = _write_project(tmp_path)
        r = mrunner.run_mutation(str(target), str(proj), budget=2,
                                 tests_glob="res://tests/", dry_run=True)
        assert r["summary"]["total_sites"] == 6
        assert r["summary"]["mutants"] == 2  # 既有 dry-run 语义：budget 截断面


class TestWarningAndFactorNeverEnterResumeKey:
    def test_resume_key_indifferent_to_warning_and_factor(self, tmp_path):
        """定案 D6 负面清单：--mutation-warning / --timeout-factor 绝不进
        resume key——调配置不得引发全库重跑。两参数不在 key 函数签名内，
        同输入恒同 key（幂等性钉死）。"""
        proj, target = _write_project(tmp_path)
        k1 = file_key(str(proj), str(target), "res://tests/")
        k2 = file_key(str(proj), str(target), "res://tests/")
        assert k1 == k2
