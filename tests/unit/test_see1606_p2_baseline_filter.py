"""SEE-1606 P2：基线过滤 + `--break-on-baseline-failure` 显式开关。

Owner 2026-11-19 口径（对齐 Stryker `break-on-initial-test-failure` 默认
false）：默认继续跑变异批次，基线失败的测试不参与 kill 判定并登记披露；
显式开开关时基线有失败即中止（fail-loud，零 mutant GUT）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.cli import build_parser
from godot_qa_toolkit.mutation import merging
from godot_qa_toolkit.mutation import runner as mrunner

TARGET_SRC = (
    "func add(a, b):\n"
    "\tif a < b:\n"
    "\t\treturn a + b\n"
    "\treturn b\n"
)

DIRTY_TAIL = "* test_pre_existing\n[Failed]\n"
CLEAN_TAIL = "* test_pre_existing\n[passed]\n"


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(TARGET_SRC)
    return proj, target


def _parse_mutation(extra: list[str]):
    parser = build_parser()
    return parser.parse_args(
        ["mutation", "calc.gd", "--project-root", "/p", *extra])


class TestCliSwitch:
    def test_default_is_off(self):
        assert _parse_mutation([]).break_on_baseline_failure is False

    def test_explicit_flag_on(self):
        args = _parse_mutation(["--break-on-baseline-failure"])
        assert args.break_on_baseline_failure is True


class TestDefaultContinueAndDisclose:
    def test_dirty_baseline_continues_and_discloses(self, tmp_path,
                                                    monkeypatch):
        proj, target = _write_project(tmp_path)
        calls = {"mutant_runs": 0}

        monkeypatch.setattr(mrunner, "_run_baseline",
                            lambda root, tests_glob, timeout_s:
                            (1, DIRTY_TAIL, 10.0))

        def fake_gut(root, timeout_s=600, tests_glob=None):
            calls["mutant_runs"] += 1
            # 与基线完全相同的失败集：不得算 killed。
            return (1, DIRTY_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake_gut)
        r = mrunner.run_mutation(str(target), str(proj), budget=2,
                                 tests_glob="res://tests/")
        assert calls["mutant_runs"] > 0, "default must keep running mutants"
        s = r["summary"]
        # 继续跑 ≠ 通过：脏基线 ∧ 差集空 → suspect（既有门禁语义不变）。
        assert s.get("suspect", 0) > 0
        assert r["ok"] is False
        assert s.get("baseline_dirty") is True
        assert s.get("baseline_failures") == 1
        assert s.get("baseline_failure_names") == ["test_pre_existing"]
        assert s.get("break_on_baseline_failure") is False
        assert s.get("killed") == 0
        assert all(f["verdict"] != "killed" for f in r["failures"])

    def test_new_failure_after_dirty_baseline_still_kills(self, tmp_path,
                                                          monkeypatch):
        proj, target = _write_project(tmp_path)
        monkeypatch.setattr(mrunner, "_run_baseline",
                            lambda root, tests_glob, timeout_s:
                            (1, DIRTY_TAIL, 10.0))
        monkeypatch.setattr(
            mrunner, "_run_gut_on_project",
            lambda root, timeout_s=600, tests_glob=None:
            (1, DIRTY_TAIL + "* test_new\n[Failed]\n"))
        r = mrunner.run_mutation(str(target), str(proj), budget=2,
                                 tests_glob="res://tests/")
        assert r["summary"].get("killed", 0) > 0


class TestExplicitBreak:
    def test_dirty_baseline_aborts_before_any_mutant_gut(self, tmp_path,
                                                          monkeypatch):
        proj, target = _write_project(tmp_path)
        calls = {"mutant_runs": 0}

        monkeypatch.setattr(mrunner, "_run_baseline",
                            lambda root, tests_glob, timeout_s:
                            (1, DIRTY_TAIL, 10.0))

        def fake_gut(root, timeout_s=600, tests_glob=None):
            calls["mutant_runs"] += 1
            return (0, "all passed")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake_gut)
        r = mrunner.run_mutation(str(target), str(proj), budget=2,
                                 tests_glob="res://tests/",
                                 break_on_baseline_failure=True)
        assert calls["mutant_runs"] == 0, "break flag must not run mutants"
        assert r["ok"] is False
        s = r["summary"]
        assert s.get("run_error") is True
        assert s.get("break_on_baseline_failure") is True
        assert s.get("baseline_failures") == 1
        assert s.get("baseline_failure_names") == ["test_pre_existing"]
        assert any("break-on-baseline-failure" in f["reason"]
                   for f in r["failures"])

    def test_clean_baseline_runs_normally_with_flag_on(self, tmp_path,
                                                       monkeypatch):
        proj, target = _write_project(tmp_path)
        monkeypatch.setattr(mrunner, "_run_baseline",
                            lambda root, tests_glob, timeout_s:
                            (0, CLEAN_TAIL, 10.0))
        monkeypatch.setattr(
            mrunner, "_run_gut_on_project",
            lambda root, timeout_s=600, tests_glob=None:
            (0, "all passed"))
        r = mrunner.run_mutation(str(target), str(proj), budget=2,
                                 tests_glob="res://tests/",
                                 break_on_baseline_failure=True)
        assert r["summary"].get("run_error") is None
        assert r["summary"].get("baseline_dirty") is False


class TestMergePassthrough:
    def test_baseline_disclosure_survives_chunk_merge(self):
        chunk = {"file": "/proj/a.gd", "total_sites": 1}
        chunk_result = {
            "tool": "mutation", "ok": True,
            "summary": {
                "file": "/proj/a.gd", "mutants": 1, "killed": 0,
                "survived": 1, "suspect": 0, "timeout": 0,
                "run_errors": 0, "invalid_mutants": 0,
                "kill_rate": 0.0, "budget": 1,
                "baseline_dirty": True, "baseline_failures": 1,
                "baseline_failure_names": ["test_pre_existing"],
                "break_on_baseline_failure": False,
            },
            "failures": [],
        }
        merged = merging._merge_results([chunk], [chunk_result], 1)
        s = merged["results"][0]["summary"]
        assert s.get("baseline_dirty") is True
        assert s.get("baseline_failures") == 1
        assert s.get("baseline_failure_names") == ["test_pre_existing"]
