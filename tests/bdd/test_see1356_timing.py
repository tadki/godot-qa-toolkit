"""SEE-1356 timing-store budget negotiation, hosted by pytest-bdd."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from godot_qa_toolkit.mutation import runner as mut
from godot_qa_toolkit.timing import TimingStore

from ._support import fail_step as _fail
from ._support import make_gut_project

scenarios("features/see1356_timing.feature")


@given(
    parsers.parse(
        "a mutation target with a cached baseline sample of {seconds} seconds"
    )
)
def given_target(bdd_state: dict, tmp_path: Path, seconds: str) -> None:
    root = tmp_path
    target = make_gut_project(root, "func add(a, b):\n\treturn a + b\n")
    store = TimingStore(
        str(root), str(target), "mutation", "res://tests/", now=time.time()
    )
    assert store.record(float(seconds)) is True
    bdd_state.update(
        root=str(root), target=str(target), store=store, seconds=float(seconds)
    )


@when("the mutation run resolves its timeout budget from the timing store")
def when_run(bdd_state: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"runs": 0, "last_timeout": None}
    monkeypatch.delenv("GQT_TIMEOUT_SCALE", raising=False)

    def fake_run_gut(
        project_root: str, timeout_s: int = 60, tests_glob: str | None = None
    ) -> tuple[int, str]:
        calls["runs"] += 1
        calls["last_timeout"] = timeout_s
        time.sleep(0.01)
        return 0, ""

    monkeypatch.setattr(mut, "_run_gut_on_project", fake_run_gut)
    bdd_state["result"] = mut.run_mutation(
        bdd_state["target"],
        bdd_state["root"],
        budget=5,
        timeout_s=60,
        tests_glob="res://tests/",
        timing_store=bdd_state["store"],
    )
    bdd_state["calls"] = calls


@then("the budget anchor comes from the cache")
def then_anchor(bdd_state: dict) -> None:
    expected = int(bdd_state["seconds"] * 2.0) + 1
    actual = bdd_state["calls"]["last_timeout"]
    if actual != expected:
        _fail(
            "budget anchor did not come from the cache",
            f"expected baseline timeout {expected}, got {actual}",
        )
    result = bdd_state["result"]
    if result["summary"]["timing_source"] != "store":
        _fail("timing_source not store", str(result["summary"]))


@then("the baseline GUT run still executes as the kill-diff control")
def then_baseline_measured(bdd_state: dict) -> None:
    if bdd_state["calls"]["runs"] < 1:
        _fail(
            "baseline GUT never ran (cache must feed the anchor only, "
            "never the baseline)"
        )
    summary = bdd_state["result"]["summary"]
    if summary["baseline_reused"] is not False:
        _fail("baseline_reused is not False — the control run was skipped")
    if summary["samples_n"] < 2:
        _fail("this run's measured sample was not recorded", str(summary))
