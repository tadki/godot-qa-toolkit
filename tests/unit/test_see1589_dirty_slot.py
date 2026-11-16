"""SEE-1589 QA D2 回归：warm 池脏槽不得产出假 run_error。

根因：行级/卡尺面每次在**项目根**物化 `.gqt-attr-select/<id>/`；warm 复用
槽只携带旧 id 目录，新 run 的 `-gtest` 目录在槽内缺失 → GUT 收集 0 测试 →
尾部无 test-run 标记 → 每个 mutant 假 `run_error`。

判据：
- 复用前把项目侧选择目录补齐/清理进槽（具名 `generated_dirs:synced`）；
- 必需 glob 在槽内仍缺失 → 整槽重建一次；重建后仍缺失 → fail-loud
  `SlotProvisionError`（具名，不得静默跑出假 verdict）；
- 池记录里出现过的目标（含被杀 run 的另一目标）被删除/漂移 → 整槽重建。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import shadowpool
from godot_qa_toolkit.mutation.shadow import SlotProvisionError

SRC = "func add(a, b):\n\treturn a + b\n"


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    (proj / "calc.gd").write_text(SRC)
    (proj / "other.gd").write_text(SRC)
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "tests" / "unit" / "test_calc.gd").write_text(
        'extends GutTest\n')
    return proj


def _selection(proj: Path, sel_id: str) -> str:
    d = proj / ".gqt-attr-select" / sel_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "test_calc.gd").write_text(
        'extends "res://tests/unit/test_calc.gd"\n')
    return f"res://.gqt-attr-select/{sel_id}/"


def test_missing_selection_dir_synced_and_stale_pruned(tmp_path):
    proj = _write_project(tmp_path)
    _selection(proj, "oldsel")  # 旧 run 的目录（建槽时被复制进槽）
    s1 = shadowpool.acquire_slot(str(proj), "linux", ["calc.gd"], "w")
    shadowpool.release_slot(s1)
    slot_dir = Path(s1.root)
    assert (slot_dir / ".gqt-attr-select" / "oldsel").is_dir()

    # 新 run 物化新 id；旧 id 已从项目清理
    import shutil
    shutil.rmtree(proj / ".gqt-attr-select" / "oldsel")
    new = _selection(proj, "newsel")
    s2 = shadowpool.acquire_slot(str(proj), "linux", ["calc.gd"], "w",
                                 required_globs=[new])
    try:
        assert (Path(s2.root) / ".gqt-attr-select" / "newsel").is_dir()
        assert not (Path(s2.root) / ".gqt-attr-select" / "oldsel").exists()
        assert s2.farm["decision"] == "reused"
        assert any("generated_dirs:synced(1,pruned=1)" in f
                   for f in s2.freshness), s2.freshness
    finally:
        shadowpool.release_slot(s2)


def test_project_absent_required_glob_is_not_slot_dirty(tmp_path):
    """项目侧不存在的 glob（调用方伪目录）不得判成 slot 制备失败。"""
    proj = _write_project(tmp_path)
    s1 = shadowpool.acquire_slot(str(proj), "linux", ["calc.gd"], "w")
    shadowpool.release_slot(s1)
    s2 = shadowpool.acquire_slot(
        str(proj), "linux", ["calc.gd"], "w",
        required_globs=["res://tests/does-not-exist/"])
    try:
        assert s2.farm["decision"] == "reused"
    finally:
        shadowpool.release_slot(s2)


def test_persistent_missing_required_glob_fails_loud(tmp_path, monkeypatch):
    """重建后仍缺（不可修复）→ SlotProvisionError 具名 fail-loud。"""
    proj = _write_project(tmp_path)
    s1 = shadowpool.acquire_slot(str(proj), "linux", ["calc.gd"], "w")
    shadowpool.release_slot(s1)
    _selection(proj, "newsel")
    missing = "res://.gqt-attr-select/newsel/"
    monkeypatch.setattr(shadowpool, "_missing_required",
                        lambda *a, **k: [missing])
    with pytest.raises(SlotProvisionError) as exc:
        shadowpool.acquire_slot(str(proj), "linux", ["calc.gd"], "w",
                                required_globs=[missing])
    msg = str(exc.value)
    assert "generated selection dirs unavailable after rebuild" in msg
    assert "newsel" in msg


def test_recorded_target_deleted_triggers_full_rebuild(tmp_path):
    proj = _write_project(tmp_path)
    s1 = shadowpool.acquire_slot(str(proj), "linux",
                                 ["calc.gd", "other.gd"], "w")
    shadowpool.release_slot(s1)
    (proj / "other.gd").unlink()
    s2 = shadowpool.acquire_slot(str(proj), "linux", ["calc.gd"], "w")
    try:
        assert s2.farm["decision"] == "rebuilt"
        assert any("target:other.gd:missing-in-repo" in f
                   for f in s2.freshness), s2.freshness
    finally:
        shadowpool.release_slot(s2)
