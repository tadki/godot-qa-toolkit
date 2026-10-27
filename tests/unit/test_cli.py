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
    """SEE-1468 M5-b 物理删除收口：gherkin 子命令已自 CLI 移除。"""

    def _run_gherkin(self, *argv: str) -> tuple[int, str]:
        # SEE-1468 M5-b 物理删除：gherkin 子命令已移除，argparse 拒绝并 exit 2。
        proc = subprocess.run(
            [sys.executable, "-m", "godot_qa_toolkit.cli", "gherkin", *argv],
            capture_output=True,
            text=True,
            cwd=str(REPO / "src"),
        )
        return proc.returncode, proc.stderr

    def test_gherkin_subcommand_is_physically_removed(self):
        rc, err = self._run_gherkin(str(FIXTURES / "sample.feature"))
        assert rc == 2
        # argparse 的 choices 文案随 Python 小版本变化（带/不带引号）——
        # 只钉「invalid choice: gherkin」这一稳定核心。
        assert "invalid choice" in err
        assert "gherkin" in err


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
