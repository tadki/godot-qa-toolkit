"""gherkin 增量判定链的共享夹具工厂（SEE-1654 tidy-R6 抽取）。

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
消费方：`test_c3r27_gherkin_verdict_namespace_see1654.py`、
`test_c2r4_qa_d7_d11_see1631.py`（D8）、`test_c3_integration_see1631.py`
（D14）。
"""

from __future__ import annotations

from godot_qa_toolkit.gherkin.compile import _ident
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
