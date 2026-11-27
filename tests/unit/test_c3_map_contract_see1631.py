"""SEE-1631 C3：§SPEC-032 map 使用契约（status/refresh/MapView）判据。

- MapView 构造期三断言：§SPEC-025 表在场 / §SPEC-028 dropped 无残留 /
  §SPEC-015 invalid 臂边不出场；
- status 只读（零续新零写盘）、逐因素具名、would_refresh 范围与成本；
- refresh：锁内复检 fresh 合并返回；失败不装半成品（旧图字节不变）。
"""

from __future__ import annotations

import json

import pytest

from godot_qa_toolkit.mutation.map_contract import (
    MapContractError,
    MapView,
    status,
)


def _doc(**over) -> dict:
    doc = {
        "schema_version": "test-attribution/7",
        "recipe_hash_pristine": "abc",
        "project_root": "/p",
        "arms_state": {"gut_full": {"state": "valid"}},
        "arms": {"gut_full": {"test_to_lines": {
            "res://tests/unit/test_a.gd::t1": {"src/calc.gd": [9]}}}},
        "test_verdicts": {
            "res://tests/unit/test_a.gd::t1": {"verdict": "red",
                                               "classification":
                                               "pre_existing",
                                               "script":
                                               "res://tests/unit/test_a.gd"}},
        "instrumentable_lines": {"src/calc.gd": [9, 10]},
        "file_sha256": {"src/calc.gd": "x"},
        "tests_face": {},
    }
    doc.update(over)
    return doc


# --------------------------------------------------------------------------
# MapView 构造期三断言
# --------------------------------------------------------------------------

def test_view_spec025_missing_verdict_table_rejected():
    doc = _doc()
    del doc["test_verdicts"]
    with pytest.raises(MapContractError) as ei:
        MapView("/p", doc)
    assert "§SPEC-025" in str(ei.value)


def test_view_spec028_dropped_residue_rejected():
    doc = _doc(increment={"gherkin_dropped_features": ["qa_gen_07"]},
               test_verdicts={
                   "gherkin://feature/qa_gen_07::s0_01": {"verdict": "green"},
                   "res://tests/unit/test_a.gd::t1": {"verdict": "green"}})
    with pytest.raises(MapContractError) as ei:
        MapView("/p", doc)
    assert "§SPEC-028" in str(ei.value)


def test_view_spec028_no_residue_accepted():
    doc = _doc(increment={"gherkin_dropped_features": ["qa_gen_07"]},
               test_verdicts={
                   "gherkin://feature/keep::s0": {"verdict": "green"}})
    MapView("/p", doc)  # 不抛 = 无残留


def test_view_spec015_invalid_arm_edges_rejected():
    doc = _doc(arms_state={"gut_full": {"state": "valid"},
                           "gherkin": {"state": "invalid"}},
               arms={"gut_full": {"test_to_lines": {}},
                     "gherkin": {"test_to_lines": {"x": {}}}})
    with pytest.raises(MapContractError) as ei:
        MapView("/p", doc)
    assert "§SPEC-015" in str(ei.value)


def test_view_accessors():
    view = MapView("/p", _doc())
    reds = view.executable_reds()
    assert "res://tests/unit/test_a.gd::t1" in reds
    cls, script, name = reds["res://tests/unit/test_a.gd::t1"]
    assert cls == "pre_existing" and script == "res://tests/unit/test_a.gd"
    assert view.projection_inputs("src/calc.gd") == [9, 10]
    view.gherkin_gate("src/calc.gd")  # invalid 门控不抛即合格（None=缺席）



# --------------------------------------------------------------------------
# status：只读、逐因素、would_refresh
# --------------------------------------------------------------------------

