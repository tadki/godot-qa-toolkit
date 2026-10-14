"""SEE-1405 §SPEC-004：testscan 换源零耦合单测。

- derive_tests_glob 签名向后兼容：project_root=None → SEE-1400 反查语义逐字节不变
- project_root 显式传入 → gdself 正向索引换源；tie-break 与反查同判（同实现）
- runner._auto_scan_tests_glob 显式传 project_root；扫描失败诚实退 None
"""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.testscan import derive_tests_glob


@pytest.fixture()
def direct_hit_tree(tmp_path):
    """反查可命中的最小树（沿 test_testscan 拓扑）。"""
    proj = tmp_path / "proj"
    tests = proj / "tests" / "unit" / "save"
    tests.mkdir(parents=True)
    (proj / "scripts").mkdir()
    (proj / "scripts" / "save_manager.gd").write_text(
        "class_name SaveManager\nextends Node\n", encoding="utf-8")
    (tests / "test_save.gd").write_text(
        'const S = preload("res://scripts/save_manager.gd")\n',
        encoding="utf-8")
    (tests / "unit" / "other").mkdir(parents=True, exist_ok=True)
    return proj


class TestSignatureCompat:
    def test_none_root_keeps_legacy_semantics(self, direct_hit_tree):
        proj = direct_hit_tree
        target = "res://scripts/save_manager.gd"
        assert derive_tests_glob(target, proj / "tests") == \
            "res://tests/unit/save/test_save.gd"
        assert derive_tests_glob(target, proj / "tests",
                                 project_root=None) == \
            "res://tests/unit/save/test_save.gd"

    def test_no_hits_returns_none_both_paths(self, direct_hit_tree):
        proj = direct_hit_tree
        assert derive_tests_glob("res://scripts/ghost.gd",
                                 proj / "tests") is None
        assert derive_tests_glob("res://scripts/ghost.gd", proj / "tests",
                                 project_root=str(proj)) is None


class TestIndexedPath:
    def test_direct_hit_same_verdict_as_legacy(self, direct_hit_tree):
        proj = direct_hit_tree
        assert derive_tests_glob("res://scripts/save_manager.gd",
                                 proj / "tests",
                                 project_root=str(proj)) == \
            "res://tests/unit/save/test_save.gd"

    def test_indirect_consumer_found_only_by_index(self, tmp_path):
        """纯数据类转手：test 只引 factory，factory 引 data——反查 0 命中，
        索引闭包命中（换源的收益本体）。"""
        proj = tmp_path / "proj"
        tests = proj / "tests"
        (proj / "systems").mkdir(parents=True)
        tests.mkdir()
        (proj / "systems" / "item_data.gd").write_text(
            "class_name ItemData\nextends RefCounted\n", encoding="utf-8")
        (proj / "systems" / "factory.gd").write_text(
            'const D = preload("res://systems/item_data.gd")\n'
            "class_name ItemFactory\n", encoding="utf-8")
        (tests / "test_factory.gd").write_text(
            'const F = preload("res://systems/factory.gd")\n'
            "var d: ItemFactory\n", encoding="utf-8")
        target = "res://systems/item_data.gd"
        assert derive_tests_glob(target, tests) is None  # 反查：假 0 命中
        assert derive_tests_glob(target, tests,
                                 project_root=str(proj)) == \
            "res://tests/test_factory.gd"  # 索引：闭包命中 → 文件级 glob

    def test_tie_break_matches_legacy_choice(self, tmp_path):
        """多目录命中：两源同 tie-break（命中数为主、路径深度次之）。"""
        proj = tmp_path / "proj"
        save = proj / "tests" / "unit" / "save"
        integration = proj / "tests" / "unit" / "integration"
        integration.mkdir(parents=True)
        save.mkdir(parents=True)
        (proj / "scripts").mkdir()
        (proj / "scripts" / "save_manager.gd").write_text(
            "class_name SaveManager\n", encoding="utf-8")
        (save / "t1.gd").write_text(
            'const S = preload("res://scripts/save_manager.gd")\n')
        (integration / "t2.gd").write_text(
            'const S = preload("res://scripts/save_manager.gd")\n')
        (integration / "t3.gd").write_text(
            "var sm: SaveManager\n")
        expected = derive_tests_glob("res://scripts/save_manager.gd",
                                     proj / "tests")
        assert expected == "res://tests/unit/integration/"  # 3 命中 > 2
        assert derive_tests_glob("res://scripts/save_manager.gd",
                                 proj / "tests",
                                 project_root=str(proj)) == expected


class TestTestsRootFallback:
    def test_multi_hit_at_tests_root_falls_back_to_full(self, tmp_path):
        """≥2 命中全部落在 tests/ 根 → 目录级退化为 res://tests/（全量）。

        两源（反查/索引）同判——tie-break 语义不漂移的根档位钉子。
        """
        proj = tmp_path / "proj"
        tests = proj / "tests"
        (proj / "systems").mkdir(parents=True)
        tests.mkdir()
        (proj / "systems" / "util.gd").write_text(
            "class_name Util\n", encoding="utf-8")
        (tests / "t1.gd").write_text(
            'const U = preload("res://systems/util.gd")\n', encoding="utf-8")
        (tests / "t2.gd").write_text(
            'const U = preload("res://systems/util.gd")\n', encoding="utf-8")
        assert derive_tests_glob("res://systems/util.gd", tests) == \
            "res://tests/"
        assert derive_tests_glob("res://systems/util.gd", tests,
                                 project_root=str(proj)) == "res://tests/"


class TestRunnerWiring:
    def test_auto_scan_passes_project_root(self, direct_hit_tree, monkeypatch):
        proj = direct_hit_tree
        seen = {}

        def fake_derive(target, tests_dir, project_root=None):
            seen["project_root"] = project_root
            return "res://tests/unit/save/test_save.gd"

        from godot_qa_toolkit.mutation import testscan
        monkeypatch.setattr(testscan, "derive_tests_glob", fake_derive)
        glob = mrunner._auto_scan_tests_glob(
            str(proj / "scripts" / "save_manager.gd"), str(proj))
        assert glob == "res://tests/unit/save/test_save.gd"
        assert seen["project_root"] == str(proj)

    def test_scan_failure_degrades_to_full_suite(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()

        def boom(target, tests_dir, project_root=None):
            raise RuntimeError("index exploded")

        from godot_qa_toolkit.mutation import testscan
        monkeypatch.setattr(testscan, "derive_tests_glob", boom)
        assert mrunner._auto_scan_tests_glob(
            str(proj / "x.gd"), str(proj)) is None
