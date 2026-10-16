"""§SPEC-1420-12：P2 coverage 前置过滤 + `--reuse-coverage`（定案 L3/D7）。

复用自家 JSON 落盘（不引 LCOV）+ provenance 校验；covered 位点 verdict 与
全量跑一致 + uncovered 全部显式标记（不进 ok 门禁、不消耗预算）；P3 卡尺
（--mutation/--lines）绕过前置过滤——随机抽查被剔位点防假阴性的通道。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.coverlink import (
    load_reusable_coverage,
    save_coverage,
)

SRC = ("func add(a, b):\n"
       "\treturn a + b\n"
       "\n"
       "func sub(a, b):\n"
       "\treturn a - b\n")

KILL_TAIL = "* test_x\n\t\ttest_x [Failed]"


def _write_project(tmp_path, src=SRC):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(src)
    return proj, target


class TestCoverageCacheProvenance:
    def test_save_and_load_roundtrip(self, tmp_path):
        proj, target = _write_project(tmp_path)
        assert save_coverage({2, 3, 5}, 6, str(proj), str(target),
                             "res://tests/") is True
        lines, reason = load_reusable_coverage(str(proj), str(target),
                                               "res://tests/")
        assert lines == {2, 3, 5}
        assert reason is None

    def test_source_change_invalidates(self, tmp_path):
        proj, target = _write_project(tmp_path)
        save_coverage({2, 3}, 6, str(proj), str(target), "res://tests/")
        target.write_text(SRC + "\n# touched\n")
        lines, reason = load_reusable_coverage(str(proj), str(target),
                                               "res://tests/")
        assert lines is None
        assert "source changed" in reason

    def test_glob_mismatch_invalidates(self, tmp_path):
        proj, target = _write_project(tmp_path)
        save_coverage({2}, 6, str(proj), str(target), "res://tests/")
        lines, reason = load_reusable_coverage(str(proj), str(target),
                                               "res://tests/unit/")
        assert lines is None
        assert "tests_glob" in reason

    def test_missing_cache_degrades_with_reason(self, tmp_path):
        proj, target = _write_project(tmp_path)
        lines, reason = load_reusable_coverage(str(proj), str(target),
                                               "res://tests/")
        assert lines is None
        assert "no cached coverage" in reason


class TestCoveragePrefilter:
    def test_uncovered_sites_marked_and_not_executed(self, tmp_path,
                                                     monkeypatch):
        proj, target = _write_project(tmp_path)
        # 行 2（add 体）覆盖；行 5（sub 体）未覆盖；位点行全部可插桩（D3）
        save_coverage({2}, 6, str(proj), str(target), "res://tests/",
                      instrumentable_lines={2, 5})
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "baseline ok")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob="res://tests/",
                                 reuse_coverage=True)
        uncovered = [f for f in r["failures"] if f["verdict"] == "uncovered"]
        assert len(uncovered) == 3  # sub 的 3 个位点全部显式标记
        assert all(f["line"] == 5 for f in uncovered)
        # covered 位点照常执行
        assert r["summary"]["coverage_prefilter"] == {
            "uncovered": 3, "covered_executed": 3}
        assert r["summary"]["killed"] == 3

    def test_uncovered_not_in_ok_gate(self, tmp_path, monkeypatch):
        """uncovered 不计入 ok 门禁失败档——quality 信号非工具失败。"""
        proj, target = _write_project(tmp_path)
        save_coverage({2}, 6, str(proj), str(target), "res://tests/",
                      instrumentable_lines={2, 5})

        state = {"n": 0}

        def kill_all(root, timeout_s=60, tests_glob=None):
            state["n"] += 1
            if state["n"] == 1:
                return (0, "baseline ok")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", kill_all)
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob="res://tests/",
                                 reuse_coverage=True)
        assert r["ok"] is True  # covered 全 killed + uncovered 显式跳过

    def test_store_miss_runs_unfiltered_with_disclosure(self, tmp_path,
                                                        monkeypatch,
                                                        capsys):
        proj, target = _write_project(tmp_path)

        state = {"n": 0}

        def kill_all(root, timeout_s=60, tests_glob=None):
            state["n"] += 1
            if state["n"] == 1:
                return (0, "baseline ok")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", kill_all)
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob="res://tests/",
                                 reuse_coverage=True)
        assert r["summary"]["coverage_prefilter"] == "store_miss"
        assert "coverage store miss" in capsys.readouterr().err
        assert r["summary"]["killed"] == 6  # 未过滤

    def test_prefilter_before_budget(self, tmp_path, monkeypatch):
        """预算只花在 covered 位点上——uncovered 不消耗预算（前置语义）。"""
        src = ("func one(a):\n"
               "\treturn a + 1\n"
               "\n"
               "func two(a):\n"
               "\treturn a + 2\n")
        proj, target = _write_project(tmp_path, src)
        # 只覆盖 two 体（行 5）；one（行 2）uncovered；位点行全可插桩（D3）
        save_coverage({5}, 6, str(proj), str(target), "res://tests/",
                      instrumentable_lines={2, 5})
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "baseline ok")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = mrunner.run_mutation(str(target), str(proj), budget=2,
                                 tests_glob="res://tests/",
                                 reuse_coverage=True)
        s = r["summary"]
        # one 的 5 位点 uncovered 显式标记；two 的 3 位点 covered，budget=2 截断
        assert s["coverage_prefilter"] == {"uncovered": 5,
                                           "covered_executed": 2}
        assert s["executed"] == 2
        uncovered = [f for f in r["failures"] if f["verdict"] == "uncovered"]
        assert len(uncovered) == 5


class TestCoveredVerdictConsistency:
    def test_covered_verdicts_match_full_run(self, tmp_path, monkeypatch):
        """§12 验收口径：covered 位点 verdict 集合与全量跑完全一致。"""
        proj, target = _write_project(tmp_path)

        def mk_kill_all():
            state = {"n": 0}

            def kill_all(root, timeout_s=60, tests_glob=None):
                state["n"] += 1
                if state["n"] == 1:
                    return (0, "baseline ok")
                return (1, KILL_TAIL)

            return kill_all

        monkeypatch.setattr(mrunner, "_run_gut_on_project", mk_kill_all())
        full = mrunner.run_mutation(str(target), str(proj), budget=10,
                                    tests_glob="res://tests/",
                                    mutate_all=True)
        assert full["summary"]["killed"] == 6
        # 只覆盖 add 体（行 2）→ covered 位点 = 3；位点行全可插桩（D3）
        save_coverage({2}, 6, str(proj), str(target), "res://tests/",
                      instrumentable_lines={2, 5})
        monkeypatch.setattr(mrunner, "_run_gut_on_project", mk_kill_all())
        filtered = mrunner.run_mutation(str(target), str(proj), budget=10,
                                        tests_glob="res://tests/",
                                        mutate_all=True, reuse_coverage=True)
        covered_ids = [f["id"] for f in full["failures"]]  # 全 killed → 空
        key = lambda r: (r["summary"]["killed"], r["summary"]["survived"],
                         r["ok"])  # noqa: E731
        # covered 位点（3 个）与全量跑同口径 killed；uncovered 3 显式标记
        assert filtered["summary"]["killed"] == 3
        assert filtered["summary"]["coverage_prefilter"]["uncovered"] == 3
        assert filtered["ok"] is True and full["ok"] is True


class TestCaliperBypassesPrefilter:
    def test_selection_reruns_uncovered_site(self, tmp_path, monkeypatch):
        """P3 卡尺随机抽查被剔位点防假阴性：显式寻址绕过 coverage 前置。"""
        proj, target = _write_project(tmp_path)
        save_coverage({2}, 6, str(proj), str(target), "res://tests/")
        sid = f"{target}#sub#1"
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "baseline ok")
            return (1, KILL_TAIL)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob="res://tests/",
                                 select_ids=[sid], reuse_coverage=True)
        # uncovered 位点仍被强制执行（1 baseline + 1 mutant）
        assert calls["n"] == 2
        assert r["summary"]["executed"] == 1
        assert r["ok"] is True
