"""BTIDY.1 特征化测试：`measurement.arms._arm_stage` 拆分前行为钉死。

钉住：返回 dict 键集、started.created 清单、reps 计算、started.json 写入
条件、hook 文件清单、backup_dir 私有模式、project.godot 缺失 fail-loud。
拆分后断言逐项不变（仅允许符号移动后的 patch 目标指向调整）。
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from godot_qa_toolkit.measurement import arms
from godot_qa_toolkit.measurement.backup import MeasurementError
from godot_qa_toolkit.measurement.prepcache import SINK_UUID_PLACEHOLDER
from godot_qa_toolkit.measurement.probe_templates import (
    _ATTR_CTX_RELPATH,
    _FLUSHER_RELPATH,
    _PRE_HOOK_RELPATH,
)

TEMPLATE = f"static var probe: Array = []\n# {SINK_UUID_PLACEHOLDER}\n"


def _make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    (proj / "project.godot").write_text('config_version=5\n')
    return proj


def _prepared(proj: Path) -> dict:
    target = proj / "src.gd"
    target.write_text("extends Node\n")
    return {
        "src.gd": {
            "abs": str(target),
            "src": "extends Node\n",
            "template": TEMPLATE,
            "template_attr": TEMPLATE,
            "probe_lines": [1, 2],
            "registry": {},
        },
    }


@pytest.fixture()
def iso_backup(tmp_path, monkeypatch):
    base = tmp_path / "backups"
    base.mkdir(mode=0o700)
    monkeypatch.setattr(arms, "_backup_base", lambda: base)
    return base


def test_arm_stage_default_shape(tmp_path, iso_backup):
    proj = _make_project(tmp_path)
    stage = arms._arm_stage(
        "gut_full", root=proj, prepared=_prepared(proj),
        recipe={"gut_face": {"dirs": []}}, test_attribution=False,
        attribution_causal=False, shadow_instrument=False, shard_width=1,
        gherkin=None, gherkin_repeats=3)
    # 键集
    assert set(stage) == {
        "run_uuid", "backup_dir", "started", "sink_by_rel",
        "manifest_by_rel", "instrumented_files", "inst_by_rel",
        "vacuous_rels", "plans", "reps", "shadow_hook_files",
        "attribution_causal"}
    # created 清单（非 causal）
    assert stage["started"]["created"] == [
        str(proj / _FLUSHER_RELPATH), str(proj / _PRE_HOOK_RELPATH)]
    assert stage["started"]["project_root"] == str(proj)
    # gut_full 单轮：reps 恒 1（gherkin=None）
    assert stage["reps"] == 1
    # 私有备份目录 + started.json 在场
    bdir = stage["backup_dir"]
    assert bdir.parent == iso_backup
    assert stat.S_IMODE(bdir.stat().st_mode) == 0o700
    assert (bdir / "started.json").is_file()
    # hook 清单基线两项
    assert [p for p, _ in stage["shadow_hook_files"]] == [
        _FLUSHER_RELPATH, _PRE_HOOK_RELPATH]
    # 装载面
    assert stage["sink_by_rel"] and stage["manifest_by_rel"]
    assert stage["inst_by_rel"]["src.gd"].count(SINK_UUID_PLACEHOLDER) == 0
    assert stage["vacuous_rels"] == []


def test_arm_stage_causal_adds_ctx(tmp_path, iso_backup):
    proj = _make_project(tmp_path)
    stage = arms._arm_stage(
        "gut_full", root=proj, prepared=_prepared(proj),
        recipe={"gut_face": {"dirs": []}}, test_attribution=True,
        attribution_causal=True, shadow_instrument=False, shard_width=1,
        gherkin=None, gherkin_repeats=1)
    assert str(proj / _ATTR_CTX_RELPATH) in stage["started"]["created"]
    assert _ATTR_CTX_RELPATH in [p for p, _ in stage["shadow_hook_files"]]


def test_arm_stage_acceptance_reps(tmp_path, iso_backup):
    proj = _make_project(tmp_path)
    suite = proj / "gherkin" / "suite_x"
    (suite / "generated").mkdir(parents=True)
    stage = arms._arm_stage(
        "gherkin", root=proj, prepared=_prepared(proj),
        recipe={"gut_face": {"dirs": []}}, test_attribution=False,
        attribution_causal=False, shadow_instrument=True, shard_width=1,
        gherkin={"groups": [{"suite_dir": str(suite), "keys": ["k"]}],
                    "snapshot": {}},
        gherkin_repeats=3)
    # 验收臂 N 次重复（上限 reps）
    assert stage["reps"] == 3
    # shadow_instrument=True → 不写 started.json
    assert not (stage["backup_dir"] / "started.json").exists()
    # plans 由 acceptance groups 派生
    assert stage["plans"] and stage["plans"][0]["keys"] == ["k"]


def test_arm_stage_acceptance_reps_floor_one(tmp_path, iso_backup):
    proj = _make_project(tmp_path)
    suite = proj / "gherkin" / "suite_x"
    (suite / "generated").mkdir(parents=True)
    stage = arms._arm_stage(
        "gherkin", root=proj, prepared=_prepared(proj),
        recipe={"gut_face": {"dirs": []}}, test_attribution=False,
        attribution_causal=False, shadow_instrument=True, shard_width=1,
        gherkin={"groups": [{"suite_dir": str(suite), "keys": ["k"]}],
                    "snapshot": {}},
        gherkin_repeats=0)
    assert stage["reps"] == 1


def test_arm_stage_missing_project_godot_fails_loud(tmp_path, iso_backup):
    proj = tmp_path / "proj"
    proj.mkdir()
    with pytest.raises(MeasurementError):
        arms._arm_stage(
            "gut_full", root=proj, prepared=_prepared(proj),
            recipe={"gut_face": {"dirs": []}}, test_attribution=False,
            attribution_causal=False, shadow_instrument=False, shard_width=1,
            gherkin=None, gherkin_repeats=1)
