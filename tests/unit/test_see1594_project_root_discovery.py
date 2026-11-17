"""SEE-1594 §SPEC-019：项目根自动发现 + 首次导入引导（单一解析源）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from godot_qa_toolkit.projectroot import (ImportBootstrapError,
                                          ProjectRootError, ensure_import_cache,
                                          resolve_project_root)


def _mkproj(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    return root


def test_explicit_wins_over_discovery(tmp_path):
    proj = _mkproj(tmp_path / "game")
    assert resolve_project_root(str(proj), target="/elsewhere/x.gd") == proj.resolve()


def test_explicit_missing_raises(tmp_path):
    with pytest.raises(ProjectRootError):
        resolve_project_root(str(tmp_path / "nope"))


def test_explicit_not_a_directory_raises(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ProjectRootError):
        resolve_project_root(str(f))


def test_discovery_from_target_file(tmp_path):
    proj = _mkproj(tmp_path / "game")
    sub = proj / "utils"
    sub.mkdir()
    tgt = sub / "a.gd"
    tgt.write_text("extends Node\n", encoding="utf-8")
    assert resolve_project_root(None, target=str(tgt)) == proj.resolve()


def test_discovery_from_cwd(tmp_path):
    proj = _mkproj(tmp_path / "game")
    sub = proj / "systems" / "x"
    sub.mkdir(parents=True)
    assert resolve_project_root(None, cwd=str(sub)) == proj.resolve()


def test_none_found_raises(tmp_path):
    with pytest.raises(ProjectRootError):
        resolve_project_root(None, cwd=str(tmp_path))


def test_ensure_import_noop_for_non_godot(tmp_path, monkeypatch):
    monkeypatch.delenv("GQT_AUTO_IMPORT", raising=False)
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    ensure_import_cache(str(tmp_path))  # no project.godot → no-op
    assert not called


def test_ensure_import_escape_hatch(tmp_path, monkeypatch):
    proj = _mkproj(tmp_path / "game")
    monkeypatch.setenv("GQT_AUTO_IMPORT", "0")
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    ensure_import_cache(str(proj), godot_bin="godot")
    assert not called


def test_ensure_import_runs_then_rechecks(tmp_path, monkeypatch):
    proj = _mkproj(tmp_path / "game")
    monkeypatch.delenv("GQT_AUTO_IMPORT", raising=False)
    calls = []

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        gd = proj / ".godot"
        gd.mkdir(exist_ok=True)
        for name in ("uid_cache.bin", "imported", "global_script_class_cache.cfg"):
            (gd / name).write_text("x", encoding="utf-8")
        return _P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    ensure_import_cache(str(proj), godot_bin="godot")
    assert calls and calls[0][1:4] == ["--headless", "--import", "--path"]


def test_ensure_import_failure_raises(tmp_path, monkeypatch):
    proj = _mkproj(tmp_path / "game")
    monkeypatch.delenv("GQT_AUTO_IMPORT", raising=False)

    class _P:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    with pytest.raises(ImportBootstrapError):
        ensure_import_cache(str(proj), godot_bin="godot")
