"""SEE-1581：mutation 真值选面（truth_select）单元判据。

覆盖：新鲜映射 → 精确脚本集/选择目录；无映射 → 全脸（不派生）；测试面
漂移/文件漂移 → 过期；臂可用性（硬失败空映射不是真值）；零引用以映射
为准；kill 证书归属来源（runtime_mapping / structural_index 兜底）。
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
    assert sel["tests_glob"].startswith("res://.qa-cache/gqt-attr-select/")
    wrapper = (proj / ".qa-cache" / "gqt-attr-select" /
               sel["tests_glob"].rstrip("/").rsplit("/", 1)[-1] /
               "test_hit.gd")
    assert wrapper.is_file()
    assert 'extends "res://tests/unit/test_hit.gd"' in wrapper.read_text(
        encoding="utf-8")


def test_no_map_falls_back_to_full_face(tmp_path):
    proj = _project(tmp_path)
    sel = truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert sel["source"] == "full_face"
    assert sel["reason"] == "no_attribution_map"
    assert sel["tests_glob"] is None
    assert sel["zero_ref"] is False


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
    sel = truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert sel["source"] == "full_face"
    assert sel["reason"] in ("tests_face_drift",
                             "auto_update_still_stale:tests_face_drift")


def test_file_drift_marks_stale(tmp_path):
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    (proj / "calc.gd").write_text(TARGET + "\n# drift\n")
    sel = truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert sel["source"] == "full_face"
    assert "file_binding" in (sel["reason"] or "")


def test_hard_failed_measurement_is_not_truth(tmp_path):
    proj = _project(tmp_path)
    path = write_map(proj, {"calc.gd": []})
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    doc["arms_state"] = {"gut_full": {"state": "invalid",
                                      "invalid_reason": "run_error",
                                      "bundles": 0, "pairs": 0}}
    Path(path).write_text(json.dumps(doc), encoding="utf-8")
    sel = truth_select.resolve_selection(proj, str(proj / "calc.gd"))
    assert sel["source"] == "full_face"
    assert "measurement_invalid" in (sel["reason"] or "")


def test_ownership_source_runtime_vs_structural(tmp_path):
    proj = _project(tmp_path)
    write_map(proj, {"calc.gd": ["test_hit.gd::test_ok"]})
    owner = truth_select.ownership_for(str(proj), str(proj / "calc.gd"))
    assert owner.source == "runtime_mapping"
    assert owner.owner_of("test_ok") == "res://tests/unit/test_hit.gd"
    # 无映射 → 结构性兜底（带来源披露），选面仍不碰静态索引
    truth_select.reset_ledger()
    (proj / ".qa-cache" / "attribution-map.json").unlink()
    owner2 = truth_select.ownership_for(str(proj), str(proj / "calc.gd"))
    assert owner2.source == "structural_index"
    assert owner2.owner_of("test_ok") == "res://tests/unit/test_hit.gd"
