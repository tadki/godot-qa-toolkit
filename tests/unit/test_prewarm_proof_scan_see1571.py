"""SEE-1571 A5 hardener：E1 扫描 / 身份 / 链解析防御面覆盖闭口。

与 test_prewarm_proof_gaps_see1571.py 同批（<800 行纪律拆分，同一 A3 缺口
清单的后半段）：
- 扫描 fail-closed 面（非 res:// remap、逃逸路径、非常规文件、scandir 与
  stat 失败、不可读 sidecar / .gd）；
- 热路径与校验后扫描歧义（有证树歧义必须重导入；双导入收敛不可证）；
- 启动器与 wrapper 链（不可解析 / 二进制本体 / 目录启动器 / 版本门四门 /
  自引用排除 / 循环 / 深度上限）与链哈希热路径失配。
"""

import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import prewarm_proof as pp

STUB_IDENTITY = {
    "command": "/fake/godot", "kind": "linux",
    "launcher_path": "/fake/godot", "launcher_sha256": "f" * 64,
    "chain_sha256": [], "version": "4.6.2-stable-test",
}


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return path


def _make_project(tmp_path: Path, name: str = "proj") -> Path:
    proj = tmp_path / name
    proj.mkdir()
    _write(proj / "project.godot", "config_version=5\n")
    _write(proj / "src.gd", "func a():\n\treturn 1\n")
    return proj


def _seed_artifacts(root: Path) -> None:
    godot = root / ".godot"
    (godot / "imported").mkdir(parents=True, exist_ok=True)
    _write(godot / "uid_cache.bin", b"uid-cache-v1")
    _write(godot / "global_script_class_cache.cfg", "cache\n")


def _install_identity(monkeypatch) -> None:
    monkeypatch.setattr(pp, "_engine_identity",
                        lambda check_version: dict(STUB_IDENTITY))


def _install_imports(monkeypatch, outcomes) -> list:
    """按序返回 (ok, reason) 的假导入；每次成功导入都补必要产物。"""
    calls: list[str] = []
    queue = list(outcomes)

    def fake(project_root: str) -> tuple[bool, str]:
        calls.append(project_root)
        ok, reason = queue.pop(0) if queue else (True, "rc=0 clean")
        if ok:
            _seed_artifacts(Path(project_root))
        return ok, reason

    monkeypatch.setattr(pp, "_run_import", fake)
    return calls


class _OsProxy:
    """局部 os 代理：只覆盖被测分支，避免污染进程级 os 模块。"""

    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(os, name)




class TestScanDefensiveFailClosed:
    def test_non_res_remap_path(self, tmp_path):
        proj = _make_project(tmp_path)
        with pytest.raises(pp.TreeAmbiguity, match="non-res:// remap path"):
            pp._resolve_res(proj.resolve(), "C:/abs/x.ctex", proj / "a.import")

    def test_remap_path_escaping_project(self, tmp_path):
        proj = _make_project(tmp_path)
        with pytest.raises(pp.TreeAmbiguity, match="escapes project"):
            pp._resolve_res(proj.resolve(), "res://../x.ctex", proj / "a.import")

    def test_remap_section_path_line_produces_output(self, tmp_path):
        proj = _make_project(tmp_path)
        side = _write(proj / "t.png.import",
                      '[remap]\n\nimporter="texture"\npath="res://t.ctex"\n')
        outs = pp._import_remap_outputs(proj.resolve(), side)
        assert [o.name for o in outs] == ["t.ctex"]

    def test_non_regular_file_is_ambiguity(self, tmp_path):
        proj = _make_project(tmp_path)
        try:
            os.mkfifo(proj / "pipe")
        except (OSError, AttributeError):
            pytest.skip("filesystem does not support mkfifo")
        with pytest.raises(pp.TreeAmbiguity, match="non-regular file"):
            pp._walk_files(proj.resolve())

    def test_scandir_failure_is_ambiguity(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path)

        def scandir_fail(path):
            raise OSError("scandir denied")

        monkeypatch.setattr(pp, "os", _OsProxy(scandir=scandir_fail))
        with pytest.raises(pp.TreeAmbiguity, match="scandir failed"):
            pp._walk_files(proj.resolve())

    def test_unreadable_import_sidecar_content(self, tmp_path):
        proj = _make_project(tmp_path)
        side = _write(proj / "x.png.import", b"\xff\xfe\x00broken")
        with pytest.raises(pp.TreeAmbiguity, match="unreadable"):
            pp._import_remap_outputs(proj.resolve(), side)

    def test_unreadable_gd_source_is_ambiguity(self, tmp_path):
        """插件扫描面对不可读 .gd fail-closed（A3 移交的最后一处行缺口）。"""
        if os.geteuid() == 0:
            pytest.skip("root 可读任意文件，无法构造 EACCES")
        proj = _make_project(tmp_path)
        bad = _write(proj / "secret.gd", "func a():\n\treturn 1\n")
        bad.chmod(0o000)
        try:
            with pytest.raises(pp.TreeAmbiguity, match=r"unreadable \.gd "):
                pp._enumerate_providers(
                    proj.resolve(), pp._walk_files(proj.resolve()))
        finally:
            bad.chmod(0o644)

    def test_entry_stat_failure_is_ambiguity(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path)

        class _BadEntry:
            name = "ghost"
            path = str(proj / "ghost")

            def is_symlink(self):
                return False

            def is_dir(self, follow_symlinks=False):
                raise OSError("stat denied")

            def is_file(self, follow_symlinks=False):
                return False

        monkeypatch.setattr(pp, "os", _OsProxy(
            scandir=lambda path: [_BadEntry()]))
        with pytest.raises(pp.TreeAmbiguity, match="stat failed"):
            pp._walk_files(proj.resolve())


