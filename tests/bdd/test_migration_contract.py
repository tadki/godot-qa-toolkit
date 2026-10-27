"""Machines gates for the gqt Gherkin -> pytest-bdd migration (SEE-1468 M0b).

The scenarios live in six ``test_*.py`` modules in this directory; this file
only verifies the migration contract: dual-source scenario count, explicit
feature binding, collection-time step resolution, timeout coverage, the JSON
contract, and removal of the legacy ``register(registry)`` glue.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest_bdd.feature import get_features

from godot_qa_toolkit.gherkin.runner import StepFailure
from tests.bdd._support import fail_step
from tests.bdd.collection import (
    BddCollection,
    SCENARIO_TIMEOUT_S,
    _scenario_template,
    write_resolution_report,
)
from tests.bdd.contract import build_contract, emit_contract, record_step_error

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
FEATURES = REPO / "features"
BDD_DIR = Path(__file__).resolve().parent
EXPECTED_SCENARIO_COUNTS = {
    "see1356_timing": 1,
    "see1400_global_gate": 3,
    "see1400_launch_guard": 2,
    "see1405_gdself": 4,
    "see1405_keys": 3,
    "see1413_default_parallel": 4,
    # SEE-1468 M5-a：see1288_fork_cli 4 场景自 KOL 收编（pending -> bdd）。
    "see1288_fork_cli": 4,
}
EXPECTED_FEATURE_NAMES = tuple(EXPECTED_SCENARIO_COUNTS)
EXPECTED_SCENARIOS = sum(EXPECTED_SCENARIO_COUNTS.values())
EXPECTED_STEPS = 68
EXPECTED_DEFINITIONS = 64
CONTRACT_MARKER = '{\n  "tool": "gherkin"'


def _source_scenario_count(name: str) -> int:
    feature = get_features([str(FEATURES / f"{name}.feature")])[0]
    total = 0
    for scenario in feature.scenarios.values():
        contexts = [
            context
            for examples in scenario.examples
            for context in examples.as_contexts()
        ]
        total += len(contexts) or 1
    return total


def _collection(request: pytest.FixtureRequest) -> BddCollection:
    collection = getattr(request.config, "_gqt_bdd_collection", None)
    assert collection is not None, "pytest-bdd collection plugin did not run"
    return collection


def _full_collection(request: pytest.FixtureRequest) -> BddCollection:
    collection = _collection(request)
    if len(collection.items) != EXPECTED_SCENARIOS:
        pytest.skip("migration ledger gates require the full tests/bdd collection")
    return collection


def _run_nested(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(REPO), str(SRC), env.get("PYTHONPATH", "")) if part
    )
    command = [sys.executable, "-m", "pytest", "-p", "tests.bdd.plugin", "-q"]
    return subprocess.run(
        [*command, *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _decode_contract(output: str) -> dict:
    start = output.rfind(CONTRACT_MARKER)
    assert start >= 0, f"contract JSON missing from stdout:\n{output}"
    contract, _ = json.JSONDecoder().raw_decode(output[start:])
    return contract


def _write_contract_case(tmp_path: Path) -> None:
    (tmp_path / "contract.feature").write_text(
        "Feature: JSON contract\n"
        "  Scenario: passes\n"
        "    Given a passing step\n"
        "\n"
        "  Scenario: fails structured\n"
        "    Given a structured failure\n",
        encoding="utf-8",
    )
    (tmp_path / "test_contract_case.py").write_text(
        "from pytest_bdd import given, scenarios\n"
        "from godot_qa_toolkit.gherkin.runner import StepFailure\n"
        "\n"
        'scenarios("contract.feature")\n'
        "\n"
        '@given("a passing step")\n'
        "def _passing():\n"
        "    pass\n"
        "\n"
        '@given("a structured failure")\n'
        "def _failing():\n"
        "    raise StepFailure(\n"
        '        reason="boom", detail="expected 1 got 2",\n'
        '        stderr_tail="probe stderr")\n',
        encoding="utf-8",
    )


def test_dual_source_scenario_count_matches_ledger(request: pytest.FixtureRequest) -> None:
    # SEE-1468 M5-a：see1288_fork_cli 4 场景收编后双源对账 17 -> 21。
    collection = _full_collection(request)
    assert len(collection.items) == EXPECTED_SCENARIOS == 21
    for name, expected in EXPECTED_SCENARIO_COUNTS.items():
        assert _source_scenario_count(name) == expected, name
        prefix = f"tests/bdd/test_{name}.py::"
        assert sum(nodeid.startswith(prefix) for nodeid in collection.items) == expected


def test_source_feature_scenario_counts_match_the_ledger() -> None:
    """Dual-source ledger, unguarded source half: scenario-count drift in a
    .feature must RED on its own, even when the drift simultaneously trips
    the full-collection skip guard of the guarded collection half (an added
    or removed scenario turns the guarded ledger tests into skips and the
    suite would stay green without this unconditional source-side gate)."""
    for name, expected in EXPECTED_SCENARIO_COUNTS.items():
        assert _source_scenario_count(name) == expected, name


def test_no_tests_collected_maps_to_caller_error(tmp_path: Path) -> None:
    """Exit-status discipline: nothing-collected (pytest native 5) is a
    caller error and must surface as exit 2 under the plugin, not as a
    successful no-op — the 3/4/5 -> 2 mapping branch of sessionfinish."""
    result = _run_nested(tmp_path)
    assert result.returncode == 2, result.stdout + result.stderr


def test_support_run_mutation_engine_policy_is_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_support.run_mutation`` passes a fixed engine policy to the
    mutation runner; drift in budget/timeout/tests_glob/resume must RED —
    the launch-guard scenarios fake the GUT layer and cannot observe these
    kwargs, so without this pin the helper policy is mutation-invisible."""
    import godot_qa_toolkit.mutation.runner as mrunner

    from tests.bdd import _support

    captured: dict = {}

    def fake_run(target: str, root: str, **kwargs: object) -> dict:
        captured["target"] = target
        captured["root"] = root
        captured.update(kwargs)
        return {"ok": True, "summary": {"mutants": 1, "killed": 1}}

    monkeypatch.setattr(mrunner, "run_mutation", fake_run)
    target = _support.make_gut_project(tmp_path)
    _support.run_mutation({"root": tmp_path, "target": target})
    assert captured == {
        "target": str(target),
        "root": str(tmp_path),
        "budget": 4,
        "timeout_s": 60,
        "tests_glob": "res://tests/",
        "resume": False,
    }


