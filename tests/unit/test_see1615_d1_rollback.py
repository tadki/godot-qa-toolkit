"""SEE-1615 §SPEC-D1 — D1 闭包阶梯回退后的严格 per-test 选面契约。

正反例固定新语义：unattributed_files 不再改变选面、稀疏行不再 fn/file
扩面；tests_allow 不含 scope 键（summary.test_filter.scope 由 runner
默认值恒 "line"）；闭包入口（registry 参数）不存在。任何一侧重新引入
闭包阶梯都会使本文件失败。
"""

from __future__ import annotations

import inspect

from godot_qa_toolkit.mutation import truth_select as _ts  # noqa: F401 — 先导破环
from godot_qa_toolkit.mutation.truth_lines import line_identity_sets


def _doc(**kw):
    base = {"line_to_tests": {"src/x.gd": {
                "10": ["test_a.gd::t1", "test_b.gd::t2"],
                "12": ["test_a.gd::t1"]}},
            "script_to_lines": {"test_c.gd": {"src/x.gd": [10]}},
            "test_to_functions": {},
            "test_to_lines": {"test_a.gd::t1": {"src/x.gd": [10]},
                              "test_b.gd::t2": {"src/x.gd": [10]}},
            "unattributed_files": []}
    base.update(kw)
    return {"arms": {"gut_full": base}}


# ---------------------------------------------------------- 契约（回退形态）


def test_closure_ladder_entry_point_absent():
    """D1 阶梯入口消失：line_identity_sets 不再接受 registry 参数。"""
    assert "registry" not in inspect.signature(line_identity_sets).parameters


# ---------------------------------------------------------- 正例（严格行级）


def test_attributed_lines_get_exact_identities():
    """有行事实的行：行级身份精确（dense 行为唯一来源），script 车道并行。"""
    idents, lane = line_identity_sets(_doc(), "src/x.gd", {10, 12})
    assert idents == {10: ["test_a.gd::t1", "test_b.gd::t2"],
                      12: ["test_a.gd::t1"]}
    assert lane == {"test_c.gd"}


# ---------------------------------------------------------- 反例（不再扩面）


def test_unattributed_listing_leaves_selection_untouched():
    """unattributed_files 命中 rel：选面不变——稀疏行无身份、无文件面。"""
    doc = _doc(unattributed_files=["src/x.gd"])
    idents, lane = line_identity_sets(doc, "src/x.gd", {99})
    assert 99 not in idents
    assert lane == set()


def test_missing_list_key_behaves_like_strict_line_semantics():
    """名单键缺失（legacy/增量产物）与在册空名单同语义：行级照常。"""
    doc = _doc()
    del doc["arms"]["gut_full"]["unattributed_files"]
    idents, lane = line_identity_sets(doc, "src/x.gd", {10})
    assert idents == {10: ["test_a.gd::t1", "test_b.gd::t2"]}
    assert lane == {"test_c.gd"}


def test_fn_edges_do_not_widen_sparse_lines():
    """test_to_functions 在册也不得为稀疏行扩面（fn 级回退已移除）。"""
    doc = _doc(test_to_functions={"test_fn.gd::t9": ["src/x.gd::work"]})
    idents, lane = line_identity_sets(doc, "src/x.gd", {77, 78})
    assert 77 not in idents and 78 not in idents
    assert lane == set()


def test_file_face_data_does_not_fill_sparse_lines():
    """文件面数据在场（test_to_lines 覆盖稀疏行）也不得补行身份。"""
    doc = _doc(test_to_lines={
        "test_a.gd::t1": {"src/x.gd": [10, 77]},
        "test_b.gd::t2": {"src/x.gd": [10]}})
    idents, _lane = line_identity_sets(doc, "src/x.gd", {77})
    assert 77 not in idents


# ------------------------------------------------- 管线级契约（resolve 侧）


def test_resolve_tests_allow_strict_line_contract(tmp_path):
    """resolve_line_group_selection：tests_allow 恰为 identities/script_lane
    两键（无 scope）；稀疏行只进 no_mapped_tests 具名披露。"""
    from test_see1610_impl_test_filter import (
        _fixture_project, _write_line_map_1610)
    from godot_qa_toolkit.mutation import truth_select

    proj, target = _fixture_project(tmp_path)
    _write_line_map_1610(proj, target)
    sel = truth_select.resolve_line_group_selection(
        proj, str(target), [4, 7, 10, 5])
    ta = sel["tests_allow"]
    assert set(ta) == {"identities", "script_lane"}
    assert set(ta["identities"]) == {"test_a.gd::t1", "test_b.gd::t2"}
    assert ta["script_lane"] == ["test_b.gd"]
    assert 5 in sel["no_mapped_tests"]
