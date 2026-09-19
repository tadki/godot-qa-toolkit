"""Unit tests for the complexity collector + gate (SEE-1268 M1, TDD)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from godot_qa_toolkit.complexity.collector import collect_file
from godot_qa_toolkit.complexity.gate import GateConfig, run_gate

REPO = Path(__file__).parent.parent.parent
FIXTURE = Path(__file__).parent.parent / "fixtures" / "complex.gd"


class TestCollector:
    def test_function_level_complexities(self):
        funcs, error = collect_file(FIXTURE)
        assert error is None
        by_name = {f.name: f for f in funcs}
        assert by_name["simple"].complexity == 1
        assert by_name["complex"].complexity == 6

    def test_lines_and_files_recorded(self):
        funcs, _ = collect_file(FIXTURE)
        by_name = {f.name: f for f in funcs}
        assert all(f.file == str(FIXTURE) for f in by_name.values())
        # gd2py rewrites the source, so lineno is Python-level ordering, not
        # the original GDScript line — both start near the top of the file.
        assert 1 <= by_name["simple"].line < by_name["complex"].line <= len(
            FIXTURE.read_text().splitlines()
        )


class TestGateConfig:
    def test_rejects_max_below_warn(self):
        with pytest.raises(ValueError):
            GateConfig(warn_complexity=15, max_complexity=10)


class TestGate:
    def test_pass_below_thresholds(self):
        result = run_gate([str(FIXTURE)], GateConfig(warn_complexity=7, max_complexity=10))
        assert result["ok"] is True
        assert result["summary"]["violations"] == 0

    def test_fail_over_max(self):
        result = run_gate([str(FIXTURE)], GateConfig(warn_complexity=1, max_complexity=3))
        assert result["ok"] is False
        assert result["summary"]["violations"] == 1
        v = result["failures"][0]
        assert v["name"] == "complex"
        assert v["complexity"] == 6

    def test_warn_band_records_without_failing(self):
        result = run_gate([str(FIXTURE)], GateConfig(warn_complexity=3, max_complexity=10))
        assert result["ok"] is True
        assert result["summary"]["warnings"] == 1
        assert result["warnings"][0]["complexity"] == 6

    def test_unified_contract_shape(self):
        result = run_gate([str(FIXTURE)])
        assert set(result) >= {"tool", "ok", "summary", "failures"}
        assert result["tool"] == "complexity"


class TestComplexityPythonFiles:
    """SEE-1319 LOW-2：gqt complexity 对 .py 文件分流（不再恒 unparseable）。"""

    def _run_cli(self, *argv):
        proc = subprocess.run(
            [sys.executable, "-m", "godot_qa_toolkit.cli", *argv],
            capture_output=True, text=True, cwd=str(REPO / "src"),
        )
        start = proc.stdout.index("{")
        return proc.returncode, json.loads(proc.stdout[start:])

    def test_py_file_parses_not_unparseable(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text("def simple(a):\n    return a\n")
        rc, out = self._run_cli("complexity", str(f))
        assert out["summary"]["unparseable"] == 0, "Python 文件不应恒报 unparseable"
        assert out["summary"]["functions"] == 1

    def test_py_file_complexity_measured(self, tmp_path):
        f = tmp_path / "deep.py"
        f.write_text(
            "def deep(x):\n"
            "    if x > 0:\n        if x > 1:\n            if x > 2:\n"
            "                if x > 3:\n                    if x > 4:\n"
            "                        if x > 5:\n                            if x > 6:\n"
            "                                if x > 7:\n                                    if x > 8:\n"
            "                                        if x > 9:\n                                            if x > 10:\n"
            "                                                if x > 11:\n                                                    if x > 12:\n"
            "                                                        return 1\n"
            "    return 0\n"
        )
        rc, out = self._run_cli("complexity", "--warn", "5", "--max", "8", str(f))
        assert rc == 1, "超阈值 .py 文件必须命中 violations"
        assert out["summary"]["violations"] >= 1
        assert out["failures"][0]["complexity"] > 8

    def test_gd_file_behavior_unchanged(self):
        rc, out = self._run_cli("complexity", "--warn", "7", "--max", "10",
                                str(REPO / "tests" / "fixtures" / "complex.gd"))
        assert out["summary"]["unparseable"] == 0
        assert out["summary"]["functions"] == 2

    def test_py_syntax_error_reports_unparseable_not_crash(self, tmp_path):
        f = tmp_path / "broken.py"
        f.write_text("def broken(\n")
        rc, out = self._run_cli("complexity", str(f))
        assert out["summary"]["unparseable"] == 1
        assert "python ast" in out["failures"][0]["reason"] or "SyntaxError" in out["failures"][0]["reason"]

    def test_directory_mixed_gd_and_py_collected(self, tmp_path):
        (tmp_path / "a.gd").write_text("extends Node\nfunc f():\n\tpass\n")
        (tmp_path / "b.py").write_text("def g():\n    pass\n")
        rc, out = self._run_cli("complexity", str(tmp_path))
        assert out["summary"]["functions"] >= 2
        assert out["summary"]["unparseable"] == 0
