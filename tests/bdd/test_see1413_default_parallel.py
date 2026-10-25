"""SEE-1413 default-parallel scenarios on pytest-bdd."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from typing_extensions import Self

from godot_qa_toolkit.gherkin.runner import StepFailure
from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation import runner as runner_mod

from ._support import make_gut_project

scenarios("features/see1413_default_parallel.feature")

GD_4_SITES = "func f(a, b):\n\tif a > 0:\n\t\treturn a + b\n\treturn a - b and a * b\n"
GD_2_SITES = "func f(a, b):\n\treturn a + b\n"
_ORIGINALS = (
    executor._worker_pool,
    executor._run_baseline_snapshot,
    runner_mod._run_gut_on_project,
    executor.prewarm_import,
    executor.os.cpu_count,
)


class _SerialPool:
    def __init__(self, max_workers: int | None = None) -> None:
        self.max_workers = max_workers

    def map(self, fn: Callable[[int], object], iterable: Iterable[int]) -> list[object]:
        return [fn(item) for item in iterable]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _write_project(root: Path, source: str) -> tuple[str, str]:
    target = make_gut_project(root, source, save_test_ref=True)
    return str(root), str(target)


@given(
    parsers.parse(
        "a mutation target with {count:d} mutation sites and a passing GUT stub"
    )
)
def given_target(
    bdd_state: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    source = GD_4_SITES if count >= 4 else GD_2_SITES
    root, target = _write_project(tmp_path, source)
    bdd_state["expected_mutants"] = count
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(
        executor,
        "_run_baseline_snapshot",
        lambda root, glob, timeout: {"rc": 0, "tail": "", "elapsed": 5.0},
    )
    monkeypatch.setattr(
        runner_mod,
        "_run_gut_on_project",
        lambda root, timeout_s=60, tests_glob=None: (0, "pass"),
    )
    monkeypatch.setattr(
        executor,
        "prewarm_import",
        lambda root: bdd_state["prewarm"].__setitem__(
            "n", bdd_state["prewarm"]["n"] + 1
        ),
    )
    bdd_state["root"], bdd_state["target"] = root, target
    bdd_state["prewarm"] = {"n": 0}


@when("gqt mutation runs the single file with jobs omitted")
def when_default_jobs(bdd_state: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from godot_qa_toolkit import cli as cli_mod

    args = cli_mod.build_parser().parse_args(
        [
            "mutation",
            bdd_state["target"],
            "--project-root",
            bdd_state["root"],
            "--tests",
            "res://tests/unit/save/",
        ]
    )
    output = io.StringIO()
    monkeypatch.setattr(executor.os, "cpu_count", lambda: 16)
    with redirect_stdout(output):
        cli_mod.cmd_mutation(args)
    bdd_state["result"] = cli_mod.json.loads(output.getvalue())


@when(parsers.re(r"gqt mutation runs the single file with jobs (?P<n>\d+)$"))
def when_explicit_jobs(bdd_state: dict, n: str) -> None:
    bdd_state["result"] = executor.run_mutation_files(
        [bdd_state["target"]],
        bdd_state["root"],
        budget=50,
        timeout_s=60,
        dry_run=False,
        tests_glob="res://tests/unit/save/",
        jobs=int(n),
    )


@when("the mutation --jobs help text is rendered")
def when_jobs_help(bdd_state: dict) -> None:
    from godot_qa_toolkit.cli import build_parser

    choices = build_parser()._subparsers._group_actions[0].choices
    action = next(
        action for action in choices["mutation"]._actions if action.dest == "jobs"
    )
    bdd_state["help_text"] = action.help


@then(parsers.parse("the report top-level shape is flat with jobs greater than {n:d}"))
def then_flat_report(bdd_state: dict, n: int) -> None:
    result = bdd_state["result"]
    if set(result) != {"tool", "ok", "summary", "failures"}:
        raise StepFailure(f"top-level shape not flat: {sorted(result)}")
    jobs = result["summary"].get("jobs")
    if jobs is None or not jobs > n:
        raise StepFailure(f"summary.jobs={jobs} not > {n}")


@then("the flat summary keeps existing keys and adds jobs")
def then_summary_keys(bdd_state: dict) -> None:
    summary = bdd_state["result"]["summary"]
    expected = {
        "file",
        "mutants",
        "killed",
        "survived",
        "suspect",
        "timeout",
        "run_errors",
        "invalid_mutants",
        "kill_rate",
        "budget",
        "adaptive_timeout_s",
        "scoped",
        "baseline_reused",
        "timing_source",
        "samples_n",
        "baseline_p90",
        "jobs",
    }
    if set(summary) != expected:
        raise StepFailure(f"summary keys changed: {sorted(summary)}")
    if summary["mutants"] != bdd_state["expected_mutants"]:
        raise StepFailure(
            f"merged mutants={summary['mutants']} != {bdd_state['expected_mutants']}"
        )


@then(parsers.parse("the report falls back to serial with jobs {n:d}"))
def then_serial_fallback(bdd_state: dict, n: int) -> None:
    result = bdd_state["result"]
    if result["jobs"] != n:
        raise StepFailure(f"expected serial jobs={n}, got {result['jobs']}")
    if result["results"][0]["summary"].get("jobs") != n:
        raise StepFailure("serial result summary.jobs mismatch")


@then("zero prewarm imports were performed")
def then_no_prewarm(bdd_state: dict) -> None:
    if bdd_state["prewarm"]["n"] != 0:
        raise StepFailure(f"prewarm ran {bdd_state['prewarm']['n']} time(s)")


@then(parsers.parse("the report reports jobs {n:d} with a single result"))
def then_single_result(bdd_state: dict, n: int) -> None:
    result = bdd_state["result"]
    if result["jobs"] != n or len(result["results"]) != 1:
        raise StepFailure(f"jobs={result['jobs']} results={len(result['results'])}")
    if result["results"][0]["summary"].get("jobs") != n:
        raise StepFailure("jobs=1 result summary.jobs mismatch")
    if bdd_state["prewarm"]["n"] != 0:
        raise StepFailure("jobs=1 unexpectedly prewarmed")


@then("the text mentions per-file AND per-mutant slicing")
def then_help_levels(bdd_state: dict) -> None:
    if "per-file AND per-mutant" not in bdd_state["help_text"]:
        raise StepFailure("help text missing per-file AND per-mutant")


@then("the text mentions single file support")
def then_help_single_file(bdd_state: dict) -> None:
    if "single file" not in bdd_state["help_text"]:
        raise StepFailure("help text missing single file note")


def test_module_patches_restored_after_scenarios() -> None:
    assert executor._worker_pool == _ORIGINALS[0]
    assert executor._run_baseline_snapshot == _ORIGINALS[1]
    assert runner_mod._run_gut_on_project == _ORIGINALS[2]
    assert executor.prewarm_import == _ORIGINALS[3]
    assert executor.os.cpu_count == _ORIGINALS[4]
    assert not hasattr(executor, "worker_pool")
    assert not hasattr(executor, "baseline")
