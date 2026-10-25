"""SEE-1400 cross-process Godot slot-gate scenarios on pytest-bdd."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from godot_qa_toolkit.mutation import godot_gate
from godot_qa_toolkit.mutation import runner as mrunner

from ._support import fail_step as _fail
from ._support import make_gut_project as make_project

scenarios("features/see1400_global_gate.feature")


@pytest.fixture(autouse=True)
def reset_gate_warning_state() -> Iterator[None]:
    godot_gate.reset_warn_state()
    yield
    godot_gate.reset_warn_state()


@given(parsers.parse("the global slot capacity is pinned to {n:d}"))
def given_capacity(
    bdd_state: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n: int
) -> None:
    root = tmp_path
    make_project(tmp_path)
    bdd_state["root"] = root
    monkeypatch.setenv(godot_gate.ENV_GLOBAL_SLOTS, str(n))


@when("two concurrent workers run GUT through the gated exit")
def when_two_workers(bdd_state: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    concurrent = {"cur": 0, "max": 0}
    lock = threading.Lock()

    class FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(*args: object, **kwargs: object) -> FakeProc:
        with lock:
            concurrent["cur"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["cur"])
        time.sleep(0.1)
        with lock:
            concurrent["cur"] -= 1
        return FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            mrunner._run_gut_on_project(str(bdd_state["root"]))
        except Exception as error:  # noqa: BLE001 - asserted by Then step.
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    bdd_state["concurrent"] = concurrent
    bdd_state["errors"] = errors


@then(
    parsers.parse("the observed concurrent godot subprocess count never exceeded {n:d}")
)
def then_max_concurrent(bdd_state: dict, n: int) -> None:
    if bdd_state["errors"]:
        _fail(f"gated workers raised: {bdd_state['errors']}")
    maximum = bdd_state["concurrent"]["max"]
    if maximum > n:
        _fail(f"concurrency exceeded {n}: max={maximum}")
    if maximum < 1:
        _fail("no gated run observed — scenario is void")


@given(parsers.parse("the global slot capacity env is set to {n:d}"))
def given_env_capacity(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    monkeypatch.setenv(godot_gate.ENV_GLOBAL_SLOTS, str(n))


@when("the gate resolves its capacity")
def when_resolve_capacity(bdd_state: dict) -> None:
    bdd_state["capacity"] = godot_gate.slot_capacity()


@then(parsers.parse("the resolved capacity is {n:d}"))
def then_capacity(bdd_state: dict, n: int) -> None:
    if bdd_state["capacity"] != n:
        _fail(f"capacity mismatch: expected {n}, got {bdd_state['capacity']}")


@given("the platform has no fcntl module")
def given_no_fcntl(
    bdd_state: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path
    make_project(tmp_path)
    bdd_state["root"] = root
    monkeypatch.setattr(godot_gate, "fcntl", None)


@when("a gated GUT run executes")
def when_gated_run(bdd_state: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: FakeProc())
    returncode, _ = mrunner._run_gut_on_project(str(bdd_state["root"]))
    bdd_state["rc"] = returncode


@then("the run completes without holding any slot")
def then_no_slot(bdd_state: dict) -> None:
    if bdd_state["rc"] != 0:
        _fail(f"gated run failed: rc={bdd_state['rc']}")
    slots = godot_gate.slots_dir(str(bdd_state["root"]))
    if slots.exists() and any(slots.glob("slot_*.lock")):
        _fail("degraded gate created slot files it should never touch")
