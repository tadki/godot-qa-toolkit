"""SEE-1654 C3-R29（D6 + D7）：cold 车道 parity 同判与中止退出语义。

D6：地图源判定基线下，cold 车道与 worker 通道同判——地图判定被实跑
证伪（方向 A：地图说绿实跑红）→ 具名 `baseline_map_mismatch` fail-loud、
零 mutant；方向 B（地图说红实跑绿）维持「排除 + 披露」残余（门不判方
向 B）；非地图源（cold legacy / off）门不启用；影子拉起失败 = 门不可用
披露跳过。GUT 失败名经真实解析器 `_parse_failing_tests` 提取（决策 19：
GUT 输出形态与解析器同源，不手写失败名集合）。

D7：中止就是失败——run_error 中止形态（基线脏中止等）经 `_inject_one`
收尾后 ok 不得被四计数重算翻真；同一中止原因经两处装载时 failures 逐字
去重；中止文案指向实际触发的旗标。
"""

from __future__ import annotations

import json

import pytest

from godot_qa_toolkit.mutation import runner as _runner
from godot_qa_toolkit.mutation.reporting import _break_on_baseline_result
from godot_qa_toolkit.mutation.verdict_source import BaselineSourceError


# ---------------------------------------------------------------------------
# 真实形态夹具：GUT 运行流经真实解析器提取失败名（决策 19）
# ---------------------------------------------------------------------------

def _gut_tail(failing: list[str], passing: list[str]) -> str:
    """真实 GUT 运行流形态：res:// 脚本头 + `* 测试名` + [Failed]/[passed]。"""
    lines = ["res://tests/unit/test_face.gd"]
    for n in passing:
        lines += [f"* {n}", "  assert ok  [passed]"]
    for n in failing:
        lines += [f"* {n}", "  assertion failed  [Failed]"]
    return "\n".join(lines)


def _slot():
    return type("S", (), {"root": "/tmp/shadow" })()


def _install_gate(monkeypatch: pytest.MonkeyPatch, tail: str,
                  calls: list) -> None:
    """把 cold parity 腿的实跑替换为真实解析器解析的运行流（零 Godot 单元
    面；调用留痕供「门是否启用」断言）。"""
    def fake_run_baseline(project_root, tests_glob, timeout_s):
        calls.append((project_root, tests_glob, timeout_s))
        return (1 if "Failed]" in tail else 0), tail, 0.01

    monkeypatch.setattr(_runner, "_run_baseline", fake_run_baseline)


def _gate(monkeypatch, *, map_source: bool, baseline_reds: list[str],
          real_reds: list[str], mutants: list | None = None,
          calls: list | None = None) -> None:
    from godot_qa_toolkit.mutation.gutrun import _parse_failing_tests

    tail = _gut_tail(real_reds, ["t_ok_00"])
    _install_gate(monkeypatch, tail, calls if calls is not None else [])
    snap = {"source": "map"} if map_source else None
    _runner._cold_parity_gate(
        slot=_slot(), project_root="/tmp/whatever", tests_glob=None,
        mutants=mutants if mutants is not None else [object()],
        baseline_failures=set(_parse_failing_tests(
            _gut_tail(baseline_reds, ["t_ok_00"]))),
        baseline_snapshot=snap)


# ---------------------------------------------------------------------------
# D6：cold 车道 parity 门
# ---------------------------------------------------------------------------

def test_cold_parity_direction_a_named_invalidation(monkeypatch):
    """方向 A（地图说绿实跑红）：具名 baseline_map_mismatch 批量作废——
    不得静默产出批量假 kill（099bbe6/cfc7c71 上无此门 = NameError/静默）。"""
    with pytest.raises(BaselineSourceError, match="baseline_map_mismatch"):
        _gate(monkeypatch, map_source=True,
              baseline_reds=[], real_reds=["test_red_always"])


