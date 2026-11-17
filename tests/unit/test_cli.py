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
        rc, out = _run_cli("measure", "complexity", "--warn", "7", "--max", "10", str(FIXTURES / "complex.gd"))
        assert rc == 0
        assert out["ok"] is True
        assert out["tool"] == "measure.complexity"

    def test_exit_code_one_on_violation(self):
        rc, out = _run_cli("measure", "complexity", "--max", "3", str(FIXTURES / "complex.gd"))
        assert rc == 1
        assert out["ok"] is False

    def test_json_is_parseable(self):
        rc, out = _run_cli("measure", "complexity", str(FIXTURES / "complex.gd"))
        assert out["summary"]["functions"] == 2


class TestGherkinCli:
    """SEE-1583：gherkin 作为现役验收管线命令回归（原 accept 迁入）。"""

    def _run_gherkin(self, *argv: str) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, "-m", "godot_qa_toolkit.cli", "gherkin", *argv],
            capture_output=True,
            text=True,
            cwd=str(REPO / "src"),
        )
        return proc.returncode, proc.stdout

    def test_gherkin_is_a_live_top_level_command(self):
        rc, out = self._run_gherkin("--help")
        assert rc == 0
        for leaf in ("compile", "run", "mutate"):
            assert leaf in out

    def test_old_accept_entry_is_rejected(self):
        proc = subprocess.run(
            [sys.executable, "-m", "godot_qa_toolkit.cli", "accept", "--help"],
            capture_output=True, text=True, cwd=str(REPO / "src"),
        )
        assert proc.returncode == 2
        assert "invalid choice" in proc.stderr


class TestMutationCliSurface:
    """A13（SEE-1594 S5）：旧 flag 物理退役——--resume 不再注册；
    --no-reuse-coverage 为真实 prefilter 关断开关。"""

    def test_resume_flag_retired(self):
        sys.path.insert(0, str(REPO / "src"))
        try:
            from godot_qa_toolkit.cli import build_parser
            import pytest as _pytest
            with _pytest.raises(SystemExit):
                build_parser().parse_args(
                    ["mutation", "a.gd", "--project-root", ".", "--resume"])
            with _pytest.raises(SystemExit):
                build_parser().parse_args(
                    ["mutation", "a.gd", "--project-root", ".", "--reuse-coverage"])
            args = build_parser().parse_args(
                ["mutation", "a.gd", "--project-root", ".", "--no-reuse-coverage"])
            assert args.no_reuse_coverage is True
        finally:
            sys.path.remove(str(REPO / "src"))
