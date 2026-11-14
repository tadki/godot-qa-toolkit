"""In-process CLI tests (SEE-1306 hardener, §SPEC-006/010).

The M1-era CLI tests exercise `python -m godot_qa_toolkit.cli` via
subprocess, so pytest-cov never sees inside `cli.py` (44%). These tests call
`build_parser().parse_args(...)` + the command function directly in-process,
closing that blind spot without replacing the subprocess suite (which pins
real interpreter behaviour: exit codes, stderr-only caller errors).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from godot_qa_toolkit import cli as cli_mod
from godot_qa_toolkit.cli import build_parser

REPO = Path(__file__).parent.parent.parent

FEATURE = """\
Feature: in-process
  Scenario: passes
    Given a domain step
"""


def _args(*argv: str) -> pytest.MonkeyPatch:
    return build_parser().parse_args(list(argv))


class TestGherkinSubcommandRemoved:
    """SEE-1583 反转 SEE-1468 收口：gherkin 子命令回归现役（原 accept
    迁入），旧 accept 入口不再注册。"""

    def test_gherkin_subcommand_registered(self):
        parser = build_parser()
        choices = parser._subparsers._group_actions[0].choices
        assert "gherkin" in choices
        assert "accept" not in choices

    def test_cmd_gherkin_present_in_cli_module(self):
        assert callable(getattr(cli_mod, "cmd_gherkin", None))

    def test_gherkin_args_rejected_at_parse(self):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["gherkin", "whatever.feature"])
        assert exc.value.code == 2
