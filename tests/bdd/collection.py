"""Collection-time pytest-bdd step-resolution audit (SEE-1468 M0b)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from _pytest.fixtures import FixtureManager
from pytest_bdd.parser import Scenario, ScenarioTemplate, Step
from pytest_bdd.scenario import (
    find_fixturedefs_for_step,
    scenario_wrapper_template_registry,
)
from pytest_bdd.steps import step_function_context_registry

SCENARIO_TIMEOUT_S = 60


@dataclass(frozen=True)
class BddCollection:
    """The BDD-only view of a pytest collection, consumed by meta tests."""

    items: list[str]
    timeouts: list[dict]
    scenarios: dict[str, dict]
    report: dict


def _scenario_template(item: pytest.Item) -> ScenarioTemplate | None:
    obj = getattr(item, "obj", None)
    if not callable(obj):
        return None
    return scenario_wrapper_template_registry.get(obj)


def _example_context(item: pytest.Item) -> dict[str, str]:
    """Return this collected item's Scenario Outline example values."""

    callspec = getattr(item, "callspec", None)
    example = getattr(callspec, "params", {}).get("_pytest_bdd_example")
    return dict(example) if example else {}


def _definition_matches(
    step: Step, fixturemanager: FixtureManager, item: pytest.Item
) -> list[dict]:
    matches = []
    fixturedefs = find_fixturedefs_for_step(step, fixturemanager, item)
    for fixturedef in fixturedefs:
        context = step_function_context_registry.get(fixturedef.func)
        if context is None:
            continue
        func = context.step_func
        matches.append(
            {
                "definition": f"{func.__module__}.{func.__qualname__}",
                "keyword": context.type or "*",
                "parser": context.parser.name,
            }
        )
    return sorted(matches, key=lambda match: match["definition"])


def _map_scenario_steps(
    item: pytest.Item, scenario: Scenario, fixturemanager: FixtureManager
) -> list[dict]:
    mappings = []
    for step in scenario.steps:
        matches = _definition_matches(step, fixturemanager, item)
        mappings.append(
            {
                "nodeid": item.nodeid,
                "scenario": scenario.name,
                "keyword": step.type,
                "text": step.name,
                "line": step.line_number,
                "match_count": len(matches),
                "matches": matches,
            }
        )
    return mappings


def _timeout_metadata(item: pytest.Item) -> dict:
    marker = item.get_closest_marker("timeout")
    if marker is None:
        item.add_marker(
            pytest.mark.timeout(SCENARIO_TIMEOUT_S, method="signal", func_only=True)
        )
        marker = item.get_closest_marker("timeout")
    assert marker is not None
    return {
        "nodeid": item.nodeid,
        "args": list(marker.args),
        "kwargs": dict(marker.kwargs),
    }


def _resolution_report(mappings: list[dict]) -> dict:
    unmatched = sum(mapping["match_count"] == 0 for mapping in mappings)
    ambiguous = sum(mapping["match_count"] > 1 for mapping in mappings)
    unique = len(mappings) - unmatched - ambiguous
    return {
        "tool": "bdd-step-resolution",
        "ok": unmatched == 0 and ambiguous == 0,
        "summary": {
            "total_steps": len(mappings),
            "unique": unique,
            "unmatched": unmatched,
            "ambiguous": ambiguous,
        },
        "steps": mappings,
    }


def collect_bdd_items(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> BddCollection:
    """Resolve every collected BDD step without executing scenario bodies."""

    nodeids: list[str] = []
    timeouts: list[dict] = []
    scenarios: dict[str, dict] = {}
    mappings: list[dict] = []
    fixturemanager = session._fixturemanager
    for item in items:
        template = _scenario_template(item)
        if template is None:
            continue
        scenario = template.render(_example_context(item))
        nodeids.append(item.nodeid)
        timeouts.append(_timeout_metadata(item))
        scenarios[item.nodeid] = {
            "scenario": scenario.name,
            "line": getattr(scenario, "line_number", 0),
        }
        mappings.extend(_map_scenario_steps(item, scenario, fixturemanager))
    return BddCollection(nodeids, timeouts, scenarios, _resolution_report(mappings))


def selected_bdd_nodeids(items: list[pytest.Item]) -> list[str]:
    """Return only scenarios remaining after pytest's deselection hooks."""

    return [item.nodeid for item in items if _scenario_template(item) is not None]


def write_resolution_report(config: pytest.Config, report: dict) -> None:
    """Persist the dry-run artifact when explicitly requested by the caller."""

    destination = config.getoption("--bdd-step-report")
    if destination is None:
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
