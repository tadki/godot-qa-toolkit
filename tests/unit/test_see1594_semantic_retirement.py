"""SEE-1594 §SPEC-027/S7：退役防复发钉（回潮即红，canonical 位置）。

本文件是 SEE-1594 退役项的唯一 pin 位置（S7 起集中管理；与
test_low_fixes_see1405 / test_cli / test_see1594_s5_flags_semantics 中
的原散落退役钉去重合并——重复者已删旧）。

覆盖：A13 flag 退役、A14 披露面退役、A09 静态 ownership 退役、
A10 静态选面退役、A12 行身份辅助、A04 /1 store 无写路径、
sink-recovery 覆盖来源退役。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

_SRC = Path(__file__).resolve().parents[2] / "src"


def _src_hits(pattern: str, allow_file_substr: str | None = None) -> list[str]:
    """src 树文本扫描：pattern 命中的文件列表（可选豁免一个具名文件）。"""
    hits = []
    for py in sorted(_SRC.rglob("*.py")):
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pattern in text:
            if allow_file_substr and allow_file_substr in str(py):
                continue
            hits.append(str(py))
    return hits


class TestA13FlagsStayRetired:
    """A13：--resume / --reuse-coverage 物理退役（回潮即红）。"""

    def test_resume_flag_not_registered(self):
        from godot_qa_toolkit.cli import build_parser
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["mutation", "a.gd", "--project-root", ".", "--resume"])

    def test_reuse_coverage_flag_not_registered(self):
        from godot_qa_toolkit.cli import build_parser
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["mutation", "a.gd", "--project-root", ".", "--reuse-coverage"])

    def test_no_reuse_coverage_is_real_off_switch(self):
        from godot_qa_toolkit.cli import build_parser
        args = build_parser().parse_args(
            ["mutation", "a.gd", "--project-root", "."])
        from godot_qa_toolkit.cli import _reuse_coverage_flag
        assert _reuse_coverage_flag(args) is True
        args2 = build_parser().parse_args(
            ["mutation", "a.gd", "--project-root", ".", "--no-reuse-coverage"])
        assert _reuse_coverage_flag(args2) is False


class TestA14DisclosureSurfaceStayRetired:
    """A14：v1 在场披露与 identity.disclose_superseded 物理退役。"""

    def test_resume2_disclose_v1_superseded_removed(self):
        from godot_qa_toolkit.mutation.resume2 import ResumeStoreV2
        assert not hasattr(ResumeStoreV2, "disclose_v1_superseded"), (
            "A14：v1 披露方法回潮")

    def test_identity_disclose_superseded_removed(self):
        from godot_qa_toolkit.mutation import identity
        assert not hasattr(identity, "disclose_superseded"), (
            "A14：identity.disclose_superseded 回潮")

    def test_no_v1_disclosure_output(self, tmp_path, capsys):
        """v1 目录在场 → 静默忽略（不读不写不删、零 stderr 披露）。"""
        from godot_qa_toolkit.mutation.resume2 import ResumeStoreV2
        proj = tmp_path / "proj"
        v1 = proj / ".qa-cache" / "gqt-resume-v1"
        v1.mkdir(parents=True)
        (v1 / "0.json").write_text("{}", encoding="utf-8")
        store = ResumeStoreV2(str(proj), str(proj / "calc.gd"),
                              "res://tests/", binary_kind=None,
                              identity={"ops_version": "x"})
        assert not hasattr(store, "disclose_v1_superseded")
        err = capsys.readouterr().err
        assert "resume v1 store found" not in err


class TestA09StaticOwnershipStayRetired:
    """A09：StructuralOwnership / TestOwnershipIndex 物理退役（src 零引用）。"""

    def test_symbols_absent_from_imports(self):
        from godot_qa_toolkit.mutation import truth_select
        assert not hasattr(truth_select, "StructuralOwnership")
        assert not hasattr(truth_select, "TestOwnershipIndex")

    def test_symbols_absent_from_src_tree(self):
        assert _src_hits("StructuralOwnership") == []
        assert _src_hits("TestOwnershipIndex") == []


class TestA10StaticSelectionStayRetired:
    """A10：静态选面链物理退役——runtime 映射为选面唯一真源；
    反查诊断面（scan_test_refs/counts）保留（自 test_low_fixes 迁移）。"""

    def test_testscan_trio_absent(self):
        from godot_qa_toolkit.mutation import testscan
        assert not hasattr(testscan, "derive_tests_glob")
        assert not hasattr(testscan, "_strength_glob")
        assert not hasattr(testscan, "_INDEX_CACHE")
        assert hasattr(testscan, "scan_test_refs")
        assert hasattr(testscan, "scan_test_ref_counts")


class TestA12IdentityHelperStayRetired:
    def test_test_func_pattern_absent_from_src_tree(self):
        assert _src_hits("_TEST_FUNC_PATTERN") == []


class TestA04CoverageStoreV1StayWriteFree:
    """A04：/1 store 无任何写入路径——schema 字面量仅存在于 coverlink
    的拒绝守卫（盘上残留条目拒绝），src 其他文件零引用。"""

    def test_v1_schema_only_in_reject_guard(self):
        hits = _src_hits("see1420-coverage/1", allow_file_substr="coverlink.py")
        assert hits == [], f"/1 schema 泄漏到守卫之外: {hits}"

    def test_no_v1_writer(self):
        """不存在以 see1420-coverage/1 落盘的写入：禁止 schema 赋值形态
        （`schema=SCHEMA_COVERAGE` / `"schema": SCHEMA_COVERAGE`）与
        常量的非定义赋值；比较与错误消息文本不在禁止之列。"""
        for py in sorted(_SRC.rglob("*.py")):
            text = py.read_text(encoding="utf-8")
            if "see1420-coverage/1" not in text:
                continue
            assert "coverlink.py" in str(py), py
            for no, line in enumerate(text.splitlines(), 1):
                assert '"schema": SCHEMA_COVERAGE,' not in line, \
                    f"{py}:{no}: v1 dict-literal write"
                assert "schema=SCHEMA_COVERAGE" not in line, \
                    f"{py}:{no}: v1 kwarg write"
                assert re.search(r"^\s*\w+\s*=\s*SCHEMA_COVERAGE\b", line) \
                    is None or "SCHEMA_COVERAGE =" in line, \
                    f"{py}:{no}: v1 constant reassignment"


class TestSinkRecoveryStayRetired:
    """sink-recovery 覆盖来源退役：src 不得存在以复原 sink 作为覆盖答案
    来源的路径（仅允许退役声明注释）。"""

    def test_only_retired_declaration_mentioned(self):
        hits = _src_hits("sink-recovery", allow_file_substr="runner.py")
        assert hits == [], f"sink-recovery 提及泄漏到声明之外: {hits}"

    def test_no_recovery_coverage_source(self):
        from godot_qa_toolkit.coverage import runner
        assert not hasattr(runner, "recover_sinks")
        assert not hasattr(runner, "sink_recovery_coverage")
