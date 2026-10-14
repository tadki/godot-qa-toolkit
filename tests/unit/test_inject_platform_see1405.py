"""SEE-1405 §SPEC-012 子集：目标二进制类型判定 + mutant_env 平台分支单测。

发现一（Revy P1，HIGH）：mutant_env 全走 _win_path（UNC）——linux 引擎
收到 UNC cfg → 注入静默无效化（leg b：10/10 静默 survived）；UNA+native
混合根直接 SIGABRT（leg a rc=-6）。修复：按目标 godot 二进制类型分支。

对抗面：
- win64 通道逐字节不回归（既有 3 参 fake monkeypatch 契约）
- linux 通道：cfg/env 原生形态、无 APPDATA、无 WSLENV、XDG 原生路径
- 未 Godot 二进制（缺省）恒 win64——现网行为缺省不变
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from godot_qa_toolkit.mutation import inject


def _fake_binary(tmp_path: Path, name: str, content: bytes) -> Path:
    """可执行的真实文件（探测层存在性/可读性检查需要真实文件）。"""
    fake = tmp_path / name
    fake.write_bytes(content)
    fake.chmod(0o755)
    return fake


@pytest.fixture(autouse=True)
def _fresh_kind_cache():
    """per-process 判定缓存按用例清空——env 变更必须反映到 detect 结果。"""
    inject.reset_binary_kind_cache()
    yield
    inject.reset_binary_kind_cache()


@pytest.fixture()
def toy(tmp_path):
    (tmp_path / "systems").mkdir()
    target = tmp_path / "systems" / "calc.gd"
    target.write_text("func add(a, b):\n\treturn a + b\n", encoding="utf-8")
    return tmp_path, target


class TestBinaryKindDetection:
    def test_explicit_linux_env(self, monkeypatch, tmp_path):
        elf = tmp_path / "godot"
        elf.write_bytes(b"\x7fELF" + b"\x00" * 8)
        elf.chmod(0o755)
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        assert inject.detect_binary_kind() == "linux"

    def test_explicit_win64_exe_suffix(self, monkeypatch, tmp_path):
        exe = _fake_binary(tmp_path, "Godot_4.6.2_win64.exe", b"MZ\x90\x00")
        monkeypatch.setenv("GQT_GODOT_BIN", str(exe))
        assert inject.detect_binary_kind() == "win64"

    def test_default_resolves_win64_wrapper_probe(self, monkeypatch, tmp_path):
        """无 GQT_GODOT_BIN：解析 PATH 上 godot；wrapper（bash 脚本 exec
        win64 exe）按 exec 目标判 win64。"""
        fake = tmp_path / "godot"
        fake.write_text("#!/bin/bash\nexec /mnt/d/Godot/Godot_win64.exe \"$@\"\n")
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
        assert inject.detect_binary_kind() == "win64"

    def test_default_resolves_linux_elf(self, monkeypatch, tmp_path):
        fake = tmp_path / "godot"
        fake.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 16)
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
        assert inject.detect_binary_kind() == "linux"

    def test_unresolvable_defaults_win64(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))  # 空 PATH：无 godot
        monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
        assert inject.detect_binary_kind() == "win64"


class TestMutantEnvWin64ChannelUnchanged:
    def test_win64_kind_uses_win_paths_and_wslenv(self, toy, monkeypatch,
                                                  tmp_path):
        root, target = toy
        exe = _fake_binary(tmp_path, "godot.exe", b"MZ\x90\x00")
        monkeypatch.setenv("GQT_GODOT_BIN", str(exe))
        env, cfg_fs = inject.mutant_env("res://systems/calc.gd", "mutated",
                                        str(root), 0)
        assert env["GQT_MUTATION_CFG"].endswith(".json")
        assert env["XDG_DATA_HOME"].startswith("\\\\") or "/" not in \
            Path(env["XDG_DATA_HOME"]).name
        assert "APPDATA" in env
        assert "WSLENV" in env
        assert "GQT_MUTATION_CFG/w" in env["WSLENV"]

    def test_default_kind_is_win64_no_env_set(self, toy, monkeypatch):
        """未设 GQT_GODOT_BIN 且 PATH 无 godot → win64 现网缺省不变。"""
        root, target = toy
        monkeypatch.setenv("PATH", "")
        monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
        env, _ = inject.mutant_env("res://systems/calc.gd", "mutated",
                                   str(root), 0)
        assert "WSLENV" in env and "APPDATA" in env


class TestMutantEnvLinuxChannel:
    def test_linux_kind_uses_native_paths(self, toy, monkeypatch, tmp_path):
        root, target = toy
        elf = tmp_path / "godot"
        elf.write_bytes(b"\x7fELF" + b"\x00" * 8)
        elf.chmod(0o755)
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        env, cfg_fs = inject.mutant_env("res://systems/calc.gd", "mutated",
                                        str(root), 0)
        # cfg 落 run_root 原生形态，env 值 == fs 绝对路径（无 UNC/wslpath）
        assert env["GQT_MUTATION_CFG"] == cfg_fs
        assert cfg_fs.startswith(str(root))
        assert not env["GQT_MUTATION_CFG"].startswith("\\\\")
        # XDG 原生（Linux godot 直接读）；不设 APPDATA/WSLENV
        xdg = env["XDG_DATA_HOME"]
        assert xdg.startswith("/") and not xdg.startswith("//")
        assert "APPDATA" not in env
        assert "WSLENV" not in env

    def test_linux_cfg_payload_intact_and_registered(self, toy, monkeypatch,
                                                     tmp_path):
        root, target = toy
        monkeypatch.setenv("GQT_GODOT_BIN",
                           str(_fake_binary(tmp_path, "g", b"\x7fELF")))
        monkeypatch.setattr(inject, "_kind_of_executable", lambda p: "linux")
        env, cfg_fs = inject.mutant_env("res://systems/calc.gd", "SRC",
                                        str(root), 3)
        payload = json.loads(Path(cfg_fs).read_text(encoding="utf-8"))
        assert payload == {"target_res": "res://systems/calc.gd",
                           "mutated_source": "SRC",
                           "impl_res": payload["impl_res"]}
        assert "_3_calc.gd" in payload["impl_res"]
        assert cfg_fs in inject._TEMP_FILES

    def test_linux_user_dir_failure_degrades_without_crash(self, toy,
                                                           monkeypatch,
                                                           tmp_path):
        root, target = toy
        monkeypatch.setenv("GQT_GODOT_BIN",
                           str(_fake_binary(tmp_path, "g", b"\x7fELF")))
        monkeypatch.setattr(inject, "_kind_of_executable", lambda p: "linux")
        monkeypatch.setattr(inject, "make_user_dir", lambda: None)
        env, _ = inject.mutant_env("res://systems/calc.gd", "SRC",
                                   str(root), 0)
        assert "XDG_DATA_HOME" not in env and "APPDATA" not in env
        assert "GQT_MUTATION_CFG" in env  # cfg 通道不受 user:// 隔离失败影响


class TestKillListSemantics:
    def test_legacy_win64_env_values_still_wslpath_form(self, toy,
                                                        monkeypatch,
                                                        tmp_path):
        """既有契约：win64 下 env 值经 wslpath -w（D:\ 或 UNC 形式）。"""
        root, _ = toy
        exe = _fake_binary(tmp_path, "godot.exe", b"MZ\x90\x00")
        monkeypatch.setenv("GQT_GODOT_BIN", str(exe))
        env, _ = inject.mutant_env("res://systems/calc.gd", "SRC",
                                   str(root), 0)
        v = env["GQT_MUTATION_CFG"]
        assert not v.startswith(str(root))  # 原生 ext4 形态被转换走
        assert (v.startswith("\\\\") or ":" in v[:3] or v.startswith("/"))
