"""SEE-1615 §SPEC-PLANFIRST 证据补强 —— 臂 invalid 细分原因与证据。

五类细分（launch_failure / shard_timeout / unattributed_failures /
new_failures / reconciliation_failed）+ 证据块（超时分片与超时值来源、
不可归因分片 rc+输出尾部、新增失败对清单 + 基线吸收数）；gate 消息
带细分原因与证据摘要，map 构建拒绝一并带出。
"""

from __future__ import annotations

from godot_qa_toolkit.measurement.arms import _classify_invalid


def _results(reps):
    return [{"plan": {"keys": "res://tests/unit/", "suite_dir": None},
             "reps": reps}]


def _rep(rc=0, out="", timed_out=False):
    return {"rc": rc, "out": out, "timed_out": timed_out}


def test_new_failures_reason_with_baseline_absorbed():
    gut_out = ("res://tests/a.gd\n* test_x\n[Failed] test_x\n"
               "Run Summary\n")
    reason, ev = _classify_invalid(
        "gut_full", results=_results([_rep(1, gut_out)]),
        timed_out=False, run_error=True,
        new_pairs={("res://tests/a.gd", "test_new1"),
                   ("res://tests/b.gd", "test_new2")},
        effective_baseline={("res://tests/a.gd", "t1"),
                            ("res://tests/a.gd", "t2"),
                            ("res://tests/a.gd", "t3")},
        timeout_s=180, timeout_source="parameter timeout_s=180s",
        reconciliation_failed=False)
    assert reason == "new_failures"
    assert ev["new_pairs"] == ["res://tests/a.gd::test_new1",
                               "res://tests/b.gd::test_new2"]
    assert ev["baseline_absorbed"] == 3


def test_shard_timeout_carries_value_and_source():
    reason, ev = _classify_invalid(
        "gut_full", results=_results([_rep(timed_out=True)]),
        timed_out=True, run_error=True, new_pairs=set(),
        effective_baseline=set(), timeout_s=240,
        timeout_source="preflight_calibrated x4 (wall 61.2s)",
        reconciliation_failed=False)
    assert reason == "shard_timeout"
    st = ev["shard_timeouts"][0]
    assert st["timeout_s"] == 240
    assert st["timeout_source"] == "preflight_calibrated x4 (wall 61.2s)"


def test_unattributed_shard_carries_rc_and_tail():
    # 真实形态：有 GUT 流证据（跑过）但 rc!=0 且无可解析失败名
    gut_out = ("res://tests/unit/test_a.gd\n* test_ok\n"
               "[Passed] test_ok\nRun Summary\nSCRIPT ERROR: boom\n")
    reason, ev = _classify_invalid(
        "gut_full", results=_results([_rep(rc=7, out=gut_out)]),
        timed_out=False, run_error=True, new_pairs=set(),
        effective_baseline=set(), timeout_s=180,
        timeout_source="parameter timeout_s=180s",
        reconciliation_failed=False)
    assert reason == "unattributed_failures"
    u = ev["unattributed_shards"][0]
    assert u["rc"] == 7
    assert any("SCRIPT ERROR: boom" in ln for ln in u["tail"])


def test_launch_failure_takes_priority():
    reason, ev = _classify_invalid(
        "gut_full", results=_results([
            _rep(rc=2, out="Godot Engine v4.6.2 — could not open project")]),
        timed_out=False, run_error=True, new_pairs=set(),
        effective_baseline=set(), timeout_s=180,
        timeout_source="parameter timeout_s=180s",
        reconciliation_failed=False)
    assert reason == "launch_failure"
    assert ev["launch_failures"][0]["rc"] == 2


def test_reconciliation_failed_reason():
    reason, ev = _classify_invalid(
        "gut_full", results=_results([_rep(0, "[Passed] t\n")]),
        timed_out=False, run_error=False, new_pairs=set(),
        effective_baseline=set(), timeout_s=180,
        timeout_source="parameter timeout_s=180s",
        reconciliation_failed=True)
    assert reason == "reconciliation_failed"


def test_gate_message_carries_reason_and_evidence():
    """map 构建拒绝时细分原因 + 证据摘要一并带出（truth_select 侧）。"""
    from godot_qa_toolkit.mutation.truth_select import _build_gate_fail_reason
    doc = {
        "arms_state": {"gut_full": {"state": "invalid",
                                    "invalid_reason": "new_failures",
                                    "invalid_evidence": {
                                        "new_pairs": ["a.gd::t1"],
                                        "baseline_absorbed": 3},
                                    "bundles": 2, "pairs": 6,
                                    "probes_fired": 9}},
        "arms": {"gut_full": {"test_failures": {"new_pairs": ["a.gd::t1"]}}},
    }
    msg = _build_gate_fail_reason(doc)
    assert "new_failures" in msg
    assert "new_pairs=['a.gd::t1']" in msg
    assert "baseline_absorbed=3" in msg
    assert "--intolerant-tests" in msg  # 声明草稿仍在（融合不回退）


def test_stage_keys_carry_prepare_phases():
    """§SPEC-PLANFIRST B：prepare 盲区消灭——映射构建路径的分段键
    （instrument_prepare/baseline_preflight/gut_full_arm/acceptance_arm）
    在 STAGE_KEYS 契约内（run_measurement 落 report.stage_timings）。"""
    from godot_qa_toolkit.mutation.stagetiming import STAGE_KEYS
    for k in ("instrument_prepare", "baseline_preflight", "gut_full_arm",
              "acceptance_arm", "total"):
        assert k in STAGE_KEYS
    clock = __import__("godot_qa_toolkit.mutation.stagetiming",
                       fromlist=["StageClock"]).StageClock()
    d = clock.disclosure()
    assert set(d.keys()) == set(STAGE_KEYS)
    assert all(v == 0.0 for v in d.values())
