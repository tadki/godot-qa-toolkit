"""SEE-1654 C3-R29（D7）· C3-R30（决策 22）：中止退出语义。

D6 的 cold parity 门已按决策 22 回退（Owner 2026-11-24 裁决：默认地图
源下 mutant 执行前零基线/验证面运行；「假绿概率微乎其微」）——门与门
专属测试项已删，cold 车道「地图说绿实跑红」按残余披露，不运行。

D7：中止就是失败——run_error 中止形态（基线脏中止等）经 `_inject_one`
收尾后 ok 不得被四计数重算翻真；同一中止原因经两处装载时 failures 逐字
去重；中止文案指向实际触发的旗标。
"""

from __future__ import annotations

from godot_qa_toolkit.mutation.reporting import _break_on_baseline_result


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
