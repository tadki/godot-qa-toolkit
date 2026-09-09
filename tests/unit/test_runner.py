"""Unit tests for the Gherkin runner (SEE-1268 M1, TDD)."""

import pytest

from godot_qa_toolkit.gherkin.parser import parse
from godot_qa_toolkit.gherkin.runner import Registry, run_feature

FEATURE = """\
Feature: Leaderboard refresh
  Background:
    Given the leaderboard is loaded

  Scenario: Player taps refresh
    Given 3 hearts
    When the player taps refresh
    Then ranks update within 200ms

  Scenario: Refresh failure
    Given 0 hearts
    When refresh fails
    Then the retry button appears
"""


class TestRunFeature:
    def test_all_pass(self):
        registry = Registry()

        @registry.step("the leaderboard is loaded", keyword="Given")
        def bg(ctx):
            ctx["loaded"] = True

        @registry.step("{n} hearts")
        def hearts(ctx, n):
            ctx["hearts"] = int(n)

        @registry.step("the player taps refresh", keyword="When")
        def tap(ctx):
            pass

        @registry.step("refresh fails", keyword="When")
        def fail_refresh(ctx):
            ctx["failed"] = True

        @registry.step("ranks update within {ms}ms", keyword="Then")
        def ranks(ctx, ms):
            assert not ctx.get("failed")

        @registry.step("the retry button appears", keyword="Then")
        def retry(ctx):
            assert ctx.get("failed")

        result = run_feature(parse(FEATURE), registry)
        assert result["ok"] is True
        assert result["summary"] == {"total": 2, "passed": 2, "failed": 0}
        assert result["failures"] == []

    def test_failure_recorded_with_context(self):
        registry = Registry()

        @registry.step("the leaderboard is loaded", keyword="Given")
        def bg(ctx):
            pass

        @registry.step("{n} hearts")
        def hearts(ctx, n):
            pass

        @registry.step("the player taps refresh", keyword="When")
        def tap(ctx):
            pass

        @registry.step("refresh fails", keyword="When")
        def fail_refresh(ctx):
            pass

        @registry.step("ranks update within {ms}ms", keyword="Then")
        def ranks(ctx, ms):
            raise AssertionError("ranks did not update")

        @registry.step("the retry button appears", keyword="Then")
        def retry(ctx):
            pass

        result = run_feature(parse(FEATURE), registry)
        assert result["ok"] is False
        assert result["summary"]["failed"] == 1
        f = result["failures"][0]
        assert f["scenario"] == "Player taps refresh"
        assert f["reason"].startswith("AssertionError")

    def test_missing_step_def_is_a_failure(self):
        registry = Registry()  # nothing registered
        result = run_feature(parse(FEATURE), registry)
        assert result["ok"] is False
        assert all(f["reason"] == "no matching step definition" for f in result["failures"])

    def test_background_runs_before_each_scenario(self):
        registry = Registry()
        calls = []

        @registry.step("the leaderboard is loaded", keyword="Given")
        def bg(ctx):
            calls.append("bg")

        @registry.step("{n} hearts")
        def hearts(ctx, n):
            calls.append("hearts")

        @registry.step("the player taps refresh", keyword="When")
        def tap(ctx):
            pass

        @registry.step("refresh fails", keyword="When")
        def fail_refresh(ctx):
            pass

        @registry.step("ranks update within {ms}ms", keyword="Then")
        def ranks(ctx, ms):
            pass

        @registry.step("the retry button appears", keyword="Then")
        def retry(ctx):
            pass

        run_feature(parse(FEATURE), registry)
        assert calls == ["bg", "hearts", "bg", "hearts"]

    def test_hooks_fire_around_scenarios(self):
        registry = Registry()
        events = []

        registry.before_scenario(lambda: events.append("before"))
        registry.after_scenario(lambda: events.append("after"))

        @registry.step("the leaderboard is loaded", keyword="Given")
        def bg(ctx):
            pass

        @registry.step("{n} hearts")
        def hearts(ctx, n):
            pass

        @registry.step("the player taps refresh", keyword="When")
        def tap(ctx):
            pass

        @registry.step("refresh fails", keyword="When")
        def fail_refresh(ctx):
            pass

        @registry.step("ranks update within {ms}ms", keyword="Then")
        def ranks(ctx, ms):
            pass

        @registry.step("the retry button appears", keyword="Then")
        def retry(ctx):
            pass

        run_feature(parse(FEATURE), registry)
        assert events == ["before", "after", "before", "after"]
