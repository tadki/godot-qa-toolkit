"""SEE-1550 §SPEC-017 —— vendored addons 显式变异目标 + 默认扫描域边界。

- 显式指定 addons/ 下目标：收集位点非零（含 §SPEC-014 新点亮的 `*` mdr
  位点）、影子注入 E2E 由 features/see1550_forms.feature 的 vendored 场景
  承接（真实引擎实跑）；
- 默认扫描域不含 vendored：目标枚举不存在自动面——唯一"扫描"是 testscan
  的 tests/ 树显式引用回查（target-driven，绝不从 addons 发明变异源）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation.ops import _collect_mutations
from godot_qa_toolkit.mutation.testscan import derive_tests_glob

REPO = Path(__file__).resolve().parents[2]
VENDOR_SRC = (REPO / "tests" / "fixtures" / "see1550_forms"
              / "addons" / "gqt_forms_vendor" / "vendor_calc.gd")


class TestVendoredExplicitTarget:
    def test_vendor_file_collects_nonzero_sites_incl_mdr_mul(self):
        """§SPEC-017 E2E 的收集面前置：vendored 文件位点非零，且含
        §SPEC-014 新点亮的 `*` mdr 位点（3 个）。"""
        src = VENDOR_SRC.read_text(encoding="utf-8")
        ms = _collect_mutations(src, str(VENDOR_SRC))
        kinds = sorted(m.original for m in ms if m.kind == "AOR")
        assert kinds.count("+") == 3
        assert kinds.count("*") == 3, "mdr_expr `*` 位点必须点亮（§SPEC-014）"
        assert len(ms) == 6

    def test_scan_finds_explicit_referencing_test(self, tmp_path):
        """显式引用驱动的回查对 addons/ 目标同样生效（显式指定通道的
        scoped 测试子集来源）。目标文件需真实在场（gdself 索引锚）。"""
        vendor = tmp_path / "addons" / "vendor"
        vendor.mkdir(parents=True)
        (vendor / "v.gd").write_text("extends RefCounted\n")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_vendor.gd").write_text(
            'extends GutTest\n\nconst V := preload("res://addons/vendor/v.gd")\n')
        glob = derive_tests_glob("res://addons/vendor/v.gd", tests,
                                 project_root=str(tmp_path))
        assert glob is not None
        assert "test_vendor" in glob

    def test_scan_never_invents_targets_from_addons(self, tmp_path):
        """默认扫描域边界：无 tests/ 引用时 addons/ 目标回查 None——扫描
        只回查显式引用，绝不把 addons/ 树自动升级为变异源枚举面。"""
        vendor = tmp_path / "addons" / "vendor"
        vendor.mkdir(parents=True)
        (vendor / "v.gd").write_text("extends RefCounted\n")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_unrelated.gd").write_text(
            'extends GutTest\n\nfunc test_x():\n\tpass\n')
        glob = derive_tests_glob("res://addons/vendor/v.gd", tests,
                                 project_root=str(tmp_path))
        assert glob is None
