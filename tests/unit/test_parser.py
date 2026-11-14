"""Unit tests for the Gherkin parser (SEE-1268 M1, TDD)."""

import pytest

from godot_qa_toolkit.gherkin.parser import (
    Feature,
    GherkinSyntaxError,
    parse,
)

SAMPLE = """\
Feature: Leaderboard refresh
  Background:
    Given the leaderboard is loaded

  @happy-path
  Scenario: Player taps refresh
    Given 3 hearts in inventory
    When the player taps refresh
    Then ranks update within 200ms
    And the streak icon is visible

  Scenario: Refresh failure
    When refresh fails
    Then the retry button appears
"""


class TestParseHappyPath:
    def test_feature_name_and_line(self):
        f = parse(SAMPLE)
        assert isinstance(f, Feature)
        assert f.name == "Leaderboard refresh"
        assert f.line == 1

    def test_background_steps_collected(self):
        f = parse(SAMPLE)
        assert [s.text for s in f.background] == ["the leaderboard is loaded"]
        assert f.background[0].keyword == "Given"

    def test_scenarios_with_tags(self):
        f = parse(SAMPLE)
        assert len(f.scenarios) == 2
        assert f.scenarios[0].name == "Player taps refresh"
        assert f.scenarios[0].tags == ["happy-path"]
        assert f.scenarios[1].tags == []

    def test_and_chains_prior_keyword(self):
        f = parse(SAMPLE)
        steps = f.scenarios[0].steps
        assert [s.keyword for s in steps] == ["Given", "When", "Then", "Then"]
        assert steps[-1].text == "the streak icon is visible"

    def test_step_lines_are_tracked(self):
        f = parse(SAMPLE)
        assert f.scenarios[0].steps[0].line == 7


class TestParseErrors:
    def test_line_before_feature_raises(self):
        with pytest.raises(GherkinSyntaxError) as exc:
            parse("Scenario: orphan\n")
        assert ":1:" in str(exc.value)

    def test_missing_feature_raises(self):
        with pytest.raises(GherkinSyntaxError):
            parse("")

    def test_unrecognized_line_raises(self):
        text = "Feature: X\n  blah blah\n"
        with pytest.raises(GherkinSyntaxError) as exc:
            parse(text, path="spec.feature")
        assert "spec.feature:2" in str(exc.value)

    def test_step_outside_scenario_raises(self):
        text = "Feature: X\n    Given nothing yet\n"
        with pytest.raises(GherkinSyntaxError):
            parse(text)

    def test_and_before_gwt_raises(self):
        text = "Feature: X\n  Scenario: s\n    And more\n"
        with pytest.raises(GherkinSyntaxError):
            parse(text)

    def test_duplicate_feature_raises(self):
        text = "Feature: A\nFeature: B\n"
        with pytest.raises(GherkinSyntaxError):
            parse(text)

    def test_invalid_tag_raises(self):
        text = "Feature: A\nnotatag\n"
        with pytest.raises(GherkinSyntaxError):
            parse(text)

    def test_comments_ignored(self):
        text = "Feature: A\n  # a comment\n  Scenario: s\n"
        f = parse(text)
        assert len(f.scenarios) == 1


class TestButKeyword:
    def test_but_chains_prior_keyword(self):
        text = "Feature: X\n  Scenario: s\n    Given a\n    Then b\n    But not c\n"
        f = parse(text)
        assert f.scenarios[0].steps[-1].keyword == "Then"


class TestAndButInheritanceBoundary:
    """last_kind 跨 block 泄漏回归（SEE-1420 hardener 裁决修复：Refacty
    tidy 移交——And/But 继承只在本 block 内生效，新 block 头重置）。"""

    SCENARIO_THEN_END = (
        "Feature: X\n"
        "  Scenario: first\n"
        "    Given a\n"
        "    Then b\n"
        "\n"
        "  Scenario: second\n"
    )

    def test_and_starting_second_scenario_raises(self):
        with pytest.raises(GherkinSyntaxError) as exc:
            parse(self.SCENARIO_THEN_END + "    And more\n")
        assert "And before any Given/When/Then" in str(exc.value)
        assert ":7:" in str(exc.value)

    def test_but_starting_second_scenario_raises(self):
        with pytest.raises(GherkinSyntaxError):
            parse(self.SCENARIO_THEN_END + "    But nope\n")

    def test_scenario_outline_header_also_resets(self):
        with pytest.raises(GherkinSyntaxError):
            parse(self.SCENARIO_THEN_END
                  + "  Scenario Outline: o\n    And more\n")

    def test_background_after_scenario_resets(self):
        text = ("Feature: X\n"
                "  Scenario: s1\n"
                "    Then done\n"
                "  Background:\n"
                "    And chained\n")
        with pytest.raises(GherkinSyntaxError):
            parse(text)

    def test_given_starting_second_scenario_still_parses(self):
        f = parse(self.SCENARIO_THEN_END + "    Given c\n    And d\n")
        steps = f.scenarios[1].steps
        assert [s.keyword for s in steps] == ["Given", "Given"]

    def test_within_scenario_chaining_preserved_after_fix(self):
        text = ("Feature: X\n"
                "  Scenario: s1\n"
                "    Given a\n"
                "    Then b\n"
                "\n"
                "  Scenario: s2\n"
                "    Given c\n"
                "    When d\n"
                "    Then e\n"
                "    And f\n"
                "    But g\n")
        f = parse(text)
        assert [s.keyword for s in f.scenarios[1].steps] == \
            ["Given", "When", "Then", "Then", "Then"]

    def test_and_first_background_step_still_raises(self):
        with pytest.raises(GherkinSyntaxError):
            parse("Feature: X\n  Background:\n    And first\n")
