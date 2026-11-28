"""SEE-1654 C3-R28（D5/决策 20）：feature 面变化走 feature 臂增量，不重建
GUT 臂。

- feature 结构名单（`features` / `count`）摘出 wholesale 哈希——新增 /
  删除 feature 不再判环境失配两臂全量；名单变化仍由 map 的
  `feature_sha256` 对账检测（status 因素 `feature_sha256: new|removed=`）。
- feature 删除进变更面（合成规范 rel）→ feature 臂增量：身份与边剔除、
  不重跑。
- feature 面自身需要全量（重跑面超比例上限、缺 `feature_sha256` 键）→
  臂级全量（reason 具名随台账：`gherkin_rerun_surface_too_large_fallback`
  / `gherkin_face_missing_fingerprint_full`），GUT 臂零进程原样继承；
  GUT 侧因素（配置面/工具链面/生产内容不可定位/GUT 测试面）仍两臂全量。
- 新 feature 身份按重跑腿账补进判定表（§SPEC-025 语义完备性）。

夹具（决策 19 口径）：install_recipe 真实布局，pristine/注入哈希由真实
build_recipe 现算；编译测试名取自真实编译命名函数（gherkin_verdict_fixtures）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_feature_project(tmp_path: Path) -> Path:
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata

    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    install_recipe(proj)
    ensure_addon_metadata(proj)
    return proj


def _add_feature(proj: Path, key: str) -> None:
    feats = proj / "addons" / "gqt_gherkin" / "features"
    (feats / f"{key}.feature").write_text(
        f"Feature: {key}\n  Scenario: s\n    Given world exists\n",
        encoding="utf-8")


def _hashes(proj: Path) -> dict:
    from godot_qa_toolkit.measurement.recipe import build_recipe
    from godot_qa_toolkit.mutation.truth_select import (
        _current_report_recipe_hash, _current_report_recipe_hash_cached,
        _fresh_recipe_hash,
    )

    # 单测同进程树变更后清缓存（真实 CLI 每次冷进程）
    _fresh_recipe_hash.cache_clear()
    _current_report_recipe_hash_cached.cache_clear()
    root = str(proj.resolve())
    return {"pristine": _fresh_recipe_hash(root),
            "report": _current_report_recipe_hash(root),
            "build_error": build_recipe(proj)[1]}


# ───────────────────── wholesale 哈希与 feature 名单 ─────────────────────

def test_feature_add_delete_keeps_wholesale_hash(tmp_path):
    """D5 根因闭合：feature 新增 / 删除不改变 wholesale 哈希（pristine /
    注入域一致）——不再被判环境失配两臂全量；face 级 feature_sha256 仍
    随名单变化（失效面对账源）。"""
    proj = _make_feature_project(tmp_path)
    h0 = _hashes(proj)
    _add_feature(proj, "zz_d5_probe")
    h1 = _hashes(proj)
    assert h1["pristine"] == h0["pristine"], (h0, h1)
    assert h1["report"] == h0["report"], (h0, h1)
    (proj / "addons" / "gqt_gherkin" / "features" / "zz_d5_probe.feature") \
        .unlink()
    h2 = _hashes(proj)
    assert h2["pristine"] == h0["pristine"]
    assert h2["report"] == h0["report"]


def test_feature_face_sha_tracks_list(tmp_path):
    """face 级 `feature_sha256` 仍随名单变化——feature 车道的对账源不被
    摘除（新增可检测）。"""
    proj = _make_feature_project(tmp_path)
    from godot_qa_toolkit.measurement.recipe import build_recipe

    r0, _ = build_recipe(proj)
    _add_feature(proj, "zz_d5_probe")
    r1, _ = build_recipe(proj)
    sha0 = (r0.get("gherkin_face") or {}).get("feature_sha256") or {}
    sha1 = (r1.get("gherkin_face") or {}).get("feature_sha256") or {}
    assert "zz_d5_probe" in sha1 and "zz_d5_probe" not in sha0


# ───────────────────── 删除进变更面（feature 臂增量） ─────────────────────

def test_feature_deletion_enters_changed_surface(tmp_path):
    """删除 feature：`_prepare_drift_state` 把合成规范 rel 放进变更面
    （classify 判 feature_removed）→ refresh 阶梯走增量（身份与边剔除、
    不重跑），不再因 changed 为空落两臂全量。"""
    proj = _make_feature_project(tmp_path)
    _add_feature(proj, "zz_d5_probe")
    from godot_qa_toolkit.mutation.verdict_source import _prepare_drift_state
    from godot_qa_toolkit.mutation.truth_select import (
        _current_report_recipe_hash_cached, _fresh_recipe_hash,
    )
    from godot_qa_toolkit.measurement.recipe import build_recipe

    _fresh_recipe_hash.cache_clear()
    _current_report_recipe_hash_cached.cache_clear()

    recipe, err = build_recipe(proj)
    assert err is None, err
    doc = {"feature_sha256": (recipe.get("gherkin_face") or {})
           .get("feature_sha256"),
           "recipe_hash_pristine": _fresh_recipe_hash(str(proj.resolve())),
           "arms": {"gut_full": {"test_to_lines": {}, "script_to_lines": {},
                                 "tests": 0, "scripts": 0, "files": 0,
                                 "pairs": 0, "function_pairs": 0,
                                 "test_to_functions": {},
                                 "script_to_functions": {},
                                 "file_to_tests": {}, "file_to_scripts": {},
                                 "line_pairs": 0, "script_line_pairs": 0,
                                 "line_to_tests": {}},
                    "gherkin": {"feature_to_lines": {"zz_d5_probe": {}},
                                "test_to_lines": {
                                    "gherkin://feature/zz_d5_probe::t_00":
                                        {}}}}}
    _add_feature(proj, "zz_d5_probe2")
    _changed, drift, _ok = _prepare_drift_state(
        doc, str(proj), [])
    assert any(d.startswith("feature_new:") for d in drift), drift
    # 删除基线内 feature（zz_d5_probe；zz_d5_probe2 是盘上新增）
    (proj / "addons" / "gqt_gherkin" / "features" / "zz_d5_probe.feature") \
        .unlink()
    _changed2, drift2, _ok2 = _prepare_drift_state(doc, str(proj), [])
    assert any(d.startswith("feature_removed:") for d in drift2), drift2
    # 变更面带删除 rel（合成规范形态），classify 可判 feature_removed
    from godot_qa_toolkit.measurement.increment import classify_changes

    classes = classify_changes(proj, sorted(_changed2), doc)
    assert classes["feature_removed"], classes


# ───────────────────── 臂级全量台账（reason 具名） ─────────────────────

def test_arm_full_ledger_names_reason_and_resets_counter(tmp_path):
    """臂级全量：台账 mode/reason 携带具名原因、`increments_since_full`
    归零（feature 臂重建属 full 族）；GUT 重跑面为空 = GUT 进程 0。"""
    proj = _make_feature_project(tmp_path)
    from godot_qa_toolkit.measurement.increment import (
        _build_increment_artifact,
    )

    baseline = {
        "schema_version": "test-attribution/7",
        "report_recipe_hash": "rh", "recipe_hash_pristine": "pr",
        "recipe_buildable": True,
        "project_root": str(proj), "identity": "res://s::t",
        "causal": {"enabled": False},
        "file_sha256": {}, "target_files": [], "tests_face": {},
        "instrumentable_lines": {},
        "arms": {"gut_full": {"test_to_lines": {}, "script_to_lines": {},
                              "tests": 0, "scripts": 0, "files": 0,
                              "pairs": 0, "function_pairs": 0,
                              "test_to_functions": {},
                              "script_to_functions": {},
                              "file_to_tests": {}, "file_to_scripts": {},
                              "line_pairs": 0, "script_line_pairs": 0,
                              "test_to_lines_id": {}},
                 "gherkin": {"test_to_lines": {}, "feature_to_lines": {}}},
        "arms_state": {"gut_full": {"state": "valid"}},
        "test_verdicts": {}, "notes": [],
        "increment": {"increments_since_full": 4},
    }
    classes = {"prod_changed": [], "prod_new": [], "prod_removed": [],
               "test_changed": [], "test_new": [], "test_removed": [],
               "feature_changed": [], "feature_new": [],
               "feature_removed": [], "unknown": []}
    artifact = _build_increment_artifact(
        baseline, dict(baseline["arms"]["gut_full"]),
        {"recipe_hash": "rh2"}, proj, classes, [], {"rerun": set(),
                                                    "dropped": set(),
                                                    "gherkin_rerun": {
                                                        "zz_d5_probe"},
                                                    "gherkin_dropped": set(),
                                                    "sources": {}},
        4, 1.0, [], gherkin_arm_full_reason="gherkin_rerun_surface_too_"
                                            "large_fallback")
    inc = artifact.get("increment") or {}
    assert inc.get("mode") == "gherkin_rerun_surface_too_large_fallback", inc
    assert inc.get("reason") == "gherkin_rerun_surface_too_large_fallback"
    assert inc.get("increments_since_full") == 0
    assert inc.get("rerun_scripts") == []
    assert inc.get("rerun_executions") == []


# ───────────────────── 新 feature 身份按腿账补判定表 ─────────────────────

def test_new_feature_identities_added_from_leg_universe():
    """新增 feature 的规范身份不在基线表 → 按重跑腿宇宙与执行账补表
    （通过 green / 失败 red+new；§SPEC-025 语义完备性）。D5 前该参数
    不存在（TypeError = 旧行为不补表）。"""
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    baseline = {"test_verdicts": {
        "gherkin://feature/alpha::keep_00": {"verdict": "green"}}}
    fresh = {"executions": [{"rc": 1, "out": "...",
                             "failing_tests": ["new_failing_00"],
                             "executed_scripts": ["res://g/new.gd"]}]}
    merged = _merged_test_verdicts(
        baseline, [], rerun_scripts=set(), dropped_scripts=set(),
        removed_rels=set(), fresh_gherkin=fresh,
        gherkin_rerun={"alpha", "beta"}, gherkin_dropped=set(),
        gherkin_identities=["gherkin://feature/alpha::keep_00",
                            "gherkin://feature/beta::new_ok_00",
                            "gherkin://feature/beta::new_failing_00"])
    assert merged["gherkin://feature/beta::new_ok_00"] == {"verdict": "green"}
    entry = merged["gherkin://feature/beta::new_failing_00"]
    assert entry["verdict"] == "red", entry
    assert entry["classification"] == "new", entry
    assert merged["gherkin://feature/alpha::keep_00"]["verdict"] == "green"


# ───────────────────── GUT 侧因素仍两臂全量（守卫，语义不回退） ─────────────────────

def test_gut_side_fallbacks_unchanged():
    """GUT 侧回退原因（生产内容新增 / 校准到期 / gut 比例上限 / hash
    失配）保持两臂全量——D5 只切分 feature 面，不把 GUT 因素放成增量。"""
    from godot_qa_toolkit.measurement.increment import _fallback_reason

    classes = {"prod_new": [], "content_new": [], "feature_new": []}
    assert _fallback_reason(True, classes, 0, 5, 0.0, 0.05,
                            feature_rerun=set(), gherkin_known=40) == \
        "incremental"
    assert _fallback_reason(False, classes, 0, 5, 0.0, 0.05,
                            feature_rerun=set(), gherkin_known=40) == \
        "hash_mismatch_fallback"
    assert _fallback_reason(True, {"prod_new": ["x.gd"], "content_new": [],
                                   "feature_new": []}, 0, 5, 0.0, 0.05,
                            feature_rerun=set(), gherkin_known=40) == \
        "new_prod_file_fallback"
    assert _fallback_reason(True, classes, 5, 5, 0.0, 0.05,
                            feature_rerun=set(), gherkin_known=40) == \
        "calibration_due_fallback"
    assert _fallback_reason(True, classes, 0, 5, 0.9, 0.05,
                            feature_rerun=set(), gherkin_known=40) == \
        "rerun_surface_too_large_fallback"
    assert _fallback_reason(True, classes, 0, 5, 0.0, 0.05,
                            feature_rerun={f"f{i}" for i in range(3)},
                            gherkin_known=40) == \
        "gherkin_rerun_surface_too_large_fallback"
