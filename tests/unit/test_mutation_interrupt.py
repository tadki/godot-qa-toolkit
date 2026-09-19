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


class TestInterruptGuardHardening:
    """SEE-1319 hardener：_interrupt_guard / make_sigterm_restore_handler 对抗性补强。

    以"默认守卫是错的"预设覆盖：降级路径、恢复失败暴露、中止契约经守卫
    恢复、信号 handler 还原。
    """

    def _write_project(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "calc.gd"
        target.write_text("func add(a, b):\n\treturn a + b\n")
        return proj, target

    def test_signal_registration_failure_degrades_to_finally_only(self, tmp_path, monkeypatch):
        # signal.signal 抛 ValueError（非主线程典型）→ 守卫不得崩，finally 兜底仍须恢复。
        proj, target = self._write_project(tmp_path)
        before = target.read_text()

        def boom(sig, handler):
            raise ValueError("signal only works in main thread")

        monkeypatch.setattr(mrunner.signal, "signal", boom)
        with mrunner._interrupt_guard(target, before):
            target.write_text("mutated")
        assert target.read_text() == before

    def test_exit_restore_failure_prints_to_stderr_and_propagates(self, tmp_path, monkeypatch, capsys):
        # __exit__ 恢复写盘失败（如目录只读）→ stderr 显式暴露，异常原样上抛。
        proj, target = self._write_project(tmp_path)
        before = target.read_text()

        def readonly_write(_path, _text, **kw):
            raise OSError("disk full / readonly fs")

        monkeypatch.setattr(mrunner.Path, "write_text", readonly_write)
        with pytest.raises(RuntimeError, match="simulated"):
            with mrunner._interrupt_guard(target, before):
                raise RuntimeError("simulated mid-loop crash")
        captured = capsys.readouterr()
        assert "restore failed" in captured.err

    def test_sigterm_handler_restore_failure_prints_stderr_before_exit(self, tmp_path, monkeypatch, capsys):
        # handler 恢复写盘失败 → stderr 暴露后仍 os._exit（不能静默吞成遗留变异）。
        target = tmp_path / "calc.gd"
        before = "func add(a, b):\n\treturn a + b\n"
        target.write_text("mutated")

        def readonly_write(_path, _text, **kw):
            raise OSError("readonly fs")

        monkeypatch.setattr(mrunner.Path, "write_text", readonly_write)
        monkeypatch.setattr(mrunner.os, "_exit", lambda code: None)
        handler = mrunner.make_sigterm_restore_handler(str(target), before)
        handler(signal.SIGTERM, None)  # os._exit stubbed → 可直接调用断言
        captured = capsys.readouterr()
        assert "SIGTERM restore failed" in captured.err

    def test_baseline_timeout_abort_contract_still_restores_file(self, tmp_path, monkeypatch):
        # baseline TimeoutExpired → 中止契约 JSON 返回；守卫必须已恢复原文件。
        proj, target = self._write_project(tmp_path)
        before = target.read_text()

        def timeout_run(root, timeout_s=60, tests_glob=None):
            import subprocess
            raise subprocess.TimeoutExpired(cmd="godot", timeout=timeout_s)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", timeout_run)
        result = run_mutation(str(target), str(proj), budget=2, timeout_s=60)
        assert result["summary"]["run_error"] is True
        assert target.read_text() == before, "中止契约路径也必须恢复原文件"

    def test_baseline_launch_failure_abort_contract_still_restores_file(self, tmp_path, monkeypatch):
        # _BaselineLaunchFailure → 中止契约 JSON；守卫必须已恢复原文件。
        proj, target = self._write_project(tmp_path)
        before = target.read_text()
        monkeypatch.setattr(
            mrunner, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None:
                (1, "ERROR: Attempt to open script 'res://addons/gut/gut_cmdln.gd' "
                    "resulted in error 'File not found'"),
        )
        result = run_mutation(str(target), str(proj), budget=2)
        assert result["summary"]["run_error"] is True
        assert target.read_text() == before

    def test_previous_signal_handlers_reinstated_after_exit(self, tmp_path):
        # __exit__ 必须还原进入前的 signal handler（不得污染进程级状态）。
        proj, target = self._write_project(tmp_path)
        before = target.read_text()
        sentinel = object()

        def prev_handler(signum, frame):
            pass

        orig = {}
        for sig in (signal.SIGTERM, signal.SIGHUP):
            orig[sig] = signal.getsignal(sig)
            signal.signal(sig, prev_handler)
        try:
            with mrunner._interrupt_guard(target, before):
                assert signal.getsignal(signal.SIGTERM) is not prev_handler
            assert signal.getsignal(signal.SIGTERM) is prev_handler, "必须还原原 handler"
            assert signal.getsignal(signal.SIGHUP) is prev_handler
        finally:
            for sig, h in orig.items():
                signal.signal(sig, h)

    def test_nested_exception_inside_guard_body_restores(self, tmp_path):
        # 嵌套异常（守卫体内层 try/except 后又抛新异常）→ 兜底仍恢复 + 新异常上抛。
        proj, target = self._write_project(tmp_path)
        before = target.read_text()
        with pytest.raises(ValueError, match="outer"):
            with mrunner._interrupt_guard(target, before):
                try:
                    raise RuntimeError("inner")
                except RuntimeError:
                    target.write_text("mutated-mid")
                    raise ValueError("outer")
        assert target.read_text() == before