def test_support_make_gut_project_writes_save_ref_fixture(tmp_path: Path) -> None:
    """``make_gut_project(save_test_ref=True)`` must materialise the
    preload-ref fixture file — the see1413 consumers fake the engine layer,
    so without this pin the branch is mutation-invisible."""
    from tests.bdd import _support

    root = tmp_path / "sp2"
    _support.make_gut_project(root, save_test_ref=True)
    save_ref = root / "tests" / "unit" / "save" / "test_save.gd"
    assert save_ref.is_file()
    assert "preload" in save_ref.read_text("utf-8")
    assert not (tmp_path / "plain" / "tests").exists()
    _support.make_gut_project(tmp_path / "plain")


def test_every_step_resolves_to_exactly_one_definition(
    request: pytest.FixtureRequest,
) -> None:
    report = _full_collection(request).report
    assert report["summary"] == {
        "total_steps": EXPECTED_STEPS,
        "unique": EXPECTED_STEPS,
        "unmatched": 0,
        "ambiguous": 0,
    }
    assert len(report["steps"]) == EXPECTED_STEPS
    definitions = set()
    for mapping in report["steps"]:
        assert mapping["match_count"] == 1, mapping
        assert len(mapping["matches"]) == 1, mapping
        definitions.add(mapping["matches"][0]["definition"])
    assert len(definitions) == EXPECTED_DEFINITIONS


