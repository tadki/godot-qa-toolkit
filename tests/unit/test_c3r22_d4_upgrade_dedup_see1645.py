"""SEE-1645 C3-R22 D4：升级过渡的增量合并按规范身份去重。

f09d5f0 旧基线（basename 身份形态）在 72dca1c 钩子（res:// 完整路径
身份形态）上做首次增量：重跑脚本的旧形态表键与新形态表键并存（QA-R2
实测判定表 3097 → 3120，同物理测试两种身份并存）——违反 §SPEC-025
「重跑重算（应替换）」的守恒语义。

- 同物理测试（basename::name 相同）→ 只保留规范（res:// 完整路径）
  条目，旧 basename 条目剔除并具名披露；
- 两个目录的同名测试仍各自保留（D2 不回退——完整路径身份互异）；
- 无法恢复规范路径的旧条目原样保留并具名披露（不静默丢证据）。
"""

from __future__ import annotations

import json
from pathlib import Path

from godot_qa_toolkit.measurement.increment import (
    _merge_arm, _merged_test_verdicts,
)

REL = "tests/unit/vendor/test_pricing_resolver.gd"
FULL_IDENT = "res://tests/unit/vendor/test_pricing_resolver.gd::fn"
BASE_IDENT = "test_pricing_resolver.gd::fn"
SCRIPT_BASE = "test_pricing_resolver.gd"


def _baseline_doc() -> dict:
    return {
        "schema_version": "test-attribution/7",
        "report_recipe_hash": "same-env-hash",
        "recipe_hash_pristine": "pristine",
        "project_root": "/proj",
        "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": {},
        "target_files": [],
        "tests_face": {REL: "sha"},
        "production_content_sha256": {},
        "arms": {"gut_full": {
            "test_to_lines": {BASE_IDENT: {"src/game.gd": [4, 5]}},
            "script_to_lines": {SCRIPT_BASE: {"src/game.gd": [4]}},
            "test_to_files": {BASE_IDENT: ["src/game.gd"]},
            "script_to_files": {SCRIPT_BASE: ["src/game.gd"]}}},
        "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                    "pairs": 1, "probes_fired": 2,
                                    "vacuous": False}},
        "test_verdicts": {BASE_IDENT: {"verdict": "red",
                                       "classification": "pre_existing",
                                       "script": f"res://{REL}"}},
        "notes": [],
    }


def _fresh_arm() -> dict:
    return {
        "test_to_files": {FULL_IDENT: ["src/game.gd"]},
        "script_to_files": {"res://" + REL: ["src/game.gd"]},
        "test_to_lines": {FULL_IDENT: {"src/game.gd": {4, 5}}},
        "script_to_lines": {"res://" + REL: {"src/game.gd": {4}}},
    }


def test_merge_arm_script_lane_deduped_to_canonical():
    """arm 合并：script 车道旧 basename 键被新 res:// 全路径键替换，
    不再并存（72dca1c 上两种形态并存 → 红）。"""
    merged = _merge_arm(_baseline_doc()["arms"]["gut_full"], _fresh_arm(),
                        dropped=set())
    scripts = set(merged.get("script_to_lines") or {})
    assert scripts == {"res://" + REL}, scripts
    tests = set(merged.get("test_to_lines") or {})
    assert tests == {FULL_IDENT}, tests


def test_verdict_merge_dedups_legacy_basename_entry():
    """verdict 合并：重跑脚本的新规范（res://）身份进表后，同物理测试
    的旧 basename 表键剔除并具名披露（72dca1c 上两键并存 → 红）。"""
    baseline = _baseline_doc()
    executions = [{"script": f"res://{REL}", "rc": 1, "out": ""}]
    collisions: list = []
    merged = _merged_test_verdicts(
        baseline, executions, rerun_scripts={SCRIPT_BASE},
        dropped_scripts=set(), removed_rels=set(),
        fresh_gherkin=None, gherkin_rerun=set(), gherkin_dropped=set(),
        tests_face=dict(baseline["tests_face"]),
        arm_identities=[FULL_IDENT], collisions=collisions)
    assert merged is not None
    assert BASE_IDENT not in merged, \
        {k: v for k, v in merged.items() if "pricing" in k}
    assert merged.get(FULL_IDENT, {}).get("verdict") == "red"
    assert any(c.get("reason") == "legacy_basename_identity_replaced_by_"
               "canonical" and c.get("resolved") == FULL_IDENT
               for c in collisions), collisions


def test_verdict_merge_two_dirs_same_name_keep_distinct():
    """D2 不回退：两个目录的同名测试是两个不同规范身份——各自的
    完整路径条目互不吞并（per-script 执行账按 basename 归一，同名
    同账 → 两目录逐测试判定同账，身份仍各自在场）。"""
    a_ident = "res://tests/a/test_x.gd::t"
    b_ident = "res://tests/b/test_x.gd::t"
    baseline = _baseline_doc()
    baseline["test_verdicts"] = {
        "test_x.gd::t": {"verdict": "red", "classification": "new"}}
    executions = [{"script": "res://tests/a/test_x.gd", "rc": 1,
                   "out": "FAILED: t"}]
    collisions: list = []
    merged = _merged_test_verdicts(
        baseline, executions, rerun_scripts={"test_x.gd"},
        dropped_scripts=set(), removed_rels=set(),
        fresh_gherkin=None, gherkin_rerun=set(), gherkin_dropped=set(),
        tests_face={"tests/a/test_x.gd": "sha", "tests/b/test_x.gd": "sha"},
        arm_identities=[a_ident, b_ident], collisions=collisions)
    assert a_ident in merged and b_ident in merged, merged
    assert "test_x.gd::t" not in merged
    # per-script 执行账按 basename 归一——script 字段的目录归属由真实
    # 建图的碰撞披露钉住（见 test_c3r21_d2_same_basename_build_*），此处
    # 只钉「身份键互不吞并」。


def test_verdict_merge_unresolvable_legacy_named_kept():
    """无法恢复规范路径的旧条目（tests_face/宇宙都查不到）→ 原样保留
    并具名披露，不静默丢弃。"""
    legacy = "test_gone.gd::t"
    baseline = _baseline_doc()
    baseline["test_verdicts"] = {legacy: {"verdict": "red",
                                          "classification": "new"}}
    collisions: list = []
    merged = _merged_test_verdicts(
        baseline, executions=[], rerun_scripts=set(),
        dropped_scripts=set(), removed_rels=set(),
        fresh_gherkin=None, gherkin_rerun=set(), gherkin_dropped=set(),
        tests_face=dict(baseline["tests_face"]), arm_identities=[],
        collisions=collisions)
    assert merged.get(legacy) == {"verdict": "red", "classification": "new"}
    assert any(c.get("reason") == "legacy_basename_identity_kept_no_"
               "canonical_path" and c.get("basename_key") == legacy
               for c in collisions), collisions
