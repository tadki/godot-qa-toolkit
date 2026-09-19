"""SEE-1321 §SPEC-015：SIGTERM/SIGINT 中断清理对抗用例。

QA 实测缺陷（MEDIUM）：worker 被 SIGTERM 中断后 `.gqt_mutation_cfg_*.json`
与 `.gqt_mutation_host_*.gd` 残留工作树（os._exit 跳过 finally/atexit）。
本组用例先 RED 实证再驱动修复。
"""

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import godot_qa_toolkit.mutation.inject as inject_mod
import godot_qa_toolkit.mutation.runner as runner_mod
from godot_qa_toolkit.mutation.inject import (
    cleanup_temp_files,
    register_temp,
)


class _ExitJump(BaseException):
    """模拟 os._exit 的控制流跳转（pytest 内不可真退出）。"""


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    (proj / "tests" / "unit" / "save").mkdir(parents=True)
    (proj / "tests" / "unit" / "save" / "test_save.gd").write_text(
        'preload("res://calc.gd")\n')
    target = proj / "calc.gd"
    target.write_text("func add(a, b):\n\treturn a + b\n")
    return str(proj), str(target)


class TestTempRegistryCleanup:
    def test_cleanup_removes_registered_files(self, tmp_path):
        f1 = tmp_path / ".gqt_mutation_host_999.gd"
        f2 = tmp_path / ".gqt_mutation_cfg_999_0.json"
        f1.write_text("x")
        f2.write_text("{}")
        register_temp(str(f1))
        register_temp(str(f2))
        cleanup_temp_files()
        assert not f1.exists() and not f2.exists()

    def test_cleanup_survives_missing_files(self, tmp_path):
        f1 = tmp_path / ".gqt_mutation_host_998.gd"
        f1.write_text("x")
        register_temp(str(f1))
        f1.unlink()
        cleanup_temp_files()  # 已删文件必须幂等，不得抛

    def test_cleanup_survives_permission_error(self, tmp_path, monkeypatch):
        f1 = tmp_path / ".gqt_mutation_host_997.gd"
        f1.write_text("x")
        f2 = tmp_path / ".gqt_mutation_cfg_997_0.json"
        f2.write_text("{}")
        register_temp(str(f1))
        register_temp(str(f2))
        orig_unlink = Path.unlink

        def denying(self, *a, **k):
            if self.name == f1.name:
                raise PermissionError(1, "denied", str(self))
            return orig_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", denying)
        cleanup_temp_files()  # 单文件 unlink 失败不得中断其余清理
        assert not f2.exists(), "remaining files must still be cleaned"


class TestSigtermHandlerCleans:
    def test_handler_invokes_temp_cleanup_before_exit(self, tmp_path, monkeypatch):
        """SIGTERM handler 恢复目标文件后必须清理临时文件再 os._exit。"""
        exited = {}

        def fake_exit(code):
            exited["code"] = code
            raise _ExitJump(code)

        monkeypatch.setattr(runner_mod.os, "_exit", fake_exit)
        # handler 调用的是 runner 命名空间里的绑定（from .inject import）
        monkeypatch.setattr(runner_mod, "cleanup_temp_files",
                            lambda: exited.setdefault("cleaned", True))
        target = tmp_path / "t.gd"
        target.write_text("mutated")
        handler = runner_mod.make_sigterm_restore_handler(str(target), "original")
        with pytest.raises(_ExitJump):
            handler(signal.SIGTERM, None)
        assert target.read_text() == "original"
        assert exited.get("cleaned") is True, "handler must sweep temp files"
        assert exited["code"] == 128 + signal.SIGTERM


class TestSigtermIntegration:
    def test_sigterm_midrun_leaves_no_temp_files(self, tmp_path):
        """QA 场景复现（密闭，无 godot 依赖）：子进程 run_mutation 在 mutant
        轮被 SIGTERM 中断 → 项目目录内 `.gqt_mutation_*` 临时文件必须清零。"""
        proj, target = _write_project(tmp_path)
        src_root = Path(__file__).parent.parent.parent / "src"
        child_script = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(src_root)!r})
            import godot_qa_toolkit.mutation.runner as R
            state = {{"calls": 0}}
            def fake_gut(root, timeout_s=60, tests_glob=None):
                state["calls"] += 1
                if state["calls"] == 1:
                    return (0, "pass")          # baseline 快速通过
                time.sleep(60)                  # mutant 轮挂起——临时文件在场
                return (0, "pass")
            R._run_gut_on_project = fake_gut
            from godot_qa_toolkit.mutation.runner import run_mutation
            run_mutation({target!r}, {proj!r}, budget=2,
                         tests_glob="res://tests/unit/save/")
        """)
        script_path = Path(proj) / "_sigterm_child.py"
        script_path.write_text(child_script)
        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 等 mutant 轮进入挂起（cfg/host 已落盘）
        deadline = time.time() + 20
        while time.time() < deadline:
            leftovers = list(Path(proj).glob(".gqt_mutation_*"))
            if leftovers:
                break
            if proc.poll() is not None:
                pytest.fail("child exited before mutant round started")
            time.sleep(0.2)
        assert list(Path(proj).glob(".gqt_mutation_*")), \
            "precondition: temp files must exist before SIGTERM"
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("child did not exit after SIGTERM (cleanup hung?)")
        leftovers = list(Path(proj).glob(".gqt_mutation_*"))
        assert not leftovers, f"temp files leaked after SIGTERM: {leftovers}"
