"""SEE-1654 C3-R27（D3）：gherkin 增量腿判定回写按与身份段同一命名空间
的证据逐名判定（§SPEC-025/028）。

命名空间纪律（决策 19）与真实形态夹具工厂见共享模块
`tests/unit/gherkin_verdict_fixtures.py`（本模块与 D8/D14 共用）。
"""

from __future__ import annotations

import copy

from godot_qa_toolkit.measurement.attribution import _gherkin_feature_of
from godot_qa_toolkit.measurement.increment import _verdict_gherkin_rerun
from tests.unit.gherkin_verdict_fixtures import (
    FAIL_SCENARIO,
    FEATURE,
    GEN_SCRIPT,
    PASS_SCENARIO,
    _baseline_table,
    _fail_ident,
    _leg_account,
    _pass_ident,
)


# --------------------------------------------------------------------------
# D3 主回归：真实形态失败腿逐名判定（在 9febfaf 上为红）
# --------------------------------------------------------------------------

def test_real_form_failed_scenario_stays_red_passing_green():
    """真实腿账（rc=1，failing_tests 与身份段同形）下：失败身份保持 red
    且分类沿基线 pre_existing；通过身份 green（不得被翻绿 = D3 根因）。"""
    merged = _baseline_table()
    fresh = {"executions": [_leg_account([FAIL_SCENARIO], [PASS_SCENARIO])]}
    _verdict_gherkin_rerun(merged, fresh, gherkin_rerun={FEATURE})
    entry = merged[_fail_ident()]
    assert entry["verdict"] == "red", entry
    assert entry["classification"] == "pre_existing", entry
    assert merged[_pass_ident()] == {"verdict": "green"}


def test_real_form_baseline_green_failure_classified_new():
    """基线绿、重跑失败 → red/new（分类不无依据改写为非 new）。"""
    merged = {_fail_ident(): {"verdict": "green"},
              _pass_ident(): {"verdict": "green"}}
    fresh = {"executions": [_leg_account([FAIL_SCENARIO], [PASS_SCENARIO])]}
    _verdict_gherkin_rerun(merged, fresh, gherkin_rerun={FEATURE})
    entry = merged[_fail_ident()]
    assert entry["verdict"] == "red", entry
    assert entry["classification"] == "new", entry
    assert merged[_pass_ident()] == {"verdict": "green"}


def test_real_form_kol_identity_shape_anchors_second_segment():
    """KOL 真实身份形态锚点：`gherkin://feature/steady_performance::
    test_50_d750…_00`——第二段是编译测试名（QA-R4 反例的身份形态）。"""
    ident = _fail_ident()
    assert ident.startswith(
        "gherkin://feature/steady_performance::test_50_d750"), ident
    assert _gherkin_feature_of(ident) == FEATURE


def test_real_form_heal_roundtrip_matches_full_table():
    """heal 往返与全量一致：失败腿两次增量（改 feature → 恢复原文）
    后判定表逐位回到基线（红保持 red/pre_existing，绿保持 green）——
    增量路径不得把红不可逆翻绿（D3 的不可恢复缺陷）。"""
    baseline = _baseline_table()
    fresh = {"executions": [_leg_account([FAIL_SCENARIO], [PASS_SCENARIO])]}
    first = copy.deepcopy(baseline)
    _verdict_gherkin_rerun(first, fresh, gherkin_rerun={FEATURE})
    second = copy.deepcopy(first)
    _verdict_gherkin_rerun(second, fresh, gherkin_rerun={FEATURE})
    assert second == baseline


def test_real_form_multi_shard_union_any_failure_counts():
    """多分片/多 rep 并集：任一账失败都算失败（失败身份红），通过的
    身份不随另一分片整体标红。"""
    pass_account = _leg_account([], [PASS_SCENARIO], rc=0)
    fail_account = _leg_account([FAIL_SCENARIO], [PASS_SCENARIO], rc=1)
    merged = {_fail_ident(): {"verdict": "green"},
              _pass_ident(): {"verdict": "green"}}
    fresh = {"executions": [pass_account, fail_account]}
    _verdict_gherkin_rerun(merged, fresh, gherkin_rerun={FEATURE})
    assert merged[_fail_ident()]["verdict"] == "red"
    assert merged[_pass_ident()] == {"verdict": "green"}


# --------------------------------------------------------------------------
# N6 保守整键红（语义不回退）
# --------------------------------------------------------------------------

def test_scenario_name_only_account_conservative_red():
    """只有 `failing_scenarios`（scenario 名命名空间）的账：无法与身份段
    （编译测试名）同命名空间逐名归因 → N6 保守整键红，不得放绿——
    scenario 名与身份段永不相等，按名比对翻绿即 D3 根因。"""
    merged = {_fail_ident(): {"verdict": "red",
                              "classification": "pre_existing",
                              "script": GEN_SCRIPT},
              _pass_ident(): {"verdict": "green"}}
    fresh = {"executions": [{"rc": 1, "out": "...",
                             "failing_scenarios": [FAIL_SCENARIO]}]}
    _verdict_gherkin_rerun(merged, fresh, gherkin_rerun={FEATURE})
    assert merged[_fail_ident()]["verdict"] == "red"
    assert merged[_pass_ident()]["verdict"] == "red"


def test_legacy_account_without_failing_names_conservative_red():
    """旧形态账（无 failing_tests 键）rc=1 → 保守整键红（既有 N6 语义）。"""
    merged = {_fail_ident(): {"verdict": "green"}}
    fresh = {"executions": [{"rc": 1, "out": "..."}]}
    _verdict_gherkin_rerun(merged, fresh, gherkin_rerun={FEATURE})
    assert merged[_fail_ident()]["verdict"] == "red"


def test_empty_failing_tests_conservative_red():
    """failing_tests 空集（腿失败但零可归因名）→ 保守整键红（N6）。"""
    merged = {_fail_ident(): {"verdict": "green"},
              _pass_ident(): {"verdict": "green"}}
    ex = _leg_account([FAIL_SCENARIO], [PASS_SCENARIO])
    ex["failing_tests"] = []
    _verdict_gherkin_rerun(merged, {"executions": [ex]},
                           gherkin_rerun={FEATURE})
    assert merged[_fail_ident()]["verdict"] == "red"
    assert merged[_pass_ident()]["verdict"] == "red"


def test_all_pass_leg_green():
    """重跑腿全部通过（rc=0 且执行证据在场）→ green（不随 feature 级
    返回码整体标红；D8 回归面）。"""
    merged = {_fail_ident(): {"verdict": "red",
                              "classification": "pre_existing",
                              "script": GEN_SCRIPT}}
    fresh = {"executions": [_leg_account([], [FAIL_SCENARIO, PASS_SCENARIO],
                                       rc=0)]}
    _verdict_gherkin_rerun(merged, fresh, gherkin_rerun={FEATURE})
    assert merged[_fail_ident()] == {"verdict": "green"}
