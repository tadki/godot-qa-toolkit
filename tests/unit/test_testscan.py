"""SEE-1321 SPEC-001/002：测试子集自动扫描器单测。"""

import pytest

from godot_qa_toolkit.mutation.testscan import (
    derive_tests_glob,
    scan_test_refs,
)


@pytest.fixture()
def tests_tree(tmp_path):
    """最小 tests/ 树：save 模块引用目标文件，vendor 不引用。"""
    tests = tmp_path / "tests"
    save = tests / "unit" / "save"
    vendor = tests / "unit" / "vendor"
    for d in (save, vendor):
        d.mkdir(parents=True)
    (save / "test_save.gd").write_text(
        'extends RefCounted\n'
        'const S = preload("res://scripts/save_manager.gd")\n'
        'M.load_script = load("res://scripts/save_manager.gd")\n'
    )
    (vendor / "test_vendor.gd").write_text(
        'extends RefCounted\n'
        'const V = preload("res://scripts/vendor.gd")\n'
    )
    (tests / "unit" / "test_misc.gd").write_text(
        'extends RefCounted\nvar sm = null\nvar manager\n'
        'var save_manager = 1\nvar other = "res://scripts/save_manager.gd.bak"\n'
    )
    return tests


class TestScanTestRefs:
    def test_preload_directive(self, tests_tree):
        hits = scan_test_refs("res://scripts/save_manager.gd", tests_tree)
        assert any("test_save.gd" in h for h in hits)

    def test_load_directive(self, tests_tree):
        hits = scan_test_refs("res://scripts/save_manager.gd", tests_tree)
        # preload 与 load 各命中同文件，去重后仍为 1
        assert sum("test_save.gd" in h for h in hits) == 1

    def test_class_name_reference(self, tests_tree):
        hits = scan_test_refs("res://scripts/save_manager.gd", tests_tree)
        assert any("test_misc.gd" in h for h in hits)

    def test_string_literal_suffix_not_a_hit(self, tests_tree):
        hits = scan_test_refs("res://scripts/save_manager.gd", tests_tree)
        # "...save_manager.gd.bak" 字面量引用的是别的文件，不得命中
        assert not any("vendor" in h for h in hits)

    def test_only_referencing_module_snapped(self, tests_tree):
        dirs = derive_tests_glob("res://scripts/save_manager.gd", tests_tree)
        assert dirs == "res://tests/unit/save/"

    def test_returns_full_res_path_for_scoring(self, tests_tree):
        hits = scan_test_refs("res://scripts/vendor.gd", tests_tree)
        assert len(hits) == 1 and hits[0].endswith("test_vendor.gd")


class TestDeriveTestsGlob:
    def test_youngest_common_directory(self, tests_tree):
        assert derive_tests_glob("res://scripts/vendor.gd", tests_tree) == \
            "res://tests/unit/vendor/"

    def test_no_hits_returns_none(self, tests_tree):
        assert derive_tests_glob("res://scripts/unknown.gd", tests_tree) is None

    def test_multiple_dirs_scored(self, tests_tree):
        tests = tests_tree
        extra = tests / "unit" / "integration"
        extra.mkdir()
        (extra / "test_integration.gd").write_text(
            'var s = preload("res://scripts/save_manager.gd")\n')
        assert derive_tests_glob("res://scripts/save_manager.gd", tests) == \
            "res://tests/unit/save/"

    def test_case_insensitive_reference(self, tmp_path):
        tests = tmp_path / "tests"
        d = tests / "unit" / "save"
        d.mkdir(parents=True)
        (d / "t.gd").write_text('preload("RES://SCRIPTS/Save_Manager.GD")\n')
        # Windows 文件系统大小写不敏感；res:// 路径按 casefold 比对
        assert derive_tests_glob("res://scripts/save_manager.gd", tests) == \
            "res://tests/unit/save/"
