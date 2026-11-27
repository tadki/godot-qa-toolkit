"""Shared pytest fixtures and the BDD migration hooks."""

from __future__ import annotations

import sys
from pathlib import Path

import os

import pytest

from tests.bdd.plugin import *

# 测试辅助模块（attribution_fixtures 等）与被测用例同目录
sys.path.insert(0, str(Path(__file__).parent / "unit"))


@pytest.fixture(autouse=True)
def _stub_godot_binary_for_vacuum(monkeypatch, tmp_path_factory):
    """§SPEC-VACUUM-ADAPT：CI/真空环境 stub godot binary。

    plan-first 将 godot binary 缺失列 error 阻断（产品行为正确）——
    历史测试夹具未提供 binary，真空形态（无 GQT_GODOT_BIN、PATH 无
    godot）大量路由契约测试被 plan_blocked。本 fixture 仅在检测到
    真空形态时注入 stub binary（非真空不动作，零行为影响）。"""
    if os.environ.get("GQT_GODOT_BIN"):
        return
    path_env = os.environ.get("PATH", "")
    if any(Path(d).joinpath("godot").is_file()
           for d in path_env.split(os.pathsep) if d):
        return
    stub = tmp_path_factory.mktemp("stub-bin") / "godot"
    # --version 输出合法版本串：identity 解析成功，避免 planner/executor
    # disclosure parity 在 stub 下不对称
    stub.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then '
                    'echo "4.6.2-stable.official.stub"; exit 0; fi\n'
                    'exit 0\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("GQT_GODOT_BIN", str(stub))


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
    monkeypatch.delenv("GQT_ATTRIBUTION_MAP", raising=False)
    # SEE-1594 §SPEC-019：单元测试不得触发真实 `godot --import`（假
    # project.godot fixture 会让自动导入外呼真实引擎）；零配置自动导入只
    # 在真实 KOL 矩阵/生产路径生效。
    monkeypatch.setenv("GQT_AUTO_IMPORT", "0")
    truth_select.reset_ledger()
    yield
    truth_select.reset_ledger()


@pytest.fixture(autouse=True)
def _mutation_baseline_default_cold(monkeypatch):
    """SEE-1631 §SPEC-020：单元测试面默认 legacy 冷启动判定基线。

    生产默认（map 源）由 CLI 消费方生效；单元套件的大量 mutation 机制
    反例（并行分片/预算重试/prefilter 复用/进度面等）以冷启动为既定主体
    ——统一在此回退 cold，map 源行为由 test_c2_verdict_source_see1631.py
    专属覆盖（该文件显式改写本默认）。"""
    monkeypatch.setenv("GQT_MUTATION_BASELINE_SOURCE", "cold")
    yield
