"""SEE-1623 H4 — acceptance 默认暂停契约在普通路径失效行为钉。

根因（SEE-1622 Refacty BUG-1，9/9 复现）：两处门控写成
`acceptance_disabled() and test_attribution`——普通 `measure run`
（无 env、无 --test-attribution）落入双臂执行分支：acceptance 臂真实
执行（额外成本 + 无关失败劫持 exit）、`gherkin_arm` 披露缺位；
显式 `GQT_MEASUREMENT_GHERKIN_DISABLED=1` 同样被无视。

行为契约（§SPEC-PLANFIRST 任务 2 / Owner 裁定：暂停是有意契约，
恢复需显式 env=0）：
1. 普通路径（test_attribution=False，env 缺省）不执行 acceptance 臂：
   双臂 execute_arms 不进入，单臂 gut_full run_arm，占位臂
   state=disabled，stage_timings.gherkin_arm=0.0。
2. `gherkin_arm` 具名披露在普通路径同样在场。
3. 显式 env=0 恢复双臂（普通路径与归属路径同权）；显式 env=1 保持暂停。
"""

from __future__ import annotations

import pytest

from godot_qa_toolkit.measurement import arms as arms_mod
from godot_qa_toolkit.measurement import orchestrator as mo

_RECIPE = {"tests_glob": "res://tests/", "recipe_hash": "h"}

_DISABLED_ARM_B = {
    "arm": "acceptance", "state": "disabled", "invalid_reason": None,
    "invalid_evidence": None, "wall_s": 0.0, "bundles": 0,
    "bundles_data": {}, "results": [], "repeats": 0, "convergence": None,
    "shards": 0, "test_failures": {}, "baseline_failures_meta": None,
    "attribution": {}, "reconciliation": None, "shadowed": False,
    "adaptive_rounds_used": 0, "arms_parallel": False,
}


def _wire(monkeypatch, *, emit_capture: list):
    """输入/聚合段 mock + 双臂与单臂 spy（记录调用形态）。"""

    def spy_run_arm(*a, **k):
        return {"arm": a[0] if a else k.get("arm"), "state": "valid",
                "wall_s": 1.0, "results": [], "bundles": 0,
                "bundles_data": {}, "repeats": 0, "shards": 0,
                "test_failures": {}, "attribution": {}}

    def spy_arms(**k):
        raise AssertionError(
            "dual-arm execute_arms entered while acceptance paused")

    monkeypatch.setattr(mo, "_measure_inputs", lambda *a, **k: {
        "root": "/p", "recipe": _RECIPE, "recipe_hash": "h",
        "baseline_pairs": set(), "declared_scoped": set(),
        "declared_bare": set(), "coverage_limitations": [],
        "pristine_recipe_hash": "h", "t_command": 0.0})
    monkeypatch.setattr(mo, "_measure_stage", lambda *a, **k: {
        "prepared": {}, "evidence_dir": None, "complete": True,
        "t_gated": 0.0, "causal_sites": {}})
    monkeypatch.setattr(arms_mod, "run_arm", spy_run_arm)
    monkeypatch.setattr(mo, "execute_arms", spy_arms)
    monkeypatch.setattr(mo, "_measure_crap", lambda *a, **k: {})
    monkeypatch.setattr(mo, "_measure_perf", lambda *a, **k: {})
    monkeypatch.setattr(mo, "_gate_comparable", lambda *a, **k: ({}, False))
    monkeypatch.setattr(mo, "_build_unions", lambda *a, **k: [])
    monkeypatch.setattr(mo, "_measure_attribution", lambda *a, **k: {})
    monkeypatch.setattr(mo, "_measure_emit",
                        lambda *a, **k: (emit_capture.append(a) or {}))


def _setup_env(monkeypatch, value: str | None):
    if value is None:
        monkeypatch.delenv("GQT_MEASUREMENT_GHERKIN_DISABLED",
                           raising=False)
    else:
        monkeypatch.setenv("GQT_MEASUREMENT_GHERKIN_DISABLED", value)


def test_plain_run_paused_no_gherkin_arm(monkeypatch):
    """显式诊断暂停（env=1，SEE-1631 起为非默认回退通道）：gherkin 臂
    不执行——单臂 gut_full + 占位 disabled 臂 + 0.0 计时 + 具名披露。
    （历史默认暂停语义由本测试以显式开关覆盖；默认已恢复双臂。）"""
    emit_capture: list = []
    _wire(monkeypatch, emit_capture=emit_capture)
    _setup_env(monkeypatch, "1")
    report = mo.run_measurement("/p", ["a.gd"], _RECIPE,
                                test_attribution=False)
    _args, _inp, _st, arm_a, arm_b = emit_capture[0][:5]
    assert (arm_a or {}).get("arm") == "gut_full"
    assert (arm_b or {}).get("state") == "disabled"
    assert report["stage_timings"]["gherkin_arm"] == 0.0
    assert "disabled_pending_atomization" in report["gherkin_arm"]


def test_plain_run_explicit_env_one_still_paused(monkeypatch):
    """显式 env=1：普通路径同样暂停（此前被 and test_attribution 无视）。"""
    emit_capture: list = []
    _wire(monkeypatch, emit_capture=emit_capture)
    _setup_env(monkeypatch, "1")
    report = mo.run_measurement("/p", ["a.gd"], _RECIPE,
                                test_attribution=False)
    assert report["stage_timings"]["gherkin_arm"] == 0.0
    assert "disabled_pending_atomization" in report["gherkin_arm"]


def test_plain_run_env_zero_restores_dual_arms(monkeypatch):
    """显式 env=0：恢复路径对普通路径生效——双臂 execute_arms 进入。"""
    calls: list = []
    _wire(monkeypatch, emit_capture=[])
    _setup_env(monkeypatch, "0")

    def spy_arms(**k):
        calls.append(k)
        arm_a = {"arm": "gut_full", "state": "valid", "wall_s": 1.0,
                 "results": [], "bundles": 0, "bundles_data": {},
                 "repeats": 0, "shards": 0, "test_failures": {},
                 "attribution": {}}
        arm_b = {"arm": "acceptance", "state": "valid", "wall_s": 2.0,
                 "results": [], "bundles": 0, "bundles_data": {},
                 "repeats": 3, "shards": 1, "test_failures": {},
                 "attribution": {}}
        return arm_a, arm_b

    monkeypatch.setattr(mo, "execute_arms", spy_arms)
    report = mo.run_measurement("/p", ["a.gd"], _RECIPE,
                                test_attribution=False)
    assert len(calls) == 1
    assert report["stage_timings"]["gherkin_arm"] == 2.0
    assert "gherkin_arm" not in report
