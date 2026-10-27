"""SEE-1468 M5-b 物理删除收口：`gherkin` 子命令已自 CLI 移除。

E 判定式（cli 子命令删除）：build_parser() 不再注册 `gherkin`——
parse_args 即 argparse invalid choice（exit 2），非横幅别名。
"""

import pytest

from godot_qa_toolkit.cli import build_parser


def test_gherkin_subcommand_is_removed():
    parser = build_parser()
    assert "gherkin" not in parser._subparsers._group_actions[0].choices


def test_gherkin_args_are_rejected():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["gherkin", "whatever.feature"])
    assert exc.value.code == 2
