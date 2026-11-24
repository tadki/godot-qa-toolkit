"""SEE-1631 C1：gherkin 归属的消费面与增量语义判据。

§SPEC-004/005：mutation 选面披露 gherkin 命中与消费方式；coverage 投影
在含 gherkin 边的 map 上行集/百分比 parity；增量续新——纯测试脚本变更
时 gherkin 边原样保留（身份稳定、不静默清空），生产/feature 变更触发
具名全脸回退（重绑、不交付半成品）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from attribution_fixtures import install_recipe, line_facts_map


# --------------------------------------------------------------------------
# §SPEC-005：选面/投影消费含 gherkin 边的 map——parity + 具名披露
# --------------------------------------------------------------------------

def _seed_project(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_a.gd").write_text(
        "extends GutTest\nfunc test_x() -> void:\n\tpass\n",
        encoding="utf-8")
    install_recipe(tmp_path)


def _doc_with_gherkin(tmp_path: Path, target_rel: str):
    _seed_project(tmp_path)
    # gut 臂身份按消费面契约（脚本名::测试名，见 attribution_fixtures.
    # write_map）；gherkin 臂为规范 gherkin:// 身份（仅披露面消费）。
    gut_facts = {"test_a.gd::test_x": {target_rel: [5]}}
    gher_facts = {"gherkin://feature/alpha::s0_01": {target_rel: [7]}}
    map_file = line_facts_map(tmp_path, gut_facts, instrumentable={
        target_rel: [5, 6, 7, 8]})
    doc = json.loads(Path(map_file).read_text(encoding="utf-8"))
    garm = {"arm": "gherkin", "tests": 1, "scripts": 0, "files": 1,
            "pairs": 1, "script_pairs": 0, "function_pairs": 0,
            "line_pairs": 1, "script_line_pairs": 0,
            "test_to_files": {"gherkin://feature/alpha::s0_01":
                              [target_rel]},
            "script_to_files": {}, "test_to_functions": {},
            "script_to_functions": {},
            "file_to_tests": {target_rel: ["gherkin://feature/alpha::s0_01"]},
            "file_to_scripts": {},
            "test_to_lines": gher_facts, "script_to_lines": {},
            "line_to_tests": {target_rel: {"7":
                                           ["gherkin://feature/alpha::s0_01"]}},
            "feature_to_lines": {"alpha": {target_rel: [7]}},
            "line_to_features": {target_rel: {"7": ["alpha"]}},
            "gherkin_scenarios": {"gherkin://feature/alpha::s0_01": {
                "feature_key": "alpha", "scenario": "s",
                "scenario_line": 3, "test_name": "s0_01"}},
            "gherkin_unmapped_identities": 0}
    doc["arms"]["gherkin"] = garm
    doc["arms_state"]["gherkin"] = {"state": "valid", "invalid_reason": None,
                                    "bundles": 1, "pairs": 1,
                                    "probes_fired": 9, "vacuous": False}
    Path(map_file).write_text(json.dumps(doc), encoding="utf-8")
    return map_file, doc


def test_projection_parity_with_gherkin_edges(tmp_path):
    """投影行集/百分比与单一真源（gut_full 投影）parity——gherkin 边在场
    不得改变覆盖答案。"""
    from godot_qa_toolkit.coverage.mapping import project_file_coverage

    target = tmp_path / "prod.gd"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("extends Node\nfunc a() -> void:\n\tpass\n",
                      encoding="utf-8")
    rel = "prod.gd"
    map_file, doc = _doc_with_gherkin(tmp_path, rel)
    out = project_file_coverage(str(tmp_path), str(target))
    # 行集 parity：仅 gut_full 车道进入 covered（行 5），gherkin 行 7
    # 披露但不并入
    assert 5 in out["covered_lines"]
    assert 7 not in out["covered_lines"]
    assert out["coverage_percent"] == round(100.0 * 1 / 4, 2)


def test_projection_discloses_gherkin_only_lines(tmp_path):
    from godot_qa_toolkit.coverage.mapping import project_file_coverage

    target = tmp_path / "prod.gd"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("extends Node\nfunc a() -> void:\n\tpass\n",
                      encoding="utf-8")
    rel = "prod.gd"
    _map_file, _doc = _doc_with_gherkin(tmp_path, rel)
    out = project_file_coverage(str(tmp_path), str(target))
    g = out.get("gherkin") or {}
    assert sorted(g.get("gherkin_only_lines") or []) == [7]
    assert g.get("covered_lines") == [7]
    assert "disclosed" in str(g.get("consumption"))


def test_resolve_selection_discloses_gherkin_overlap(tmp_path):
    from godot_qa_toolkit.mutation.truth_select import resolve_selection

    target = tmp_path / "prod.gd"
    target.write_text("extends Node\nfunc a() -> void:\n\tpass\n",
                      encoding="utf-8")
    rel = "prod.gd"
    _map_file, _doc = _doc_with_gherkin(tmp_path, rel)
    sel = resolve_selection(str(tmp_path), str(target), renew=False)
    g = sel.get("gherkin")
    assert g is not None
    assert "alpha" in (g.get("feature_keys") or [])
    assert 7 in (g.get("lines") or [])
    assert "gut" in str(g.get("consumption")).lower()


def test_resolve_selection_no_gherkin_block_when_absent(tmp_path):
    """无 gherkin 臂（旧形态/暂停产物）→ 披露块缺席（形状零变化）。"""
    from godot_qa_toolkit.mutation.truth_select import resolve_selection

    target = tmp_path / "prod.gd"
    target.write_text("extends Node\nfunc a() -> void:\n\tpass\n",
                      encoding="utf-8")
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_a.gd").write_text(
        "extends GutTest\nfunc test_x() -> void:\n\tpass\n",
        encoding="utf-8")
    install_recipe(tmp_path)
    line_facts_map(tmp_path, {"res://tests/test_a.gd::test_x":
                              {"prod.gd": [5]}},
                   instrumentable={"prod.gd": [5, 6]})
    sel = resolve_selection(str(tmp_path), str(target), renew=False)
    assert sel.get("gherkin") is None


# --------------------------------------------------------------------------
# §SPEC-004：增量——feature 分类、失效面、具名回退、gherkin 边不静默清空
# --------------------------------------------------------------------------

def _baseline_doc(tmp_path: Path) -> dict:
    from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash
    from godot_qa_toolkit.measurement.attribution import ATTRIBUTION_SCHEMA

    install_recipe(tmp_path)
    recipe, error = build_recipe(tmp_path)
    assert error is None
    raw = recipe_hash({k: v for k, v in recipe.items() if k != "recipe_hash"})
    gher_ident = "gherkin://feature/alpha::s0_01"
    return {
        "schema_version": ATTRIBUTION_SCHEMA,
        "report_recipe_hash": raw, "recipe_hash_pristine": raw,
        "project_root": str(tmp_path),
        "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": {"prod.gd": "x"},
        "target_files": ["prod.gd"],
        "tests_face": {}, "instrumentable_lines": {"prod.gd": [5]},
        "arms": {
            "gut_full": {"test_to_files": {}, "script_to_files": {},
                         "tests": 0, "scripts": 0, "files": 0, "pairs": 0,
                         "function_pairs": 0, "test_to_functions": {},
                         "script_to_functions": {}, "file_to_tests": {},
                         "file_to_scripts": {}, "line_pairs": 0,
                         "script_line_pairs": 0, "test_to_lines": {},
                         "script_to_lines": {}, "line_to_tests": {}},
            "gherkin": {
                "test_to_lines": {gher_ident: {"prod.gd": [7]}},
                "script_to_lines": {},
                "feature_to_lines": {"alpha": {"prod.gd": [7]}},
                "line_to_features": {"prod.gd": {"7": ["alpha"]}},
                "gherkin_scenarios": {gher_ident: {
                    "feature_key": "alpha", "scenario": "s",
                    "scenario_line": 3, "test_name": "s0_01"}},
                "gherkin_unmapped_identities": 0},
        },
        "arms_state": {
            "gut_full": {"state": "valid", "bundles": 1, "pairs": 0,
                         "probes_fired": 5, "vacuous": False},
            "gherkin": {"state": "valid", "bundles": 1, "pairs": 1,
                        "probes_fired": 9, "vacuous": False}},
        "notes": [],
    }


def test_classify_feature_change_known_key(tmp_path):
    from godot_qa_toolkit.measurement.increment import classify_changes

    feat = tmp_path / "addons" / "gqt_gherkin" / "features" / "alpha.feature"
    feat.parent.mkdir(parents=True, exist_ok=True)
    feat.write_text("Feature: alpha\n", encoding="utf-8")
    baseline = _baseline_doc(tmp_path)
    out = classify_changes(tmp_path, [str(feat)], baseline)
    assert out["feature_changed"] == ["addons/gqt_gherkin/features/"
                                      "alpha.feature"]
    assert out["unknown"] == []


def test_classify_feature_new_key(tmp_path):
    from godot_qa_toolkit.measurement.increment import classify_changes

    feat = tmp_path / "addons" / "gqt_gherkin" / "features" / "beta.feature"
    feat.parent.mkdir(parents=True, exist_ok=True)
    feat.write_text("Feature: beta\n", encoding="utf-8")
    baseline = _baseline_doc(tmp_path)
    out = classify_changes(tmp_path, [str(feat)], baseline)
    assert out["feature_new"] and not out["unknown"]


def test_classify_feature_removed(tmp_path):
    from godot_qa_toolkit.measurement.increment import classify_changes

    baseline = _baseline_doc(tmp_path)
    out = classify_changes(
        tmp_path, ["addons/gqt_gherkin/features/alpha.feature"], baseline)
    assert out["feature_removed"] == ["addons/gqt_gherkin/features/"
                                      "alpha.feature"]


def test_invalidation_reruns_gherkin_touching_features(tmp_path):
    from godot_qa_toolkit.measurement.increment import (
        classify_changes,
        compute_invalidation,
    )

    baseline = _baseline_doc(tmp_path)
    # prod.gd 必须可分类为 prod 变更：给它基线 gut 边（file_to_tests）
    baseline["arms"]["gut_full"]["file_to_tests"] = {"prod.gd": []}
    (tmp_path / "prod.gd").write_text("extends Node\n", encoding="utf-8")
    classes = classify_changes(tmp_path, ["prod.gd"], baseline)
    assert classes["prod_changed"] == ["prod.gd"]
    inv = compute_invalidation(tmp_path, baseline, classes, None)
    assert "alpha" in inv["feature_rerun"]


def test_feature_invalidation_forces_named_full_fallback(tmp_path):
    from godot_qa_toolkit.measurement.increment import _fallback_reason

    assert _fallback_reason(
        True, {"prod_new": [], "feature_new": []}, 0, 5, 0.0, 0.05,
        feature_rerun={"alpha"}) == "gherkin_invalidation_fallback"
    assert _fallback_reason(
        True, {"prod_new": [], "feature_new": ["beta"]}, 0, 5, 0.0, 0.05,
        feature_rerun=set()) == "new_feature_fallback"
    assert _fallback_reason(
        True, {"prod_new": [], "feature_new": []}, 0, 5, 0.0, 0.05,
        feature_rerun=set()) == "incremental"


def test_increment_artifact_carries_gherkin_arm(tmp_path):
    """纯测试脚本变更（无生产/feature 变更）：gherkin 臂原样保留进产物，
    不因增量被静默清空（§SPEC-004）。"""
    from godot_qa_toolkit.measurement.increment import (
        _build_increment_artifact,
    )

    baseline = _baseline_doc(tmp_path)
    merged = dict(baseline["arms"]["gut_full"])
    artifact = _build_increment_artifact(
        baseline, merged, {"recipe_hash": baseline["report_recipe_hash"]},
        tmp_path, {"prod_changed": [], "prod_new": [], "prod_removed": [],
                   "test_changed": ["tests/test_a.gd"], "test_new": [],
                   "test_removed": [], "feature_changed": [], "feature_new":
                   [], "feature_removed": [], "unknown": []},
        ["test_a"], {"rerun": {"test_a"}, "dropped": set(),
                     "feature_rerun": set(), "sources": {}},
        0, 1.0, [])
    arms = artifact.get("arms") or {}
    assert "gut_full" in arms
    assert (arms.get("gherkin") or {}).get("feature_to_lines") == \
        {"alpha": {"prod.gd": [7]}}
    assert artifact["arms_state"]["gherkin"]["pairs"] == 1
