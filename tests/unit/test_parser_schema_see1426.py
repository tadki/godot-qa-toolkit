"""SEE-1426 P2a — parser schema 冻结五件套单测（§SPEC-1426-04/05/09）。

Examples 一等公民展开 + fail-fast 矩阵：表宽不齐 / 占位符无列 / Outline 无
Examples / 无 Outline 的 `# types:` 行 / 残留 `{x}` / 非 Outline `<x>` /
Then 绑定 numeric 无类型 全 parse error；`# types:` 同名冲突 = parse
error、`enum{...}` 域声明合法；`# unchanged:` 槽解析。
"""

from __future__ import annotations

import pytest

from godot_qa_toolkit.gherkin.parser import GherkinSyntaxError, parse

OUTLINE_OK = """Feature: demo
  Scenario Outline: attack <enemy>
    Given hp is <hp>
    Then remaining hp is <left>

    # types: <hp>:int, <left>:numeric
    Examples:
      | enemy | hp | left |
      | slime | 10 | 7    |
"""


def test_examples_first_class_expansion() -> None:
    f = parse(OUTLINE_OK)
    s = f.scenarios[0]
    assert s.outline is True
    rows = s.expansions()
    assert len(rows) == 1
    _, values, steps, types = rows[0]
    assert values == {"enemy": "slime", "hp": "10", "left": "7"}
    assert [st.text for st in steps] == ["hp is 10", "remaining hp is 7"]
    assert types == {"hp": "int", "left": "numeric"}


def test_outline_without_examples_is_parse_error() -> None:
    with pytest.raises(GherkinSyntaxError, match="no Examples"):
        parse("Feature: d\n  Scenario Outline: s <x>\n    Given a <x>\n")


def test_ragged_table_is_parse_error() -> None:
    src = """Feature: d
  Scenario Outline: s <x>
    Given a <x>
    Examples:
      | x | y |
      | 1 |
"""
    with pytest.raises(GherkinSyntaxError, match="row width"):
        parse(src)


def test_placeholder_without_column_is_parse_error() -> None:
    src = """Feature: d
  Scenario Outline: s <x>
    Given a <missing>
    Examples:
      | x |
      | 1 |
"""
    with pytest.raises(GherkinSyntaxError, match="no matching Examples column"):
        parse(src)


def test_non_outline_placeholder_is_parse_error() -> None:
    with pytest.raises(GherkinSyntaxError, match="non-Outline"):
        parse("Feature: d\n  Scenario: s\n    Given a <x>\n")


def test_runtime_placeholder_surviving_expansion_is_parse_error() -> None:
    src = """Feature: d
  Scenario Outline: s <x>
    Given a {param} <x>
    Examples:
      | x |
      | 1 |
"""
    with pytest.raises(GherkinSyntaxError, match="runtime placeholder"):
        parse(src)


def test_types_line_without_outline_is_parse_error() -> None:
    with pytest.raises(GherkinSyntaxError, match="no Scenario Outline"):
        parse("Feature: d\n  # types: <hp>:int\n  Scenario: s\n    Given a 1\n")


def test_numeric_then_without_types_is_parse_error() -> None:
    src = """Feature: d
  Scenario Outline: s <hp>
    Then remaining hp is <left>
    Examples:
      | hp | left |
      | 10 | 7    |
"""
    with pytest.raises(GherkinSyntaxError, match="no # types: declaration"):
        parse(src)


def test_types_conflict_is_parse_error() -> None:
    src = """Feature: d
  # types: <hp>:int
  Scenario Outline: s <hp>
    Given hp is <hp>
    # types: <hp>:numeric
    Then remaining hp is <left>
    # types: <left>:int
    Examples:
      | hp | left |
      | 10 | 7    |
"""
    with pytest.raises(GherkinSyntaxError, match="conflicting type declarations"):
        parse(src)


def test_enum_domain_declaration_is_legal() -> None:
    src = """Feature: d
  Scenario Outline: s <hp>
    Then mode is <mode>
    # types: <hp>:int, <mode>:enum{normal,stun}
    Examples:
      | hp | mode   |
      | 10 | normal |
"""
    f = parse(src)
    assert f.scenarios[0].types["mode"] == "enum{normal,stun}"


def test_malformed_types_entry_is_parse_error() -> None:
    with pytest.raises(GherkinSyntaxError, match="malformed # types:"):
        parse("Feature: d\n  # types: hp int\n  Scenario Outline: s <hp>\n"
              "    Given hp is <hp>\n    Examples:\n      | hp |\n      | 1 |\n")


def test_unchanged_slot_parsed() -> None:
    src = """Feature: d
  Scenario: s
    # unchanged: player.gold, world.tick
    Given a thing
"""
    f = parse(src)
    assert f.scenarios[0].unchanged == ["player.gold", "world.tick"]


def test_unchanged_malformed_entry_is_parse_error() -> None:
    with pytest.raises(GherkinSyntaxError, match="malformed # unchanged:"):
        parse("Feature: d\n  Scenario: s\n    # unchanged: gold\n    Given a\n")


def test_unchanged_outside_scenario_is_parse_error() -> None:
    with pytest.raises(GherkinSyntaxError, match="outside a Scenario"):
        parse("Feature: d\n  # unchanged: a.b\n  Scenario: s\n    Given a\n")


def test_plain_scenario_still_parses() -> None:
    f = parse("Feature: d\n  Scenario: s\n    Given a thing\n    Then it works\n")
    assert f.scenarios[0].expansions()[0][2][0].text == "a thing"
