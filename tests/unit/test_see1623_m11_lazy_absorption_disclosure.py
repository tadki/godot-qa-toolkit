"""SEE-1623 M11 — 红基线懒吸收路径具名披露行为钉。

根因（SEE-1622 Refacty F2）：文档承诺「按名排除 + map 标 partial」的
预检披露语义，lazy 路径实测 `state=valid、partial=false、
excluded_tests=[]`，红测保留在映射面且无任何等价具名披露——只有
「only NEW failures gate the arm」一句话而无名单。

行为契约：懒吸收 >0 对时，baseline_failures_meta 携带
status=partial + excluded_tests 具名名单，stderr 同步具名（与预检
路径披露对齐）。
"""

from __future__ import annotations

import subprocess as sp

import pytest

import test_see1594_s63_lazy_baseline as s63
from test_see1594_s63_lazy_baseline import (  # noqa: F401 — pytest fixture
    LazyScenarioGodot,
    project,
)
from godot_qa_toolkit.measurement.orchestrator import run_measurement


@pytest.fixture()
def _restore_subprocess():
    orig_run = sp.run
    yield
    sp.run = orig_run


def test_lazy_absorption_named_partial_disclosure(
        project, tmp_path, _restore_subprocess, capsys):
    """懒吸收路径：meta status=partial + excluded_tests 具名 + stderr 名单
    （与预检路径披露语义对齐，F2 缺口收口）。"""
    fake = LazyScenarioGodot(project, scenario="absorb")
    sp.run = fake
    report = run_measurement(
        project, s63._targets(project), s63._recipe(project, tmp_path),
        test_attribution=True,
        attribution_out=str(tmp_path / "map.json"))
    gut = report["arms"][0]
    meta = gut.get("baseline_failures_meta") or {}
    assert meta.get("status") == "partial"
    excluded = meta.get("excluded_tests") or []
    assert "res://tests/unit/test_game.gd::test_font_row" in excluded
    err = capsys.readouterr().err
    assert "EXCLUDED by name" in err
    assert "test_font_row" in err
    assert "partial" in err


def test_zero_absorption_no_partial_marker(
        project, tmp_path, _restore_subprocess):
    """懒基线零失败（无吸收）→ 不带 partial 标记（无排除即无披露）。"""
    from test_see1623_h5_intolerance_lane import CleanLazyGodot
    fake = CleanLazyGodot(project, scenario="garbage")
    sp.run = fake
    report = run_measurement(
        project, s63._targets(project), s63._recipe(project, tmp_path),
        test_attribution=True,
        attribution_out=str(tmp_path / "map.json"))
    meta = (report["arms"][0].get("baseline_failures_meta") or {})
    assert meta.get("status") != "partial"
    assert not (meta.get("excluded_tests") or [])
