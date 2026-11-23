"""BTIDY.1 特征化测试：`measurement.orchestrator.run_measurement` 编排装配。

钉住：recipe 非法 / 无目标 fail-loud；setup→stage→arms→unions→crap→perf
→attr→assemble 的参数流向（recipe_hash 取注入后值、pristine 排除
recipe_hash、algo_used 由 block_compress/test_attribution 派生、evidence
dir 创建、recovered/complete 透传、attr_section 合并）。拆分后断言不变。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_qa_toolkit.measurement import orchestrator as orch
from godot_qa_toolkit.measurement.backup import MeasurementError


def _wire(monkeypatch, tmp_path):
    seen = {}
    # SEE-1623 H4：本文件 fake 的是双臂 execute_arms 装配——acceptance
    # 默认暂停契约下编排走单臂 run_arm，须显式恢复路径才触达该 fake。
    monkeypatch.setenv("GQT_MEASUREMENT_ACCEPTANCE_DISABLED", "0")
    monkeypatch.setattr(orch, "verify_recipe", lambda r: (True, None))

    def fake_inject(root, recipe):
        out = dict(recipe)
        out["recipe_hash"] = "INJECTED"
        return out

    monkeypatch.setattr(orch, "_inject_toolchain", fake_inject)
    monkeypatch.setattr(orch, "_parse_baseline_failures", lambda b: set())
    monkeypatch.setattr(orch, "_parse_intolerant_config",
                        lambda c: ([], set(), set(), []))
    monkeypatch.setattr(orch, "_prepare_targets",
                        lambda root, targets, **k: {
                            "prepared": {"a.gd": {"src": "s"}},
                            "unmeasured": [], "causal_sites": [],
                            "cache_hits": 1, "cache_misses": 2})
    monkeypatch.setattr(orch, "_scope_complete", lambda s, t: True)
    monkeypatch.setattr(orch, "_recover_stale_backups", lambda root: 3)
    monkeypatch.setattr(orch, "execute_arms",
                        lambda **k: ({"arm": "gut_full"},
                                     {"arm": "acceptance"}))
    monkeypatch.setattr(orch, "_build_unions",
                        lambda a, b, rh: [{"valid": True, "file": "res://a.gd"}])
    monkeypatch.setattr(orch, "_gate_comparable",
                        lambda u, a, b, c: ({}, None))
    monkeypatch.setattr(orch, "_crap_records_for_report",
                        lambda *a: ([], {"lane": 0}, "crap-v"))
    monkeypatch.setattr(orch, "_attach_limitation_reasons", lambda *a: None)
    monkeypatch.setattr(orch, "_measurable_stats", lambda u: ([], 1, 1))
    monkeypatch.setattr(orch, "_performance", lambda *a: {"perf": True})
    monkeypatch.setattr(orch, "_tests_face_map", lambda root: {})
    monkeypatch.setattr(orch, "_attribution_section",
                        lambda *a, **k: {"test_attribution": {"schema": "x"}})

    def fake_assemble(*a, **k):
        seen["assemble_args"] = a
        return {"tool": "measure.run", "ok": True, "summary": {}, "failures": []}

    monkeypatch.setattr(orch, "_assemble_report", fake_assemble)

    def fake_evidence(out):
        d = tmp_path / "ev"
        seen["evidence"] = d
        return d

    monkeypatch.setattr(orch, "evidence_dir_for", fake_evidence)
    return seen


def test_run_measurement_contract_flow(tmp_path, monkeypatch):
    seen = _wire(monkeypatch, tmp_path)
    recipe = {"recipe_version": "measurement-suite/2", "gut_face": {"dirs": []},
              "recipe_hash": "OLD"}
    report = orch.run_measurement(
        tmp_path, ["a.gd"], recipe, evidence_out=str(tmp_path / "evout"),
        probe_keys=["k"], block_compress=True, test_attribution=False,
        attribution_causal=False, arms_parallel=False)
    # attr_section 合并进报告
    assert report["test_attribution"] == {"schema": "x"}
    assert report["tool"] == "measure.run"
    # evidence dir 被创建
    assert seen["evidence"].is_dir()
    # assemble 收到注入后 recipe_hash（"INJECTED" 而非 "OLD"）
    args = seen["assemble_args"]
    assert "INJECTED" in args


def test_run_measurement_invalid_recipe_fails_loud(tmp_path, monkeypatch):
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(orch, "verify_recipe", lambda r: (False, "bad"))
    with pytest.raises(MeasurementError):
        orch.run_measurement(tmp_path, ["a.gd"], {})


def test_run_measurement_no_targets_fails_loud(tmp_path, monkeypatch):
    _wire(monkeypatch, tmp_path)
    with pytest.raises(MeasurementError):
        orch.run_measurement(tmp_path, [], {})
