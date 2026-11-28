"""SEE-1631 C1-R1：invalid 标签真实性（§SPEC-018）、无效臂消费防护
（§SPEC-015）、feature 臂增量对齐 gut_full（§SPEC-016）的行为判据。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import attribution_fixtures


# --------------------------------------------------------------------------
# §SPEC-018：invalid_reason 反映真实首因
# --------------------------------------------------------------------------

def test_comparison_green_routes_to_intolerance_lane_and_valid():
    """§SPEC-017/018：对照绿 → 疑似插桩不耐受车道 + 声明草稿，臂保持
    valid；对账未执行如实标注（不得虚构 reconciliation_failed）。"""
    from godot_qa_toolkit.measurement import arms as arms_mod

    pair_script = ("res://gherkin/generated/.gqt-run-*/generated/"
                   "test_alpha.gd")
    result = {
        "state": "invalid", "invalid_reason": "new_failures",
        "invalid_evidence": {"all_causes": ["new_failures"]},
        "reconciliation": None,
        "arm_fail_pairs": {(pair_script, "test_s0")},
        "new_pairs": {(pair_script, "test_s0")},
        "test_failures": {"observed": ["test_s0"],
                          "observed_pairs": [f"{pair_script}::test_s0"],
                          "new": ["test_s0"],
                          "new_pairs": [f"{pair_script}::test_s0"],
                          "instrumentation_intolerant": []},
        "gut_ok": False,
    }
    orig = arms_mod._gherkin_uninstrumented_comparison
    arms_mod._gherkin_uninstrumented_comparison = (
        lambda root, failed, timeout_s: {
            "green": {"alpha"}, "red": set(), "no_evidence": set(),
            "executions": [{"key": "alpha", "rc": 0}]})
    try:
        arms_mod._apply_gherkin_comparison(result, Path("/p"), ["alpha"], 60)
    finally:
        arms_mod._gherkin_uninstrumented_comparison = orig
    assert result["state"] == "valid"
    assert result["invalid_reason"] is None
    tf = result["test_failures"]
    # 车道形态与 gut_full 一致（"s::t" 对字符串），dict 草稿在
    # suspected_intolerance_draft
    assert tf["instrumentation_intolerant"] and tf["new"] == []
    assert tf["instrumentation_intolerant"][0].endswith("::test_s0")
    draft = result["suspected_intolerance_draft"]
    assert draft[0]["script"].endswith("test_alpha.gd")
    assert draft[0]["name"] == "test_s0" and draft[0]["reason"]
    rec = result["reconciliation"]
    assert rec["executed"] is False and rec["ok"] is None


def test_comparison_red_absorbed_as_pre_existing_valid():
    """§SPEC-017：对照红 = 既有失败（具名吸收），臂保持 valid。"""
    from godot_qa_toolkit.measurement import arms as arms_mod

    pair_script = ("res://gherkin/generated/.gqt-run-*/generated/"
                   "test_alpha.gd")
    result = {
        "state": "invalid", "invalid_reason": "new_failures",
        "invalid_evidence": None, "reconciliation": None,
        "arm_fail_pairs": {(pair_script, "test_s0")},
        "new_pairs": {(pair_script, "test_s0")},
        "test_failures": {"observed": ["test_s0"],
                          "observed_pairs": [f"{pair_script}::test_s0"],
                          "new": ["test_s0"],
                          "new_pairs": [f"{pair_script}::test_s0"],
                          "instrumentation_intolerant": []},
        "gut_ok": False,
    }
    orig = arms_mod._gherkin_uninstrumented_comparison
    arms_mod._gherkin_uninstrumented_comparison = (
        lambda root, failed, timeout_s: {
            "green": set(), "red": {"alpha"}, "no_evidence": set(),
            "executions": [{"key": "alpha", "rc": 1, "pairs": 1}]})
    try:
        arms_mod._apply_gherkin_comparison(result, Path("/p"), ["alpha"], 60)
    finally:
        arms_mod._gherkin_uninstrumented_comparison = orig
    assert result["state"] == "valid"
    tf = result["test_failures"]
    assert tf["pre_existing_failures"] == [f"{pair_script}::test_s0"]
    assert tf["new"] == []
    assert tf["instrumentation_intolerant"] == []


def test_comparison_no_evidence_keeps_arm_invalid():
    """§SPEC-017：对照取不到证据 → 臂保持 invalid（new_failures），不
    静默转绿。"""
    from godot_qa_toolkit.measurement import arms as arms_mod

    pair_script = ("res://gherkin/generated/.gqt-run-*/generated/"
                   "test_alpha.gd")
    result = {
        "state": "invalid", "invalid_reason": "new_failures",
        "invalid_evidence": None, "reconciliation": None,
        "arm_fail_pairs": {(pair_script, "test_s0")},
        "new_pairs": {(pair_script, "test_s0")},
        "test_failures": {"observed": ["test_s0"],
                          "observed_pairs": [f"{pair_script}::test_s0"],
                          "new": ["test_s0"],
                          "new_pairs": [f"{pair_script}::test_s0"],
                          "instrumentation_intolerant": []},
        "gut_ok": False,
    }
    orig = arms_mod._gherkin_uninstrumented_comparison
    arms_mod._gherkin_uninstrumented_comparison = (
        lambda root, failed, timeout_s: {
            "green": set(), "red": set(), "no_evidence": {"alpha"},
            "executions": [{"key": "alpha", "rc": 3, "pairs": 0}]})
    try:
        arms_mod._apply_gherkin_comparison(result, Path("/p"), ["alpha"], 60)
    finally:
        arms_mod._gherkin_uninstrumented_comparison = orig
    assert result["state"] == "invalid"
    assert result["invalid_reason"] == "new_failures"
    assert result["test_failures"]["new"] == ["test_s0"]


def test_observed_failed_keys_scoped_to_groups():
    from godot_qa_toolkit.measurement.arms import _observed_failed_keys

    result = {"test_failures": {"observed_pairs": [
        "res://gherkin/generated/.gqt-run-*/generated/test_alpha.gd::t1",
        "res://gherkin/generated/.gqt-run-*/generated/test_beta.gd::t2"]}}
    assert _observed_failed_keys(
        result, [{"keys": ["alpha"]}]) == ["alpha"]


def test_reconciliation_only_failure_keeps_reconciliation_failed():
    """§SPEC-018 类 2：仅对账规则失败（无新增失败）→ reconciliation_failed
    且与 all_causes 一致。"""
    from godot_qa_toolkit.measurement.arms import _classify_invalid

    reason, ev = _classify_invalid(
        "gherkin", results=[], timed_out=False, run_error=True,
        new_pairs=set(), effective_baseline=set(), timeout_s=60,
        timeout_source="parameter", reconciliation_failed=True)
    assert reason == "reconciliation_failed"
    assert (ev.get("all_causes") or [None])[0] == "reconciliation_failed"


def test_launch_failure_first_cause_consistent():
    from godot_qa_toolkit.measurement.arms import _classify_invalid

    launch_rep = {"reps": [{"timed_out": False, "rc": 3,
                            "out": "attempt to open script"}],
                  "plan": {"keys": ["k1"]}}
    reason, ev = _classify_invalid(
        "gherkin", results=[launch_rep], timed_out=False, run_error=True,
        new_pairs=set(), effective_baseline=set(), timeout_s=60,
        timeout_source="parameter", reconciliation_failed=False)
    assert reason == "launch_failure"
    assert (ev.get("all_causes") or [None])[0] == "launch_failure"


def test_timeout_first_cause_consistent():
    from godot_qa_toolkit.measurement.arms import _classify_invalid

    to_rep = {"reps": [{"timed_out": True, "rc": None, "out": ""}],
              "plan": {"keys": ["k1"]}}
    reason, ev = _classify_invalid(
        "gherkin", results=[to_rep], timed_out=True, run_error=True,
        new_pairs=set(), effective_baseline=set(), timeout_s=60,
        timeout_source="parameter", reconciliation_failed=False)
    assert reason == "shard_timeout"
    assert (ev.get("all_causes") or [None])[0] == "shard_timeout"


# --------------------------------------------------------------------------
# §SPEC-015：无效 gherkin 臂的边不得被消费面当命中披露
# --------------------------------------------------------------------------

def _doc_with_gherkin_state(tmp_path: Path, state: str, invalid_reason=None):
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import install_recipe, line_facts_map

    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_a.gd").write_text(
        "extends GutTest\nfunc test_x() -> void:\n\tpass\n",
        encoding="utf-8")
    (tmp_path / "prod.gd").write_text(
        "extends Node\nfunc a() -> void:\n\tpass\n", encoding="utf-8")
    install_recipe(tmp_path)
    gut_facts = {"test_a.gd::test_x": {"prod.gd": [5]}}
    gher_facts = {"gherkin://feature/alpha::s0_01": {"prod.gd": [7]}}
    map_file = line_facts_map(tmp_path, gut_facts,
                              instrumentable={"prod.gd": [5, 6, 7]})
    doc = json.loads(Path(map_file).read_text(encoding="utf-8"))
    doc["arms"]["gherkin"] = {
        "test_to_lines": gher_facts, "script_to_lines": {},
        "line_to_tests": {"prod.gd": {"7": ["gherkin://feature/alpha::s0_01"]}},
        "feature_to_lines": {"alpha": {"prod.gd": [7]}},
        "line_to_features": {"prod.gd": {"7": ["alpha"]}},
        "gherkin_scenarios": {"gherkin://feature/alpha::s0_01": {
            "feature_key": "alpha", "scenario": "s", "scenario_line": 3,
            "test_name": "s0_01"}},
        "gherkin_unmapped_identities": 0}
    doc["arms_state"]["gherkin"] = {
        "state": state, "invalid_reason": invalid_reason,
        "bundles": 1, "pairs": 1, "probes_fired": 9, "vacuous": False}
    Path(map_file).write_text(json.dumps(doc), encoding="utf-8")
    return map_file


def test_invalid_gherkin_arm_disclosure_unavailable(tmp_path):
    from godot_qa_toolkit.mutation.truth_select import resolve_selection

    _doc_with_gherkin_state(tmp_path, "invalid",
                            invalid_reason="new_failures")
    sel = resolve_selection(str(tmp_path), str(tmp_path / "prod.gd"),
                            renew=False)
    g = sel.get("gherkin")
    assert g is not None and g.get("unavailable") is True
    assert g.get("state") == "invalid"
    assert g.get("invalid_reason") == "new_failures"
    assert not g.get("lines") and not g.get("feature_keys")


def test_valid_gherkin_arm_disclosure_unchanged(tmp_path):
    from godot_qa_toolkit.mutation.truth_select import resolve_selection

    _doc_with_gherkin_state(tmp_path, "valid")
    sel = resolve_selection(str(tmp_path), str(tmp_path / "prod.gd"),
                            renew=False)
    g = sel.get("gherkin") or {}
    assert g.get("unavailable") is None or g.get("unavailable") is False
    assert g.get("lines") == [7]
    assert "alpha" in (g.get("feature_keys") or [])


def test_invalid_gherkin_arm_projection_disclosure_unavailable(tmp_path):
    from godot_qa_toolkit.mutation.truth_select import load_map
    from godot_qa_toolkit.coverage.mapping import (
        _gherkin_projection_disclosure,
    )

    _doc_with_gherkin_state(tmp_path, "invalid",
                            invalid_reason="new_failures")
    doc = load_map(str(tmp_path / ".qa-cache" / "attribution-map.json"))
    d = _gherkin_projection_disclosure(doc, "prod.gd", covered={5})
    assert d is not None and d.get("unavailable") is True
    assert d.get("invalid_reason") == "new_failures"
    assert not d.get("gherkin_only_lines")
    # valid 恢复
    doc["arms_state"]["gherkin"]["state"] = "valid"
    d2 = _gherkin_projection_disclosure(doc, "prod.gd", covered={5})
    assert d2.get("unavailable") is None or d2.get("unavailable") is False
    assert d2.get("gherkin_only_lines") == [7]


def test_increment_carries_invalid_state_without_whitewash(tmp_path):
    """§SPEC-015：增量继承 invalid 状态不被洗白。"""
    from godot_qa_toolkit.measurement.increment import (
        _build_increment_artifact,
    )
    from godot_qa_toolkit.measurement.attribution import ATTRIBUTION_SCHEMA

    baseline = {
        "schema_version": ATTRIBUTION_SCHEMA,
        "report_recipe_hash": "rh", "recipe_hash_pristine": "pr",
        "project_root": str(tmp_path), "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": {"prod.gd": "x"}, "target_files": ["prod.gd"],
        "tests_face": {}, "instrumentable_lines": {"prod.gd": [5]},
        "arms": {"gut_full": {"test_to_files": {}, "script_to_files": {},
                              "tests": 0, "scripts": 0, "files": 0,
                              "pairs": 0, "function_pairs": 0,
                              "test_to_functions": {},
                              "script_to_functions": {},
                              "file_to_tests": {}, "file_to_scripts": {},
                              "line_pairs": 0, "script_line_pairs": 0,
                              "test_to_lines": {}, "script_to_lines": {},
                              "line_to_tests": {}},
                 "gherkin": {"test_to_lines": {
                     "gherkin://feature/alpha::s0_01": {"prod.gd": [7]}},
                     "script_to_lines": {},
                     "feature_to_lines": {"alpha": {"prod.gd": [7]}},
                     "line_to_features": {"prod.gd": {"7": ["alpha"]}},
                     "gherkin_scenarios": {}, "gherkin_unmapped_identities":
                     0}},
        "arms_state": {
            "gut_full": {"state": "valid", "bundles": 1, "pairs": 0,
                         "probes_fired": 5, "vacuous": False},
            "gherkin": {"state": "invalid", "invalid_reason": "new_failures",
                        "bundles": 1, "pairs": 1, "probes_fired": 9,
                        "vacuous": False}},
        "notes": [],
    }
    artifact = _build_increment_artifact(
        baseline, dict(baseline["arms"]["gut_full"]),
        {"recipe_hash": "rh"}, tmp_path,
        {"prod_changed": [], "prod_new": [], "prod_removed": [],
         "test_changed": [], "test_new": [], "test_removed": [],
         "feature_changed": [], "feature_new": [], "feature_removed": [],
         "unknown": []},
        [], {"rerun": set(), "dropped": set(), "feature_rerun": set(),
             "sources": {}},
        0, 1.0, [])
    assert artifact["arms_state"]["gherkin"]["state"] == "invalid"
    assert artifact["arms_state"]["gherkin"]["invalid_reason"] == \
        "new_failures"
    assert artifact["arms"]["gherkin"]["feature_to_lines"] == {
        "alpha": {"prod.gd": [7]}}


# --------------------------------------------------------------------------
# §SPEC-016：feature 粒度增量（对齐 gut_full）
# --------------------------------------------------------------------------

def _baseline_with_gherkin(tmp_path: Path) -> dict:
    from godot_qa_toolkit.measurement.attribution import ATTRIBUTION_SCHEMA
    from attribution_fixtures import install_recipe
    from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash

    install_recipe(tmp_path)
    recipe, error = build_recipe(tmp_path)
    raw = recipe_hash({k: v for k, v in recipe.items() if k != "recipe_hash"})
    pristine = attribution_fixtures.pristine_recipe_hash(tmp_path)
    base = _doc_with_gherkin_state.__wrapped__ if False else None
    ident_a = "gherkin://feature/alpha::s0_01"
    ident_b = "gherkin://feature/beta::s0_01"
    return {
        "schema_version": ATTRIBUTION_SCHEMA,
        "report_recipe_hash": raw, "recipe_hash_pristine": pristine,
        "project_root": str(tmp_path), "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": {"alpha_only.gd": "x", "shared.gd": "y"},
        "target_files": ["alpha_only.gd", "shared.gd"],
        "tests_face": {}, "instrumentable_lines": {},
        "arms": {"gut_full": {"test_to_files": {}, "script_to_files": {},
                              "tests": 0, "scripts": 0, "files": 0,
                              "pairs": 0, "function_pairs": 0,
                              "test_to_functions": {},
                              "script_to_functions": {},
                              "file_to_tests": {}, "file_to_scripts": {},
                              "line_pairs": 0, "script_line_pairs": 0,
                              "test_to_lines": {}, "script_to_lines": {},
                              "line_to_tests": {}},
                 "gherkin": {
                     "test_to_lines": {ident_a: {"alpha_only.gd": [7]},
                                       ident_b: {"shared.gd": [9]}},
                     "script_to_lines": {},
                     "feature_to_lines": {"alpha": {"alpha_only.gd": [7]},
                                          "beta": {"shared.gd": [9]}},
                     "line_to_features": {"alpha_only.gd": {"7": ["alpha"]},
                                          "shared.gd": {"9": ["beta"]}},
                     "gherkin_scenarios": {ident_a: {"feature_key": "alpha"},
                                           ident_b: {"feature_key": "beta"}},
                     "gherkin_unmapped_identities": 0}},
        "arms_state": {
            "gut_full": {"state": "valid", "bundles": 1, "pairs": 0,
                         "probes_fired": 5, "vacuous": False},
            "gherkin": {"state": "valid", "bundles": 1, "pairs": 2,
                        "probes_fired": 9, "vacuous": False}},
        "notes": [],
    }


def test_feature_rerun_does_not_force_full_fallback(tmp_path):
    """Owner 裁决：feature 失效面走增量重跑，不再一刀切整臂回退。"""
    from godot_qa_toolkit.measurement.increment import _fallback_reason

    classes = {"prod_new": [], "feature_new": []}
    assert _fallback_reason(True, classes, 0, 5, 0.0, 0.05,
                            feature_rerun={"alpha"},
                            gherkin_known=40) == "incremental"


def test_gherkin_ratio_cap_named_fallback(tmp_path):
    from godot_qa_toolkit.measurement.increment import _fallback_reason

    classes = {"prod_new": [], "feature_new": []}
    assert _fallback_reason(True, classes, 0, 5, 0.0, 0.05,
                            feature_rerun={"a", "b", "c"},
                            gherkin_known=40) == \
        "gherkin_rerun_surface_too_large_fallback"
    assert _fallback_reason(True, classes, 0, 5, 0.0, 0.05,
                            feature_rerun={"a"},
                            gherkin_known=40) == "incremental"


def test_feature_new_reruns_incrementally(tmp_path):
    """新增 feature → 重跑该 feature（并入失效面），不再整臂回退。"""
    from godot_qa_toolkit.measurement.increment import (
        _gherkin_invalidation_surface,
    )

    classes = {"feature_changed": [], "feature_new":
               ["addons/gqt_gherkin/features/gamma.feature"],
               "prod_changed": [], "prod_removed": []}
    baseline = _baseline_with_gherkin(tmp_path)
    surf = _gherkin_invalidation_surface(baseline, classes)
    assert "gamma" in surf


def test_merge_gherkin_arm_replaces_rerun_keys_keeps_others():
    from godot_qa_toolkit.measurement.increment import _merge_gherkin_arm

    baseline = {"test_to_lines": {
        "gherkin://feature/alpha::s0_01": {"alpha_only.gd": [7]},
        "gherkin://feature/beta::s0_01": {"shared.gd": [9]}},
        "script_to_lines": {},
        "feature_to_lines": {"alpha": {"alpha_only.gd": [7]},
                             "beta": {"shared.gd": [9]}},
        "line_to_features": {"alpha_only.gd": {"7": ["alpha"]},
                             "shared.gd": {"9": ["beta"]}},
        "gherkin_scenarios": {
            "gherkin://feature/alpha::s0_01": {"feature_key": "alpha"},
            "gherkin://feature/beta::s0_01": {"feature_key": "beta"}},
        "gherkin_unmapped_identities": 0}
    fresh = {"test_to_lines": {
        "gherkin://feature/alpha::s0_01": {"alpha_only.gd": [7, 8]}},
        "script_to_lines": {},
        "feature_to_lines": {"alpha": {"alpha_only.gd": [7, 8]}},
        "line_to_features": {"alpha_only.gd": {"7": ["alpha"],
                                               "8": ["alpha"]}},
        "gherkin_scenarios": {
            "gherkin://feature/alpha::s0_01": {"feature_key": "alpha"}},
        "gherkin_unmapped_identities": 0}
    merged = _merge_gherkin_arm(baseline, fresh,
                                rerun_keys={"alpha"},
                                dropped_keys=set(), removed_rels=set())
    # alpha 重绑为 fresh 行集；beta 原样继承（逐位不变）
    assert merged["test_to_lines"]["gherkin://feature/alpha::s0_01"] == \
        {"alpha_only.gd": [7, 8]}
    assert merged["test_to_lines"]["gherkin://feature/beta::s0_01"] == \
        {"shared.gd": [9]}
    assert merged["feature_to_lines"]["beta"] == {"shared.gd": [9]}
    assert merged["line_to_features"]["alpha_only.gd"]["8"] == ["alpha"]
    assert merged["gherkin_scenarios"][
        "gherkin://feature/beta::s0_01"] == {"feature_key": "beta"}
    assert merged["pairs"] >= 2 and merged["line_pairs"] == 3


def test_merge_gherkin_arm_drops_removed_features_and_rels():
    from godot_qa_toolkit.measurement.increment import _merge_gherkin_arm

    baseline = {"test_to_lines": {
        "gherkin://feature/alpha::s0_01": {"alpha_only.gd": [7],
                                          "deleted.gd": [3]},
        "gherkin://feature/beta::s0_01": {"deleted.gd": [5]}},
        "script_to_lines": {},
        "feature_to_lines": {"alpha": {"alpha_only.gd": [7],
                                       "deleted.gd": [3]},
                             "beta": {"deleted.gd": [5]}},
        "line_to_features": {"alpha_only.gd": {"7": ["alpha"]},
                             "deleted.gd": {"3": ["alpha"], "5": ["beta"]}},
        "gherkin_scenarios": {
            "gherkin://feature/alpha::s0_01": {"feature_key": "alpha"},
            "gherkin://feature/beta::s0_01": {"feature_key": "beta"}},
        "gherkin_unmapped_identities": 0}
    merged = _merge_gherkin_arm(baseline, None, rerun_keys=set(),
                                dropped_keys={"beta"},
                                removed_rels={"deleted.gd"})
    assert "deleted.gd" not in merged["test_to_lines"][
        "gherkin://feature/alpha::s0_01"]
    assert "beta" not in merged["feature_to_lines"]
    assert merged["gherkin_scenarios"].get(
        "gherkin://feature/beta::s0_01") is None