def test_see1413_steps_resolve_to_their_intended_definitions(
    request: pytest.FixtureRequest,
) -> None:
    mappings = {
        mapping["text"]: mapping["matches"][0]["definition"]
        for mapping in _full_collection(request).report["steps"]
        if mapping["text"].startswith("gqt mutation runs the single file with jobs")
    }
    suffix = "tests.bdd.test_see1413_default_parallel"
    assert mappings["gqt mutation runs the single file with jobs omitted"] == (
        f"{suffix}.when_default_jobs"
    )
    for count in (1, 8):
        text = f"gqt mutation runs the single file with jobs {count}"
        assert mappings[text] == f"{suffix}.when_explicit_jobs"


def test_every_feature_is_explicitly_bound_by_scenarios_call() -> None:
    feat_names = {path.stem for path in FEATURES.glob("*.feature")}
    assert feat_names == set(EXPECTED_FEATURE_NAMES)
    for name in EXPECTED_FEATURE_NAMES:
        module = (BDD_DIR / f"test_{name}.py").read_text("utf-8")
        pattern = rf'scenarios\(\s*"features/{re.escape(name)}\.feature"\s*\)'
        assert len(re.findall(pattern, module)) == 1, name


def test_legacy_glue_removed_and_runner_tests_retained() -> None:
    assert list(FEATURES.glob("steps_*.py")) == []
    assert (REPO / "tests" / "unit" / "test_runner.py").is_file()
    assert (REPO / "tests" / "unit" / "test_runner_p0.py").is_file()


def test_every_bdd_scenario_has_a_per_scenario_timeout(
    request: pytest.FixtureRequest,
) -> None:
    collection = _full_collection(request)
    assert len(collection.timeouts) == EXPECTED_SCENARIOS
    for timeout in collection.timeouts:
        assert timeout["args"][0] == SCENARIO_TIMEOUT_S, timeout
        assert timeout["kwargs"].get("method") == "signal", timeout
        assert timeout["kwargs"].get("func_only") is True, timeout


def test_json_contract_shape_and_structured_step_failure(tmp_path: Path) -> None:
    _write_contract_case(tmp_path)
    result = _run_nested(tmp_path, "test_contract_case.py")
    assert result.returncode == 1, result.stdout + result.stderr
    contract = _decode_contract(result.stdout)
    assert set(contract) == {"tool", "ok", "summary", "failures"}
    assert contract["tool"] == "gherkin"
    assert contract["ok"] is False
    assert contract["summary"] == {"total": 2, "passed": 1, "failed": 1}
    failure = contract["failures"][0]
    assert failure["scenario"] == "fails structured"
    assert failure["line"] == 6
    assert failure["keyword"] == "Given"
    assert failure["text"] == "a structured failure"
    assert failure["reason"] == "StepFailure: boom"
    assert failure["detail"] == "expected 1 got 2"
    assert failure["stderr_tail"] == "probe stderr"


