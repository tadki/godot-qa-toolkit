"""Runner P0 additions (SEE-1306 BL-8): scenario timeout + failure context.

The three-layer debugging chain the plan-debate settled on:
  1. normal failure  -> `detail` field carried by the raised exception
  2. timeout         -> ScenarioTimeout failure, ctx `_detail` preserved
  3. silent success  -> detected game-side (stderr scan), surfaced here as a
                        step failure with `stderr_tail` populated
"""

import time

import pytest

from godot_qa_toolkit.gherkin.parser import parse
from godot_qa_toolkit.gherkin.runner import (
    Registry,
    ScenarioTimeout,
    StepFailure,
    run_feature,
)

FEATURE_HANG = """\
Feature: timeout behaviour
  Scenario: hangs
    When the probe hangs

  Scenario: answers
    When the probe answers
"""

FEATURE_CTX = """\
Feature: failure context
  Scenario: fails with detail
    When the probe fails with detail

  Scenario: answers
    When the probe answers
"""


def _hang_registry(hang_s: float = 0.0, pre_detail: str | None = None) -> Registry:
    registry = Registry()

    @registry.step("the probe hangs", keyword="When")
    def hang(ctx):
        if pre_detail is not None:
            ctx["_detail"] = pre_detail
        time.sleep(hang_s)

    @registry.step("the probe answers", keyword="When")
    def answer(ctx):
        pass

    return registry


def _ctx_registry() -> Registry:
    registry = Registry()

    @registry.step("the probe fails with detail", keyword="When")
    def fail(ctx):
        raise StepFailure(
            reason="like_total mismatch",
            detail="expected 1.0, got 0.0",
            stderr_tail="SCRIPT ERROR: boom\n",
        )

    @registry.step("the probe answers", keyword="When")
    def answer(ctx):
        pass

    return registry


class TestScenarioTimeout:
    def test_hanging_scenario_fails_with_timeout_reason(self):
        result = run_feature(parse(FEATURE_HANG), _hang_registry(hang_s=5.0), scenario_timeout_s=0.2)
        assert result["ok"] is False
        f = result["failures"][0]
        assert f["scenario"] == "hangs"
        assert f["reason"].startswith("ScenarioTimeout")

    def test_scenario_within_budget_passes(self):
        result = run_feature(parse(FEATURE_HANG), _hang_registry(hang_s=0.0), scenario_timeout_s=5.0)
        assert result["summary"] == {"total": 2, "passed": 2, "failed": 0}

    def test_timeout_does_not_abort_remaining_scenarios(self):
        result = run_feature(parse(FEATURE_HANG), _hang_registry(hang_s=5.0), scenario_timeout_s=0.2)
        # One timeout is one failed scenario, not a dead run — CI must report
        # the whole picture.
        assert result["summary"] == {"total": 2, "passed": 1, "failed": 1}

    def test_no_timeout_configured_keeps_legacy_behaviour(self):
        result = run_feature(parse(FEATURE_HANG), _hang_registry(hang_s=0.0))
        assert result["summary"]["passed"] == 2

    def test_timeout_keeps_detail_recorded_before_the_hang(self):
        registry = _hang_registry(hang_s=5.0, pre_detail="probe pid 4242")
        result = run_feature(parse(FEATURE_HANG), registry, scenario_timeout_s=0.2)
        f = result["failures"][0]
        assert f["detail"] == "probe pid 4242"


class TestScenarioTimeoutIsolation:
    def test_timeout_class_is_exported(self):
        assert issubclass(ScenarioTimeout, Exception)

    def test_platform_without_sigalrm_degrades_to_untimed(self, monkeypatch):
        # Windows has no SIGALRM: the runner must still execute (un-timed)
        # instead of crashing. The hang protection there is the game-side
        # probe's own subprocess timeout.
        import types

        import godot_qa_toolkit.gherkin.runner as runner_mod

        monkeypatch.setattr(runner_mod, "signal", types.SimpleNamespace(), raising=True)
        result = run_feature(parse(FEATURE_HANG), _hang_registry(hang_s=0.0),
                             scenario_timeout_s=5.0)
        assert result["summary"]["passed"] == 2

    def test_hooks_still_fire_after_a_timeout(self):
        registry = _hang_registry(hang_s=5.0)
        events = []
        registry.before_scenario(lambda: events.append("before"))
        registry.after_scenario(lambda: events.append("after"))

        run_feature(parse(FEATURE_HANG), registry, scenario_timeout_s=0.2)

        # Cleanup hooks must survive a timed-out scenario: save isolation
        # depends on after_scenario running even when a step hung.
        assert events == ["before", "after", "before", "after"]


class TestFailureContext:
    def test_detail_and_stderr_tail_reach_the_verdict(self):
        result = run_feature(parse(FEATURE_CTX), _ctx_registry())
        f = result["failures"][0]
        assert f["scenario"] == "fails with detail"
        assert "like_total mismatch" in f["reason"]
        assert f["detail"] == "expected 1.0, got 0.0"
        assert f["stderr_tail"] == "SCRIPT ERROR: boom\n"

    def test_plain_exception_still_reports_reason_without_noise(self):
        registry = Registry()

        @registry.step("the probe fails with detail", keyword="When")
        def fail(ctx):
            raise AssertionError("plain")

        @registry.step("the probe answers", keyword="When")
        def answer(ctx):
            pass

        result = run_feature(parse(FEATURE_CTX), registry)
        f = result["failures"][0]
        assert f["reason"].startswith("AssertionError")
        # Absent context is omitted rather than emitted as empty noise.
        assert "detail" not in f
        assert "stderr_tail" not in f


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
