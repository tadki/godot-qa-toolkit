"""SEE-1594 S6.2：映射构建链接入 baseline_failures 门控反例。

- 6a/6b（基线内失败→臂有效 / 新增失败→invalid 具名）由
  test_canonical_report_see1546.py 既有用例钉住；本文件补：
- 6c：基线采集失败（无 GUT 运行流）→ fail-loud，不得按空基线静默继续。
- 6d：元数据形状契约——_acquire_baseline meta 形状、harness 解析器
  与测量侧逐对可比（同一解析器）、增量产物携带 baseline_failures_meta。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

_HARNESS = (Path(__file__).resolve().parents[1] / "integration"
            / "run_see1594_mapping_perf.py")


def _load_harness():
    spec = importlib.util.spec_from_file_location("see1594_mapping_perf",
                                                  _HARNESS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


class TestS62AcquireBaseline:
    def test_no_run_stream_fails_loud(self, monkeypatch):
        """6c：无 GUT 运行流（rc 异常且无失败清单）→ fail-loud 错误串。"""
        mp = _load_harness()
        monkeypatch.setattr(mp, "_run_uninstr_gut",
                            lambda root, timeout, select=None:
                            (1, "Godot Engine v4.4.1 — nothing ran"))
        pairs, err = mp._acquire_baseline(Path("/tmp/kol"), 60)
        assert pairs == set()
        assert isinstance(err, str)
        assert "fail-loud" in err and "no run stream" in err

    def test_launch_failure_fails_loud(self, monkeypatch):
        """6c：引擎启动失败 → fail-loud，不得静默返回空基线。"""
        mp = _load_harness()
        monkeypatch.setattr(
            mp, "_run_uninstr_gut",
            lambda root, timeout, select=None:
            (255, "Failed to load resource: res://project.godot"))
        pairs, err = mp._acquire_baseline(Path("/tmp/kol"), 60)
        assert pairs == set()
        assert isinstance(err, str) and "fail-loud" in err

    def test_meta_shape_contract(self, monkeypatch):
        """6d：meta 形状 = {method, face, pairs, count, sha256, wall_s}；
        pairs 为 (script, test) 字符串对；count == len(pairs)。"""
        mp = _load_harness()
        monkeypatch.setattr(mp, "_run_uninstr_gut",
                            lambda root, timeout, select=None:
                            (1, _GUT_OK_OUTPUT))
        pairs, meta = mp._acquire_baseline(Path("/tmp/kol"), 60,
                                           face="res://tests")
        assert not isinstance(meta, str)
        assert set(meta) == {"method", "face", "pairs", "count",
                             "sha256", "wall_s"}
        assert meta["method"] == "uninstrumented_run"
        assert meta["face"] == "res://tests"
        # 混合形态：1 个归因对 + 1 个 Run Summary 未归因回显
        assert meta["count"] == len(pairs) == 2
        assert ("res://tests/unit/test_game.gd", "test_already_red") in pairs
        assert ("", "test_already_red") in pairs
        assert all(isinstance(x, str) for p in meta["pairs"] for x in p)
        assert isinstance(meta["sha256"], str) and len(meta["sha256"]) == 64

    def test_parser_parity_with_arm_observation(self):
        """基线解析与臂观测同一解析器（§SPEC-063c/064a 混合形态逐对可比）：
        归因对 + Run Summary 未归因回显都在集合内。"""
        from godot_qa_toolkit.measurement.orchestrator import (
            _parse_failing_pairs)
        mp = _load_harness()
        pairs = mp._pairs_from_output(_GUT_OK_OUTPUT)
        assert pairs == _parse_failing_pairs(_GUT_OK_OUTPUT)
        assert ("res://tests/unit/test_game.gd", "test_already_red") in pairs
        assert ("", "test_already_red") in pairs


class TestS62IncrementCarry:
    def test_increment_artifact_carries_baseline_meta(self):
        """6d：增量产物携带 full build 注入的 baseline_failures_meta
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


# 6a/6b（基线内失败→臂有效转绿 / 新增失败→invalid 具名）端到端由
# test_canonical_report_see1546.py::test_baseline_failures_absorb_preexisting_failures
# 及其前置用例（FakeGodot 设施）钉住，此处不重复端到端架设。
