"""Gherkin scenario runner (SEE-1268 M1; SEE-1306 timeout + failure context).

Executes parsed scenarios against a step-def registry. A step-def maps a
step pattern (exact text or a {param} placeholder matcher) to a callable
receiving the resolved parameters. Runner output is the machine verdict:
plain dicts that the CLI serializes to the unified JSON contract.
"""

from __future__ import annotations

import re
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .parser import Feature, Step

StepFn = Callable[..., None]


class StepFailure(Exception):
    """Structured step failure (SEE-1306 three-layer debugging chain).

    `detail` and `stderr_tail` carry the context the Coder needs
    (expected/actual, probe stderr tail) beyond the machine-facing reason.
    """

    def __init__(self, reason: str, detail: str | None = None, stderr_tail: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.stderr_tail = stderr_tail


class ScenarioTimeout(Exception):
    """A scenario blew its wall-clock budget (SEE-1306 §SPEC-006)."""


@dataclass
class StepDef:
    pattern: str
    keyword: str | None  # None matches any of Given/When/Then
    fn: StepFn
    _regex: re.Pattern | None = field(default=None, init=False, repr=False)

    def match(self, step: Step) -> dict[str, str] | None:
        if self.keyword is not None and self.keyword != step.keyword:
            return None
        if self._regex is None:
            # {name} placeholders capture one or more non-whitespace chars.
            regex_src = re.sub(r"\{(\w+)\}", r"(?P<\1>\\S+)", re.escape(self.pattern).replace(r"\{", "{").replace(r"\}", "}"))
            self._regex = re.compile(f"^{regex_src}$")
        m = self._regex.match(step.text)
        return m.groupdict() if m else None


class Registry:
    """Step-def registry. Decorator or direct registration both supported."""

    def __init__(self) -> None:
        self._defs: list[StepDef] = []
        self._hooks: dict[str, list[Callable[[], None]]] = {
            "before_scenario": [],
            "after_scenario": [],
        }

    def step(self, pattern: str, keyword: str | None = None) -> Callable[[StepFn], StepFn]:
        def deco(fn: StepFn) -> StepFn:
            self._defs.append(StepDef(pattern=pattern, keyword=keyword, fn=fn))
            return fn

        return deco

    def before_scenario(self, fn: Callable[[], None]) -> Callable[[], None]:
        self._hooks["before_scenario"].append(fn)
        return fn

    def after_scenario(self, fn: Callable[[], None]) -> Callable[[], None]:
        self._hooks["after_scenario"].append(fn)
        return fn

    def find(self, step: Step) -> tuple[StepDef, dict[str, str]] | None:
        for d in self._defs:
            params = d.match(step)
            if params is not None:
                return d, params
        return None


def _run_steps(steps: list[Step], registry: Registry, ctx: dict) -> dict | None:
    """Run steps in order; return a failure dict on first failure, else None."""
    for step in steps:
        found = registry.find(step)
        if found is None:
            return {"keyword": step.keyword, "text": step.text, "line": step.line,
                    "reason": "no matching step definition"}
        d, params = found
        try:
            ctx["_step"] = {"keyword": step.keyword, "text": step.text, "line": step.line}
            d.fn(ctx, **params)
        except ScenarioTimeout:
            # The deadline handler owns this failure (it knows the budget and
            # can attach ctx detail); never flatten it into a step error.
            raise
        except StepFailure as e:
            out = {"keyword": step.keyword, "text": step.text, "line": step.line,
                   "reason": f"{type(e).__name__}: {e.reason}"}
            # Structured context is optional: emit only what the step supplied
            # so ordinary assertion failures stay free of empty-field noise.
            if e.detail is not None:
                out["detail"] = e.detail
            if e.stderr_tail is not None:
                out["stderr_tail"] = e.stderr_tail
            return out
        except Exception as e:  # noqa: BLE001 — the failure IS the result (P0')
            return {"keyword": step.keyword, "text": step.text, "line": step.line,
                    "reason": f"{type(e).__name__}: {e}"}
    return None


def _run_steps_with_deadline(
    steps: list[Step], registry: Registry, ctx: dict, scenario_timeout_s: float
) -> dict | None:
    """Run steps under a wall-clock budget enforced by SIGALRM.

    A hung step (a probe that never returns) must fail its scenario, not hang
    the run: the alarm aborts the in-process execution and the scenario is
    reported as ScenarioTimeout while remaining scenarios still execute.
    Cleanup hooks stay under the caller's control (run_feature), so save
    isolation's after_scenario cleanup runs even after a timeout.
    """
    # PEP 8 aside: hasattr on the module so the Windows (no-SIGALRM) branch is
    # honestly exercised instead of assumed.
    if not hasattr(signal, "SIGALRM"):
        # No interval timers on this platform: degrade to un-timed execution.
        # The CI target is Linux; primary hang protection still lives in the
        # game-side probe's own subprocess timeout.
        return _run_steps(steps, registry, ctx)

    def _on_alarm(signum, frame):  # noqa: ANN001 — signal handler signature
        raise ScenarioTimeout(f"exceeded {scenario_timeout_s}s budget")

    old_handler = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, scenario_timeout_s)
    try:
        return _run_steps(steps, registry, ctx)
    except ScenarioTimeout as e:
        # Anchor the timeout on the step that was executing (or the last one
        # recorded) so CI output points somewhere actionable.
        step_info = ctx.get("_step") or {"keyword": "", "text": "", "line": 0}
        out: dict = {
            "keyword": step_info.get("keyword", ""),
            "text": step_info.get("text", ""),
            "line": step_info.get("line", 0),
            "reason": f"ScenarioTimeout: {e}",
        }
        # A step may have recorded context before hanging (probe pid, phase).
        detail = ctx.get("_detail")
        if detail is not None:
            out["detail"] = detail
        return out
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def run_feature(feature: Feature, registry: Registry, scenario_timeout_s: float | None = None) -> dict:
    """Run every scenario; returns the unified-contract result dict.

    A per-scenario wall-clock budget applies only when `scenario_timeout_s` is
    given; without it the runner keeps the legacy un-timed behaviour.
    """
    failures: list[dict] = []
    passed = 0
    for scenario in feature.scenarios:
        for hook in registry._hooks["before_scenario"]:
            hook()
        ctx: dict = {"_scenario": scenario.name}
        if scenario_timeout_s is None:
            failure = _run_steps(feature.background + scenario.steps, registry, ctx)
        else:
            failure = _run_steps_with_deadline(
                feature.background + scenario.steps, registry, ctx, scenario_timeout_s
            )
        for hook in registry._hooks["after_scenario"]:
            hook()
        if failure is None:
            passed += 1
        else:
            failures.append({"scenario": scenario.name, "line": scenario.line, **failure})
    total = len(feature.scenarios)
    return {
        "tool": "gherkin",
        "ok": not failures,
        "feature": feature.name,
        "summary": {"total": total, "passed": passed, "failed": total - passed},
        "failures": failures,
    }
