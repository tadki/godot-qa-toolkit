"""SEE-1594 §SPEC-022/027 A21：coverage 真实文件清洁。

旧 direct coverage 就地插桩 + 事务恢复（SIGTERM/SIGKILL 残留）已随执行体
物理退役（S1，`5beeb4b`→映射投影）。新路径**构造上不写真实项目文件**：
covered 行集来自行级映射投影，零就地插桩、零 GUT。这里验收：
- 新 `run_coverage` 在任何退出路径都不改写目标文件（bytes 恒定）；
- 目标若已是旧版残留插桩态 → fail-loud（要求从版本库还原），不再二次放大；
- 投影路径不 spawn godot。
共享插桩原语（`_instrument_blocks` 等）仍为映射构建器使用，其正确性由
mutation/measurement 既有测试覆盖（S1 未删除）。
"""

from __future__ import annotations

import pytest

import attribution_fixtures as fx
import godot_qa_toolkit.mutation.inject as inject
from godot_qa_toolkit.coverage.runner import run_coverage

SRC = ("extends RefCounted\n\n"
       "func f(a):\n\tif a > 0:\n\t\treturn 1\n\treturn 0\n")


def _proj(tmp_path):
    proj = tmp_path / "proj"
    (proj / "utils").mkdir(parents=True)
    target = proj / "utils" / "t.gd"
    target.write_text(SRC, encoding="utf-8")
    (proj / "tests").mkdir()
    (proj / "tests" / "test_x.gd").write_text("extends Node\n", encoding="utf-8")
    fx.write_map(proj, {"utils/t.gd": ["test_x.gd::test_a"]})
    return proj, target


def _no_godot(monkeypatch):
    monkeypatch.setattr(
        "godot_qa_toolkit.mutation.inject.detect_binary_kind",
        lambda *a, **k: "linux")
    monkeypatch.setattr(
        "godot_qa_toolkit.mutation.identity.identity_pack",
        lambda *a, **k: {"kind": "linux"})


def test_new_path_never_writes_real_file(tmp_path, monkeypatch):
    proj, target = _proj(tmp_path)
    _no_godot(monkeypatch)
    before = target.read_bytes()
    run_coverage(str(target), str(proj))
    assert target.read_bytes() == before


def test_new_path_does_not_spawn_godot(tmp_path, monkeypatch):
    proj, target = _proj(tmp_path)
    _no_godot(monkeypatch)
    spawned = []
    monkeypatch.setattr(inject, "spawn_godot",
                        lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(inject, "godot_command", lambda: "godot")
    run_coverage(str(target), str(proj))
    assert spawned == []


def test_legacy_residue_target_fails_loud(tmp_path, monkeypatch):
    proj, target = _proj(tmp_path)
    _no_godot(monkeypatch)
    target.write_text(SRC + "\nstatic func __qa_cov_probe_0():\n\tpass\n",
                      encoding="utf-8")
    res = run_coverage(str(target), str(proj))
    assert res["ok"] is False
    assert "already instrumented" in res["summary"]["error"]
