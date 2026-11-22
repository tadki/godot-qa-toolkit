"""SEE-1615 §SPEC-PLANFIRST 根修 —— _roots_of 按 project_root 锚定。

CWD≠project_root 时相对路径目标面不得静默放大到全工程（KOL 实测
824 文件含 tests/，源码契约断言必坏）；工程外路径的 fallback 具名披露。
"""

from __future__ import annotations

import os
from pathlib import Path

from godot_qa_toolkit.mutation.truth_select import _roots_of, _targets_under


def _mk(root: Path, rel: str, text: str = "extends RefCounted\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_relative_path_anchors_to_project_root_when_cwd_differs(
        tmp_path, monkeypatch):
    """判据 3：CWD≠project_root 时 `utils/container_utils.gd` 的目标面
    仍为 utils/**，不触及 tests/。"""
    proj = tmp_path / "proj"
    _mk(proj, "utils/container_utils.gd")
    _mk(proj, "utils/deep/other.gd")
    _mk(proj, "tests/unit/test_a.gd")
    monkeypatch.chdir(tmp_path)  # CWD=工程外目录
    roots = _roots_of(["utils/container_utils.gd"], str(proj))
    assert roots == [str(proj / "utils")]
    kept = _targets_under(roots)
    assert all("/tests/" not in f for f in kept)
    assert str(proj / "utils" / "container_utils.gd") in kept
    assert str(proj / "utils" / "deep" / "other.gd") in kept


def test_top_level_file_falls_back_with_disclosure(tmp_path, monkeypatch,
                                                   capsys):
    """首层文件 → 面=工程根（合法 fallback），但必须具名披露
    targets_root_fallback，不再静默。"""
    proj = tmp_path / "proj"
    _mk(proj, "boot.gd")
    monkeypatch.chdir(tmp_path)
    roots = _roots_of(["boot.gd"], str(proj))
    assert roots == [str(proj)]
    out = capsys.readouterr().err
    assert "targets_root_fallback" in out


def test_outside_project_paths_disclosed_and_widened(tmp_path, monkeypatch,
                                                     capsys):
    other = tmp_path / "elsewhere"
    _mk(other, "x.gd")
    proj = tmp_path / "proj"
    _mk(proj, "calc.gd")
    monkeypatch.chdir(tmp_path)
    roots = _roots_of([str(other / "x.gd")], str(proj))
    assert roots == [str(proj)]  # 工程外 → 具名放大
    _ = _targets_under(roots)  # 放大面可照常枚举（披露在 roots 侧）
    assert "targets_root_fallback" in capsys.readouterr().err


def test_absolute_path_inside_project_unaffected(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    _mk(proj, "utils/calc.gd")
    _mk(proj, "tests/unit/test_a.gd")
    monkeypatch.chdir(tmp_path)
    roots = _roots_of([str(proj / "utils" / "calc.gd")], str(proj))
    assert roots == [str(proj / "utils")]
