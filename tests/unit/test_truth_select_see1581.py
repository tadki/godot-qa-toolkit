"""SEE-1581：mutation 真值选面（truth_select）单元判据。

覆盖：新鲜映射 → 精确脚本集/选择目录（`.gqt-attr-select/`，位于影子
排除面之外）；映射不可用/过期/臂失效 → **fail-loud**（Owner 定稿：取消
自动全脸，全脸仅显式审计）；lenient 旁路（证书归属）返回 full_face 描述；
零引用以映射为准；kill 证书归属来源两态。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from godot_qa_toolkit.mutation import truth_select
from attribution_fixtures import write_map

TARGET = "func add(a, b):\n\treturn a + b\n"


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "tests" / "unit" / "test_hit.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tpass\n")
    (proj / "calc.gd").write_text(TARGET)
    return proj


def test_fresh_map_yields_exact_selection_dir(tmp_path):
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    sel = truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert sel["source"] == "runtime_mapping"
    assert sel["zero_ref"] is False
    assert sel["tests"] == ["res://tests/unit/test_hit.gd"]
    assert sel["tests_glob"].startswith("res://.gqt-attr-select/")
    wrapper = (proj / ".gqt-attr-select" /
               sel["tests_glob"].rstrip("/").rsplit("/", 1)[-1] /
               "test_hit.gd")
    assert wrapper.is_file()
    assert 'extends "res://tests/unit/test_hit.gd"' in wrapper.read_text(
        encoding="utf-8")


def test_no_map_fails_loud_strict(tmp_path):
    proj = _project(tmp_path)
    with pytest.raises(truth_select.MutationSelectionError) as ei:
        truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert ei.value.reason == "no_attribution_map"
    # lenient 旁路（证书归属）返回 full_face 描述，不抛
    sel = truth_select.resolve_selection(proj, str(proj / "calc.gd"),
                                         strict=False)
    assert sel["source"] == "full_face"
    assert sel["tests_glob"] is None


def test_zero_ref_from_mapping_only(tmp_path):
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": []})
    sel = truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert sel["source"] == "runtime_mapping"
    assert sel["zero_ref"] is True
    assert sel["tests_glob"] is None


def test_test_face_drift_marks_stale(tmp_path):
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    (proj / "tests" / "unit" / "test_hit.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tpass\n# drift\n")
    with pytest.raises(truth_select.MutationSelectionError) as ei:
        truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert "tests_face_drift" in ei.value.reason
    sel = truth_select.resolve_selection(proj, str(proj / "calc.gd"),
                                         strict=False)
    assert sel["source"] == "full_face"


def test_file_drift_marks_stale(tmp_path):
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    (proj / "calc.gd").write_text(TARGET + "\n# drift\n")
    with pytest.raises(truth_select.MutationSelectionError) as ei:
        truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert "file_binding" in ei.value.reason


def test_hard_failed_measurement_is_not_truth(tmp_path):
    proj = _project(tmp_path)
    path = write_map(proj, {"calc.gd": []})
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    doc["arms_state"] = {"gut_full": {"state": "invalid",
                                      "invalid_reason": "run_error",
                                      "bundles": 0, "pairs": 0}}
    Path(path).write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(truth_select.MutationSelectionError) as ei:
        truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert "measurement_invalid" in ei.value.reason


def test_function_selection_and_file_fallback(tmp_path):
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    # 构造 fn 边：test_to_functions 由 helper 之外补齐（直接改产物）
    path = proj / ".qa-cache" / "attribution-map.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["arms"]["gut_full"]["test_to_functions"] = {
        "test_hit.gd::test_ok": ["calc.gd::add"]}
    path.write_text(json.dumps(doc), encoding="utf-8")
    fn = truth_select.resolve_function_selection(proj, str(proj / "calc.gd"),
                                                 "calc.gd::add")
    assert fn["source"] == "function_mapping"
    assert fn["tests"] == ["res://tests/unit/test_hit.gd"]
    assert fn["tests_glob"].startswith("res://.gqt-attr-select/")
    # 未命中的函数 → 文件级回退并披露
    fb = truth_select.resolve_function_selection(proj, str(proj / "calc.gd"),
                                                 "calc.gd::ghost")
    assert fb["source"] == "file_fallback"
    assert fb["reason"] == "function_not_in_mapping"


def test_refresh_validation_rejects_lost_identities(tmp_path):
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok",
                                 "test_other.gd::test_y"]})
    before = truth_select.load_map(proj / ".qa-cache" /
                                   "attribution-map.json")
    after = json.loads(json.dumps(before))
    after["increment"] = {"rerun_scripts": ["test_hit.gd"],
                          "dropped_scripts": []}
    after["arms"]["gut_full"]["test_to_files"] = {
        "test_hit.gd::test_ok": ["calc.gd"]}
    with pytest.raises(truth_select.MutationSelectionError) as ei:
        truth_select.validate_refresh(before, after)
    assert ei.value.reason == "refresh_lost_identities"


def test_unique_tmp_files_differ(tmp_path):
    a = truth_select._unique_tmp(str(tmp_path / "m.json"))
    b = truth_select._unique_tmp(str(tmp_path / "m.json"))
    assert a != b and Path(a).parent == tmp_path
    Path(a).unlink()
    Path(b).unlink()


def test_ownership_source_runtime_and_none_without_map(tmp_path, capsys):
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    owner = truth_select.ownership_for(str(proj), str(proj / "calc.gd"))
    assert owner.source == "runtime_mapping"
    assert owner.owner_of("test_ok") == "res://tests/unit/test_hit.gd"
    # A09：无映射 → None（证书不产出，保守重跑）+ 具名披露；不静态兜底
    truth_select.reset_ledger()
    (proj / ".qa-cache" / "attribution-map.json").unlink()
    owner2 = truth_select.ownership_for(str(proj), str(proj / "calc.gd"))
    assert owner2 is None
    err = capsys.readouterr().err
    assert "no kill certificate" in err and "conservative rerun" in err
