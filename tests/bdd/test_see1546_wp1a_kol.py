"""SEE-1546 WP1a real-KOL-tree scenarios (Final Review HIGH-1 split).

The three real-tree scenarios live in their own module so the KOL guard can
be a *collection-time* module-level skip: a skip raised inside a step/fixture
is recorded by the BDD contract as a scenario failure (the SEE-1468 M5-a
lesson, see test_see1288_fork_cli.py). KOL tree resolution order — explicit
judgment variable, CI checkout, side-by-side workspace — keeps CI (SEE1288_REPO
set), the judgment harness (KOL_ROOT) and bare local runs working unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pytest_bdd import given, scenarios, then, when

from godot_qa_toolkit.complexity.collector import collect_file
from godot_qa_toolkit.complexity.gate import run_gate
from godot_qa_toolkit.complexity.gd_ast_oracle import parity_report

from ._support import fail_step as _fail


def _kol_root() -> Path:
    for env in ("KOL_ROOT", "SEE1288_REPO"):
        value = os.environ.get(env)
        if value:
            return Path(value)
    # 本工作区双仓并排布局：gqt/../KingOfLikes-Godot（同 test_see1288_fork_cli）
    return Path(__file__).resolve().parents[3] / "KingOfLikes-Godot"


_KOL_TREE = _kol_root()
# SEE1288_SKIP_KOL=1 is the CI's explicit "no KOL today" signal; the path
# guards below are the structural equivalent for runs without that variable.
_KOL_READY = (
    os.environ.get("SEE1288_SKIP_KOL") != "1"
    and (_KOL_TREE / "systems").is_dir()
    and (_KOL_TREE / "scenes").is_dir()
    and (_KOL_TREE / "autoload").is_dir()
)
if not _KOL_READY:
    pytest.skip(
        "KOL tree not found (set KOL_ROOT or SEE1288_REPO to run these "
        "real-tree scenarios)",
        allow_module_level=True,
    )

KOL_ROOT = str(_KOL_TREE)

scenarios("features/see1546_wp1a_kol.feature")

SEVEN_VAR_DEF_FILES = [
    "systems/effect/effect_definition_factory.gd",
    "scenes/ui/sane/sane_effect_formatter.gd",
    "autoload/effect_manager.gd",
    "autoload/hardware_manager.gd",
    "autoload/hardware_multi_driver.gd",
    "autoload/sane_subsystem.gd",
    "autoload/shadowconsole_subsystem.gd",
]

SEVEN_FILE_COUNTS = {
    "systems/effect/effect_definition_factory.gd": 14,
    "scenes/ui/sane/sane_effect_formatter.gd": 2,
    "autoload/effect_manager.gd": 36,
    "autoload/hardware_manager.gd": 30,
    "autoload/hardware_multi_driver.gd": 7,
    "autoload/sane_subsystem.gd": 45,
    "autoload/shadowconsole_subsystem.gd": 36,
}

PREEXISTING_VIOLATIONS = {
    ("active_range_calculator.gd", "_compute", 19),
    ("fair_absorption_solver.gd", "solve", 25),
    ("debug_panel.gd", "_row_less", 19),
    ("vendor_panel.gd", "_connect_signals", 16),
    ("vendor_panel.gd", "_disconnect_signals", 16),
}


@given("the real KOL worktree with systems scenes and autoload trees")
def given_kol_worktree() -> None:
    return None


@when("the complexity gate runs over the three trees")
def when_gate(bdd_state: dict) -> None:
    dirs = [str(Path(KOL_ROOT) / d) for d in ("systems", "scenes", "autoload")]
    bdd_state["gate"] = run_gate(dirs)


@then("at least 1561 functions are reported with zero unparseable files")
def then_rebaseline(bdd_state: dict) -> None:
    # §SPEC-042 anti-regression: 1561 is the FLOOR — growth is allowed
    # (KOL master moves), only a drop below the floor fails the step
    summary = bdd_state["gate"]["summary"]
    if summary["functions"] < 1561 or summary["unparseable"] != 0:
        _fail(
            "baseline regression — functions dropped below the 1561 floor "
            "or unparseable files reappeared",
            f"functions={summary['functions']} unparseable={summary['unparseable']}",
        )


@then("the seven var def files contribute exactly 170 recovered functions")
def then_170(bdd_state: dict) -> None:
    recovered = 0
    for rel in SEVEN_VAR_DEF_FILES:
        funcs, error = collect_file(Path(KOL_ROOT) / rel)
        if error is not None:
            _fail(f"{rel} still unparseable", str(error))
        recovered += len(funcs)
    if recovered != 170:
        _fail("recovered function total mismatch", str(recovered))


@then(
    "mini parity over the 170 functions shows no mismatch beyond the disclosed lambda gaps"
)
def then_mini_parity(bdd_state: dict) -> None:
    match = gap = mismatch = 0
    for rel in SEVEN_VAR_DEF_FILES:
        report = parity_report((Path(KOL_ROOT) / rel).read_text(encoding="utf-8"))
        for row in report:
            if row["kind"] == "match":
                match += 1
            elif row["kind"] == "lambda_gap":
                gap += 1
            else:
                mismatch += 1
    if mismatch != 0:
        _fail("unexplained parity mismatches", str(mismatch))
    if match != 167 or gap != 3:
        _fail(f"lane totals drifted: match={match} gap={gap}", "")


@given("the real KOL worktree with the seven var def files present")
def given_kol_seven_files() -> None:
    for rel in SEVEN_VAR_DEF_FILES:
        if not (Path(KOL_ROOT) / rel).exists():
            _fail("expected var def file missing from KOL worktree", rel)


@when("the complexity gate runs on each of the seven files alone")
def when_gate_per_file(bdd_state: dict) -> None:
    bdd_state["per_file_gate"] = {
        rel: run_gate([str(Path(KOL_ROOT) / rel)]) for rel in SEVEN_VAR_DEF_FILES
    }


@then("every single-file gate verdict is ok=true")
def then_single_file_ok(bdd_state: dict) -> None:
    bad = {
        rel: result["failures"][:2]
        for rel, result in bdd_state["per_file_gate"].items()
        if result["ok"] is not True
    }
    if bad:
        _fail("some var def files still fail their own gate", str(bad))


@then("the per-file function counts are exactly 14 2 36 30 7 45 36 totalling 170")
def then_per_file_counts(bdd_state: dict) -> None:
    drifted = {
        rel: result["summary"]["functions"]
        for rel, result in bdd_state["per_file_gate"].items()
        if result["summary"]["functions"] != SEVEN_FILE_COUNTS[rel]
    }
    if drifted:
        _fail("per-file function counts drifted", str(drifted))
    total = sum(r["summary"]["functions"] for r in bdd_state["per_file_gate"].values())
    if total != 170:
        _fail("seven-file total is not 170", str(total))


@then(
    "exactly 5 violations remain with their exact file function and complexity identity"
)
def then_violations_pinned(bdd_state: dict) -> None:
    result = bdd_state["gate"]
    violations = {
        (Path(v["file"]).name, v["name"], v["complexity"])
        for v in result["failures"]
        if "complexity" in v
    }
    if len(result["failures"]) != 5:
        _fail("violation count is not 5", str(result["failures"]))
    if violations != PREEXISTING_VIOLATIONS:
        _fail("pre-existing violation identity drifted", str(violations))
