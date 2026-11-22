"""BTIDY.1 特征化测试：`measurement.arms._arm_result` 读回段契约钉死。

钉住：invalid_reason 优先级（reconciliation_failed > timeout > run_error）、
test_failures 形状、repeats/convergence/adaptive_rounds_used、vacuous 并入、
xdg 清理、shadow_disclosure 求和、bundles 计数。拆分后断言不变。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_qa_toolkit.measurement import arms


def _wire(monkeypatch):
    seen = {}

    def fake_hit(name, prepared, sink_by_rel, manifest_by_rel, xdg_homes,
                 root, run_uuid, gut_ok, run_error, timed_out, evidence_dir,
                 shard_log_dir):
        seen["hit"] = {"xdg": list(xdg_homes), "gut_ok": gut_ok}
        return {"a.gd": {"bundle": 1}}

    def fake_attr(enabled, root, run_uuid, name, sink_by_rel, xdg_homes,
                  prepared=None):
        seen["attr"] = enabled
        return {"arm_map": True} if enabled else None

    def fake_vac(name, prepared, vacuous_rels, run_uuid, gut_ok, run_error,
                 timed_out, evidence_dir):
        return {rel: {"vacuous": True} for rel in vacuous_rels}

    monkeypatch.setattr(arms, "_arm_hit_bundles", fake_hit)
    monkeypatch.setattr(arms, "_collect_attribution", fake_attr)
    monkeypatch.setattr(arms, "_arm_vacuous_bundles", fake_vac)
    return seen


def _call(tmp_path: Path, **over):
    xdg = tmp_path / "xdg1"
    xdg.mkdir()
    kw = dict(
        name="gut_full", root=tmp_path, prepared={"a.gd": {}},
        acceptance=None,
        arms_parallel=False, evidence_dir=None, shard_log_dir=None,
        timeout_s=10, adaptive_reps=False, test_attribution=True,
        results=[{"reps": [], "shadow_disclosure":
                  {"symlinks_dereferenced": 2, "dangling_links_removed": 1}}],
        run_uuid="u1", wall_s=3.0, gut_ok=True, run_error=False,
        timed_out=False, arm_failures={"test_a"}, arm_fail_pairs={("","test_a")},
        new_pairs={(("", "test_a"))}, exempt_pairs=set(),
        baseline_meta=None, reconciliation=None, arm_valid=True,
        invalid_reason=None, reps=1, plans=[{"args": []}],
        sink_by_rel={"a.gd": "s"}, manifest_by_rel={"a.gd": [1]},
        xdg_homes=[str(xdg)], vacuous_rels=["b.gd"])
    kw.update(over)
    return arms._arm_result(**kw), xdg


def test_arm_result_valid_shape(tmp_path, monkeypatch):
    seen = _wire(monkeypatch)
    doc, xdg = _call(tmp_path)
    assert doc["arm"] == "gut_full" and doc["state"] == "valid"
    assert doc["invalid_reason"] is None
    assert doc["test_failures"]["observed"] == ["test_a"]
    assert doc["test_failures"]["new"] == ["test_a"]
    assert doc["repeats"] == 1 and doc["convergence"] is None
    assert doc["adaptive_rounds_used"] is None  # adaptive_reps=False
    # vacuous 并入 bundles：a.gd(hit) + b.gd(vacuous)
    assert set(doc["bundles_data"]) == {"a.gd", "b.gd"} and doc["bundles"] == 2
    assert doc["shadow_disclosure"] == {"symlinks_dereferenced": 2,
                                        "dangling_links_removed": 1}
    # xdg 清理
    assert not xdg.exists()
    assert seen["hit"]["xdg"] == [str(xdg)] or seen["hit"]["xdg"] == []


def test_arm_result_invalid_reason_timeout(tmp_path, monkeypatch):
    _wire(monkeypatch)
    doc, _ = _call(tmp_path, gut_ok=False, timed_out=True, arm_valid=False,
                   run_error=True)
    assert doc["invalid_reason"] == "shard_timeout"  # §SPEC-PLANFIRST 细分
    assert doc["state"] == "invalid"


def test_arm_result_invalid_reason_run_error(tmp_path, monkeypatch):
    _wire(monkeypatch)
    doc, _ = _call(tmp_path, gut_ok=False, timed_out=False, arm_valid=False,
                   run_error=True)
    assert doc["invalid_reason"] == "run_error"


def test_arm_result_explicit_invalid_reason_wins(tmp_path, monkeypatch):
    _wire(monkeypatch)
    doc, _ = _call(tmp_path, gut_ok=False, timed_out=True, arm_valid=False,
                   run_error=True, invalid_reason="reconciliation_failed")
    assert doc["invalid_reason"] == "reconciliation_failed"


def test_arm_result_acceptance_convergence(tmp_path, monkeypatch):
    _wire(monkeypatch)
    from godot_qa_toolkit.measurement import orchestrator as orch
    monkeypatch.setattr(orch, "_acceptance_convergence",
                        lambda *a, **k: {"rounds_used": 2})
    doc, _ = _call(tmp_path, name="acceptance", reps=3, acceptance={"x": 1},
                   test_attribution=False, adaptive_reps=True,
                   results=[{"reps": [{}, {}], "shadow_disclosure": {}}])
    assert doc["convergence"] == {"rounds_used": 2}
    assert doc["adaptive_rounds_used"] == 2
