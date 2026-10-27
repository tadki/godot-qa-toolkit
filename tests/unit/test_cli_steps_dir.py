"""Steps-dir CLI arguments under the M5-a retirement alias period.

SEE-1468 M5-a：`gqt gherkin` 退役为横幅 + exit 2——目录聚合 loader
（_try_load_steps）随入口退役；本文件钉住「任意 steps 组合仍走横幅」
的收口面，防参数路径残留旧行为。
"""

from __future__ import annotations

from pathlib import Path

from godot_qa_toolkit import cli as cli_mod
from godot_qa_toolkit.cli import build_parser


def _args(*argv: str):
    return build_parser().parse_args(list(argv))


def test_steps_dir_argument_still_exits_with_banner(tmp_path, capsys):
    args = _args("gherkin", "whatever.feature", "--steps", str(tmp_path))
    rc = cli_mod.cmd_gherkin(args)
    captured = capsys.readouterr()
    assert rc == 2
    assert "retired" in captured.err


def test_steps_file_argument_still_exits_with_banner(tmp_path, capsys):
    steps = tmp_path / "steps.py"
    steps.write_text("def register(r):\n    pass\n", encoding="utf-8")
    args = _args("gherkin", "whatever.feature", "--steps", str(steps))
    rc = cli_mod.cmd_gherkin(args)
    captured = capsys.readouterr()
    assert rc == 2
    assert "retired" in captured.err


def test_missing_steps_argument_still_exits_with_banner(capsys):
    args = _args("gherkin", "whatever.feature", "--steps", "no/such/dir")
    rc = cli_mod.cmd_gherkin(args)
    captured = capsys.readouterr()
    assert rc == 2
    assert "retired" in captured.err


def test_directory_loader_is_retired_with_the_entry():
    # _try_load_steps 随入口退役删除（M5-b 物理删除 runner/expectations）。
    assert not hasattr(cli_mod, "_try_load_steps")
