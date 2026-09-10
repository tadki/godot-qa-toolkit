"""Coverage runner unit tests (SEE-1268 M2, TDD)."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.coverage.runner import (
    _collect_executable_lines,
    run_coverage,
)

GD_SOURCE = """extends Node

func add(a: int, b: int) -> int:
	return a + b

func is_positive(a: int) -> bool:
	if a > 0:
		return true
	return false
"""


class TestCollectExecutableLines:
    def test_finds_return_and_if(self):
        lines = _collect_executable_lines(GD_SOURCE)
        nums = {l.line for l in lines}
        assert 4 in nums  # return a + b
        assert 7 in nums  # if a > 0 (if_branch first token)

    def test_positions_are_line_numbers(self):
        lines = _collect_executable_lines(GD_SOURCE)
        assert all(l.line > 0 for l in lines)

    def test_empty_source_has_no_lines(self):
        lines = _collect_executable_lines("extends Node\n")
        assert lines == []


class TestRunCoverage:
    def _write_project(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "calc.gd"
        target.write_text(GD_SOURCE)
        return proj, target

    def test_full_coverage_ok(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)
        # Simulate: hits file gets every executable line
        monkeypatch.setattr(
            "godot_qa_toolkit.coverage.runner.subprocess.run",
            self._fake_run_writing_hits(proj, {3, 4, 7, 8, 9}),
        )
        result = run_coverage(str(target), str(proj), min_percent=80.0)
        assert result["ok"] is True
        assert result["summary"]["coverage_percent"] == 100.0

    def test_below_threshold_fails(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)
        monkeypatch.setattr(
            "godot_qa_toolkit.coverage.runner.subprocess.run",
            self._fake_run_writing_hits(proj, {4}),
        )
        result = run_coverage(str(target), str(proj), min_percent=80.0)
        assert result["ok"] is False
        assert result["failures"]
        assert "below threshold" in result["failures"][0]["reason"]

    def test_original_file_restored(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)
        before = target.read_text()
        monkeypatch.setattr(
            "godot_qa_toolkit.coverage.runner.subprocess.run",
            self._fake_run_writing_hits(proj, set()),
        )
        run_coverage(str(target), str(proj))
        assert target.read_text() == before

    def test_gut_timeout_fails(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)

        def timeout_run(*a, **k):
            import subprocess
            raise subprocess.TimeoutExpired(cmd="godot", timeout=120)

        monkeypatch.setattr(
            "godot_qa_toolkit.coverage.runner.subprocess.run", timeout_run
        )
        result = run_coverage(str(target), str(proj))
        assert result["ok"] is False
        assert "timed out" in result["failures"][0]["reason"]

    def test_unified_contract_shape(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)
        monkeypatch.setattr(
            "godot_qa_toolkit.coverage.runner.subprocess.run",
            self._fake_run_writing_hits(proj, set()),
        )
        result = run_coverage(str(target), str(proj))
        assert set(result) >= {"tool", "ok", "summary", "failures"}
        assert result["tool"] == "coverage"

    def test_godot_launch_failure_is_run_error_not_zero_coverage(self, tmp_path, monkeypatch):
        # Revy QA FAIL (same root cause as mutation): a godot "File not found"
        # launch failure exits 1 with empty hits — the runner must report a
        # run_error (untrustworthy data), not a misleading 0.0% coverage.
        proj, target = self._write_project(tmp_path)

        def fake(*args, **kwargs):
            class R:
                returncode = 1
                stdout = ""
                stderr = "ERROR: Attempt to open script 'res://addons/gut/gut_cmdln.gd' resulted in error 'File not found'"
            return R()

        monkeypatch.setattr("godot_qa_toolkit.coverage.runner.subprocess.run", fake)
        result = run_coverage(str(target), str(proj))
        assert result["ok"] is False
        assert result["summary"].get("run_error") is True, result["summary"]
        assert any("run_error" in f or "godot" in f.get("reason", "").lower()
                   for f in result["failures"])

    def test_uses_res_path_for_gut(self):
        from godot_qa_toolkit.coverage import runner as c
        assert c.GUT_SCRIPT_RES_PATH == "res://addons/gut/gut_cmdln.gd"

    def _fake_run_writing_hits(self, proj, lines):
        def fake(*args, **kwargs):
            hits_file = os.path.join(proj, "qa-coverage-hits.txt")
            with open(hits_file, "w") as f:
                for ln in sorted(lines):
                    f.write(f"{ln}\n")
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        return fake
