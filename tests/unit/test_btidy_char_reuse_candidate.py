"""BTIDY.1 特征化测试：`mutation.shadowpool._reuse_candidate` 分支表钉死。

钉住分层新鲜度门的每个分支（树缺失 / 哨兵-三方-census 歧义 / generated
重建 / synced / 正常复用）及返回值、freshness 披露、farm reason、
SlotProvisionError 具名失败。拆分后断言不变。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_qa_toolkit.mutation import shadowpool as sp
from godot_qa_toolkit.mutation.shadowpool import MutationSlot, SlotProvisionError


def _slot(tmp_path: Path, make_dir: bool = True) -> MutationSlot:
    root = tmp_path / "slot"
    if make_dir:
        root.mkdir()
    return MutationSlot(root=str(root), project_root=str(tmp_path),
                        kind="linux", mode="warm", targets=["res://a.gd"])


def _rec(path: str) -> dict:
    return {"path": path, "in_use": True, "last_used_at": 0.0,
            "wiring_hash": "old", "sentinel": {}}


class _Recorder:
    def __init__(self):
        self.rebuild = []
        self.farm = []
        self.saved = 0
        self.proof = 0
        self.released = 0


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    rec = _Recorder()

    def _rebuild(project_root, pool, slot, r, wiring_hash, now):
        rec.rebuild.append(r["path"])

    def _farm_after(slot, project_root, reason):
        rec.farm.append(reason)

    monkeypatch.setattr(sp, "_rebuild_locked", _rebuild)
    monkeypatch.setattr(sp, "_farm_after_rebuild", _farm_after)
    monkeypatch.setattr(sp, "_sentinel_layer", lambda *a: True)
    monkeypatch.setattr(sp, "_targeted_three_way", lambda *a: True)
    monkeypatch.setattr(sp, "_full_census_layer", lambda *a: True)
    monkeypatch.setattr(sp, "_sync_repo_delta", lambda *a: None)
    monkeypatch.setattr(sp, "_save_pool", lambda *a: setattr(rec, "saved", rec.saved + 1))
    monkeypatch.setattr(sp, "_release_slot_lock", lambda s: setattr(rec, "released", rec.released + 1))
    monkeypatch.setattr(sp, "_proof_guard_or_rebuild",
                        lambda *a, **k: setattr(rec, "proof", rec.proof + 1))
    return rec, monkeypatch


def test_tree_missing_rebuilds(tmp_path, wired):
    rec, mp = wired
    slot = _slot(tmp_path, make_dir=False)
    out = sp._reuse_candidate(str(tmp_path), {}, _rec(""), slot, "h", 1.0, None)
    assert out is slot
    assert slot.freshness[0] == "slot_tree:missing"
    assert rec.rebuild == [""]
    assert rec.farm == ["slot_tree_missing"]


def test_sentinel_ambiguity_rebuilds(tmp_path, wired):
    rec, mp = wired
    mp.setattr(sp, "_sentinel_layer", lambda *a: False)
    slot = _slot(tmp_path)
    out = sp._reuse_candidate(str(tmp_path), {}, _rec(str(slot.root)), slot,
                              "h", 1.0, None)
    assert out is slot
    assert rec.farm == ["freshness_ambiguity"]
    assert rec.rebuild


def test_census_ambiguity_rebuilds(tmp_path, wired):
    rec, mp = wired
    mp.setattr(sp, "_full_census_layer", lambda *a: False)
    slot = _slot(tmp_path)
    sp._reuse_candidate(str(tmp_path), {}, _rec(str(slot.root)), slot,
                        "h", 1.0, None)
    assert rec.farm == ["freshness_ambiguity"]


def test_generated_rebuild_still_missing_raises(tmp_path, wired):
    rec, mp = wired
    slot = _slot(tmp_path)
    mp.setattr(sp, "_sync_generated_dirs",
               lambda *a, **k: {"status": "rebuild", "missing": ["x"]})
    mp.setattr(sp, "_missing_required", lambda *a: ["x"])
    with pytest.raises(SlotProvisionError):
        sp._reuse_candidate(str(tmp_path), {}, _rec(str(slot.root)), slot,
                            "h", 1.0, ["res://x"])
    assert rec.released == 1


def test_generated_rebuild_ok_reuses(tmp_path, wired):
    rec, mp = wired
    slot = _slot(tmp_path)
    mp.setattr(sp, "_sync_generated_dirs",
               lambda *a, **k: {"status": "rebuild", "missing": ["x"]})
    mp.setattr(sp, "_missing_required", lambda *a: [])
    out = sp._reuse_candidate(str(tmp_path), {}, _rec(str(slot.root)), slot,
                              "h", 1.0, None)
    assert out is slot
    assert slot.freshness[0] == "warm:reused"


def test_synced_updates_and_guards(tmp_path, wired):
    rec, mp = wired
    slot = _slot(tmp_path)
    mp.setattr(sp, "_sync_generated_dirs",
               lambda *a, **k: {"status": "ok", "synced": 2, "pruned": 1})
    r = _rec(str(slot.root))
    out = sp._reuse_candidate(str(tmp_path), {}, r, slot, "newhash", 5.0, None)
    assert out is slot
    assert any(s.startswith("generated_dirs:synced(2") for s in slot.freshness)
    assert r["last_used_at"] == 5.0 and r["wiring_hash"] == "newhash"
    assert rec.saved == 1 and rec.proof == 1
