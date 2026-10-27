"""SEE-1463 hardener — parser 拆解（31ef28c）行为等价验证 survivor 补刀用例。

§SPEC-011② 注入-kill-还原第一轮 6 处变异中 2 处存活，暴露的判别力盲区：
- P01 `_validate_non_outline_steps` 校验顺序互换存活——既有夹具无非 Outline
  步骤文本同时含 `{x}` 与 `<x>` 的形态，`{x}` 优先报错未被钉死；
- P04 `_require_examples_table` 弱化为只拒"无场景"存活——普通（非 Outline）
  Scenario 下出现 `Examples:` 表的 fail-fast 分支零守护，变异后垃圾表静默
  被收进 non-outline 场景且 `_validate_scenario` 提前 return 不再校验。

本文件两用例分别钉死上述顺序与分支（对原实现绿、对变异体红）。
"""

from __future__ import annotations

import pytest

from godot_qa_toolkit.accept.parser import GherkinSyntaxError, parse


def test_non_outline_mixed_placeholders_runtime_check_first() -> None:
    # 同一步骤文本同时含 {x} 与 <x>：runtime {x} 校验先于 <x> 校验触发，
    # 错误信息必须是 runtime placeholder 形态（P01 顺序互换变异判别器）。
    src = """Feature: d
  Scenario: plain
    Given save {hp} into <target>
"""
    with pytest.raises(GherkinSyntaxError, match=r"runtime placeholder \{hp\}"):
        parse(src)


def test_examples_under_plain_scenario_parse_error() -> None:
    # 普通 Scenario（非 Outline）下出现 Examples: 表 = parse error；
    # 变异后（只拒无场景）该表被静默收进 non-outline 场景且不再校验
    # （P04 判别器）。
    src = """Feature: d
  Scenario: plain
    Given a step
    Examples:
      | a |
      | 1 |
"""
    with pytest.raises(GherkinSyntaxError, match="outside a Scenario Outline"):
        parse(src)
