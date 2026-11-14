"""SEE-1583：`gherkin` 子命令回归现役（原 `accept` 迁入），旧 `accept`
入口保持物理删除。

本文件断言两个方向：`gherkin` 在 build_parser() 注册且 `accept` 不再
注册；对 `gherkin` 传非法二级子命令仍 argparse invalid choice（exit 2）。
"""

import pytest

from godot_qa_toolkit.cli import build_parser


def test_gherkin_subcommand_is_registered():
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert "gherkin" in choices
    assert "accept" not in choices


def test_gherkin_args_are_rejected():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["gherkin", "whatever.feature"])
    assert exc.value.code == 2
