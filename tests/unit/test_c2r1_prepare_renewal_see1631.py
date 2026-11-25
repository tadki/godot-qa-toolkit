"""SEE-1631 C2-R1：map 过期在 prepare 自动续新（§SPEC-021）的行为判据。

- map 缺失 → prepare 走全脸建图阶梯（ensure_map）；
- 目标文件/tests_face 漂移且 recipe 守恒 → 走增量续新（只重跑失效面）；
- recipe pristine 失配 → 走全脸重建阶梯（先移除 stale 旧图）；
- 续新后仍不一致 / 续新失败 → BaselineSourceError 具名 fail-loud；
- P1 gate：map 源下 attribution_map_missing_or_stale 不再阻断（自动续新）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from godot_qa_toolkit.mutation.map_contract import MapContractError

from attribution_fixtures import install_recipe


def _fresh_map(tmp_path: Path, files: dict[str, str]) -> dict:
    import hashlib
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    from godot_qa_toolkit.measurement.attribution import ATTRIBUTION_SCHEMA
    from godot_qa_toolkit.mutation.truth_select import _fresh_recipe_hash

    install_recipe(tmp_path)
    pristine = _fresh_recipe_hash(str(tmp_path))
    assert pristine
    face = {rel: hashlib.sha256(c.encode()).hexdigest()
            for rel, c in files.items()}
    return {
        "schema_version": ATTRIBUTION_SCHEMA,
        "report_recipe_hash": "rh", "recipe_hash_pristine": pristine,
        "project_root": str(tmp_path),
        "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": dict(face), "target_files": sorted(face),
        "tests_face": dict(face), "instrumentable_lines": {},
        "arms": {"gut_full": {"test_to_lines": {}, "script_to_lines": {},
                              "tests": 0, "scripts": 0, "files": 0,
                              "pairs": 0, "function_pairs": 0,
                              "test_to_functions": {},
                              "script_to_functions": {},
                              "file_to_tests": {}, "file_to_scripts": {},
                              "line_pairs": 0, "script_line_pairs": 0,
                              "test_to_lines": {}, "script_to_lines": {},
                              "line_to_tests": {}}},
        "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                    "pairs": 0, "probes_fired": 5,
                                    "vacuous": False}},
        "test_verdicts": {}, "notes": [],
    }


def _write_map(tmp_path: Path, doc: dict) -> None:
    cache = tmp_path / ".qa-cache"
    cache.mkdir(exist_ok=True)
    (cache / "attribution-map.json").write_text(json.dumps(doc),
                                                encoding="utf-8")


def test_drift_detection_classifies_changed_files(tmp_path):
    from godot_qa_toolkit.mutation.verdict_source import _map_drift

    files = {"tests/unit/test_a.gd": "extends GutTest\n",
             "prod.gd": "extends Node\n"}
    (tmp_path / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "unit" / "test_a.gd").write_text(files[
        "tests/unit/test_a.gd"])
    (tmp_path / "prod.gd").write_text(files["prod.gd"])
    doc = _fresh_map(tmp_path, files)
    (tmp_path / "prod.gd").write_text("extends Node\n# changed\n")
    changed, drift = _map_drift(doc, str(tmp_path),
                                ["prod.gd", "tests/unit/test_a.gd"])
    assert "prod.gd" in changed
    assert all("tests/unit/test_a.gd" not in d for d in drift)


def test_prepare_missing_map_uses_ensure_map(tmp_path, monkeypatch):
    import godot_qa_toolkit.mutation.verdict_source as vs
    from godot_qa_toolkit.mutation import map_contract as _mc

    calls = []
    from godot_qa_toolkit.mutation import truth_select as _ts
    monkeypatch.setattr(
        _ts, "ensure_map",
        lambda root, files, timeout_s=600, **kw: calls.append(("ensure", root)))
    monkeypatch.setattr(
        vs, "_verify_prepared",
        lambda root, files: str(Path(root) / ".qa-cache"
                                / "attribution-map.json"))
    # C3 契约：refresh 装填后校验（assert_map_fresh）——路由测试以桩
    # 放行校验步（路由断言语义不变；桩改动系行为变化必需的夹具更新）
    monkeypatch.setattr(
        _mc, "_load_map_doc",
        lambda root: {"schema_version": "test-attribution/7",
                      "arms_state": {"gut_full": {"state": "valid"}},
                      "test_verdicts": {}})
    monkeypatch.setattr(_mc, "assert_map_fresh", lambda *a, **k: None)
    (tmp_path / "systems").mkdir(parents=True, exist_ok=True)
    (tmp_path / "systems" / "calc.gd").write_text("extends Node\n")
    out = vs.prepare_map_for_baseline(str(tmp_path),
                                      ["tests/unit/test_a.gd"])
    assert out.endswith("attribution-map.json")
    assert calls and calls[0][0] == "ensure"


def test_prepare_drift_uses_increment(tmp_path, monkeypatch):
    import godot_qa_toolkit.mutation.verdict_source as vs

    files = {"tests/unit/test_a.gd": "extends GutTest\n",
             "prod.gd": "extends Node\n"}
    (tmp_path / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "unit" / "test_a.gd").write_text(files[
        "tests/unit/test_a.gd"])
    (tmp_path / "prod.gd").write_text(files["prod.gd"])
    doc = _fresh_map(tmp_path, files)
    _write_map(tmp_path, doc)
    (tmp_path / "prod.gd").write_text("extends Node\n# drifted\n")
    calls = {}

    def fake_increment(root, targets, recipe, baseline, changed, out,
                       **kw):
        calls["changed"] = list(changed)
        # C3-R3 项2：增量产物写唯一 tmp 后原子替换——桩须在 out 落一份
        # 产物（否则 os.replace 无源）
        import json as _json
        from pathlib import Path as _P
        _P(out).write_text(_json.dumps({"mode": "incremental"}),
                            encoding="utf-8")
        return {"mode": "incremental"}

    monkeypatch.setattr(
        "godot_qa_toolkit.measurement.increment.run_attribution_increment",
        fake_increment)
    # 假 increment 不重写磁盘 map——一致性复检桩为通过形态
    monkeypatch.setattr(
        vs, "_verify_prepared",
        lambda root, files: str(Path(root) / ".qa-cache"
                                / "attribution-map.json"))
    # C3 契约：装填后校验桩（同上，路由断言语义不变）
    from godot_qa_toolkit.mutation import map_contract as _mc
    monkeypatch.setattr(
        _mc, "_load_map_doc",
        lambda root: {"schema_version": "test-attribution/7",
                      "arms_state": {"gut_full": {"state": "valid"}},
                      "test_verdicts": {}})
    monkeypatch.setattr(_mc, "assert_map_fresh", lambda *a, **k: None)
    out = vs.prepare_map_for_baseline(str(tmp_path),
                                      ["prod.gd", "tests/unit/test_a.gd"])
    assert out == str(tmp_path / ".qa-cache" / "attribution-map.json")
    assert calls["changed"] == ["prod.gd"], "只重跑失效面（prod.gd）"


def test_prepare_recipe_mismatch_rebuilds(tmp_path, monkeypatch):
    import godot_qa_toolkit.mutation.verdict_source as vs

    files = {"tests/unit/test_a.gd": "extends GutTest\n"}
    (tmp_path / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "unit" / "test_a.gd").write_text(files[
        "tests/unit/test_a.gd"])
    doc = _fresh_map(tmp_path, files)
    doc["recipe_hash_pristine"] = "stale"
    _write_map(tmp_path, doc)
    unlink_calls = []
    real_unlink = Path.unlink

    def spy_unlink(self, *a, **k):
        unlink_calls.append(str(self))
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", spy_unlink)
    from godot_qa_toolkit.mutation import truth_select as _ts
    monkeypatch.setattr(
        _ts, "ensure_map",
        lambda root, files, timeout_s=600, **kw: unlink_calls.append(
            "ensure"))
    monkeypatch.setattr(
        vs, "_verify_prepared",
        lambda root, files: str(Path(root) / ".qa-cache"
                                / "attribution-map.json"))
    # C3 契约：装填后校验桩（路由断言语义不变）
    from godot_qa_toolkit.mutation import map_contract as _mc
    monkeypatch.setattr(
        _mc, "_load_map_doc",
        lambda root: {"schema_version": "test-attribution/7",
                      "arms_state": {"gut_full": {"state": "valid"}},
                      "test_verdicts": {}})
    monkeypatch.setattr(_mc, "assert_map_fresh", lambda *a, **k: None)
    vs.prepare_map_for_baseline(str(tmp_path), ["tests/unit/test_a.gd"])
    # C3-R3 项2：recipe 失配 → 原子重建（force + out_path），**不再移除
    # 旧图**（旧图字节在失败路径下必须不变）
    assert any("ensure" == u for u in unlink_calls), \
        "recipe 失配须走全量重建"
    assert not any("attribution-map.json" in u for u in unlink_calls), \
        "重建不得先删除旧图（旧图字节须在失败路径下不变）"


def test_prepare_still_inconsistent_fail_loud(tmp_path, monkeypatch):
    from godot_qa_toolkit.mutation.verdict_source import (
        BaselineSourceError, prepare_map_for_baseline,
    )

    files = {"tests/unit/test_a.gd": "extends GutTest\n"}
    (tmp_path / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "unit" / "test_a.gd").write_text(files[
        "tests/unit/test_a.gd"])
    doc = _fresh_map(tmp_path, files)
    doc["tests_face"]["tests/unit/test_a.gd"] = "deadbeef"
    _write_map(tmp_path, doc)
    # C3 契约：续新后复检仍 stale → MapContractError 具名（旧图字节不变）；
    # BaselineSourceError（§SPEC-021 老形态）同样保留为合法具名出口。
    with pytest.raises((MapContractError, BaselineSourceError)) as ei:
        prepare_map_for_baseline(str(tmp_path), ["tests/unit/test_a.gd"])
    assert ("still stale" in str(ei.value) or "prepare renewal" in
            str(ei.value) or "still inconsistent" in str(ei.value))


def test_p1_gate_map_blocker_auto_continues_under_map_source(
        tmp_path, monkeypatch):
    """§SPEC-021：map 源下 attribution_map_missing_or_stale 不再阻断
    （prepare 自动续新接管）；其他 P1 仍阻断。"""
    import godot_qa_toolkit.cli as cli

    args = type("A", (), {})()
    args.dry_run = False
    args.auto_prepare = False
    args.select_mode = "line"
    args.tests = None
    args.files = ["systems/calc.gd"]
    args.project_root = str(tmp_path)
    args.attribution_map = None
    plan = {"problems": [
        {"level": "P1", "severity": "warning",
         "code": "attribution_map_missing_or_stale",
         "auto_prepare_covered": True,
         "evidence": {"present": False, "fresh": False, "reason": "x"}},
    ]}
    monkeypatch.setenv("GQT_MUTATION_BASELINE_SOURCE", "map")
    rc = cli._p1_prepare_gate(args, plan)
    assert rc is None, "map 阻断项在 map 源下必须自动续新放行"


def test_p1_gate_other_blockers_still_block_under_map_source(
        tmp_path, monkeypatch):
    import godot_qa_toolkit.cli as cli

    args = type("A", (), {})()
    args.dry_run = False
    args.auto_prepare = False
    args.select_mode = "line"
    args.tests = None
    args.files = ["systems/calc.gd"]
    args.project_root = str(tmp_path)
    args.attribution_map = None
    # 目标文件必须真实存在（gate 的目标存在性校验先于阻断 emit）
    (tmp_path / "systems").mkdir(parents=True, exist_ok=True)
    (tmp_path / "systems" / "calc.gd").write_text("extends Node\n",
                                                  encoding="utf-8")
    plan = {"problems": [
        {"level": "P1", "severity": "warning",
         "code": "gutconfig_missing",
         "auto_prepare_covered": False,
         "evidence": {}},
    ]}
    monkeypatch.setenv("GQT_MUTATION_BASELINE_SOURCE", "map")
    rc = cli._p1_prepare_gate(args, plan)
    assert rc is not None, "非 map 阻断项仍按既有契约阻断"
