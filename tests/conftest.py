"""Shared pytest fixtures and the BDD migration hooks."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.bdd.plugin import *

# 测试辅助模块（attribution_fixtures 等）与被测用例同目录
sys.path.insert(0, str(Path(__file__).parent / "unit"))


@pytest.fixture
def bdd_state() -> dict:
    """Fresh mutable scenario context, discarded after each scenario."""

    return {}


@pytest.fixture(autouse=True)
def _attribution_map_default_off(monkeypatch):
    """SEE-1581：测试面默认关闭归属映射的自动生成/刷新（映射不可用即 fail-loud）。

    需要映射语义的用例显式写映射（attribution_fixtures.write_map），新鲜
    映射仍会被正常消费；auto 生成属于 CLI 生产默认，不在单元测试触发。
    """
    from godot_qa_toolkit.mutation import truth_select
    monkeypatch.setenv(truth_select.ENV_MODE, "off")
    monkeypatch.delenv(truth_select.ENV_MAP, raising=False)
    # SEE-1594 §SPEC-019：单元测试不得触发真实 `godot --import`（假
    # project.godot fixture 会让自动导入外呼真实引擎）；零配置自动导入只
    # 在真实 KOL 矩阵/生产路径生效。
    monkeypatch.setenv("GQT_AUTO_IMPORT", "0")
    truth_select.reset_ledger()
    yield
    truth_select.reset_ledger()
