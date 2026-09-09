"""Gherkin scenario runner (SEE-1268 M1).

Executes parsed scenarios against a step-def registry. A step-def maps a
step pattern (exact text or a {param} placeholder matcher) to a callable
receiving the resolved parameters. Runner output is the machine verdict:
plain dicts that the CLI serializes to the unified JSON contract.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .parser import Feature, Step

StepFn = Callable[..., None]


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
            d.fn(ctx, **params)
        except Exception as e:  # noqa: BLE001 — the failure IS the result (P0')
            return {"keyword": step.keyword, "text": step.text, "line": step.line,
                    "reason": f"{type(e).__name__}: {e}"}
    return None


def run_feature(feature: Feature, registry: Registry) -> dict:
    """Run every scenario; returns the unified-contract result dict."""
    failures: list[dict] = []
    passed = 0
    for scenario in feature.scenarios:
        for hook in registry._hooks["before_scenario"]:
            hook()
        failure = _run_steps(feature.background + scenario.steps, registry, {})
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
