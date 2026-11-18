"""SEE-1594 S6.2/S6.3：基线门控与懒获取反例。

- 6a/6b（基线内失败→臂有效 / 新增失败→invalid 具名）由
  test_canonical_report_see1546.py 既有用例钉住；产品路径端到端在
  test_see1594_s63_lazy_baseline.py；本文件补：
- 6c：基线采集失败（无 GUT 运行流 / 启动失败）→ fail-loud，不得按
  空基线静默继续（产品 helper `_acquire_lazy_baseline`）。
- 6d：元数据形状契约——懒获取 meta 形状、混合形态解析、增量产物
  携带 baseline_failures_meta。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

_GUT_OK_OUTPUT = (
    "Godot Engine v4.4.1.stable.official\n"
    "res://tests/unit/test_game.gd\n"
    "  * test_already_red\n"
    "\t\ttest_already_red [Failed]\n"
    "  * test_green\n"
    "\t\ttest_green [Passed]\n"
    "= Run Summary\n"
    "==============================================\n"
    "res://tests/unit/test_game.gd\n"
    "- test_already_red\n"
    "    [Failed]:  Expected one thing to be another\n"
)
# 同一失败的两种披露形态：运行流归因对 + Run Summary 未归因回显
# （真实 GUT 格式：- 名单行 + 下一行 [Failed]: 详情）。


def _fake_gut(rc: int, out: str):
    return lambda root, args, timeout: (
        SimpleNamespace(returncode=rc, stdout=out, stderr=""), 0.0)


class TestS62LazyAcquireBaseline:
    def test_no_run_stream_fails_loud(self, monkeypatch, tmp_path):
        """6c：无 GUT 运行流（rc 异常且无失败清单）→ MeasurementError
        fail-loud，不得按空基线静默继续。"""
        from godot_qa_toolkit.measurement import orchestrator as orch
        monkeypatch.setattr(orch, "_run_one_gut", _fake_gut(
            1, "Godot Engine v4.4.1 — nothing ran"))
        with pytest.raises(orch.MeasurementError, match="no run stream"):
            orch._acquire_lazy_baseline(tmp_path, ["-gdir=res://tests"], 60)

    def test_launch_failure_fails_loud(self, monkeypatch, tmp_path):
        """6c：引擎启动失败 → fail-loud，不得静默返回空基线。"""
        from godot_qa_toolkit.measurement import orchestrator as orch
        monkeypatch.setattr(orch, "_run_one_gut", _fake_gut(
            255, "Failed to load resource: res://project.godot"))
        with pytest.raises(orch.MeasurementError, match="fail-loud"):
            orch._acquire_lazy_baseline(tmp_path, [], 60)

    def test_meta_shape_contract(self, monkeypatch, tmp_path):
        """6d：meta 形状 = {method, lazy, face, pairs, count, sha256,
        wall_s}；pairs 为 (script, test) 字符串对；count == len(pairs)。"""
        from godot_qa_toolkit.measurement import orchestrator as orch
        monkeypatch.setattr(orch, "_run_one_gut", _fake_gut(
            1, _GUT_OK_OUTPUT))
        pairs, meta = orch._acquire_lazy_baseline(
            tmp_path, ["-gdir=res://tests"], 60)
        assert set(meta) == {"method", "lazy", "face", "pairs", "count",
                             "sha256", "wall_s"}
        assert meta["method"] == "uninstrumented_run"
        assert meta["lazy"] is True
        assert meta["face"] == "res://tests"
        # 混合形态：1 个归因对 + 1 个 Run Summary 未归因回显
        assert meta["count"] == len(pairs) == 2
        assert ("res://tests/unit/test_game.gd", "test_already_red") in pairs
        assert ("", "test_already_red") in pairs
        assert all(isinstance(x, str) for p in meta["pairs"] for x in p)
        assert isinstance(meta["sha256"], str) and len(meta["sha256"]) == 64

    def test_face_default_when_no_gdir(self, monkeypatch, tmp_path):
        from godot_qa_toolkit.measurement import orchestrator as orch
        monkeypatch.setattr(orch, "_run_one_gut", _fake_gut(0, "[Passed]"))
        _pairs, meta = orch._acquire_lazy_baseline(tmp_path, [], 60)
        assert meta["face"] == "gutconfig_default"

    def test_parser_parity_with_arm_observation(self):
        """基线解析与臂观测同一解析器（§SPEC-063c/064a 混合形态逐对可比）。"""
        from godot_qa_toolkit.measurement.orchestrator import (
    _parse_failing_pairs,
)
        pairs = _parse_failing_pairs(_GUT_OK_OUTPUT)
        assert ("res://tests/unit/test_game.gd", "test_already_red") in pairs
        assert ("", "test_already_red") in pairs

    def test_gutconfig_face_args_merged(self):
        """同面参数 = recipe gut_face dirs 并集（与分片计划同源）。"""
        from godot_qa_toolkit.measurement.orchestrator import (
    _gutconfig_face_args,
)
        assert _gutconfig_face_args(
            {"gut_face": {"dirs": ["res://tests/a", "res://tests/b"]}}) \
            == ["-gdir=res://tests/a,res://tests/b"]
        assert _gutconfig_face_args({"gut_face": {"dirs": []}}) == []
        assert _gutconfig_face_args({}) == []


class TestS62IncrementCarry:
    def test_increment_artifact_carries_baseline_meta(self):
        """6d：增量产物携带 full build 写入的 baseline_failures_meta
        （披露随映射走，不因增量刷新丢失）。"""
        from godot_qa_toolkit.measurement.increment import (
            _build_increment_artifact)
        meta = {"method": "uninstrumented_run", "face": "res://tests",
                "count": 53, "sha256": "a" * 64, "wall_s": 1.0}
        baseline = {"recipe_hash_pristine": "rh", "identity": {},
                    "arms_state": {}, "notes": [],
                    "baseline_failures_meta": meta}
        artifact = _build_increment_artifact(
            baseline, {}, {"recipe_hash": "rh2"}, Path("/tmp/kol"), {},
            [], {"dropped": [], "sources": {}}, 0, 1.0)
        assert artifact["baseline_failures_meta"] == meta

    def test_increment_without_meta_has_none(self):
        from godot_qa_toolkit.measurement.increment import (
            _build_increment_artifact)
        baseline = {"recipe_hash_pristine": "rh", "identity": {},
                    "arms_state": {}, "notes": []}
        artifact = _build_increment_artifact(
            baseline, {}, {"recipe_hash": "rh2"}, Path("/tmp/kol"), {},
            [], {"dropped": [], "sources": {}}, 0, 1.0)
        assert artifact["baseline_failures_meta"] is None
