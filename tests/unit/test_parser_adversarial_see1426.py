"""SEE-1426 Round A hardener — parser schema 冻结对抗性单测（§SPEC-1426-04/05 面）。

对抗预设：schema 冻结存在静默放行路径。覆盖既有套件未及的 fail-fast 形态：
行宽大于表头、重复 Examples 表、表头零数据行、负数 numeric cell 无类型、
types 行只声明空白。正例面：feature 级 types 归一化传递入场景 IR；
per-scenario types 语义（两场景同名占位符各声明各型）不得互相误报冲突。

发现披露（见 hardener 汇报，不修复）：
- Examples 重复列名当前静默去重（后列覆盖前列），fail-fast 语义缺口；
- 非 Outline 场景 `{x}` 静默进 IR（runner 轨共享 parser，拒绝应在 compile
  侧落）——违反「编译轨/IR 只认 <x>」归一化。
"""

from __future__ import annotations

import pytest

from godot_qa_toolkit.accept.compile import build_ir
from godot_qa_toolkit.accept.parser import GherkinSyntaxError, parse


# ---------- fail-fast 盲区补齐（必 parse error） ----------

def test_row_wider_than_header_parse_error() -> None:
    # 既有套件只测行比头窄；行比头宽同为 ragged，必须打回。
    src = """Feature: d
  Scenario Outline: s <x>
    Given a <x>
    Examples:
      | x |
      | 1 | 2 |
"""
    with pytest.raises(GherkinSyntaxError, match="row width"):
        parse(src)


def test_duplicate_examples_table_parse_error() -> None:
    src = """Feature: d
  Scenario Outline: s <x>
    Given a <x>
    Examples:
      | x |
      | 1 |
    Examples:
      | x |
      | 2 |
"""
    with pytest.raises(GherkinSyntaxError, match="duplicate Examples"):
        parse(src)


def test_examples_zero_data_rows_parse_error() -> None:
    # 表头在场但零数据行：Outline 无可执行实例，等同 no Examples。
    src = """Feature: d
  Scenario Outline: s <x>
    Given a <x>
    Examples:
      | x |
"""
    with pytest.raises(GherkinSyntaxError, match="no Examples"):
        parse(src)


def test_negative_numeric_then_without_types_parse_error() -> None:
    # 负数/小数形态同属 numeric cell 判定域（-3.5 含负号与小数点）。
    src = """Feature: d
  Scenario Outline: s <hp>
    Then remaining hp is <left>
    Examples:
      | hp | left |
      | 10 | -3.5 |
"""
    with pytest.raises(GherkinSyntaxError, match="no # types: declaration"):
        parse(src)


def test_types_line_whitespace_only_parse_error() -> None:
    with pytest.raises(GherkinSyntaxError, match="declares nothing"):
        parse("Feature: d\n  # types:   \n  Scenario Outline: s <x>\n"
              "    Given a <x>\n    Examples:\n      | x |\n      | 1 |\n")


# ---------- 正例面（归一化与 per-scenario 语义） ----------

def test_feature_types_flow_into_scenario_ir() -> None:
    # feature 级 # types: 必须传入场景 IR（归一化单点，不因声明层级丢失）。
    src = """Feature: d
  # types: <hp>:int, <left>:numeric
  Scenario Outline: s <hp>
    Given hp is <hp>
    Then remaining hp is <left>
    Examples:
      | hp | left |
      | 10 | 7    |
"""
    ir = build_ir(src, "f.feature")
    assert ir["cases"][0]["types"] == {"hp": "int", "left": "numeric"}


def test_same_placeholder_name_across_scenarios_no_false_conflict() -> None:
    # 同名占位符在两个场景各自声明不同类型 = per-scenario 语义，合法；
    # 冲突校验只对同一声明域生效，跨场景误报 = 过严缺陷。
    src = """Feature: d
  Scenario Outline: a <hp>
    Given hp is <hp>
    # types: <hp>:int
    Examples:
      | hp |
      | 1  |
  Scenario Outline: b <hp>
    Then remaining hp is <hp>
    # types: <hp>:numeric
    Examples:
      | hp  |
      | 2.5 |
"""
    f = parse(src)
    assert f.scenarios[0].types["hp"] == "int"
    assert f.scenarios[1].types["hp"] == "numeric"
