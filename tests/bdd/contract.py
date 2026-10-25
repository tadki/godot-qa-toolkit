"""Unified gqt JSON contract projected from pytest-bdd reports."""

from __future__ import annotations

import json

import pytest
from _pytest.outcomes import Failed
from pytest_bdd.parser import Scenario, Step

from godot_qa_toolkit.gherkin.runner import StepFailure


def initialize(config: pytest.Config) -> None:
    config._gqt_bdd_nodeids = []
    config._gqt_bdd_reports = {}
    config._gqt_bdd_step_failures = {}


def _step_reason(exception: Exception) -> str:
    text = str(exception)
    if isinstance(exception, Failed) and "timeout" in text.lower():
        return f"ScenarioTimeout: {text}"
    return f"{type(exception).__name__}: {text}"


def record_step_error(
    request: pytest.FixtureRequest,
    scenario: Scenario,
    step: Step,
    exception: Exception,
    state: dict | None = None,
) -> None:
    """Keep fields that pytest's traceback-only report cannot represent."""

    failure = {
        "scenario": scenario.name,
        "line": step.line_number,
        "keyword": step.type.capitalize(),
        "text": step.name,
        "reason": _step_reason(exception),
    }
    if isinstance(exception, StepFailure):
        if exception.detail is not None:
            failure["detail"] = exception.detail
        if exception.stderr_tail is not None:
            failure["stderr_tail"] = exception.stderr_tail
    elif state is not None and state.get("_detail") is not None:
        failure["detail"] = str(state["_detail"])
    request.config._gqt_bdd_step_failures[request.node.nodeid] = failure


def record_test_report(item: pytest.Item, report: pytest.TestReport) -> None:
    if item.nodeid not in item.config._gqt_bdd_nodeids:
        return
    reports = item.config._gqt_bdd_reports.setdefault(item.nodeid, {})
    reports[report.when] = report


def _generic_failure(nodeid: str, report: pytest.TestReport, metadata: dict) -> dict:
    name = metadata.get("scenario", nodeid)
    line = metadata.get("line")
    if line is None:
        line = report.location[1] + 1 if report.location else 0
    return {
        "scenario": name,
        "line": line,
        "keyword": "",
        "text": name,
        "reason": str(report.longrepr) if report.longrepr else "failed",
    }


def _scenario_metadata(config: pytest.Config, nodeid: str) -> dict:
    collection = getattr(config, "_gqt_bdd_collection", None)
    scenarios = getattr(collection, "scenarios", {})
    return scenarios.get(nodeid, {})


def _scenario_failure(
    nodeid: str, config: pytest.Config, metadata: dict
) -> dict | None:
    structured = config._gqt_bdd_step_failures.get(nodeid)
    if structured is not None:
        return dict(structured)
    reports = config._gqt_bdd_reports.get(nodeid, {})
    for phase in ("setup", "call", "teardown"):
        report = reports.get(phase)
        if report is not None and report.failed:
            return _generic_failure(nodeid, report, metadata)
    for phase in ("setup", "call", "teardown"):
        report = reports.get(phase)
        if report is not None and report.skipped:
            return {
                "scenario": metadata.get("scenario", nodeid),
                "line": metadata.get("line", 0),
                "keyword": "",
                "text": metadata.get("scenario", nodeid),
                "reason": "Scenario skipped",
            }
    if any(reports.get(phase) is None for phase in ("setup", "call", "teardown")):
        return {
            "scenario": metadata.get("scenario", nodeid),
            "line": metadata.get("line", 0),
            "keyword": "",
            "text": metadata.get("scenario", nodeid),
            "reason": "Incomplete pytest reports",
        }
    return None


def build_contract(config: pytest.Config) -> dict:
    """Collapse setup/call/teardown into one old-runner result per scenario."""

    failures = []
    passed = 0
    for nodeid in config._gqt_bdd_nodeids:
        metadata = _scenario_metadata(config, nodeid)
        failure = _scenario_failure(nodeid, config, metadata)
        if failure is None:
            passed += 1
        else:
            failures.append(failure)
    total = len(config._gqt_bdd_nodeids)
    return {
        "tool": "gherkin",
        "ok": not failures,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
        },
        "failures": failures,
    }


def emit_contract(config: pytest.Config) -> dict | None:
    if not config._gqt_bdd_nodeids:
        return None
    contract = build_contract(config)
    print(json.dumps(contract, ensure_ascii=False, indent=2))
    return contract