def test_json_contract_counts_only_selected_scenarios(tmp_path: Path) -> None:
    _write_contract_case(tmp_path)
    report_path = tmp_path / "step-report.json"
    result = _run_nested(
        tmp_path,
        "test_contract_case.py",
        "-k",
        "passes",
        f"--bdd-step-report={report_path}",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    contract = _decode_contract(result.stdout)
    assert contract["ok"] is True
    assert contract["summary"] == {"total": 1, "passed": 1, "failed": 0}
    assert contract["failures"] == []
    report = json.loads(report_path.read_text("utf-8"))
    assert report["summary"]["total_steps"] == 2
    assert report["summary"]["unique"] == 2


def test_missing_feature_is_a_collection_error_without_contract(tmp_path: Path) -> None:
    (tmp_path / "test_missing.py").write_text(
        'from pytest_bdd import scenarios\nscenarios("missing.feature")\n',
        encoding="utf-8",
    )
    result = _run_nested(tmp_path, "test_missing.py")
    assert result.returncode == 2, result.stdout + result.stderr
    assert CONTRACT_MARKER not in result.stdout


def test_ambiguous_step_is_rejected_during_collection_dry_run(tmp_path: Path) -> None:
    (tmp_path / "ambiguous.feature").write_text(
        "Feature: ambiguity\n"
        "  Scenario: broad parser steals exact text\n"
        "    Given jobs omitted\n",
        encoding="utf-8",
    )
    (tmp_path / "test_ambiguous.py").write_text(
        "from pytest_bdd import given, parsers, scenarios\n"
        "\n"
        'scenarios("ambiguous.feature")\n'
        "\n"
        '@given(parsers.parse("jobs {value}"))\n'
        "def _broad(value):\n"
        "    pass\n"
        "\n"
        '@given("jobs omitted")\n'
        "def _exact():\n"
        "    pass\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "step-report.json"
    result = _run_nested(
        tmp_path,
        "test_ambiguous.py",
        "--collect-only",
        f"--bdd-step-report={report_path}",
    )
    assert result.returncode == 2, result.stdout + result.stderr
    report = json.loads(report_path.read_text("utf-8"))
    assert report["ok"] is False
    assert report["summary"]["total_steps"] == 1
    assert report["summary"]["ambiguous"] == 1
    assert report["steps"][0]["match_count"] == 2


def test_unmatched_step_is_rejected_during_collection_dry_run(tmp_path: Path) -> None:
    (tmp_path / "unmatched.feature").write_text(
        "Feature: missing definition\n"
        "  Scenario: no step implementation exists\n"
        "    Given a step with no implementation\n",
        encoding="utf-8",
    )
    (tmp_path / "test_unmatched.py").write_text(
        'from pytest_bdd import scenarios\n\nscenarios("unmatched.feature")\n',
        encoding="utf-8",
    )
    report_path = tmp_path / "step-report.json"
    result = _run_nested(
        tmp_path,
        "test_unmatched.py",
        "--collect-only",
        f"--bdd-step-report={report_path}",
    )
    assert result.returncode == 2, result.stdout + result.stderr
    report = json.loads(report_path.read_text("utf-8"))
    assert report["ok"] is False
    assert report["summary"]["total_steps"] == 1
    assert report["summary"]["unmatched"] == 1
    assert report["steps"][0]["match_count"] == 0


def test_pytest_timeout_is_wired_to_the_scenario(tmp_path: Path) -> None:
    (tmp_path / "timeout.feature").write_text(
        "Feature: timeout\n"
        "  Scenario: hanging step is cut off\n"
        "    Given a hanging step\n",
        encoding="utf-8",
    )
    (tmp_path / "test_timeout_case.py").write_text(
        "from pytest_bdd import given, scenarios\n"
        "import pytest\n"
        "\n"
        'scenarios("timeout.feature")\n'
        'pytestmark = pytest.mark.timeout(0.2, method="signal", func_only=True)\n'
        "\n"
        "@pytest.fixture\n"
        "def bdd_state():\n"
        "    return {}\n"
        "\n"
        "@pytest.fixture(autouse=True)\n"
        "def _cleanup():\n"
        "    yield\n"
        "    from pathlib import Path\n"
        '    Path("cleaned.txt").write_text("done")\n'
        "\n"
        '@given("a hanging step")\n'
        "def _hang(bdd_state):\n"
        '    bdd_state["_detail"] = "probe pid 4242"\n'
        "    import time\n"
        "    time.sleep(5)\n",
        encoding="utf-8",
    )
    result = _run_nested(tmp_path, "test_timeout_case.py")
    assert result.returncode == 1, result.stdout + result.stderr
    contract = _decode_contract(result.stdout)
    assert contract["ok"] is False
    failure = contract["failures"][0]
    assert failure["reason"].startswith("ScenarioTimeout:")
    assert failure["detail"] == "probe pid 4242"
    assert (tmp_path / "cleaned.txt").is_file()


def test_collection_dry_run_executes_no_fixture_or_step(tmp_path: Path) -> None:
    (tmp_path / "dry_run.feature").write_text(
        "Feature: dry run\n  Scenario: collection only\n    Given no execution\n",
        encoding="utf-8",
    )
    (tmp_path / "test_dry_run.py").write_text(
        "from pathlib import Path\n"
        "import pytest\n"
        "from pytest_bdd import given, scenarios\n\n"
        'scenarios("dry_run.feature")\n\n'
        "@pytest.fixture(autouse=True)\n"
        "def _fixture_sentinel():\n"
        '    Path("fixture-ran").write_text("yes")\n\n'
        '@given("no execution")\n'
        "def _step_sentinel():\n"
        '    Path("step-ran").write_text("yes")\n',
        encoding="utf-8",
    )
    result = _run_nested(tmp_path, "test_dry_run.py", "--collect-only")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "fixture-ran").exists()
    assert not (tmp_path / "step-ran").exists()
    assert CONTRACT_MARKER not in result.stdout


def test_missing_reports_cannot_be_counted_as_pass() -> None:
    config = SimpleNamespace(
        _gqt_bdd_nodeids=["missing"],
        _gqt_bdd_reports={},
        _gqt_bdd_step_failures={},
    )
    contract = build_contract(config)
    assert contract["ok"] is False
    assert contract["summary"] == {"total": 1, "passed": 0, "failed": 1}
    assert contract["failures"][0]["reason"] == "Incomplete pytest reports"


def test_support_helpers_fail_and_write_reports(tmp_path: Path) -> None:
    with pytest.raises(StepFailure, match="boom"):
        fail_step("boom", "detail")
    assert _scenario_template(SimpleNamespace(obj=None)) is None
    report_path = tmp_path / "nested-report.json"
    config = SimpleNamespace(getoption=lambda name: str(report_path))
    write_resolution_report(config, {"ok": True})
    assert json.loads(report_path.read_text("utf-8")) == {"ok": True}


def test_contract_direct_failure_paths(capsys: pytest.CaptureFixture[str]) -> None:
    config = SimpleNamespace(
        _gqt_bdd_nodeids=[],
        _gqt_bdd_reports={},
        _gqt_bdd_step_failures={},
    )
    request = SimpleNamespace(config=config, node=SimpleNamespace(nodeid="node"))
    scenario = SimpleNamespace(name="scenario")
    step = SimpleNamespace(line_number=3, type="given", name="text")
    record_step_error(
        request,
        scenario,
        step,
        StepFailure(reason="boom", detail="detail", stderr_tail="stderr"),
    )
    record_step_error(request, scenario, step, ValueError("bad"), {"_detail": "probe"})
    assert config._gqt_bdd_step_failures["node"]["reason"] == "ValueError: bad"
    assert config._gqt_bdd_step_failures["node"]["detail"] == "probe"
    assert emit_contract(config) is None
    assert capsys.readouterr().out == ""

    report = SimpleNamespace(
        failed=True,
        skipped=False,
        location=("tests/test_case.py", 4, None),
        longrepr="setup boom",
    )
    config._gqt_bdd_nodeids = ["generic"]
    config._gqt_bdd_reports = {"generic": {"setup": report}}
    contract = build_contract(config)
    assert contract["failures"][0]["scenario"] == "generic"
    assert contract["failures"][0]["line"] == 5

    skipped = SimpleNamespace(failed=False, skipped=True, location=None, longrepr=None)
    config._gqt_bdd_nodeids = ["skipped"]
    config._gqt_bdd_reports = {
        "skipped": {phase: skipped for phase in ("setup", "call", "teardown")}
    }
    contract = build_contract(config)
    assert contract["failures"][0]["reason"] == "Scenario skipped"


def test_non_bdd_pytest_run_does_not_emit_gherkin_contract(tmp_path: Path) -> None:
    (tmp_path / "test_plain.py").write_text(
        "def test_plain():\n    assert True\n", encoding="utf-8"
    )
    result = _run_nested(tmp_path, "test_plain.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert CONTRACT_MARKER not in result.stdout


def test_teardown_failure_is_not_a_pass(tmp_path: Path) -> None:
    (tmp_path / "teardown.feature").write_text(
        "Feature: teardown\n  Scenario: teardown fails\n    Given a passing step\n",
        encoding="utf-8",
    )
    (tmp_path / "test_teardown.py").write_text(
        "import pytest\n"
        "from pytest_bdd import given, scenarios\n\n"
        'scenarios("teardown.feature")\n\n'
        "@pytest.fixture(autouse=True)\n"
        "def _broken_teardown():\n"
        "    yield\n"
        '    raise RuntimeError("teardown boom")\n\n'
        '@given("a passing step")\n'
        "def _passing():\n"
        "    pass\n",
        encoding="utf-8",
    )
    result = _run_nested(tmp_path, "test_teardown.py")
    assert result.returncode == 1, result.stdout + result.stderr
    contract = _decode_contract(result.stdout)
    assert contract["summary"] == {"total": 1, "passed": 0, "failed": 1}
    failure = contract["failures"][0]
    assert failure["scenario"] == "teardown fails"
    assert failure["text"] == "teardown fails"
    assert failure["line"] == 2
    assert failure["keyword"] == ""
    assert "teardown boom" in failure["reason"]


def test_skipped_scenario_contract_gates_exit_code(tmp_path: Path) -> None:
    (tmp_path / "skip.feature").write_text(
        "Feature: skip\n  Scenario: skipped\n    Given a passing step\n",
        encoding="utf-8",
    )
    (tmp_path / "test_skip_case.py").write_text(
        "import pytest\n"
        "from pytest_bdd import given, scenarios\n"
        "\n"
        'scenarios("skip.feature")\n'
        'pytestmark = pytest.mark.skip(reason="not run")\n'
        "\n"
        '@given("a passing step")\n'
        "def _passing():\n"
        "    pass\n",
        encoding="utf-8",
    )
    result = _run_nested(tmp_path, "test_skip_case.py")
    assert result.returncode == 1, result.stdout + result.stderr
    contract = _decode_contract(result.stdout)
    assert contract["ok"] is False
    assert contract["summary"] == {"total": 1, "passed": 0, "failed": 1}
    assert contract["failures"][0]["reason"] == "Scenario skipped"


def test_scenario_outline_examples_are_audited_and_executed(tmp_path: Path) -> None:
    (tmp_path / "outline.feature").write_text(
        "Feature: outline\n"
        "  Scenario Outline: values\n"
        "    Given a value of <n>\n"
        "\n"
        "    Examples:\n"
        "      | n |\n"
        "      | 2 |\n"
        "      | 3 |\n",
        encoding="utf-8",
    )
    (tmp_path / "test_outline_case.py").write_text(
        "from pytest_bdd import given, parsers, scenarios\n"
        "\n"
        'scenarios("outline.feature")\n'
        "\n"
        '@given(parsers.parse("a value of {n:d}"))\n'
        "def _value(n):\n"
        "    assert n > 0\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "step-report.json"
    result = _run_nested(
        tmp_path, "test_outline_case.py", f"--bdd-step-report={report_path}"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    contract = _decode_contract(result.stdout)
    assert contract["summary"] == {"total": 2, "passed": 2, "failed": 0}
    report = json.loads(report_path.read_text("utf-8"))
    assert report["summary"]["total_steps"] == 2
    assert {mapping["text"] for mapping in report["steps"]} == {
        "a value of 2",
        "a value of 3",
    }


def test_doctest_items_do_not_crash_the_collection_audit(tmp_path: Path) -> None:
    (tmp_path / "test_doctest.py").write_text(
        '""">>> 1 + 1\n2\n"""\n', encoding="utf-8"
    )
    result = _run_nested(tmp_path, "test_doctest.py", "--doctest-modules")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "internal error" not in result.stdout.lower()
    assert CONTRACT_MARKER not in result.stdout
