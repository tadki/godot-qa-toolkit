"""SEE-1606 P1：mutation 全命令面默认 timeout 60 → 600（Owner 2026-11-19 已决）。

实测依据（SEE-1604 QA）：默认 60s 下 159 脚本并集面 ~77s 必触发 baseline
快照超时 → 文档化串行降级。判据拆三组：

1. CLI 单源默认 = 600（parser + help 文案同步）；
2. 执行臂 API 默认口径一致 = 600（函数签名兜底不得留 60）；
3. D3 提升阶梯语义复核：默认值等于预算上限后，显式低 timeout 仍可
   raise 到 600，命中/超过上限必须走 narrow_face，不得回显同值 timeout。
"""

from __future__ import annotations

import inspect

import pytest

from godot_qa_toolkit.cli import build_parser
from godot_qa_toolkit.mutation import executor as executor_mod
from godot_qa_toolkit.mutation import runner as runner_mod
from godot_qa_toolkit.timing import (
    BUDGET_CAP_S,
    suggested_action,
    suggested_action_kind,
)

DEFAULT_TIMEOUT_S = 600


def _mutation_args(extra: list[str] | None = None):
    parser = build_parser()
    return parser.parse_args(
        ["mutation", "x.gd", "--project-root", "/p", *(extra or [])])


def _mutation_timeout_action():
    parser = build_parser()
    for action in parser._subparsers._group_actions:
        sub = action.choices.get("mutation")
        if sub is not None:
            return next(a for a in sub._actions if a.dest == "timeout")
    raise AssertionError("mutation subparser has no --timeout action")


class TestCliDefault:
    def test_parser_default_is_600(self):
        assert _mutation_args().timeout == DEFAULT_TIMEOUT_S

    def test_explicit_timeout_still_wins(self):
        assert _mutation_args(["--timeout", "30"]).timeout == 30

    def test_help_advertises_600(self):
        help_text = " ".join(_mutation_timeout_action().help.split())
        assert "(default 600)" in help_text, help_text


@pytest.mark.parametrize("func", [
    runner_mod._run_gut_on_project,
    runner_mod.run_mutation,
    runner_mod._run_mutation_core,
    executor_mod.run_mutation_files,
    executor_mod._run_mutation_files_impl,
])
def test_execution_api_default_is_600(func):
    param = inspect.signature(func).parameters["timeout_s"]
    assert param.default == DEFAULT_TIMEOUT_S


class TestRaiseLadderStillCoherent:
    """D3 契约复核：默认 600 == 预算上限，阶梯语义不变。"""

    def test_default_equals_budget_cap(self):
        assert DEFAULT_TIMEOUT_S == BUDGET_CAP_S

    def test_default_timeout_reports_narrow_face(self):
        assert suggested_action_kind(DEFAULT_TIMEOUT_S) == "narrow_face"

    def test_default_timeout_never_echoes_same_value(self):
        assert f"--timeout {BUDGET_CAP_S}" not in suggested_action(
            DEFAULT_TIMEOUT_S)

    def test_explicit_low_timeout_still_raises_to_cap(self):
        assert suggested_action_kind(60) == "raise_timeout"
        assert suggested_action(60) == f"rerun with --timeout {BUDGET_CAP_S}"
