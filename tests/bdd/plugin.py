"""Pytest hooks for the gqt pytest-bdd migration (SEE-1468 M0b)."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from pytest_bdd.parser import Scenario, Step

from .collection import (
    collect_bdd_items,
    selected_bdd_nodeids,
    write_resolution_report,
)
from .contract import (
    emit_contract,
    initialize,
    record_step_error,
    record_test_report,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--bdd-step-report",
        action="store",
        default=None,
        help="write collection-time pytest-bdd step-resolution JSON",
    )


def pytest_configure(config: pytest.Config) -> None:
    initialize(config)
    config._gqt_bdd_collection = None


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    collection = collect_bdd_items(session, config, items)
    config._gqt_bdd_collection = collection
    write_resolution_report(config, collection.report)
    if not collection.report["ok"]:
        pytest.exit(
            "pytest-bdd step resolution is not unique; see mapping report",
            returncode=2,
        )


def pytest_collection_finish(session: pytest.Session) -> None:
    """Snapshot selected BDD items after -k/-m deselection for JSON totals."""

    session.config._gqt_bdd_nodeids = selected_bdd_nodeids(session.items)


def pytest_bdd_step_error(
    request: pytest.FixtureRequest,
    scenario: Scenario,
    step: Step,
    exception: Exception,
    **_kwargs: object,
) -> None:
    state = None
    if "bdd_state" in request.fixturenames:
        state = request.getfixturevalue("bdd_state")
    record_step_error(request, scenario, step, exception, state)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo
) -> Generator[None, object, None]:
    outcome = yield
    record_test_report(item, outcome.get_result())


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if exitstatus in (3, 4, 5):
        session.exitstatus = 2
    if session.config.option.collectonly or exitstatus in (2, 3, 4, 5):
        return
    contract = emit_contract(session.config)
    if contract is not None and not contract["ok"] and session.exitstatus == 0:
        session.exitstatus = 1


__all__ = [
    "pytest_addoption",
    "pytest_bdd_step_error",
    "pytest_collection_finish",
    "pytest_collection_modifyitems",
    "pytest_configure",
    "pytest_runtest_makereport",
    "pytest_sessionfinish",
]
