"""SEE-1654 harden-R1 钉点④：`target_not_in_map_universe` 经生产调用点
（`renew=False`）常驻钉。

`_universe_target_gate`（SEE-1623 M10）既有常驻测试
（`test_see1623_m10_empty_face_and_universe.py`）只覆盖 `resolve_selection`
默认 `renew=True` 的调用形态；而 run 侧的全部生产调用点——coverage 投影
（`coverage/mapping.py:_mapping_select`）、kill 证书归属
（`mutation/truth_lines.py:ownership_for`）、行级选面
（`truth_select.resolve_line_group_selection` 的内部 resolve）——按
§SPEC-035/S4 固定 `renew=False`。门与续新解耦（SEE-1638 tidy-R4
`aa7a8ac`：门条件去掉 `renew and not explicit`）之后，宇宙外目标经
renew=False 形态同样必须零 GUT 具名失败，而不是退化成误导性的
`auto_update_still_stale:*`。此前没有任何测试钉住 renew=False 形态。

- 钉 1（红锚）：`resolve_selection(..., renew=False)`（run 侧生产调用
  形态本体）——宇宙外目标 → `target_not_in_map_universe` + 重建指引，
  且零续新（`_resolve_renew` 不被触达）。
- 钉 2–4（生产入口）：coverage 投影 → `CoverageMappingError`（具名
  reason 透传）；`ownership_for` → None（无 kill 证书）+ stderr 具名
  披露；`resolve_line_group_selection` → 具名 MutationSelectionError。

红绿咬合：钉 1 在 `775ebdd`（基点 master——门仍要求 `renew=True`，
renew=False 调用不命中门）上为红（实得
`auto_update_still_stale:file_binding_missing_or_stale` 一族）；钉 2–4
在 `775ebdd` 上为绿（当时调用点尚未固定 renew=False，经默认 renew=True
命中旧门）——它们钉的是「当前生产入口的具名出口」本体。
夹具与 M10 同款（attribution_fixtures.write_map），测量臂以 boom
monkeypatch 证明零白跑。
"""

from __future__ import annotations

import pytest


def _universe_fixture(tmp_path, monkeypatch):
    """M10 同款 + scope 收口：预存映射只绑定 src/a.gd，目标 src/other.gd
    被范围规则排除在宇宙外（真实语义：宇宙内新文件会先触发 ensure 重建，
    只有宇宙外目标才以「地图新鲜但不绑定」形态命中门）；mode=auto；
    测量臂 boom（零白跑）。"""
    import json as _json

    from attribution_fixtures import write_map

    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "a.gd").write_text("extends RefCounted\n")
    (root / "src" / "other.gd").write_text(
        "extends RefCounted\n\n\nfunc probe() -> int:\n\treturn 1\n")
    (root / ".gqt-scope.json").write_text(_json.dumps(
        {"production": {"include":
                        [{"kind": "file", "value": "src/a.gd"}]}}),
        encoding="utf-8")
    write_map(root, {"src/a.gd": []})
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    from godot_qa_toolkit.mutation import truth_select as ts
    ts._fresh_recipe_hash.cache_clear()

    def _boom(*_a, **_k):
        raise AssertionError("measurement arm must not run for an "
                             "unbound target")

    monkeypatch.setattr(
        "godot_qa_toolkit.measurement.orchestrator.run_measurement", _boom)
    return root, ts


def test_run_side_call_form_renew_false_names_unbound_target(
        tmp_path, monkeypatch):
    """钉 1（红锚，775ebdd 上为红）：run 侧生产调用形态 renew=False 下
    宇宙外目标 → 具名 `target_not_in_map_universe` + 重建指引；零续新
    （`_resolve_renew` 不被触达）。"""
    root, ts = _universe_fixture(tmp_path, monkeypatch)

    def _renew_boom(*_a, **_k):
        raise AssertionError("renew step must not run when renew=False")

    monkeypatch.setattr(ts, "_resolve_renew", _renew_boom)
    from godot_qa_toolkit.mutation.truth_select import (
        MutationSelectionError,
        resolve_selection,
    )

    with pytest.raises(MutationSelectionError) as ei:
        resolve_selection(str(root), str(root / "src" / "other.gd"),
                          renew=False)
    assert ei.value.reason == "target_not_in_map_universe", ei.value.reason
    assert "does not bind" in str(ei.value), str(ei.value)
    assert "map-update" in str(ei.value), str(ei.value)


def test_coverage_projection_entry_names_unbound_target(tmp_path,
                                                        monkeypatch):
    """钉 2：coverage 投影生产入口（project_file_coverage →
    _mapping_select，renew=False）→ 具名 CoverageMappingError，reason
    透传 target_not_in_map_universe；测量臂 boom（夹具）保证零白跑。"""
    root, _ts = _universe_fixture(tmp_path, monkeypatch)
    from godot_qa_toolkit.coverage import mapping as cm

    with pytest.raises(cm.CoverageMappingError) as ei:
        cm.project_file_coverage(str(root), str(root / "src" / "other.gd"))
    assert ei.value.reason == "target_not_in_map_universe", ei.value.reason
    assert "does not bind" in str(ei.value), str(ei.value)


def test_ownership_entry_returns_none_with_named_disclosure(
        tmp_path, monkeypatch, capsys):
    """钉 3：kill 证书归属生产入口（ownership_for，renew=False）——
    宇宙外目标 → None（保守重跑，无证书）+ stderr 具名披露。"""
    root, _ts = _universe_fixture(tmp_path, monkeypatch)
    from godot_qa_toolkit.mutation.truth_lines import ownership_for

    owner = ownership_for(str(root), str(root / "src" / "other.gd"))
    assert owner is None, owner
    captured = capsys.readouterr()
    assert "target_not_in_map_universe" in captured.err, captured.err[:400]


def test_line_group_entry_names_unbound_target(tmp_path, monkeypatch):
    """钉 4：行级选面生产入口（resolve_line_group_selection，内部
    resolve 固定 renew=False）→ 具名 MutationSelectionError。"""
    root, _ts = _universe_fixture(tmp_path, monkeypatch)
    from godot_qa_toolkit.mutation.truth_select import (
        MutationSelectionError,
        resolve_line_group_selection,
    )

    with pytest.raises(MutationSelectionError) as ei:
        resolve_line_group_selection(str(root),
                                     str(root / "src" / "other.gd"), [2])
    assert ei.value.reason == "target_not_in_map_universe", ei.value.reason