def test_cold_parity_direction_b_not_rejected(monkeypatch):
    """方向 B（地图说红实跑绿）：维持「排除 + 披露」残余——门不判方向 B
    （观察缺红不触发作废）。"""
    _gate(monkeypatch, map_source=True,
          baseline_reds=["test_red_always"], real_reds=[])


def test_cold_parity_match_passes(monkeypatch):
    """地图红集与实跑一致 → 门放行（零失配）。"""
    _gate(monkeypatch, map_source=True,
          baseline_reds=["test_red_always"], real_reds=["test_red_always"])


def test_cold_parity_off_for_non_map_source(monkeypatch):
    """非地图源（cold legacy / off）：门不启用——无地图可证伪，实跑成本
    不支付（S-3②：cold legacy 维持原语义）。"""
    calls: list = []
    _install_gate(monkeypatch, _gut_tail(["test_red_always"], []), calls)
    snap = None
    _runner._cold_parity_gate(
        slot=_slot(), project_root="/tmp/whatever", tests_glob=None,
        mutants=[object()], baseline_failures=set(),
        baseline_snapshot=snap)
    assert calls == []


def test_cold_parity_launch_failure_skips_gate(monkeypatch):
    """影子拉起失败 = 门不可用：具名披露后跳过（不误判失配）。尾部带
    既有 launch-failure marker（与 `_loop_baseline` 同一识别规则）。"""
    def fake_run_baseline(project_root, tests_glob, timeout_s):
        return 127, ("error: failed to load script res://x.gd — "
                     "Godot launch failed"), 0.01

    monkeypatch.setattr(_runner, "_run_baseline", fake_run_baseline)
    err = capsys_stderr(lambda: _runner._cold_parity_gate(
        slot=_slot(), project_root="/p", tests_glob=None, mutants=[object()],
        baseline_failures=set(), baseline_snapshot={"source": "map"}))
    assert "cold parity gate unavailable" in err


def capsys_stderr(fn):
    import io
    from contextlib import redirect_stderr

    buf = io.StringIO()
    with redirect_stderr(buf):
        try:
            fn()
        except BaseException:
            pass
    return buf.getvalue()


def test_cold_parity_gate_skips_zero_mutants(monkeypatch):
    """零 mutant（无腿可作废）：门不支付实跑。"""
    calls: list = []
    _install_gate(monkeypatch, _gut_tail(["test_red_always"], []), calls)
    _runner._cold_parity_gate(
        slot=_slot(), project_root="/tmp/whatever", tests_glob=None,
        mutants=[], baseline_failures=set(),
        baseline_snapshot={"source": "map"})
    assert calls == []


# ---------------------------------------------------------------------------
# D7：中止就是失败
# ---------------------------------------------------------------------------

def _abort_envelope() -> dict:
    return _break_on_baseline_result("src/calc_sign.gd",
                                     ["test_red_always"])


def _inject_ctx() -> dict:
    """`_inject_one` 收尾夹具（与 test_see1589 E5 同形态——既有约定）。"""
    from types import SimpleNamespace

    plan = SimpleNamespace(reused={}, diff_active=False, mutate_all=False,
                           executed={})
    return {"plan": plan, "test_selection": {"source": "runtime_mapping"},
            "selection_mode": "line", "selection_groups": ["g1"],
            "requested_jobs": 2, "src": "", "executed": {}, "uncovered": [],
            "untouched": []}


def test_abort_envelope_ok_not_flipped_by_inject_one():
    """run_error 中止形态经 `_inject_one` 收尾后 ok 保持 False——四计数
    全 0 的中止不得被翻成 ok:true（并行/cold 通道的 D7 根因）。"""
    from godot_qa_toolkit.mutation.executor import _inject_one

    r = _abort_envelope()
    _inject_one(r, _inject_ctx())
    assert r["ok"] is False, r["summary"]
    assert r["summary"]["run_error"] is True


