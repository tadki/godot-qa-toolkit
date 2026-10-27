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


class TestLoadStepsModuleRetired:
    """SEE-1468 M5-a：目录/单文件 loader（_load_steps_module）随 `gqt gherkin`
    入口退役删除——横幅路径对 steps 参数仍 exit 2，且 loader 不再存在。"""

    def test_loader_removed_from_cli_module(self):
        assert not hasattr(cli_mod, "_load_steps_module")

    def test_steps_arguments_route_to_banner(self, tmp_path, capsys):
        steps = tmp_path / "steps.py"
        steps.write_text("def register(r):\n    pass\n", encoding="utf-8")
        args = _args("gherkin", "whatever.feature", "--steps", str(steps))
        rc = cli_mod.cmd_gherkin(args)
        captured = capsys.readouterr()
        assert rc == 2
        assert "retired" in captured.err
