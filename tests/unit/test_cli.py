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
    """SEE-1468 M5-a 退役别名期：`gqt gherkin` 仅保留迁移指引横幅
    （stderr 一行 + exit 2）；判定面已由 `gqt accept` / pytest-bdd 承接。"""

    def _run_gherkin_banner(self, *argv: str) -> tuple[int, str]:
        # 别名期横幅走 stderr（stdout 无 JSON 契约），独立于 _run_cli。
        proc = subprocess.run(
            [sys.executable, "-m", "godot_qa_toolkit.cli", "gherkin", *argv],
            capture_output=True,
            text=True,
            cwd=str(REPO / "src"),
        )
        return proc.returncode, proc.stderr

    def test_missing_steps_module_still_exits_with_banner(self):
        rc, err = self._run_gherkin_banner(str(FIXTURES / "sample.feature"))
        assert rc == 2
        assert "retired" in err and "gqt accept" in err

    def test_feature_without_feature_line_still_exits_with_banner(self, tmp_path):
        bad = tmp_path / "bad.feature"
        bad.write_text("Scenario: orphan\n", encoding="utf-8")
        rc, err = self._run_gherkin_banner(str(bad))
        assert rc == 2
        assert "retired" in err


class TestMutationCliSurface:
    """SEE-1400 §SPEC-004：mutation 参数面注册 --resume（向后兼容：缺省 False）。"""

    def test_resume_flag_registered(self):
        sys.path.insert(0, str(REPO / "src"))
        try:
            from godot_qa_toolkit.cli import build_parser
            args = build_parser().parse_args(
                ["mutation", "a.gd", "--project-root", ".", "--resume"])
            assert args.resume is True
            args2 = build_parser().parse_args(
                ["mutation", "a.gd", "--project-root", "."])
            assert args2.resume is False
        finally:
            sys.path.remove(str(REPO / "src"))
