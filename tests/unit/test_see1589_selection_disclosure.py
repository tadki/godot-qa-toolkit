"""SEE-1589 §SPEC-007（A-M4/M5/L1/L5）：selection_mode 全档 + 并发上限披露。

判据：
- 有效选面恒可判别：line / fn / file / full / `--lines`(lines) /
  `--mutation`(mutation)；
- 显式 `--tests` 标为接管（explicit_tests），不得仍报 line；
- 行级并发被签名组数钳制时，输出披露 `jobs_limit_reason=selection_groups`
  与 `selection_groups`。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation.executor import run_mutation_files
from godot_qa_toolkit.mutation.testscan import (concurrency_disclosure,
                                                resolve_selection_mode)
import godot_qa_toolkit.mutation.runner as runner_mod

GD_TARGET = ("func add(a, b):\n"
             "\tif a > b:\n"
             "\t\treturn a - b\n"
             "\treturn a + b\n")


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True, exist_ok=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    save = proj / "tests" / "unit" / "save"
    save.mkdir(parents=True, exist_ok=True)
    (save / "test_save.gd").write_text('preload("res://calc.gd")\n')
    target = proj / "calc.gd"
    target.write_text(GD_TARGET)
    return str(proj), str(target)


def _install_fakes(monkeypatch):
    monkeypatch.setattr(runner_mod, "_run_gut_on_project",
                        lambda root, timeout_s=60, tests_glob=None:
                            (1, "* test_killed\n\t[Failed]\n"))
    monkeypatch.setattr(runner_mod, "_run_baseline",
                        lambda root, glob, timeout_s:
                            (0, "OK. All tests passed", 1.5))
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})


class TestResolveSelectionMode:
    def test_modes(self):
        assert resolve_selection_mode("line", {}) == "line"
        assert resolve_selection_mode("fn", {}) == "fn"
        assert resolve_selection_mode("file", {}) == "file"
        assert resolve_selection_mode("full", {}) == "full"

    def test_caliper(self):
        assert resolve_selection_mode("line", {}, select_ids=["x#1"]) \
            == "mutation"
        assert resolve_selection_mode("line", {}, select_lines=[13, 14]) \
            == "lines"

    def test_explicit_tests_overrides_line(self):
        meta = {"source": "explicit_tests", "tests_glob": "res://t/"}
        assert resolve_selection_mode("line", meta) == "explicit_tests"

    def test_unknown_mode_fails_loud(self):
        try:
            resolve_selection_mode("bogus", {})
        except ValueError as e:
            assert "bogus" in str(e)
        else:  # pragma: no cover
            raise AssertionError("未知选面必须 fail-loud")


class TestConcurrencyDisclosure:
    def test_groups_cap_disclosed(self):
        d = concurrency_disclosure("line", groups=2, jobs=16)
        assert d == {"jobs": 2, "selection_groups": 2,
                     "jobs_limit_reason": "selection_groups"}

    def test_no_cap_when_groups_cover_jobs(self):
        assert concurrency_disclosure("line", groups=16, jobs=16) == {}

    def test_non_grouped_mode_not_capped(self):
        assert concurrency_disclosure("file", groups=2, jobs=16) == {}


class TestRuntimeDisclosure:
    def test_explicit_tests_reports_override(self, tmp_path, monkeypatch):
        proj, _target = _write_project(tmp_path)
        _install_fakes(monkeypatch)
        env = run_mutation_files(["calc.gd"], proj, budget=2,
                                 tests_glob="res://tests/unit/save/",
                                 select_mode="line")
        summary = env["results"][0]["summary"]
        assert summary["selection_mode"] == "explicit_tests", summary.get(
            "selection_mode")
        assert summary.get("test_selection", {}).get("source") == \
            "explicit_tests"

    def test_full_mode_reports_full(self, tmp_path, monkeypatch):
        proj, _target = _write_project(tmp_path)
        _install_fakes(monkeypatch)
        env = run_mutation_files(["calc.gd"], proj, budget=2,
                                 select_mode="full")
        summary = env["results"][0]["summary"]
        assert summary["selection_mode"] == "full"

    def test_line_groups_cap_disclosed(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        _install_fakes(monkeypatch)
        env = run_mutation_files(["calc.gd"], proj, budget=6, jobs=8)
        summary = env["results"][0]["summary"]
        groups = summary.get("selection_groups")
        if groups and groups < 8:
            assert summary.get("jobs_limit_reason") == "selection_groups"
            assert summary.get("jobs") == groups