class TestHotPathAmbiguityBranches:
    def _seal(self, tmp_path, monkeypatch) -> Path:
        proj = _make_project(tmp_path)
        _install_identity(monkeypatch)
        _install_imports(monkeypatch, [(True, "rc=0 clean")] * 2)
        assert pp.prewarm_with_proof(str(proj))["decision"] == (
            "imported_certified")
        return proj

    def test_hot_path_ambiguity_forces_reimport(
            self, tmp_path, monkeypatch, capsys):
        """有证热路径扫描歧义 → 重导入（不得沿用旧证跳过）。"""
        proj = self._seal(tmp_path, monkeypatch)
        os.symlink(proj / "src.gd", proj / "late-link.gd")
        _install_imports(monkeypatch, [(True, "rc=0 clean")] * 2)
        out = pp.prewarm_with_proof(str(proj))
        err = capsys.readouterr().err
        assert out["decision"] != "fresh_skip"
        assert "re-import (tree ambiguity:" in err
        assert out["reason"].startswith("post-import scan ambiguous:")

    def test_post_verify_scan_ambiguous(self, tmp_path, monkeypatch, capsys):
        """校验导入后的二次扫描歧义 → NO_CERT（双导入收敛不可证）。"""
        proj = _make_project(tmp_path)
        _install_identity(monkeypatch)
        counter = {"n": 0}

        def fake(project_root: str):
            counter["n"] += 1
            root = Path(project_root)
            _seed_artifacts(root)
            if counter["n"] == 2:
                os.symlink(root / "src.gd", root / "late-link.gd")
            return True, "rc=0 clean"

        monkeypatch.setattr(pp, "_run_import", fake)
        out = pp.prewarm_with_proof(str(proj))
        assert out["decision"] == "imported_uncertified"
        assert out["reason"].startswith("post-verify scan ambiguous:")
        assert counter["n"] == 2


class TestChainLauncherDefensive:
    def test_directory_launcher_cannot_reach_terminal(self, tmp_path):
        """启动器不可读（目录）→ 链解析 NO_CERT，不降级绑定直接目标。"""
        directory = tmp_path / "launcher_dir"
        directory.mkdir()
        with pytest.raises(pp.TreeAmbiguity,
                           match="cannot reach terminal binary"):
            pp._resolve_chain(directory)


