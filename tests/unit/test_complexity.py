"""Unit tests for the complexity collector + gate (SEE-1268 M1, TDD)."""

from pathlib import Path

import pytest

from godot_qa_toolkit.complexity.collector import collect_file
from godot_qa_toolkit.complexity.gate import GateConfig, run_gate

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
