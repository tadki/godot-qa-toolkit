"""CLI-level tests: unified JSON contract + exit codes (SEE-1268 M1)."""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
FIXTURES = REPO / "tests" / "fixtures"


def _run_cli(*argv: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-m", "godot_qa_toolkit.cli", *argv],
        capture_output=True,
        text=True,
        cwd=str(REPO / "src"),
    )
    # CLI prints the contract on stdout; import-path noise would break parsing,
    # so run with src on sys.path via cwd and parse the LAST JSON object.
    start = proc.stdout.index("{")
    return proc.returncode, json.loads(proc.stdout[start:])


class TestComplexityCli:
    def test_exit_code_zero_on_pass(self):
        rc, out = _run_cli("complexity", "--warn", "7", "--max", "10", str(FIXTURES / "complex.gd"))
        assert rc == 0
        assert out["ok"] is True
        assert out["tool"] == "complexity"

    def test_exit_code_one_on_violation(self):
        rc, out = _run_cli("complexity", "--max", "3", str(FIXTURES / "complex.gd"))
        assert rc == 1
        assert out["ok"] is False

    def test_json_is_parseable(self):
        rc, out = _run_cli("complexity", str(FIXTURES / "complex.gd"))
        assert out["summary"]["functions"] == 2


class TestGherkinCli:
    def test_missing_steps_module_reports_unmatched(self):
        rc, out = _run_cli("gherkin", str(FIXTURES / "sample.feature"))
        # Steps module not provided -> every step unmatched -> fail verdict.
        assert rc == 1
        assert out["ok"] is False
        assert out["failures"][0]["reason"] == "no matching step definition"

    def test_feature_without_feature_line_fails(self, tmp_path):
        bad = tmp_path / "bad.feature"
        bad.write_text("Scenario: orphan\n", encoding="utf-8")
        rc, out = _run_cli("gherkin", str(bad))
        assert rc == 1
        assert ":1:" in out["failures"][0]["reason"]
