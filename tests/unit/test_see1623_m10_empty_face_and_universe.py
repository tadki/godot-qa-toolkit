"""SEE-1623 M10 — 空测试面哨兵 + 映射 universe 缺目标行为钉。

根因（SEE-1622 Refacty F3/F5，组8 定向复核翻转确认）：
- 零测试面（tests 目录存在但 0 测试）→ gut_full 臂自报 state=valid
  且无具名原因（唯一线索 attribution_usable=false）；
- 预存映射 universe 不含目标 → coverage 硬失败且先白跑一次 gut_full
  臂（unknown_changed_paths 必失败），错误前缀双写；无映射直跑同目标
  却成功。

行为契约：空测试面 → 臂具名 invalid（empty_test_face）；universe 缺
目标 → 零 GUT 具名失败（target_not_in_map_universe，指向重建路径）；
错误信封单前缀（F4）。
"""

from __future__ import annotations

import json
import subprocess as sp
from pathlib import Path

import pytest

import test_see1594_s63_lazy_baseline as s63
from godot_qa_toolkit.mutation.truth_select import (
    MutationSelectionError,
    resolve_selection,
)


class TestEmptyTestFaceSentinel:
    def test_zero_key_arm_invalid_named(self, tmp_path, monkeypatch):
        """gut_full 臂选面 0 个测试键 → 具名 invalid（empty_test_face），
        不再自报 valid 无原因（F3：零执行证据不可能成立）。"""
        from godot_qa_toolkit.measurement import arms as arms_mod

        stage = {"run_uuid": "u", "plans": [{"keys": []}], "reps": 1,
                 "sink_by_rel": {}, "manifest_by_rel": {},
                 "vacuous_rels": [], "backup_dir": None, "started": None,
                 "inst_by_rel": {}, "instrumented_files": [],
                 "shadow_hook_files": [], "attribution_causal": False}

        def fake_execute(name, **k):
            return ([], True, False, False, 0.0, [], set(), set(), [], [])

        def fake_stage(name, **k):
            return stage

        monkeypatch.setattr(arms_mod, "_arm_execute", fake_execute)
        monkeypatch.setattr(arms_mod, "_arm_stage", fake_stage)
        monkeypatch.setattr(arms_mod, "_arm_reconcile",
                            lambda *a, **k: (None, True, None, False, False))
        monkeypatch.setattr(
            arms_mod, "_arm_readback",
            lambda *a, **k: (None, None, {}))

        class _Slot:
            mode = "fresh"
            freshness: list = []
            farm: dict = {}

        arm = arms_mod.run_arm(
            "gut_full", root=tmp_path, prepared={}, recipe={},
            timeout_s=60)
        assert arm["state"] == "invalid"
        assert arm["invalid_reason"] == "empty_test_face"
        assert "0 test keys" in (arm.get("invalid_evidence") or {})["note"]


class TestTargetNotInMapUniverse:
    def test_unbound_target_named_before_any_arms(self, tmp_path,
                                                  monkeypatch):
        """预存映射 universe 不含目标 → 具名 MutationSelectionError 且
        零测量臂（不白跑）；重建指引随 detail 出信封。"""
        from attribution_fixtures import write_map

        root = tmp_path / "proj"
        root.mkdir()
        (root / "src").mkdir()
        (root / "src" / "a.gd").write_text("extends RefCounted\n")
        (root / "src" / "other.gd").write_text("extends RefCounted\n")
        write_map(root, {"src/a.gd": []})
        monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
        from godot_qa_toolkit.mutation import truth_select as ts
        ts._fresh_recipe_hash.cache_clear()

        def _boom(*a, **k):
            raise AssertionError("measurement arm must not run for an "
                                 "unbound target")

        monkeypatch.setattr(
            "godot_qa_toolkit.measurement.orchestrator.run_measurement",
            _boom)
        with pytest.raises(MutationSelectionError) as ei:
            resolve_selection(str(root), str(root / "src" / "other.gd"))
        assert ei.value.reason == "target_not_in_map_universe"
        assert "does not bind" in str(ei.value)
        assert "map-update" in str(ei.value)


class TestSinglePrefixEnvelope:
    def test_stale_error_detail_not_double_prefixed(self, tmp_path,
                                                    monkeypatch):
        """stale 错误信封单前缀（F4：reason 不因 str(e) 再拼一次）。"""
        from godot_qa_toolkit.coverage import mapping as cm

        err = cm.CoverageMappingError("some_reason", "detail bits")
        assert str(err) == "some_reason: detail bits"
        assert err.reason == "some_reason"
