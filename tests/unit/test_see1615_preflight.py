"""SEE-1615 §SPEC-PREFLIGHT — 基线红短停与具名判据。

P1 基线臂前置：映射构建路径（test_attribution + baseline_preflight）基线红
即 fail-fast（不进插桩臂、map 零写入），reason 具名（阶段/总数/top-N 测试
名 + 放行指引）；--allow-red-baseline 具名放行且基线结果复用给 arms（P4
零懒获取）；绿基线同样复用空集。默认（未传 baseline_preflight）既有
measure 语义零变化。
"""

from __future__ import annotations

import pytest

from godot_qa_toolkit.measurement import orchestrator as mo
from godot_qa_toolkit.measurement.backup import MeasurementError

_RECIPE = {"tests_glob": "res://tests/", "recipe_hash": "h"}


class _ArmsCalled(Exception):
    """execute_arms spy 哨兵——arms 被调用即抛，下游聚合不进判据。"""


def _run_measure(monkeypatch, *, pairs, allow_red=False,
                 acquire_impl=None, request_preflight=True):
    """预检段装配：输入校验 mock；execute_arms spy 记录 kwargs 后抛
    _ArmsCalled。返回 (arms_calls, exc_info)。"""
    calls: list = []

    def spy_arms(**k):
        calls.append(k)
        raise _ArmsCalled()

    monkeypatch.setattr(mo, "_measure_inputs", lambda *a, **k: {
        "root": "/p", "recipe": _RECIPE, "recipe_hash": "h",
        "baseline_pairs": set(), "declared_scoped": set(),
        "declared_bare": set()})
    monkeypatch.setattr(mo, "_measure_stage", lambda *a, **k: {
        "prepared": {}, "evidence_dir": None})
    monkeypatch.setattr(mo, "execute_arms", spy_arms)
    monkeypatch.setattr(mo, "_measure_crap", lambda *a, **k: {})
    monkeypatch.setattr(mo, "_measure_perf", lambda *a, **k: {})
    if acquire_impl is not None:
        monkeypatch.setattr(mo, "_acquire_lazy_baseline", acquire_impl)
    else:
        monkeypatch.setattr(mo, "_acquire_lazy_baseline",
                            lambda root, args, t: (set(pairs), {
                                "method": "uninstrumented_run", "face":
                                "gutconfig_default", "count": len(pairs),
                                "wall_s": 0.1,
                                "pairs": sorted([list(p) for p in pairs])}))
    if allow_red:
        monkeypatch.setenv("GQT_MEASUREMENT_FAIL_ON_RED_BASELINE", "1")
    else:
        monkeypatch.delenv("GQT_MEASUREMENT_FAIL_ON_RED_BASELINE",
                           raising=False)
    with pytest.raises((_ArmsCalled, MeasurementError)) as ei:
        mo.run_measurement("/p", ["a.gd"], _RECIPE, test_attribution=True,
                           baseline_preflight=request_preflight,
                           attribution_out="/tmp/never.json")
    return calls, ei


def test_baseline_red_partial_continue_by_default(monkeypatch):
    """PLANFIRST 默认：基线红**不短停**——红测试按名排除（基线复用给
    arms 吸收），run 继续（meta.status=partial 语义由 stderr 披露 +
    attribution doc.baseline_preflight 承载）。"""
    calls, ei = _run_measure(
        monkeypatch, pairs=[("res://tests/a.gd", "test_x1"),
                            ("res://tests/b.gd", "test_y2")])
    assert isinstance(ei.value, _ArmsCalled)  # 继续进臂，不短停
    assert calls[0]["arm_kwargs"]["baseline_failures"] == [
        ("res://tests/a.gd", "test_x1"), ("res://tests/b.gd", "test_y2")]


def test_fail_on_red_fails_fast_named(monkeypatch):
    """显式开关（默认关）：基线红即短停——arms 不被调用（不进插桩臂）、
    异常 reason 含阶段名/总数/top-N 具名测试名。"""
    calls, ei = _run_measure(
        monkeypatch, pairs=[("res://tests/a.gd", "test_x1"),
                            ("res://tests/b.gd", "test_y2")], allow_red=True)
    assert not isinstance(ei.value, _ArmsCalled)
    msg = str(ei.value)
    assert "baseline_preflight" in msg
    assert "2 failing test pair" in msg
    assert "test_x1" in msg and "test_y2" in msg
    assert calls == []  # 不进插桩臂


def test_green_baseline_reused_empty(monkeypatch):
    """基线绿：空集同样显式传 arms（零懒获取路径），不短停。"""
    calls, ei = _run_measure(monkeypatch, pairs=[])
    assert isinstance(ei.value, _ArmsCalled)
    assert calls[0]["arm_kwargs"]["baseline_failures"] == []


def test_preflight_off_by_default(monkeypatch):
    """默认（未传 baseline_preflight）不触发预检——既有 measure 语义零
    变化（懒基线兜底照旧，baseline_failures 仍为 None）。"""

    def boom(*a, **k):
        raise AssertionError("preflight must not run when disabled")

    calls, ei = _run_measure(monkeypatch, pairs=[], acquire_impl=boom,
                             request_preflight=False)
    assert isinstance(ei.value, _ArmsCalled)
    assert calls[0]["arm_kwargs"]["baseline_failures"] is None


# ------------- PLANFIRST steer：三类路径分派 + 不耐受中断报告形态


def test_new_failure_gate_reports_intolerance_draft(tmp_path):
    """steer-1/2：插桩臂相对基线的新增失败 → gate 中断报告归类
    suspected_instrumentation_intolerance，消息含具名对 + 证据 +
    --intolerant-tests 声明草稿 JSON + 三处置选项（端到端：ensure_map）。"""
    import json as _json
    from godot_qa_toolkit.mutation.truth_select import (
        MutationSelectionError, _build_gate_fail_reason)

    doc = {
        "arms_state": {"gut_full": {"state": "invalid",
                                    "invalid_reason": "run_error",
                                    "bundles": 2, "pairs": 6,
                                    "probes_fired": 9}},
        "arms": {"gut_full": {"test_failures": {"new_pairs": [
            "res://tests/unit/test_game.gd::test_other_fail"]}}},
    }
    msg = _build_gate_fail_reason(doc)
    assert "suspected_instrumentation_intolerance" in msg
    assert "test_other_fail" in msg
    assert "passes uninstrumented baseline, fails instrumented run" in msg
    assert "--intolerant-tests" in msg
    draft = msg[msg.index("[{"):msg.rindex("}]") + 2]
    item = _json.loads(draft)[0]
    assert item["script"] == "res://tests/unit/test_game.gd"
    assert item["test"] == "test_other_fail"
    assert "reason" in item
    assert "GQT_MEASUREMENT_EXPLICIT_PARTIAL_CONTINUE" in msg


def test_three_way_dispatch_matrix(monkeypatch):
    """steer-5 分派矩阵：red → 复用继续（partial）；timeout → 既有
    timed_out→invalid 语义（本子步不改，具名交回）；new-failure → gate
    中断报告（上一用例）。此处钉 red 与显式 fail-on-red 两支。"""
    # red 默认继续（partial）
    calls, ei = _run_measure(
        monkeypatch, pairs=[("res://tests/a.gd", "test_x1")])
    assert isinstance(ei.value, _ArmsCalled)
    assert calls[0]["arm_kwargs"]["baseline_failures"]
    # red + 显式开关 → 短停
    calls2, ei2 = _run_measure(
        monkeypatch, pairs=[("res://tests/a.gd", "test_x1")], allow_red=True)
    assert not isinstance(ei2.value, _ArmsCalled)