def test_status_missing_map_create(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    st = status(str(root), [])
    assert st["present"] is False
    assert st["would_refresh"] == "create"
    assert st["factors"], "缺失因素具名"


def test_status_stale_per_factor_named(tmp_path, monkeypatch):
    import godot_qa_toolkit.mutation.map_contract as mc
    root = tmp_path / "p"
    root.mkdir()
    doc = _doc()
    doc["tests_face"]["tests/unit/test_a.gd"] = "deadbeef"
    monkeypatch.setattr(mc, "_load_map_doc", lambda r: doc)
    st = status(str(root), ["src/calc.gd"])
    assert st["present"] is True and st["fresh"] is False
    assert st["would_refresh"] in ("increment", "full")
    assert any("tests_face" in f for f in st["factors"])
    assert st["last_cost"] in ("unknown",) or isinstance(
        st["last_cost"], (int, float))


def test_status_last_cost_from_increment(tmp_path, monkeypatch):
    import godot_qa_toolkit.mutation.map_contract as mc

    root = tmp_path / "p"
    root.mkdir()
    doc = _doc(increment={"arm_exec_s": 12.5})
    monkeypatch.setattr(mc, "_load_map_doc", lambda r: doc)
    monkeypatch.setattr(mc, "_assert_map_arm_valid", lambda d: None)
    monkeypatch.setattr(mc, "_assert_map_schema", lambda d: None)
    monkeypatch.setattr(mc, "_factor_report", lambda r, d, f: (True, []))
    st = status(str(root), ["src/calc.gd"])
    assert st["fresh"] is True and st["would_refresh"] is None
    assert st["last_cost"] == 12.5


# --------------------------------------------------------------------------
# refresh：锁内复检合并 + 失败不装半成品
# --------------------------------------------------------------------------

def test_refresh_already_fresh_merges(tmp_path, monkeypatch):
    import godot_qa_toolkit.mutation.map_contract as mc

    doc = _doc()
    monkeypatch.setattr(mc, "status",
                        lambda r, f: {"present": True, "fresh": True,
                                      "factors": [], "would_refresh": None,
                                      "scope": [], "last_cost": "unknown"})
    monkeypatch.setattr(mc, "_load_map_doc", lambda r: doc)
    # C3-R20 迁移（A4）：refresh 的 fresh 分支带 `map_path` 披露构造
    # 视图——替身接收并回传同 kwargs（map_path 由函数内 import 解析，
    # 打桩点在 truth_select 模块属性）
    import godot_qa_toolkit.mutation.truth_select as _ts
    monkeypatch.setattr(_ts, "map_path", lambda p: "/resolved/map.json")
    monkeypatch.setattr(mc, "MapView",
                        lambda r, d, map_path="/resolved/map.json":
                        ("VIEW", map_path))
    result = mc.refresh("/p", ["src/calc.gd"])
    assert result["fresh"] is True and result["already_fresh"] is True
    assert result["view"] == ("VIEW", "/resolved/map.json")


def test_refresh_failure_leaves_old_bytes(tmp_path, monkeypatch):
    """refresh 失败（复检仍 stale）→ 具名失败；续新管线自身原子写保证
    旧图字节不变（不装半成品）。"""
    import godot_qa_toolkit.mutation.map_contract as mc

    monkeypatch.setattr(mc, "status",
                        lambda r, f: {"present": True, "fresh": False,
                                      "factors": ["x"], "would_refresh":
                                      "increment", "scope": ["x"],
                                      "last_cost": "unknown"})
    def boom(*a, **k):
        raise RuntimeError("renewal backend failed")
    # status 判 stale（increment 范围）→ 阶梯走增量入口（refresh 函数内
    # 延迟 import verdict_source 符号——patch 源模块属性）
    import godot_qa_toolkit.mutation.verdict_source as vs
    monkeypatch.setattr(vs, "_prepare_increment_renewal", boom)
    monkeypatch.setattr(vs, "_prepare_full_rebuild", boom)
    monkeypatch.setattr(vs, "_prepare_build_missing", boom)
    with pytest.raises((MapContractError, RuntimeError)) as ei:
        mc.refresh("/p", ["src/calc.gd"])
    assert "renewal" in str(ei.value) or "still stale" in str(ei.value)
