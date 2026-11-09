"""SEE-1321 §SPEC-015 → SEE-1550 磁盘影子注入的中断清理契约重钉。

旧机制（impl/cfg/host 三类 `.gqt_mutation_*` 落真实仓工作树）已退役：
- beacon cfg 现落影子槽内（linux 通道 = /tmp 农场；win64 通道 =
  `.qa-cache` 池，自忽略），SIGTERM 后 `.qa-cache` 外零残留；
- per-process 临时文件登记表 + atexit 兜底保留（beacon cfg / beacon 宿主
  .gd 经 register_temp 覆盖 SIGINT/未知退出路径）；
- SIGKILL 残留 mutant 的修正由槽侧定向三向承接
  （test_mutation_interrupt.py 的 SIGKILL 格）。
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
    def test_beacon_cfg_registered_and_cleaned(self, tmp_path):
        """SEE-1550：beacon cfg（影子槽内临时文件）必须可入 per-process 登记
        表——SIGTERM/atexit 兜底覆盖（impl 文件已随内存注入机制退役；
        runner 在 mutant 轮 dispatch 时 register_temp cfg）。"""
        proj, _target = _write_project(tmp_path)
        slot_root = Path(proj) / ".qa-cache" / "mutation-pool" / "slots" / "s"
        slot_root.mkdir(parents=True)
        from godot_qa_toolkit.mutation import beacon
        cfg = beacon.write_beacon_cfg(str(slot_root), "res://calc.gd",
                                      "deadbeef", "")
        inject_mod.register_temp(str(cfg))
        # 登记在 dispatch 时即发生（即使 Godot 侧尚未读 cfg）
        assert str(cfg) in inject_mod._TEMP_FILES
        cleanup_temp_files()
        assert not cfg.exists()

    def test_cleanup_removes_registered_files(self, tmp_path):
        f1 = tmp_path / ".gqt_beacon_host_999.gd"
        f2 = tmp_path / ".gqt_beacon_cfg_999.json"
        f1.write_text("x")
        f2.write_text("{}")
        register_temp(str(f1))
        register_temp(str(f2))
        cleanup_temp_files()
        assert not f1.exists() and not f2.exists()

    def test_cleanup_survives_missing_files(self, tmp_path):
        f1 = tmp_path / ".gqt_beacon_host_998.gd"
        f1.write_text("x")
        register_temp(str(f1))
        f1.unlink()
        cleanup_temp_files()  # 已删文件必须幂等，不得抛

    def test_cleanup_survives_permission_error(self, tmp_path, monkeypatch):
        f1 = tmp_path / ".gqt_beacon_host_997.gd"
        f1.write_text("x")
        f2 = tmp_path / ".gqt_beacon_cfg_997.json"
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


class TestSigtermIntegration:
    def test_sigterm_midrun_leaves_no_residue_outside_qa_cache(self, tmp_path):
        """QA 场景复现（密闭，无 godot 依赖）：子进程 run_mutation 在 mutant
        轮被 SIGTERM 中断 → `.qa-cache` 外零 `.gqt_*` 残留（cfg 现落影子槽
        内），真实仓目标文件保持原码。影子槽本身在 win64 通道位于
        `.qa-cache`（自忽略）、linux 通道位于 /tmp——两者都不污染 git 面。"""
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
                    return (0, "pass [passed]")   # baseline 快速通过
                time.sleep(60)                    # mutant 轮挂起——cfg 已落影子槽
                return (0, "pass [passed]")
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
        # 等 mutant 轮进入挂起（beacon cfg 已落影子槽——win64 通道在
        # .qa-cache 池内，linux 通道在 /tmp 农场；两者都可能有）
        deadline = time.time() + 30
        found = []
        while time.time() < deadline:
            found = list((Path(proj) / ".qa-cache").rglob(".gqt_beacon_cfg_*")) \
                + list(Path(tempdir()).glob(f"gqt-mutation-farm/*/*/.gqt_beacon_cfg_*")) \
                + list(Path(tempdir()).glob("gqt-slot-*/**/.gqt_beacon_cfg_*"))
            if found:
                break
            if proc.poll() is not None:
                pytest.fail("child exited before mutant round started")
            time.sleep(0.2)
        assert found, "precondition: beacon cfg must exist before SIGTERM"
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("child did not exit after SIGTERM (cleanup hung?)")
        outside = [p for p in Path(proj).rglob(".gqt_*")
                   if ".qa-cache" not in p.parts]
        assert not outside, f"residue outside .qa-cache after SIGTERM: {outside}"
        assert Path(target).read_text() == "func add(a, b):\n\treturn a + b\n"


def tempdir():
    import tempfile
    return tempfile.gettempdir()
