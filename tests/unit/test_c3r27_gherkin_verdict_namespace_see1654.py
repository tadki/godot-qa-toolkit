"""SEE-1654 C3-R27（D3）：gherkin 增量腿判定回写按与身份段同一命名空间
的证据逐名判定（§SPEC-025/028）。

命名空间纪律（决策 19）：
- 规范身份 `gherkin://feature/<key>::<第二段>` 的第二段是**编译测试名**
  （`gherkin/compile.py` `_ident(scenario, index) + "_<case:02d>"`），
  scenario 名不是身份分段；
- 腿执行账 `failing_tests` 是编译测试名（截断前 `_parse_failing_tests`
  提取）——与身份段同命名空间，判定按它逐名比对；
- 腿执行账 `failing_scenarios` 是 scenario 名（经 `scenario_of_test` 桥
  回写）——**不得**与身份段直接比对（D3 根因：两个命名空间永不相等，
  失败场景被强制 green，且增量路径无法恢复）。

夹具工厂（决策 19：不手写合成身份）全部由真实产物派生：编译测试名用
真实编译器命名函数 `_ident`；`failing_tests` 用真实解析器
`_parse_failing_tests` 从 GUT 运行流提取；`executed_scripts` 用真实
识别规则 `executed_script_headers`；`failing_scenarios` 走真实腿端回写
链 `_scenario_map_from_manifests` + `_failing_scenarios_backfill`。
场景名取 KOL 真实形态（`steady_performance` feature）。
"""

from __future__ import annotations

import copy

from godot_qa_toolkit.gherkin.compile import _ident
from godot_qa_toolkit.measurement.attribution import _gherkin_feature_of
from godot_qa_toolkit.measurement.increment import _verdict_gherkin_rerun
from godot_qa_toolkit.mutation.gutrun import (
    _parse_failing_tests,
    executed_script_headers,
)

FEATURE = "steady_performance"
FAIL_SCENARIO = "50×50 最坏混合布局单秒稳态求解 p95 预算"
PASS_SCENARIO = "初始点赞数为零时基线成立"
GEN_SCRIPT = ("res://gherkin/generated/.gqt-run-0123456789ab/generated/"
              "test_steady_performance.gd")


# scenario 的编译索引由 feature 文件位次固定（真实编译形态：同一 scenario
# 每次编译得同一测试名，跨分片/跨轮一致）。
_SCENARIO_INDEX = {FAIL_SCENARIO: 0, PASS_SCENARIO: 1}


def _test_name(scenario: str) -> str:
    """真实编译产物形态：`_ident(scenario.name, index) + "_<case:02d>"`。"""
    return f"{_ident(scenario, _SCENARIO_INDEX[scenario])}_00"


def _ident_of(test_name: str) -> str:
    return f"gherkin://feature/{FEATURE}::{test_name}"


def _gut_out(failing: list[str], passing: list[str]) -> str:
    """真实 GUT 运行流形态：res:// 脚本头 + `* 测试名` + [Failed]/[passed]。"""
    lines = [GEN_SCRIPT]
    for name in passing:
        lines += [f"* {name}", "  assert ok  [passed]"]
    for name in failing:
        lines += [f"* {name}", "  assertion failed  [Failed]"]
    return "\n".join(lines)


def _leg_account(failing_scenarios: list[str],
                 passing_scenarios: list[str],
                 rc: int = 1) -> dict:
    """真实腿执行账（`increment_runner._gherkin_leg_execute` 产物的形态）：
    failing_tests 由真实解析器从完整输出提取（截断前）；executed_scripts
    由真实识别规则提取；failing_scenarios 由真实回写链生成。"""
    fail_names = [_test_name(s) for s in failing_scenarios]
    pass_names = [_test_name(s) for s in passing_scenarios]
    out = _gut_out(fail_names, pass_names)
    from godot_qa_toolkit.measurement.increment_runner import (
        _failing_scenarios_backfill,
        _scenario_map_from_manifests,
    )

    manifest = {"manifests": {FEATURE: {"attribution": [
        {"test_name": n, "scenario": s}
        for s, n in list(zip(failing_scenarios, fail_names))
        + list(zip(passing_scenarios, pass_names))]}}}
    scenario_of_test = _scenario_map_from_manifests(manifest, [FEATURE])
    ex = {"shard": GEN_SCRIPT.rsplit("/", 1)[0], "rc": rc,
          "out": out[-4000:],
          "failing_tests": sorted(_parse_failing_tests(out)),
          "executed_scripts": sorted(executed_script_headers(out)),
          "error": None}
    _failing_scenarios_backfill([ex], scenario_of_test)
    return ex


def _baseline_table() -> dict:
    """全量建图形态的判定表基线（失败身份 red/pre_existing，其余 green）。"""
    table = {_ident_of(_test_name(PASS_SCENARIO)):
             {"verdict": "green"}}
    table[_ident_of(_test_name(FAIL_SCENARIO))] = {
        "verdict": "red", "classification": "pre_existing",
        "script": GEN_SCRIPT}
    return table


def _fail_ident() -> str:
    return _ident_of(_test_name(FAIL_SCENARIO))


def _pass_ident() -> str:
    return _ident_of(_test_name(PASS_SCENARIO))


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
