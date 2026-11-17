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
    """SEE-1594 §SPEC-025：run_coverage 现在由行级映射投影驱动（零 GUT）。

    旧的直接插桩/GUT/sink 行为已退役；这些用例改为验证映射投影语义、
    阈值、零真实文件写入与 fail-loud。GUT/探针路径的旧用例在
    test_coverage_spec1312 等处保留为 shared-primitive 测试或标记 obsolete。
    """

    def _project(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / "utils").mkdir(parents=True)
        target = proj / "utils" / "t.gd"
        target.write_text(GD_SOURCE, encoding="utf-8")
        (proj / "tests").mkdir()
        (proj / "tests" / "test_x.gd").write_text("extends Node\n",
                                                  encoding="utf-8")
        return proj, target

    def _install_map(self, proj):
        import attribution_fixtures as fx
        fx.write_map(proj, {"utils/t.gd": ["test_x.gd::test_a"]})

    def _no_godot(self, monkeypatch):
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.inject.detect_binary_kind",
            lambda *a, **k: "linux")
        monkeypatch.setattr(
            "godot_qa_toolkit.mutation.identity.identity_pack",
            lambda *a, **k: {"kind": "linux"})

    def test_projection_above_threshold_ok(self, tmp_path, monkeypatch):
        proj, target = self._project(tmp_path)
        self._install_map(proj)
        self._no_godot(monkeypatch)
        # 映射夹具：covered={2}/inst={2,3} → 50%
        result = run_coverage(str(target), str(proj), min_percent=50.0)
        assert result["ok"] is True
        assert result["summary"]["coverage_percent"] == 50.0
        assert result["summary"]["coverage_source"] == "line_mapping_projection"

    def test_below_threshold_fails(self, tmp_path, monkeypatch):
        proj, target = self._project(tmp_path)
        self._install_map(proj)
        self._no_godot(monkeypatch)
        result = run_coverage(str(target), str(proj), min_percent=80.0)
        assert result["ok"] is False
        assert result["failures"]
        assert "below threshold" in result["failures"][0]["reason"]

    def test_zero_real_file_write(self, tmp_path, monkeypatch):
        proj, target = self._project(tmp_path)
        self._install_map(proj)
        self._no_godot(monkeypatch)
        before = target.read_bytes()
        run_coverage(str(target), str(proj))
        assert target.read_bytes() == before  # 新路径绝不写真实文件

    def test_no_map_fail_loud(self, tmp_path, monkeypatch):
        proj, target = self._project(tmp_path)  # 不写映射
        self._no_godot(monkeypatch)
        result = run_coverage(str(target), str(proj))
        assert result["ok"] is False
        assert result["summary"].get("run_error") is True

    def test_unified_contract_shape(self, tmp_path, monkeypatch):
        proj, target = self._project(tmp_path)
        self._install_map(proj)
        self._no_godot(monkeypatch)
        result = run_coverage(str(target), str(proj))
        assert set(result) >= {"tool", "ok", "summary", "failures"}
        assert result["tool"] == "measure.coverage"
        assert result["summary"]["probes_source"] == "map_arms_state"

    def test_uses_res_path_for_gut(self):
        from godot_qa_toolkit.coverage import runner as c
        assert c.GUT_SCRIPT_RES_PATH == "res://addons/gut/gut_cmdln.gd"

    def _fake_run_writing_hits(self, proj, ids, monkeypatch, captured):
        # SEE-1312: sink 为 ID-keyed 数据面 + run 唯一——fake 写 probe_id。
        # PR-B (§SPEC-025): sink is per-run UUID, unpredictable before the
        # run — a spy on _instrument captures this run's sink path at
        # instrumentation time, and the subprocess fake writes the ids into
        # exactly that sink (the old PID-name pre-write is gone).
        import godot_qa_toolkit.coverage.runner as c

        real_instrument = c._instrument

        def spy(src, sink_name):
            captured["sink"] = os.path.join(proj, sink_name)
            return real_instrument(src, sink_name)

        monkeypatch.setattr(c, "_instrument", spy)

        def fake(*args, **kwargs):
            with open(captured["sink"], "w") as f:
                for pid in sorted(ids):
                    f.write(f"{pid}\n")
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        monkeypatch.setattr(c.subprocess, "run", fake)
        return captured
