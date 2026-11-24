"""SEE-1631 C2-R2：QA D1～D4 缺陷窄修（§SPEC-024～027）的行为判据。

D1 §SPEC-024  verdict 表身份归一（basename::test），恒红 = red/pre_existing；
D2 §SPEC-025  增量产物 verdict 表守恒（继承/重算/剔除），缺表 fail-loud；
D3 §SPEC-026  懒基线吸收 partial 落到臂文档与 arms_state；
D4 §SPEC-027  baseline_skip 生效后空差集 = 真实 survived（不落 suspect）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_qa_toolkit.measurement.attribution import _test_verdict_table
from godot_qa_toolkit.mutation.ops import Mutant
from godot_qa_toolkit.mutation.reporting import (
    _classify_worker_leg_impl,
    _classify_mutant_run,
)
from godot_qa_toolkit.mutation.verdict_source import (
    BaselineSourceError, _red_entries,
)


# --------------------------------------------------------------------------
# §SPEC-024 / D1：身份归一，恒红 = red/pre_existing
# --------------------------------------------------------------------------

def _mut(*a, **k):
    from godot_qa_toolkit.mutation.ops import Mutation

    return Mutation(kind="AOR", original="+", mutated="-", line=9, col=18)


def test_d1_verdict_table_normalizes_res_path_to_basename_universe():
    """红对 res://tests/unit/test_a.gd::test_red1 与行事实宇宙键
    test_a.gd::test_red1 归一 → red/pre_existing（不落 green 兜底）。"""
    gut = {
        "arm": "gut_full", "state": "valid",
        "baseline_failures_meta": {"method": "uninstrumented_run",
                                   "lazy": True, "status": "partial",
                                   "count": 1,
                                   "pairs": [["res://tests/unit/"
                                              "test_red.gd",
                                              "test_red_always_fails"]]},
        "test_failures": {"observed": ["test_red_always_fails"],
                          "observed_pairs": [
                              "res://tests/unit/test_red.gd::"
                              "test_red_always_fails"],
                          "new": [], "new_pairs": [],
                          "instrumentation_intolerant": []},
        "attribution": {"test_to_lines": {
            "test_red.gd::test_red_always_fails": {"src/calc.gd": [32, 33]},
            "test_green.gd::test_green": {"src/calc.gd": [9]},
        }},
    }
    table = _test_verdict_table((gut,))
    entry = table.get("test_red.gd::test_red_always_fails")
    assert entry is not None, "恒红测试必须出现在 verdict 表"
    assert entry["verdict"] == "red"
    assert entry["classification"] == "pre_existing"
    assert entry["script"] == "res://tests/unit/test_red.gd"
    assert table["test_green.gd::test_green"]["verdict"] == "green"


def test_d1_map_source_synthesis_carries_red_for_skip():
    """map 源合成基线：归一后的红对进入 red_pairs（物理跳过可消费）。"""
    from godot_qa_toolkit.mutation.verdict_source import (
        map_baseline_snapshots,
    )

    doc = {
        "schema_version": "test-attribution/7",
        "report_recipe_hash": "rh", "recipe_hash_pristine": "pr",
        "project_root": "/p", "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": {"src/calc.gd": "x"}, "target_files": ["src/calc.gd"],
        "tests_face": {}, "instrumentable_lines": {},
        "arms": {"gut_full": {"test_to_lines": {}, "script_to_lines": {},
                              "tests": 0, "scripts": 0, "files": 0,
                              "pairs": 0, "function_pairs": 0,
                              "test_to_functions": {},
                              "script_to_functions": {},
                              "file_to_tests": {}, "file_to_scripts": {},
                              "line_pairs": 0, "script_line_pairs": 0,
                              "test_to_lines": {}, "script_to_lines": {},
                              "line_to_tests": {}}},
        "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                    "pairs": 2, "probes_fired": 5,
                                    "vacuous": False}},
        "test_verdicts": {
            "test_red.gd::test_red_always_fails": {
                "verdict": "red", "classification": "pre_existing",
                "script": "res://tests/unit/test_red.gd"},
            "test_green.gd::test_green": {"verdict": "green"}},
        "notes": [],
    }
    import godot_qa_toolkit.mutation.verdict_source as vs
    orig_load = vs._load_map_doc
    vs._load_map_doc = lambda root: doc
    try:
        baselines, failures = vs.map_baseline_snapshots(
            ["src/calc.gd"], "/p", None)
    finally:
        vs._load_map_doc = orig_load
    snap = baselines[None]
    assert snap["rc"] == 1
    names = {n for _s, n in snap["red_pairs"]}
    assert names == {"test_red_always_fails"}


def test_d1_missing_verdict_table_fail_loud_not_empty_red_set():
    """§SPEC-025 消费侧：/7 产物缺 test_verdicts → 具名 fail-loud，
    不得当空红集。"""
    doc = {"schema_version": "test-attribution/7", "arms": {},
           "arms_state": {"gut_full": {"state": "valid"}}}
    with pytest.raises(BaselineSourceError) as ei:
        _red_entries(doc)
    assert "test_verdicts" in str(ei.value)


# --------------------------------------------------------------------------
# §SPEC-025 / D2：增量 verdict 表守恒
# --------------------------------------------------------------------------

def test_d2_merged_verdicts_recompute_and_inherit():
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    baseline = {"test_verdicts": {
        "test_red.gd::test_red_always_fails": {
            "verdict": "red", "classification": "pre_existing",
            "script": "res://tests/unit/test_red.gd"},
        "test_a.gd::test_kept": {"verdict": "green"},
        "test_a.gd::test_rerun_green": {"verdict": "red",
                                        "classification": "pre_existing",
                                        "script": "res://tests/unit/"
                                                  "test_a.gd"},
        "test_gone.gd::test_removed": {"verdict": "green"},
    }}
    executions = [
        {"script": "/p/tests/unit/test_a.gd", "rc": 0, "out": "ok"},
    ]
    merged = _merged_test_verdicts(
        baseline, executions, rerun_scripts={"test_a.gd"},
        dropped_scripts=set(), removed_rels=set(),
        fresh_gherkin=None, gherkin_rerun=set(), gherkin_dropped=set())
    assert merged is not None
    # 未重跑身份逐位一致
    assert merged["test_red.gd::test_red_always_fails"] == \
        baseline["test_verdicts"]["test_red.gd::test_red_always_fails"]
    assert merged["test_a.gd::test_kept"] == {"verdict": "green"}
    # 重跑身份按新执行结果重算：rc=0 无失败 → green
    assert merged["test_a.gd::test_rerun_green"]["verdict"] == "green"


def test_d2_merged_verdicts_recompute_failing_rerun():
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    baseline = {"test_verdicts": {
        "test_a.gd::test_rerun_red": {"verdict": "green",
                                      "script": "res://tests/unit/test_a.gd"},
    }}
    executions = [
        {"script": "/p/tests/unit/test_a.gd", "rc": 1,
         "out": "res://tests/unit/test_a.gd\n* test_rerun_red\n"
                "[Failed] test_rerun_red\n"},
    ]
    merged = _merged_test_verdicts(
        baseline, executions, rerun_scripts={"test_a.gd"},
        dropped_scripts=set(), removed_rels=set(),
        fresh_gherkin=None, gherkin_rerun=set(), gherkin_dropped=set())
    assert merged["test_a.gd::test_rerun_red"]["verdict"] == "red"
    assert merged["test_a.gd::test_rerun_red"]["classification"] == "new"


def test_d2_removed_test_script_drops_verdicts():
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    baseline = {"test_verdicts": {
        "test_gone.gd::test_x": {"verdict": "green"},
    }}
    merged = _merged_test_verdicts(
        baseline, [], rerun_scripts=set(),
        dropped_scripts={"tests/unit/test_gone.gd"}, removed_rels=set(),
        fresh_gherkin=None, gherkin_rerun=set(), gherkin_dropped=set())
    assert "test_gone.gd::test_x" not in merged


def test_d2_missing_table_returns_none_consumer_fail_loud():
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts
    from godot_qa_toolkit.mutation.verdict_source import _red_entries

    assert _merged_test_verdicts({"arms": {}}, [], set(), set(), set(),
                                 None, set(), set()) is None
    with pytest.raises(BaselineSourceError):
        _red_entries({"schema_version": "test-attribution/7", "arms": {}})


# --------------------------------------------------------------------------
# §SPEC-026 / D3：懒基线吸收 partial 落到臂文档与 arms_state
# --------------------------------------------------------------------------

def test_d3_lazy_absorption_partial_lands_on_arm_doc():
    from godot_qa_toolkit.measurement.arms import _arm_doc

    meta = {"method": "uninstrumented_run", "lazy": True,
            "status": "partial", "count": 1,
            "pairs": [["res://tests/unit/test_red.gd",
                       "test_red_always_fails"]],
            "excluded_tests": ["res://tests/unit/test_red.gd::"
                               "test_red_always_fails"]}
    doc = _arm_doc(
        "gut_full", "u1", 1.0, gut_ok=True, timed_out=False, arm_valid=True,
        invalid_reason=None, run_error=False, baseline_meta=meta,
        arm_failures=set(), arm_fail_pairs=set(), new_pairs=set(),
        exempt_pairs=set(), reps=1, convergence=None,
        adaptive_rounds_used=0, arms_parallel=False, attribution=None,
        bundles={}, reconciliation=None, plans=[], results=[],
        timeout_excluded=[])
    assert doc["partial"] is True
    assert "res://tests/unit/test_red.gd::test_red_always_fails" \
        in doc["excluded_tests"]


def test_d3_no_absorption_partial_false_excluded_empty():
    from godot_qa_toolkit.measurement.arms import _arm_doc

    doc = _arm_doc(
        "gut_full", "u1", 1.0, gut_ok=True, timed_out=False, arm_valid=True,
        invalid_reason=None, run_error=False, baseline_meta=None,
        arm_failures=set(), arm_fail_pairs=set(), new_pairs=set(),
        exempt_pairs=set(), reps=1, convergence=None,
        adaptive_rounds_used=0, arms_parallel=False, attribution=None,
        bundles={}, reconciliation=None, plans=[], results=[],
        timeout_excluded=[])
    assert doc["partial"] is False
    assert doc["excluded_tests"] == []


# --------------------------------------------------------------------------
# §SPEC-027 / D4：baseline_skip 生效后空差集 = 真实存活
# --------------------------------------------------------------------------

def _mk_mutant():
    return Mutant(file="/p/systems/calc.gd", line=9, column=18,
                  operator="AOR", original="+", mutated="-", kind="AOR")


def test_d4_cold_skip_applied_empty_diff_survived():
    """cold 车道：baseline_skip 生效（marked>=1）+ 差集空 → survived。"""
    tail = ("res://tests/unit/test_red.gd\n* test_red_always_fails\n"
            "GQT_SKIP_RED marked=2 requested=2 names=\n")
    m = _mk_mutant()
    rec = _classify_mutant_run(
        m, rc=1, tail=tail, baseline_failures={"test_red_always_fails"},
        baseline_dirty=True)
    assert rec["verdict"] == "survived"
    assert "baseline_skip applied" in rec["reason"]
    assert rec.get("suspect") in (None, False)


def test_d4_cold_dirty_no_skip_still_suspect():
    tail = "res://tests/unit/test_red.gd\n* test_red_always_fails\n"
    m = _mk_mutant()
    rec = _classify_mutant_run(
        m, rc=1, tail=tail, baseline_failures={"test_red_always_fails"},
        baseline_dirty=True)
    assert rec["verdict"] == "suspect"


def test_d4_worker_skip_applied_empty_diff_survived():
    """worker 车道：结构化 skip_red/test_face 证据在场 + 差集空 →
    survived（不落 suspect）。"""

    class Leg:
        gut_ran = True
        fail_count = 0
        tests_ran = 2
        hash_ok = True
        target_compile_invalid = False
        failing_tests = set()
        failing_scripts = set()
        skip_red_info = {"requested": 2, "marked": 2, "names":
                         ["test_red_always_fails"]}
        allow_info = None
        bail_info = None

    m = _mk_mutant()
    rec = _classify_worker_leg_impl(
        m, Leg(), baseline_failures={"test_red_always_fails"},
        baseline_dirty=True, ownership_fn=lambda *a, **k: None,
        project_root="/p", original_src="src")
    assert rec["verdict"] == "survived"
    assert "baseline_skip applied" in rec["reason"]


def test_d4_worker_dirty_without_skip_keeps_preexisting_survived():
    class Leg:
        gut_ran = True
        fail_count = 0
        tests_ran = 2
        hash_ok = True
        skip_red_info = None
        allow_info = None

    m = _mk_mutant()
    rec = _classify_worker_leg_impl(
        m, Leg(), baseline_failures=set(), baseline_dirty=True,
        ownership_fn=lambda *a, **k: None, project_root="/p",
        original_src="src")
    # worker 既有契约：fail_count==0 → survived 早退先于 dirty suspect
    assert rec["verdict"] == "survived"
    assert "tests passed after" in rec["reason"]
