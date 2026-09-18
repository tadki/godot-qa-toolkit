"""SEE-1319 缺陷1：mutation 外层中断恢复守卫单元测试。

缺口实证（Revy QA + Atlas 复核）：外部 kill -TERM / TaskStop 硬杀进程时
_run_single_mutant 的 finally 不执行，工作树遗留活跃变异体。守卫 =
run_mutation 层 try/except BaseException + SIGTERM handler 主动恢复。
"""

import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.runner import run_mutation


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text("func add(a, b):\n\treturn a + b\n")
    return proj, target


def test_run_mutation_restores_on_exception_after_write(tmp_path, monkeypatch):
    """异常发生在变异体已写入之后（分类阶段抛错）→ 外层兜底必须恢复原文件。"""
    proj, target = _write_project(tmp_path)
    before = target.read_text()

    def crash_classify(m, rc, tail, baseline_failures, baseline_dirty):
        raise RuntimeError("simulated SIGTERM mid-loop after mutant write")

    calls = {"n": 0}

    def fake_run(root, timeout_s=60, tests_glob=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (0, "baseline pass")
        return (0, "mutant run")

    monkeypatch.setattr(mrunner, "_run_gut_on_project", fake_run)
    monkeypatch.setattr(mrunner, "_classify_mutant_run", crash_classify)
    with pytest.raises(RuntimeError):
        run_mutation(str(target), str(proj), budget=2)
    assert target.read_text() == before, "外层异常路径必须恢复原文件，不得遗留变异体"


def test_run_mutation_restores_on_keyboard_interrupt(tmp_path, monkeypatch):
    proj, target = _write_project(tmp_path)
    before = target.read_text()

    def crash_classify(m, rc, tail, baseline_failures, baseline_dirty):
        raise KeyboardInterrupt

    calls = {"n": 0}

    def fake_run(root, timeout_s=60, tests_glob=None):
        calls["n"] += 1
        return (0, "pass")

    monkeypatch.setattr(mrunner, "_run_gut_on_project", fake_run)
    monkeypatch.setattr(mrunner, "_classify_mutant_run", crash_classify)
    with pytest.raises(KeyboardInterrupt):
        run_mutation(str(target), str(proj), budget=2)
    assert target.read_text() == before


def test_sigterm_handler_restores_backup_and_exits(tmp_path, monkeypatch):
    """SIGTERM 硬杀路径：handler 必须先恢复备份再终止（finally 不保证执行）。"""
    proj, target = _write_project(tmp_path)
    before = target.read_text()
    target.write_text("func add(a, b):\n\treturn a + 0\n")  # 模拟变异体遗留态

    exited = {}

    def fake_exit(code):
        exited["code"] = code
        raise SystemExit(code)

    monkeypatch.setattr(mrunner.os, "_exit", fake_exit)
    handler = mrunner.make_sigterm_restore_handler(str(target), before)
    with pytest.raises(SystemExit):
        handler(signal.SIGTERM, None)
    assert target.read_text() == before, "SIGTERM handler 必须恢复原文件"
    assert exited["code"] == 128 + signal.SIGTERM
