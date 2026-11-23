"""BTIDY.1 特征化测试：`measurement.arms._arm_execute` 执行段行为钉死。

钉住：非 shadow 根树注入、分片扇出调用、失败扫描、finally 还原、返回
8 元组、worker 生命周期异常 → XDG 清理后重抛。拆分后断言不变。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_qa_toolkit.measurement import arms


def _stage(tmp_path: Path) -> dict:
    return {
        "run_uuid": "uuid1234", "backup_dir": tmp_path / "b",
        "started": {"files": {}},
        "sink_by_rel": {"a.gd": "sink"}, "manifest_by_rel": {"a.gd": [1]},
        "inst_by_rel": {"a.gd": "inst"}, "plans": [{"args": []}, {"args": []}],
        "reps": 1, "shadow_hook_files": [("rel", "txt")],
        "attribution_causal": False,
        "instrumented_files": {str(tmp_path / "a.gd"): "inst"},
    }


def _wire(monkeypatch, *, leg_raises=False):
    calls = {"inject": 0, "restore": 0, "scan": 0, "leg": 0}

    monkeypatch.setattr(arms, "_arm_inject_root",
                        lambda *a, **k: calls.__setitem__("inject", calls["inject"] + 1))
    monkeypatch.setattr(arms, "_arm_restore",
                        lambda *a, **k: calls.__setitem__("restore", calls["restore"] + 1))

    def fake_scan(results):
        calls["scan"] += 1
        return (True, False, False, set(), set())

    def fake_leg(ip, **kw):
        calls["leg"] += 1
        if leg_raises:
            raise RuntimeError("boom")
        return {"plan": ip[1], "reps": [], "shadow_disclosure": {}}

    monkeypatch.setattr(arms, "_arm_fail_scan", fake_scan)
    monkeypatch.setattr(arms, "_run_shard_leg", fake_leg)
    return calls


def test_arm_execute_default(tmp_path, monkeypatch):
    calls = _wire(monkeypatch)
    out = arms._arm_execute("gut_full", root=tmp_path, shadow_instrument=False,
                            timeout_s=10, adaptive_reps=False,
                            shard_log_dir=None, stage=_stage(tmp_path))
    assert calls["inject"] == 1 and calls["restore"] == 1
    assert calls["leg"] == 2 and calls["scan"] == 1
    results, gut_ok, run_error, timed_out, wall, xdg, af, ap = out
    assert gut_ok is True and run_error is False and timed_out is False
    assert len(results) == 2 and isinstance(wall, float)


def test_arm_execute_shadow_skips_inject(tmp_path, monkeypatch):
    calls = _wire(monkeypatch)
    arms._arm_execute("gherkin", root=tmp_path, shadow_instrument=True,
                      timeout_s=10, adaptive_reps=True, shard_log_dir=None,
                      stage=_stage(tmp_path))
    assert calls["inject"] == 0 and calls["restore"] == 1


def test_arm_execute_worker_error_restores(tmp_path, monkeypatch):
    calls = _wire(monkeypatch, leg_raises=True)
    with pytest.raises(RuntimeError):
        arms._arm_execute("gut_full", root=tmp_path, shadow_instrument=False,
                          timeout_s=10, adaptive_reps=False,
                          shard_log_dir=None, stage=_stage(tmp_path))
    # 异常仍走 finally 还原；且注入已发生
    assert calls["restore"] == 1 and calls["inject"] == 1
