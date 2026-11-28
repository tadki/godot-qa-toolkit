"""SEE-1631 C1：gherkin（原 acceptance）臂 attribution 完整接入的行为判据。

§SPEC-001 结论（git 证据）：归属功能诞生时（SEE-1581 §SPEC-083）两臂同权，
方向②（3117c87）引入 `name == "gut_full"` 门控摘除 acceptance 臂归属——
本批按原语义恢复双臂接入，feature/scenario 身份模型为新增层。

覆盖：schema major-bump（/6 fail-loud）、默认双臂恢复与显式暂停、
gherkin 臂 attr 模板/钩子/收集接线、gherkin 身份改写与 feature/scenario
反查视图、产物 notes 披露。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# --------------------------------------------------------------------------
# §SPEC-003：schema major-bump——旧 /6 产物被消费方具名拒绝
# --------------------------------------------------------------------------

def test_schema_bumped_to_7():
    from godot_qa_toolkit.measurement.attribution import ATTRIBUTION_SCHEMA
    from godot_qa_toolkit.mutation import truth_select

    assert ATTRIBUTION_SCHEMA == "test-attribution/7"
    assert truth_select.SCHEMA_VERSION == "test-attribution/7"


def test_old_schema_6_map_rejected_by_load_map(tmp_path):
    from godot_qa_toolkit.mutation.truth_select import load_map

    doc = {"schema_version": "test-attribution/6",
           "arms": {}, "arms_state": {}}
    p = tmp_path / "old-map.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(Exception) as ei:
        load_map(str(p))
    assert "test-attribution" in str(ei.value)


def test_old_schema_6_rejected_by_projection(tmp_path):
    from godot_qa_toolkit.mutation.coverlink import project_coverage_from_map

    doc = {"schema_version": "test-attribution/6"}
    covered, reason = project_coverage_from_map(
        str(tmp_path), doc, str(tmp_path / "t.gd"), [], "res://tests/")
    assert covered is None
    assert reason is not None and "test-attribution" in str(reason)


# --------------------------------------------------------------------------
# §SPEC-002：默认双臂恢复；显式暂停开关保留为诊断回退
# --------------------------------------------------------------------------

def test_gherkin_arm_enabled_by_default(monkeypatch):
    from godot_qa_toolkit.measurement.orchestrator import gherkin_arm_disabled

    monkeypatch.delenv("GQT_MEASUREMENT_GHERKIN_DISABLED", raising=False)
    monkeypatch.delenv("GQT_MEASUREMENT_ACCEPTANCE_DISABLED", raising=False)
    assert gherkin_arm_disabled() is False


def test_gherkin_arm_explicit_pause_still_works(monkeypatch):
    from godot_qa_toolkit.measurement.orchestrator import gherkin_arm_disabled

    monkeypatch.setenv("GQT_MEASUREMENT_GHERKIN_DISABLED", "1")
    assert gherkin_arm_disabled() is True
    monkeypatch.setenv("GQT_MEASUREMENT_GHERKIN_DISABLED", "0")
    assert gherkin_arm_disabled() is False


def test_retired_acceptance_env_still_fail_loud(monkeypatch):
    from godot_qa_toolkit.measurement.orchestrator import (
        MeasurementError,
        gherkin_arm_disabled,
    )

    monkeypatch.setenv("GQT_MEASUREMENT_ACCEPTANCE_DISABLED", "0")
    with pytest.raises(MeasurementError):
        gherkin_arm_disabled()


def test_explicit_pause_disclosure_unchanged():
    """显式暂停时报告顶层 gherkin_arm 披露值不变（诊断回退通道契约）。"""
    from godot_qa_toolkit.measurement import orchestrator as orch

    assert "disabled_pending_atomization" in orch.GHERKIN_PAUSED_DISCLOSURE
    assert "GQT_MEASUREMENT_GHERKIN_DISABLED=0" in \
        orch.GHERKIN_PAUSED_DISCLOSURE


# --------------------------------------------------------------------------
# §SPEC-002/003：gherkin 臂 attr 模板与钩子接线（同 gut_full 语义）
# --------------------------------------------------------------------------

def _prepared_item(tmp_path: Path, rel: str) -> dict:
    from godot_qa_toolkit.measurement.prepcache import SINK_UUID_PLACEHOLDER

    src = "extends Node\nfunc f() -> int:\n\treturn 1\n"
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")
    return {"abs": str(p), "src": src,
            "template": f"PLAIN {SINK_UUID_PLACEHOLDER}\n",
            "template_attr": f"ATTR {SINK_UUID_PLACEHOLDER}\n",
            "probe_lines": [3], "block_map": {}, "registry": {"entries": []}}


def test_gherkin_arm_stage_files_use_attr_template(tmp_path):
    from godot_qa_toolkit.measurement.arms_stage import _stage_arm_files

    item = _prepared_item(tmp_path, "res/f.gd")
    prepared = {"res/f.gd": item}
    (started, _sinks, _man, inst_files, inst_by_rel,
     _vac) = _stage_arm_files("gherkin", prepared, "u1", True)
    assert "ATTR" in inst_by_rel["res/f.gd"]
    assert "PLAIN" not in inst_by_rel["res/f.gd"]
    assert list(inst_files.values()) == [inst_by_rel["res/f.gd"]]


def test_gherkin_arm_stage_files_use_plain_template_when_off(tmp_path):
    from godot_qa_toolkit.measurement.arms_stage import _stage_arm_files

    item = _prepared_item(tmp_path, "res/f.gd")
    prepared = {"res/f.gd": item}
    (_started, _sinks, _man, _inst, inst_by_rel,
     _vac) = _stage_arm_files("gherkin", prepared, "u1", False)
    assert "PLAIN" in inst_by_rel["res/f.gd"]


def _stage_for(tmp_path, test_attribution):
    import shutil

    from godot_qa_toolkit.measurement.arms import _arm_stage

    root = tmp_path
    (root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    item = _prepared_item(tmp_path, "res/f.gd")
    recipe = {"gut_face": {"dirs": []}}
    stage = _arm_stage("gherkin", root=root, prepared={"res/f.gd": item},
                       recipe=recipe, test_attribution=test_attribution,
                       attribution_causal=False, shadow_instrument=False,
                       shard_width=1, gherkin={"groups": [], "snapshot": {}},
                       gherkin_repeats=1)
    # 直调 _arm_stage 不走臂管线 finally——备份目录显式清理（零残留）
    shutil.rmtree(stage["backup_dir"], ignore_errors=True)
    return stage


def test_gherkin_arm_hooks_carry_attribution_when_enabled(tmp_path):
    stage = _stage_for(tmp_path, True)
    texts = [t for _rel, t in stage["shadow_hook_files"]]
    assert any("gqt_cov_attr_registry_sink" in t for t in texts), (
        "gherkin arm must inject the attribution pre-hook (same semantics "
        "as gut_full) when test_attribution is on")


def test_gherkin_arm_hooks_plain_when_disabled(tmp_path):
    stage = _stage_for(tmp_path, False)
    texts = [t for _rel, t in stage["shadow_hook_files"]]
    assert not any("gqt_cov_attr_registry_sink" in t for t in texts)


# --------------------------------------------------------------------------
# §SPEC-003：gherkin 身份改写 + feature/scenario 双级反查
# --------------------------------------------------------------------------

def _gherkin_ctx():
    groups = [{"suite_dir": "/p/gherkin/generated/.gqt-run-a1",
               "keys": ["alpha"]}]
    identities = {"alpha": [
        {"test_name": "s0_01", "scenario": "first scenario",
         "scenario_line": 3},
        {"test_name": "s1_01", "scenario": "second", "scenario_line": 9},
    ]}
    return groups, identities


def test_identity_rewrite_maps_generated_scripts():
    from godot_qa_toolkit.measurement.attribution import (
        _gherkin_identity_rewrite,
    )

    groups, identities = _gherkin_ctx()
    rw = _gherkin_identity_rewrite(Path("/p"), groups, identities)
    raw = "res://gherkin/generated/.gqt-run-a1/generated/test_alpha.gd::s0_01"
    assert rw["test"][raw] == "gherkin://feature/alpha::s0_01"
    raw_script = ("res://gherkin/generated/.gqt-run-a1/generated/"
                  "test_alpha.gd")
    assert rw["script"][raw_script] == "gherkin://feature/alpha"
    assert rw["scenarios"]["gherkin://feature/alpha::s0_01"]["scenario"] == \
        "first scenario"


def test_identity_rewrite_matches_registry_basename_shape():
    """KOL 真机锚定（SEE-1631 实测）：GUT 注册表身份的脚本段为裸 basename
    `test_<key>.gd`——改写表必须命中该形态，否则身份改写静默落空。"""
    from godot_qa_toolkit.measurement.attribution import (
        _gherkin_identity_rewrite,
    )

    groups, identities = _gherkin_ctx()
    rw = _gherkin_identity_rewrite(Path("/p"), groups, identities)
    assert rw["test"]["test_alpha.gd::s0_01"] == \
        "gherkin://feature/alpha::s0_01"
    assert rw["script"]["test_alpha.gd"] == "gherkin://feature/alpha"


def test_build_attribution_map_basename_registry_feature_views():
    """basename 形态注册表 → 规范身份 + feature/scenario 反查（真机形态
    端到端语义）。"""
    from godot_qa_toolkit.measurement.attribution import (
        _gherkin_identity_rewrite,
        build_attribution_map,
    )

    groups, identities = _gherkin_ctx()
    rw = _gherkin_identity_rewrite(Path("/p"), groups, identities)
    pid = 11
    registries = ({pid: {1: "test_alpha.gd::s0_01"}},
                  {pid: {2: "test_alpha.gd"}})
    attr_by_rel = {"res/f.gd": {pid: {
        "seen": {1: 1, 2: 1}, "cnt": {1: 3, 2: 1},
        "fn": {0: {1}}, "unattr_counts": {}, "phases": [0, 0, 0, 0]}}}
    payload = build_attribution_map(
        "gherkin", attr_by_rel, registries,
        probe_lines_by_rel={"res/f.gd": [10]},
        block_map_by_rel={"res/f.gd": {10: [10, 11]}},
        identity_rewrite=rw)
    ident = "gherkin://feature/alpha::s0_01"
    assert ident in payload["test_to_lines"]
    assert payload["feature_to_lines"]["alpha"]["res/f.gd"] == [10, 11]
    assert payload["line_to_features"]["res/f.gd"]["10"] == ["alpha"]
    assert payload["gherkin_unmapped_identities"] == 0


def _attr_input(rw):
    pid = 11
    test_tables = {pid: {1: "res://gherkin/generated/.gqt-run-a1/"
                             "generated/test_alpha.gd::s0_01"}}
    script_tables = {pid: {2: "res://gherkin/generated/.gqt-run-a1/"
                                "generated/test_alpha.gd"}}
    attr_by_rel = {"res/f.gd": {pid: {
        "seen": {1: 1, 2: 1}, "cnt": {1: 3, 2: 1},
        "fn": {0: {1}}, "unattr_counts": {}, "phases": [0, 0, 0, 0]}}}
    return attr_by_rel, (test_tables, script_tables)


def test_build_attribution_map_gherkin_feature_scenario_views():
    from godot_qa_toolkit.measurement.attribution import (
        _gherkin_identity_rewrite,
        build_attribution_map,
    )

    groups, identities = _gherkin_ctx()
    rw = _gherkin_identity_rewrite(Path("/p"), groups, identities)
    attr_by_rel, registries = _attr_input(rw)
    payload = build_attribution_map(
        "gherkin", attr_by_rel, registries,
        probe_lines_by_rel={"res/f.gd": [10]},
        block_map_by_rel={"res/f.gd": {10: [10, 11, 12]}},
        identity_rewrite=rw)
    ident = "gherkin://feature/alpha::s0_01"
    assert ident in payload["test_to_lines"]
    assert payload["test_to_lines"][ident]["res/f.gd"] == [10, 11, 12]
    assert payload["feature_to_lines"]["alpha"]["res/f.gd"] == [10, 11, 12]
    assert payload["line_to_features"]["res/f.gd"]["10"] == ["alpha"]
    assert payload["pairs"] > 0
    scen = payload["gherkin_scenarios"][ident]
    assert scen["scenario"] == "first scenario"
    assert scen["feature_key"] == "alpha"


def test_build_attribution_map_gherkin_unmapped_identity_kept_raw():
    from godot_qa_toolkit.measurement.attribution import (
        _gherkin_identity_rewrite,
        build_attribution_map,
    )

    groups, identities = _gherkin_ctx()
    rw = _gherkin_identity_rewrite(Path("/p"), groups, identities)
    attr_by_rel, registries = _attr_input(rw)
    # 注册表多一个未知槽位（不在改写表内），且有 fn 级归属证据：不得猜测、
    # 不得丢弃——原始身份保留进行事实并具名计数披露
    registries[0][11][3] = ("res://gherkin/generated/.gqt-run-zz/"
                            "generated/test_ghost.gd::t9")
    attr_by_rel["res/f.gd"][11]["fn"] = {0: {1, 3}}
    payload = build_attribution_map(
        "gherkin", attr_by_rel, registries,
        probe_lines_by_rel={"res/f.gd": [10]},
        block_map_by_rel={"res/f.gd": {10: [10]}},
        identity_rewrite=rw)
    raw = ("res://gherkin/generated/.gqt-run-zz/generated/"
           "test_ghost.gd::t9")
    assert raw in payload["test_to_lines"]
    assert payload["gherkin_unmapped_identities"] >= 1


def test_artifact_notes_disclose_gherkin_attribution():
    from godot_qa_toolkit.measurement.attribution import (
        _ATTRIBUTION_NOTES,
        ATTRIBUTION_SCHEMA,
    )

    joined = " ".join(_ATTRIBUTION_NOTES)
    assert "gherkin" in joined
    assert "attribution off by design" not in joined
    assert ATTRIBUTION_SCHEMA == "test-attribution/7"


def test_arms_state_pairs_from_gherkin_attribution():
    from godot_qa_toolkit.measurement.attribution import _arms_state

    arm = {"arm": "gherkin", "state": "valid", "bundles": 2,
           "bundles_data": {"a": {"sentinels": {"probes_fired": 5,
                                                "vacuous": False}}},
           "attribution": {"test_to_files": {"gherkin://feature/x::t":
                                             ["res/f.gd"]}}}
    states = _arms_state((arm,))
    assert states["gherkin"]["pairs"] == 1
    assert states["gherkin"]["probes_fired"] == 5


def test_gherkin_flow_addon_error_named_not_escaped(tmp_path, monkeypatch):
    """§SPEC-007：编译期 AddonError（缺根/空 features/未知 key）必须转
    具名 invalid 臂文档，不得裸 traceback 逃逸（空 features 注入实测
    抓到的逃逸面）。"""
    from godot_qa_toolkit.gherkin.addon import AddonError
    from godot_qa_toolkit.measurement import arms as arms_mod

    root = tmp_path
    (root / "project.godot").write_text("config_version=5\n",
                                        encoding="utf-8")
    monkeypatch.setattr(arms_mod, "compile_preflight",
                        lambda r, **k: None)
    monkeypatch.setattr(arms_mod, "_ensure_gherkin_harness", lambda r: None)
    monkeypatch.setattr(arms_mod, "_gherkin_groups",
                        lambda keys, shards: [["k1"]])
    monkeypatch.setattr(arms_mod, "create_suite_dir",
                        lambda r, **k: root / "suite")
    def boom(sd, r, chunk):
        raise AddonError("no .feature discovered at addons/gqt_gherkin/"
                         "features")
    monkeypatch.setattr(arms_mod, "_compile_into", boom)
    result = arms_mod.run_gherkin_flow(
        root=root, recipe={"gherkin_face": {"features": [{"key": "k1"}]}},
        gherkin_keys=["k1"], gherkin_shards=1, arms_parallel=False,
        gherkin_adaptive=False, arm_kwargs={})
    assert result["state"] == "invalid"
    assert "suite_error" in str(result.get("invalid_reason"))
    assert "no .feature discovered" in str(result.get("invalid_reason"))