class TestEngineIdentity:
    def _binary(self, tmp_path, name="godot_lin"):
        from tests.unit.test_godot_bin_unify_see1525 import ELF_MAGIC
        path = tmp_path / name
        path.write_bytes(ELF_MAGIC)
        path.chmod(0o755)
        return path

    def test_launcher_unresolvable(self, monkeypatch):
        from godot_qa_toolkit.mutation import inject

        monkeypatch.setattr(inject, "godot_command",
                            lambda: "/no/such/godot")
        with pytest.raises(pp.TreeAmbiguity, match="launcher unresolvable"):
            pp._engine_identity(check_version=False)

    def test_binary_launcher_has_empty_chain(self, tmp_path, monkeypatch):
        from godot_qa_toolkit.mutation import inject

        elf = self._binary(tmp_path)
        monkeypatch.setattr(inject, "godot_command", lambda: str(elf))
        identity = pp._engine_identity(check_version=False)
        assert identity["chain_sha256"] == []
        assert identity["launcher_sha256"] == pp._sha256_file(elf)

    def _version_run(self, monkeypatch, proc_or_exc):
        import subprocess as sp
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if isinstance(proc_or_exc, Exception):
                raise proc_or_exc
            return proc_or_exc

        monkeypatch.setattr(pp, "subprocess", types.SimpleNamespace(
            run=run, TimeoutExpired=sp.TimeoutExpired))
        return calls

    def test_version_gate_records_version(self, tmp_path, monkeypatch):
        from godot_qa_toolkit.mutation import inject

        elf = self._binary(tmp_path)
        monkeypatch.setattr(inject, "godot_command", lambda: str(elf))
        calls = self._version_run(monkeypatch, types.SimpleNamespace(
            returncode=0, stdout="4.6.2.stable.official\n", stderr=""))
        identity = pp._engine_identity(check_version=True)
        assert identity["version"] == "4.6.2.stable.official"
        assert calls[0][0] == str(elf) and calls[0][1] == "--version"

    def test_version_gate_nonzero_rc(self, tmp_path, monkeypatch):
        from godot_qa_toolkit.mutation import inject

        monkeypatch.setattr(inject, "godot_command",
                            lambda: str(self._binary(tmp_path)))
        self._version_run(monkeypatch, types.SimpleNamespace(
            returncode=1, stdout="", stderr=""))
        with pytest.raises(pp.TreeAmbiguity,
                           match="--version rc=1 unresolvable"):
            pp._engine_identity(check_version=True)

    def test_version_gate_empty_stdout(self, tmp_path, monkeypatch):
        from godot_qa_toolkit.mutation import inject

        monkeypatch.setattr(inject, "godot_command",
                            lambda: str(self._binary(tmp_path)))
        self._version_run(monkeypatch, types.SimpleNamespace(
            returncode=0, stdout="   \n", stderr=""))
        with pytest.raises(pp.TreeAmbiguity,
                           match="--version rc=0 unresolvable"):
            pp._engine_identity(check_version=True)

    def test_version_gate_oserror(self, tmp_path, monkeypatch):
        from godot_qa_toolkit.mutation import inject

        monkeypatch.setattr(inject, "godot_command",
                            lambda: str(self._binary(tmp_path)))
        self._version_run(monkeypatch, OSError("exec denied"))
        with pytest.raises(pp.TreeAmbiguity,
                           match="godot --version failed"):
            pp._engine_identity(check_version=True)

    def test_godot_targets_binary_body_has_no_targets(self, tmp_path):
        elf = self._binary(tmp_path)
        assert pp._godot_targets(elf) == []

    def test_godot_targets_unreadable_path_is_empty(self, tmp_path):
        directory = tmp_path / "a_dir"
        directory.mkdir()
        assert pp._godot_targets(directory) == []


class TestChainDepthLimit:
    def test_chain_depth_over_four_is_ambiguity(self, tmp_path):
        """链深 > 4 层 → TreeAmbiguity（不得只绑定直接目标）。"""
        proj = _make_project(tmp_path)
        links = [proj / f"w{i}" for i in range(6)]
        final = proj / "fin"
        final.write_bytes(b"\x7fELF\x00binary")
        final.chmod(0o755)
        for i, link in enumerate(links):
            nxt = links[i + 1] if i + 1 < len(links) else final
            link.write_text(f'#!/bin/sh\nexec "{nxt}" "$@"\n')
            link.chmod(0o755)
        with pytest.raises(pp.TreeAmbiguity, match="depth exceeds 4"):
            pp._resolve_chain(links[0])
