"""SEE-1623 M9 — 空选择面静默 0.0% 哨兵行为钉。

根因（SEE-1622 Refacty F1，定向复核翻转确认）：干净绿图下 `measure
coverage --tests <不存在目录>` 返回 exit 0 / ok=true / 0.0% /
selection_scripts=0 且无警告——与「真实零覆盖」不可区分，权威外观假象。

行为契约（filter_selection_face 单一出处）：显式 tests glob 过滤映射
选择后 0 脚本 → 具名 CoverageMappingError（→ 既有 JSON 失败信封，
exit 1），不出分；glob 命中脚本但覆盖 0 行 = 真实零覆盖（语义不变）。
"""

from __future__ import annotations

import pytest

from godot_qa_toolkit.coverage.mapping import (
    CoverageMappingError,
    filter_selection_face,
)


class TestEmptySelectionFaceSentinel:
    def test_nonexistent_dir_glob_raises_named_sentinel(self):
        with pytest.raises(CoverageMappingError) as ei:
            filter_selection_face(
                ["res://tests/unit/test_calc.gd"], "res://tests/nosuch")
        assert "empty selection face" in str(ei.value)
        assert "res://tests/nosuch" in str(ei.value)

    def test_file_form_glob_no_match_raises(self):
        with pytest.raises(CoverageMappingError):
            filter_selection_face(
                ["res://tests/unit/test_calc.gd"], "res://tests/nosuch.gd")

    def test_matching_dir_keeps_face_scripts(self):
        out = filter_selection_face(
            ["res://tests/unit/test_calc.gd",
             "res://tests/other/test_x.gd"], "res://tests/unit")
        assert out == ["res://tests/unit/test_calc.gd"]

    def test_exact_file_match_kept(self):
        out = filter_selection_face(
            ["res://tests/unit/test_calc.gd"],
            "res://tests/unit/test_calc.gd")
        assert out == ["res://tests/unit/test_calc.gd"]

    def test_prefix_boundary_not_substring(self):
        """边界匹配：`res://tests/unit2/` 不得被 `res://tests/unit` 前缀误吸。"""
        with pytest.raises(CoverageMappingError):
            filter_selection_face(
                ["res://tests/unit2/test_x.gd"], "res://tests/unit")
