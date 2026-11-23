"""SEE-1623 M4 — 数值参数校验前置行为钉。

根因（SEE-1622 Bachi BUG-8/BUG-11、Refacty L9）：`--budget 0/-3` 返回
假绿（exit 0 / mutants=0 / ok=true）、负 `--timeout` 被接受（两臂瞬间
超时）、非法 `--lines` 形态泄露 Python `int()` 转换异常。

行为契约（§5 用法/装载错误 exit 2，stdout 无 JSON，stderr 可读提示；
H2 §SPEC-002 的「非法参数在 dry-run 即拒」同源达成）：
1. `--budget < 1` / `--jobs < 1` / `--timeout < 1` → exit 2 + 可读提示。
2. 非法 `--lines` 形态 → exit 2 + 可读提示（零 Python 异常字样泄漏）。
3. measure 面负 `--timeout` 同拒。
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from godot_qa_toolkit.cli import build_parser


def _run(argv) -> tuple[int, str, str]:
    args = build_parser().parse_args(argv)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = args.func(args)
    except SystemExit as e:
        code = int(e.code or 0)
    return code, buf.getvalue(), ""


def test_budget_zero_rejected_usage_error(tmp_path, capsys):
    code, out, _ = _run([
        "mutation", "src/calc.gd", "--project-root", str(tmp_path),
        "--budget", "0"])
    err = capsys.readouterr().err
    assert code == 2
    assert out == "", "用法错误 stdout 无 JSON（§5）"
    assert "budget" in err


def test_budget_negative_rejected(tmp_path, capsys):
    code, out, _ = _run([
        "mutation", "src/calc.gd", "--project-root", str(tmp_path),
        "--budget", "-3"])
    assert code == 2
    assert out == ""


def test_jobs_zero_rejected(tmp_path, capsys):
    code, out, _ = _run([
        "mutation", "src/calc.gd", "--project-root", str(tmp_path),
        "--jobs", "0"])
    assert code == 2
    assert out == ""


def test_negative_timeout_rejected(tmp_path, capsys):
    code, out, _ = _run([
        "mutation", "src/calc.gd", "--project-root", str(tmp_path),
        "--timeout", "-1"])
    assert code == 2
    assert out == ""


def test_lines_invalid_form_rejected_no_python_leak(tmp_path, capsys):
    code, out, _ = _run([
        "mutation", "src/calc.gd", "--project-root", str(tmp_path),
        "--lines", "abc"])
    err = capsys.readouterr().err
    assert code == 2
    assert out == ""
    assert "invalid literal for int()" not in err
    assert "--lines" in err


def test_measure_run_negative_timeout_rejected(tmp_path, capsys):
    code, out, _ = _run([
        "measure", "run", "src", "--project-root", str(tmp_path),
        "--recipe", str(tmp_path / "r.json"),
        "--out", str(tmp_path / "o.json"), "--timeout", "-5"])
    assert code == 2
    assert out == ""


def test_valid_values_still_accepted(tmp_path):
    """合法值不被误拒（budget 1 / lines 区间形态）。"""
    args = build_parser().parse_args([
        "mutation", "src/calc.gd", "--project-root", str(tmp_path),
        "--budget", "1", "--lines", "10-20,30"])
    # 校验器直连：合法形态零报错（不触达任何贵操作）
    from godot_qa_toolkit.cli import _mutation_numeric_arg_error
    assert _mutation_numeric_arg_error(args) is None
