"""SEE-1405 修复循环（发现三 HIGH）：argv 判定/启动同源化 + 毒条目拒写。

发现三：`GQT_GODOT_BIN` 显式指向 Linux ELF 时 detect_binary_kind=linux、
env 走 _linux_env，但 `_run_gut_on_project` argv 硬编码 `"godot"`（PATH
首匹配）→ 实际拉起 win64 wrapper → env 断连 → 10/10 run_error；断连期间
linux-kind key 写入 win64 引擎产 run_error 毒条目，后续 --resume 误复用。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from godot_qa_toolkit.mutation import inject
from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.resume import ResumeStore
from godot_qa_toolkit.mutation.resume2 import ResumeStoreV2

TARGET_SRC = (
    "func add(a, b):\n"
    "\tif a < b:\n"
    "\t\treturn a + b\n"
    "\treturn b\n"
)


def _mk_project(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "addons" / "gut").mkdir(parents=True)
    (tmp_path / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    target = tmp_path / "calc.gd"
    target.write_text(TARGET_SRC, encoding="utf-8")
    return tmp_path, target


@pytest.fixture(autouse=True)
def _fresh_kind_cache():
    inject.reset_binary_kind_cache()
    yield
    inject.reset_binary_kind_cache()


class TestGodotCommandSameSource:
    def test_explicit_env_wins_over_path(self, tmp_path, monkeypatch):
        """对抗主断言：GQT_GODOT_BIN 显式 → argv 首元 = 解析后路径非 "godot"。"""
        root, _ = _mk_project(tmp_path / "proj")
        elf = tmp_path / "elf-godot"
        elf.write_bytes(b"\x7fELF" + b"\x00" * 8)
        elf.chmod(0o755)
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        import contextlib
        monkeypatch.setattr(mrunner, "GlobalGodotGate",
                            contextlib.nullcontext)
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(mrunner.subprocess, "run", fake_run)
        mrunner._run_gut_on_project(str(root), timeout_s=5)
        assert seen["argv"][0] == str(elf)
        assert seen["argv"][0] != "godot"

    def test_path_channel_resolves_first_match(self, tmp_path, monkeypatch):
        """PATH 通道：argv 首元 = PATH 首匹配的绝对路径（同源判定仍成立）。"""
        root, _ = _mk_project(tmp_path / "proj")
        wrapper = tmp_path / "bin" / "godot"
        wrapper.parent.mkdir()
        wrapper.write_text("#!/bin/bash\nexec /mnt/d/Godot/G_w64.exe \"$@\"\n")
        wrapper.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))
        monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
        import contextlib
        monkeypatch.setattr(mrunner, "GlobalGodotGate",
                            contextlib.nullcontext)
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(mrunner.subprocess, "run", fake_run)
        mrunner._run_gut_on_project(str(root), timeout_s=5)
        assert seen["argv"][0] == str(wrapper)

    def test_unresolvable_falls_back_to_literal_godot(self, tmp_path,
                                                      monkeypatch):
        """PATH 无 godot 且无显式 env → 回退字面量 "godot"（现网缺省不变）。"""
        root, _ = _mk_project(tmp_path / "proj")
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
        import contextlib
        monkeypatch.setattr(mrunner, "GlobalGodotGate",
                            contextlib.nullcontext)
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(mrunner.subprocess, "run", fake_run)
        mrunner._run_gut_on_project(str(root), timeout_s=5)
        assert seen["argv"][0] == "godot"

    def test_godot_command_matches_detect_source(self, tmp_path, monkeypatch):
        """同源契约：godot_command() 与 detect_binary_kind() 读同一解析源。"""
        elf = tmp_path / "elf-godot"
        elf.write_bytes(b"\x7fELF" + b"\x00" * 8)
        elf.chmod(0o755)
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        assert inject.godot_command() == str(elf)
        assert inject.detect_binary_kind() == "linux"


class TestPoisonEntryGuard:
    @staticmethod
    def _fake_gut(calls: dict, mode: str):
        """mode=inject_fail → rc=3 + GQT_INJECT_FAIL；launch_fail → rc=1
        启动失败形态；ok → baseline 后全过。"""
        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "baseline ok")
            if mode == "inject_fail":
                return (3, "--- gqt: stderr ---\nGQT_INJECT_FAIL: "
                          "GQT_MUTATION_CFG env missing")
            if mode == "launch_fail":
                return (1, "Godot Engine v4.6\nAttempt to open script "
                           "res://x.gd File not found")
            return (0, "all passed")
        return fake

    def _run(self, tmp_path, mode, resume=True):
        root, target = _mk_project(tmp_path / "proj")
        calls = {"n": 0}
        monkey_runs = self._fake_gut(calls, mode)
        orig_gut = mrunner._run_gut_on_project
        mrunner._run_gut_on_project = monkey_runs
        try:
            # SEE-1420：落盘面迁移到 resume v2 store（per-id 条目）。
            store = ResumeStoreV2(str(root), str(target), None)
            result = mrunner.run_mutation(
                file_path=str(target), project_root=str(root), budget=4,
                resume2_store=store, resume=resume)
        finally:
            mrunner._run_gut_on_project = orig_gut
        return result, store

    def test_injection_failure_verdicts_not_persisted(self, tmp_path):
        """毒条目防护主断言：注入失败 run_error 不得写入 resume store。"""
        result, store = self._run(tmp_path, "inject_fail")
        assert result["summary"]["run_errors"] == 4
        assert store.load_entries() == {}

    def test_launch_failure_verdicts_not_persisted(self, tmp_path):
        """启动失败 run_error 同样拒写（win64 引擎产物不进 linux-kind key）。"""
        result, store = self._run(tmp_path, "launch_fail")
        assert result["summary"]["run_errors"] >= 1
        assert store.load_entries() == {}

    def test_normal_verdicts_still_persisted(self, tmp_path):
        """正对照：正常判定（survived）照常落盘——防护不误伤 resume 语义。"""
        result, store = self._run(tmp_path, "ok", resume=True)
        assert result["summary"]["run_errors"] == 0
        assert len(store.load_entries()) == 4

    def test_poisoned_entry_rejected_on_resume(self, tmp_path):
        """端到端：已存在的注入失败条目（模拟断连期写入）在 --resume 时
        不被复用——宁重跑，不脏合并。"""
        root, target = _mk_project(tmp_path / "proj")
        store = ResumeStore(str(root), str(target), None, 4)
        bad = {"kind": "AOR", "original": "a + b", "mutated": "a - b",
               "line": 1, "verdict": "run_error",
               "reason": "memory injection failed: GQT_MUTATION_CFG env "
                         "missing"}
        store.record(0, bad)
        mutant = SimpleNamespace(kind="AOR", original="a + b",
                                 mutated="a - b", line=1)
        assert store.load_completed([mutant], 0, 1) == {}