def test_inject_one_normal_result_still_recomputed():
    """非中止形态的 ok 重算语义不回退（无失败 → ok:true）。"""
    from godot_qa_toolkit.mutation.executor import _inject_one

    r = {"ok": None, "summary": {"file": "f", "mutants": 2, "killed": 2,
                                 "survived": 0, "timeout": 0,
                                 "run_errors": 0, "suspect": 0},
         "failures": []}
    _inject_one(r, _inject_ctx())
    assert r["ok"] is True


def test_inject_one_failed_result_still_false():
    """有幸存者 → ok:false（既有语义不放宽）。"""
    from godot_qa_toolkit.mutation.executor import _inject_one

    r = {"ok": None, "summary": {"file": "f", "mutants": 2, "killed": 1,
                                 "survived": 1, "timeout": 0,
                                 "run_errors": 0, "suspect": 0},
         "failures": []}
    _inject_one(r, _inject_ctx())
    assert r["ok"] is False


def test_abort_failure_text_points_to_actual_flag():
    """`--fail-on-red-baseline` 触发的中止文案指向该旗标（不再硬编码
    另一旗标名）。"""
    r = _break_on_baseline_result("f", ["t"],
                                  trigger="--fail-on-red-baseline")
    assert "--fail-on-red-baseline" in r["summary"]["error"]
    assert "--break-on-baseline-failure" not in r["summary"]["error"]
    assert "--fail-on-red-baseline" in r["failures"][0]["reason"]


def test_trigger_registration_from_cli_flag():
    """CLI 派发单点：`--fail-on-red-baseline` 登记后，中止出口文案指向
    它；复位后回默认。"""
    from godot_qa_toolkit.cli import _baseline_break_flag
    import argparse
    from godot_qa_toolkit.mutation import runner as r

    args = argparse.Namespace(break_on_baseline_failure=False,
                              fail_on_red_baseline=True)
    assert _baseline_break_flag(args) is True
    out = _break_on_baseline_result("f", ["t"],
                                    trigger=r._BASELINE_BREAK_TRIGGER)
    assert "--fail-on-red-baseline" in out["failures"][0]["reason"]
    r.set_baseline_break_trigger("--break-on-baseline-failure")


# ---------------------------------------------------------------------------
# D7：failures 逐字去重（同一中止原因经两处装载）
# ---------------------------------------------------------------------------

def test_merge_dedupes_identical_abort_failures():
    """两个 chunk 各自装载同一中止原因 → 顶层 failures 只留一条
    （099bbe6/cfc7c71 上为两条逐字重复）。"""
    from godot_qa_toolkit.mutation.merging import _merge_results

    abort = _abort_envelope()["failures"][0]

    def cr(seq):
        return {"file": "src/calc_sign.gd", "ok": False,
                "summary": {"file": "src/calc_sign.gd", "mutants": 0,
                            "killed": 0, "survived": 0, "timeout": 0,
                            "run_errors": 0, "suspect": 0},
                "failures": [abort]}

    chunks = [{"file": "src/calc_sign.gd"}, {"file": "src/calc_sign.gd"}]
    merged = _merge_results(chunks, [cr(0), cr(1)], jobs=2)
    assert len(merged["failures"]) == 1, merged["failures"]
    assert merged["ok"] is False


def test_merge_keeps_distinct_failures():
    """不同 failure（内容不同）不去重。"""
    from godot_qa_toolkit.mutation.merging import _merge_results

    f1 = {"reason": "a"}
    f2 = {"reason": "b"}
    chunks = [{"file": "f"}, {"file": "f"}]
    merged = _merge_results(chunks, [
        {"file": "f", "ok": False,
         "summary": {"file": "f", "mutants": 0, "killed": 0, "survived": 0,
                     "timeout": 0, "run_errors": 0, "suspect": 0},
         "failures": [f1]},
        {"file": "f", "ok": False,
         "summary": {"file": "f", "mutants": 0, "killed": 0, "survived": 0,
                     "timeout": 0, "run_errors": 0, "suspect": 0},
         "failures": [f2]}], jobs=2)
    assert len(merged["failures"]) == 2
