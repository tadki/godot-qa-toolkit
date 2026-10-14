"""SEE-1405 ③ hardener——gdself_stats 对抗性补强。

缺口行 80/107：S3 份额与场景链计数补「零命中目标仅经资产链可达」端到端；
缺口行 138：模块入口 ``if __name__ == "__main__"`` 以 runpy 进程内触发。
对抗点：资产节点一律计 S3（绑定非目标文件时不得虚计）。
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from godot_qa_toolkit.mutation.gdself_stats import collect_stats


def _proj(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "systems").mkdir(parents=True)
    (root / "scenes").mkdir()
    (root / "tests").mkdir()
    (root / "project.godot").write_text("[application]\n", encoding="utf-8")
    return root


@pytest.fixture()
def scene_chain_tree(tmp_path):
    """零命中目标仅经 .tres 场景链可达：testscan 反查 0，gdself S3 命中。"""
    root = _proj(tmp_path)
    (root / "systems" / "view_model.gd").write_text(
        "class_name ViewModel\nextends RefCounted\n", encoding="utf-8")
    (root / "scenes" / "vm.tres").write_text(
        '[gd_resource type="Resource" format=3]\n'
        '[ext_resource type="Script" '
        'path="res://systems/view_model.gd" id="1"]\n', encoding="utf-8")
    (root / "tests" / "t.gd").write_text(
        'const S = preload("res://scenes/vm.tres")\n', encoding="utf-8")
    return root


class TestSceneChainShares:
    def test_s3_share_and_scene_chain_counted(self, scene_chain_tree):
        stats = collect_stats(str(scene_chain_tree))
        core = stats["scope_core"]
        assert core["production_gd_files"] == 1
        assert core["legacy_zero_hit"] == 1  # testscan 反查 0（假 0 命中）
        assert core["gdself_eliminated"] == 1
        assert core["per_source_any_hit"]["S3"] == 1
        assert core["per_source_any_hit"]["S4"] == 1  # test 经 path 中转
        assert core["scene_chain_targets"] == 1
        assert core["remaining_unresolved_reasons"] == {}

    def test_s3_absent_when_binding_points_elsewhere(self, tmp_path):
        root = _proj(tmp_path)
        (root / "systems" / "orphan.gd").write_text(
            "class_name Orphan\n", encoding="utf-8")
        (root / "scenes" / "other.tres").write_text(
            '[gd_resource format=3]\n'
            '[ext_resource type="Script" '
            'path="res://systems/someone_else.gd" id="1"]\n',
            encoding="utf-8")
        (root / "tests" / "t.gd").write_text(
            'const S = preload("res://scenes/other.tres")\n',
            encoding="utf-8")
        stats = collect_stats(str(root))
        core = stats["scope_core"]
        assert core["legacy_zero_hit"] == 1
        assert core["gdself_eliminated"] == 0
        assert core["per_source_any_hit"]["S3"] == 0
        assert core["scene_chain_targets"] == 0
        assert core["remaining_unresolved_reasons"] == {"no_tests": 1}


class TestModuleEntrypoint:
    def test_dunder_main_exits_zero(self, scene_chain_tree, monkeypatch):
        monkeypatch.setattr(sys, "argv",
                            ["gdself_stats",
                             "--project-root", str(scene_chain_tree)])
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("godot_qa_toolkit.mutation.gdself_stats",
                             run_name="__main__")
        assert excinfo.value.code == 0
