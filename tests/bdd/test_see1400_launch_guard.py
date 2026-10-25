"""SEE-1400 launch-failure guard scenarios on pytest-bdd."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_bdd import given, scenarios, then, when

from godot_qa_toolkit.mutation import runner as mrunner

from ._support import fail_step as _fail
from ._support import make_gut_project as make_project
from ._support import run_mutation

scenarios("features/see1400_launch_guard.feature")


@given("a mutation target whose GUT rounds carry unrelated resource warnings")
def given_noisy_gut(bdd_state: dict, tmp_path: Path) -> None:
    noise = (
        "ERROR: Failed loading resource: "
        "res://scenes/ui/container/item_container_visual.tscn\n"
    )
    root = tmp_path
    target = make_project(tmp_path)
    calls = {"n": 0}

    def fake(
        root: str, timeout_s: int = 60, tests_glob: str | None = None
    ) -> tuple[int, str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return 0, noise + "* test_x\n  [passed]\n"
        return (
            1,
            noise + "* test_new\n  [Failed] boom\n"
            "---- Run Summary ----\n- test_new\n  [Failed] boom\n",
        )

    bdd_state.update(root=root, target=target, calls=calls, fake_fn=fake)


@given("a mutation target whose GUT rounds fail to launch the runner script")
def given_launch_failure(bdd_state: dict, tmp_path: Path) -> None:
    root = tmp_path
    target = make_project(tmp_path)
    calls = {"n": 0}

    def fake(
        root: str, timeout_s: int = 60, tests_glob: str | None = None
    ) -> tuple[int, str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return 0, "* test_x\n  [passed]\n"
        return (
            1,
            (
                "ERROR: Attempt to open script 'res://addons/gut/gut_cmdln.gd' "
                "resulted in error 'File not found'"
            ),
        )

    bdd_state.update(root=root, target=target, calls=calls, fake_fn=fake)


@when("the mutation run executes")
def when_plain_run(bdd_state: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mrunner, "_run_gut_on_project", bdd_state["fake_fn"])
    bdd_state["result"] = run_mutation(bdd_state)


@then("every mutant is killed and none is run_error")
def then_all_killed(bdd_state: dict) -> None:
    summary = bdd_state["result"]["summary"]
    if summary.get("run_errors", 0) != 0:
        _fail("noisy rounds swallowed into run_error", detail=str(summary))
    if summary["mutants"] == 0 or summary["killed"] != summary["mutants"]:
        _fail("mutants not all killed", detail=str(summary))
    if bdd_state["result"]["ok"] is not True:
        _fail("run not ok despite all killed", detail=str(summary))


@then("every mutant verdict is run_error and none is killed")
def then_all_run_error(bdd_state: dict) -> None:
    summary = bdd_state["result"]["summary"]
    if summary.get("killed", 0) != 0:
        _fail("launch failure counted as kill", detail=str(summary))
    if summary["mutants"] == 0 or summary.get("run_errors", 0) != summary["mutants"]:
        _fail("launch failure not classified run_error", detail=str(summary))
    if bdd_state["result"]["ok"] is not False:
        _fail("run_error verdict must gate ok=False", detail=str(summary))
