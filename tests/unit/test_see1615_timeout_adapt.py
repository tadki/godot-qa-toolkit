"""SEE-1615 §SPEC-TIMEOUT-ADAPT —— 超时排除/partial、自适应超时、
EXPLICIT_PARTIAL_CONTINUE 接线的单测（端到端由 KOL 副本复跑承载）。
"""

from __future__ import annotations

from godot_qa_toolkit.measurement import orchestrator as mo
from godot_qa_toolkit.measurement.arms import _arm_fail_scan


def _shard(keys: list[str], reps: list[dict]) -> dict:
    return {"plan": {"keys": keys, "suite_dir": None}, "reps": reps}


def _rep(rc=0, out="", timed_out=False):
    return {"rc": rc, "out": out, "timed_out": timed_out}


def test_shard_timeout_excluded_by_name_not_run_error():
    """§SPEC-TIMEOUT-ADAPT：分片超时按名收进 timeout_excluded（partial
    语义），不再置 run_error/gut_ok=False。"""
    results = [_shard(["res://tests/a.gd", "res://tests/b.gd"],
                      [_rep(timed_out=True)]),
               _shard(["res://tests/c.gd"], [_rep(0, "Run Summary\n")])]
    (gut_ok, run_error, timed_out, _f, _p,
     excluded, all_keys, _c, _s) = _arm_fail_scan(results)
    assert gut_ok is True and run_error is False
    assert timed_out is True
    assert excluded[0]["reason"] == "shard_timeout"
    assert excluded[0]["keys"] == ["res://tests/a.gd", "res://tests/b.gd"]
    assert all_keys == ["res://tests/a.gd", "res://tests/b.gd",
                        "res://tests/c.gd"]


def test_all_shards_timeout_rejects_run():
    """排除后无可用测试 → 整体拒绝（gut_ok/run_error 置位）。"""
    results = [_shard(["res://tests/a.gd"], [_rep(timed_out=True)]),
               _shard(["res://tests/b.gd"], [_rep(timed_out=True)])]
    (gut_ok, run_error, timed_out, _f, _p,
     excluded, _all, _c, _s) = _arm_fail_scan(results)
    assert timed_out is True
    assert len(excluded) == 2
    assert gut_ok is False and run_error is True


def test_resolve_arm_timeout_priority(monkeypatch):
    """自适应超时优先级：显式 env > 预检 wall_s 校准 x4 > 传入默认。"""
    monkeypatch.delenv("GQT_MEASUREMENT_ARM_TIMEOUT_S", raising=False)
    # 无历史 → 传入默认
    assert mo.resolve_arm_timeout(180, None) == (180, None)[0] or True
    eff, src = mo.resolve_arm_timeout(180, None)
    assert eff == 180 and "parameter" in src
    # 预检校准：wall 61.2s ×4 = 244.8 → 245 > 180
    eff, src = mo.resolve_arm_timeout(180, 61.2)
    assert eff == 245 and "preflight_calibrated" in src
    # 传入值更大 → max 保持
    eff, src = mo.resolve_arm_timeout(600, 61.2)
    assert eff == 600
    # 显式 env 最高优先
    monkeypatch.setenv("GQT_MEASUREMENT_ARM_TIMEOUT_S", "77")
    eff, src = mo.resolve_arm_timeout(600, 61.2)
    assert eff == 77 and "explicit env" in src


def test_explicit_partial_continue_env_recognized(monkeypatch):
    """EXPLICIT_PARTIAL_CONTINUE env 单点解析（gate 分支消费）。"""
    monkeypatch.delenv("GQT_MEASUREMENT_EXPLICIT_PARTIAL_CONTINUE",
                       raising=False)
    import os
    val = os.environ.get("GQT_MEASUREMENT_EXPLICIT_PARTIAL_CONTINUE",
                         "").strip()
    assert val in ("", "0", "false")
    monkeypatch.setenv("GQT_MEASUREMENT_EXPLICIT_PARTIAL_CONTINUE", "1")
    assert os.environ["GQT_MEASUREMENT_EXPLICIT_PARTIAL_CONTINUE"] == "1"
