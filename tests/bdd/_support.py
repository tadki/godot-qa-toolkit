"""Shared helpers for the migrated pytest-bdd scenarios."""

from __future__ import annotations

from pathlib import Path

from godot_qa_toolkit.mutation import runner as mrunner


class StepFailure(Exception):
    """pytest-bdd 步骤失败载体（SEE-1468 M5-b：gherkin runner 物理删除后，
    bdd 套件自持该异常——reason/detail/stderr_tail 三字段与原 runner 同形）。"""

    def __init__(self, reason: str, detail: str | None = None,
                 stderr_tail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.stderr_tail = stderr_tail

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
