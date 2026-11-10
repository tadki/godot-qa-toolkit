"""SEE-1550 §SPEC-015 —— `.godot` 缺导入缓存前置守卫单测（preimport_guard）。

真实 Godot 项目（project.godot 在场）缺 `.godot` 或缺导入产物时，首个 GUT
调用点必须 fail-fast（明确报错 + 修复指令），绝不放行进逐位点 600s timeout
级联（Revy O-1 实证）。无 project.godot 的玩具工程不受影响（既有替身测试
形态零变化）。
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import shadow
from godot_qa_toolkit.mutation.shadow import SlotProvisionError
from godot_qa_toolkit.mutation import runner as mrunner


def _real_project(tmp_path, godot_dir=False, artifacts=False):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    (proj / "project.godot").write_text("config_version=5\n")
    if godot_dir:
        g = proj / ".godot"
        g.mkdir()
        if artifacts:
            (g / "uid_cache.bin").write_bytes(b"x")
            (g / "imported").mkdir()
    return proj


class TestRequireImportCache:
    def test_missing_godot_dir_fails_fast_with_fix_hint(self, tmp_path):
        proj = _real_project(tmp_path, godot_dir=False)
        with pytest.raises(SlotProvisionError) as ei:
            shadow.require_import_cache(str(proj))
        msg = str(ei.value)
        assert "missing" in msg
        assert "--import" in msg, "报错必须带机器可执行修复指令"
        assert str(proj) in msg, "报错必须含项目根上下文"

    def test_godot_dir_without_artifacts_fails_fast(self, tmp_path):
        """`.godot` 在场但零导入产物 = 未导入 → 同样 fail-fast。"""
        proj = _real_project(tmp_path, godot_dir=True, artifacts=False)
        with pytest.raises(SlotProvisionError) as ei:
            shadow.require_import_cache(str(proj))
        assert "not imported" in str(ei.value)

    def test_uid_cache_artifact_passes(self, tmp_path):
        proj = _real_project(tmp_path, godot_dir=True, artifacts=True)
        shadow.require_import_cache(str(proj))  # 不抛

    def test_imported_dir_alone_passes(self, tmp_path):
        proj = _real_project(tmp_path, godot_dir=True, artifacts=False)
        (proj / ".godot" / "imported").mkdir()
        shadow.require_import_cache(str(proj))  # 不抛

    def test_toy_project_without_project_godot_is_noop(self, tmp_path):
        """无 project.godot 的玩具工程（既有替身测试形态）不设门。"""
        toy = tmp_path / "toy"
        toy.mkdir()
        shadow.require_import_cache(str(toy))  # 不抛


class TestGutSeamWiring:
    def test_gut_on_project_fires_guard_before_subprocess(self, tmp_path,
                                                          monkeypatch):
        """接线实证：守卫在 `_run_gut_on_project` 首行触发——缺导入缓存时
        subprocess 绝不被拉起（那正是 600s 级联的入口）。"""
        proj = _real_project(tmp_path, godot_dir=False)

        def _boom(*a, **k):
            raise AssertionError("subprocess must not launch past the guard")

        monkeypatch.setattr(mrunner.subprocess, "run", _boom)
        with pytest.raises(SlotProvisionError):
            mrunner._run_gut_on_project(str(proj), timeout_s=60)

    def test_baseline_loop_translates_guard_to_run_error_contract(
            self, tmp_path, monkeypatch):
        """端到端契约：缺导入缓存的 run 以 run_error JSON 收场（中止契约），
        绝不进 mutant 轮。"""
        proj = _real_project(tmp_path, godot_dir=False)
        target = proj / "calc.gd"
        target.write_text("func add(a, b):\n\treturn a + b\n")

        def _no_gut(project_root, timeout_s=60, tests_glob=None):
            mrunner.shadow.require_import_cache(project_root)
            return 0, "pass [passed]"

        monkeypatch.setattr(mrunner, "_run_gut_on_project", _no_gut)
        result = mrunner.run_mutation(str(target), str(proj), budget=2)
        s = result["summary"]
        assert s.get("run_error") is True
        assert "--import" in (s.get("error", "") + " ".join(
            f.get("reason", "") for f in result["failures"]))
