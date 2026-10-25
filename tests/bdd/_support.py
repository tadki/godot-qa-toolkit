"""Shared helpers for the migrated pytest-bdd scenarios."""

from __future__ import annotations

from pathlib import Path

from godot_qa_toolkit.gherkin.runner import StepFailure
from godot_qa_toolkit.mutation import runner as mrunner

TARGET_SRC = "func add(a, b):\n\tif a < b:\n\t\treturn a + b\n\treturn b\n"


def fail_step(reason: str, detail: str | None = None) -> None:
    raise StepFailure(reason=reason, detail=detail)


def make_gut_project(
    root: Path, source: str = TARGET_SRC, *, save_test_ref: bool = False
) -> Path:
    """Write the minimal fake GUT project and return its mutation target."""

    gut = root / "addons" / "gut"
    gut.mkdir(parents=True)
    (gut / "gut_cmdln.gd").write_text("# gut\n", encoding="utf-8")
    if save_test_ref:
        save = root / "tests" / "unit" / "save"
        save.mkdir(parents=True)
        (save / "test_save.gd").write_text('preload("res://calc.gd")\n')
    target = root / "calc.gd"
    target.write_text(source, encoding="utf-8")
    return target


def run_mutation(state: dict) -> dict:
    return mrunner.run_mutation(
        str(state["target"]),
        str(state["root"]),
        budget=4,
        timeout_s=60,
        tests_glob="res://tests/",
        resume=False,
    )
