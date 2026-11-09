"""SEE-1319 缺陷1 → SEE-1550 磁盘影子注入的中断语义重钉。

旧机制（内存注入 + 真实仓写回守卫）已退役：磁盘影子架构下 mutant 只写
影子槽内目标文件，真实仓目标文件零写入——中断/硬杀不可能把变异体遗留进
git 工作树。本文件钉住新语义：
1. run_mutation 任意异常路径（分类崩溃 / KeyboardInterrupt / baseline 中止
   契约）→ 真实仓目标文件 == 原码（从未被写）；
2. SIGKILL 等价残留（mutant 留在槽内、进程消失）→ 下一次 acquire_slot 的
   定向三向捕获 inode 漂移并重链仓原版（W 矩阵 SIGKILL 格）；
3. 槽归还断言失败 → stderr 显式暴露且槽标死（不静默）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation import shadow, shadowpool
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
    """异常发生在变异体已注入影子之后（分类阶段抛错）→ 真实仓文件必须
    保持原码（mutant 只存在于影子槽，finally release 必然归还）。"""
    proj, target = _write_project(tmp_path)
    before = target.read_text()

    def crash_classify(m, rc, tail, baseline_failures, baseline_dirty):
        raise RuntimeError("simulated SIGTERM mid-loop after mutant write")

    calls = {"n": 0}

    def fake_run(root, timeout_s=60, tests_glob=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return (0, "baseline pass [passed]")
        return (0, "mutant run [passed]")

    monkeypatch.setattr(mrunner, "_run_gut_on_project", fake_run)
    monkeypatch.setattr(mrunner, "_classify_mutant_run", crash_classify)
    with pytest.raises(RuntimeError):
        run_mutation(str(target), str(proj), budget=2)
    assert target.read_text() == before, "异常路径真实仓不得遗留变异体"


def test_run_mutation_restores_on_keyboard_interrupt(tmp_path, monkeypatch):
    proj, target = _write_project(tmp_path)
    before = target.read_text()

    def crash_classify(m, rc, tail, baseline_failures, baseline_dirty):
        raise KeyboardInterrupt

    def fake_run(root, timeout_s=60, tests_glob=None):
        return (0, "pass [passed]")

    monkeypatch.setattr(mrunner, "_run_gut_on_project", fake_run)
    monkeypatch.setattr(mrunner, "_classify_mutant_run", crash_classify)
    with pytest.raises(KeyboardInterrupt):
        run_mutation(str(target), str(proj), budget=2)
    assert target.read_text() == before


def test_sigkill_residual_mutant_relinked_on_next_acquire(tmp_path, monkeypatch):
    """SIGKILL 等价残留（warm 池通道）：mutant 留在槽内、持有进程消失
    （flock 自动释放）→ 下一次 acquire 的定向三向捕获 inode 漂移并重链
    仓原版（W 矩阵 SIGKILL 格）。linux fresh 农场每 run 新建、无残留面，
    本格钉的是 warm 池复用路径。"""
    proj, target = _write_project(tmp_path)
    target_rel = "calc.gd"
    slot = shadowpool.acquire_slot(str(proj), "win64", [target_rel],
                                   wiring_hash="w1")
    # 模拟 worker 在 mutant 注入后被 SIGKILL：槽内目标被 mutant 覆盖、
    # 不经过 release_slot（锁句柄直接遗弃 → fd 关闭模拟进程死亡）。
    shadow.inject_mutant(slot.root, target_rel,
                         "func add(a, b):\n\treturn a - b\n")
    if slot._slot_lock_fd is not None:
        import fcntl, os
        fcntl.flock(slot._slot_lock_fd, fcntl.LOCK_UN)
        os.close(slot._slot_lock_fd)
        slot._slot_lock_fd = None
    slot.released = True  # 阻止测试句柄再次释放

    slot2 = shadowpool.acquire_slot(str(proj), "win64", [target_rel],
                                    wiring_hash="w1")
    try:
        assert "residual-mutant" in " ".join(slot2.freshness) or \
            "inode-drift" in " ".join(slot2.freshness), \
            f"freshness must disclose the SIGKILL residual: {slot2.freshness}"
        disk = (Path(slot2.root) / target_rel).read_text()
        assert disk == target.read_text(), "定向三向必须重链仓原版"
    finally:
        shadowpool.release_slot(slot2)
    assert target.read_text() == "func add(a, b):\n\treturn a + b\n"


class TestSlotReturnGuarantees:
    """SEE-1550 hardener：槽归还断言 / 中止契约 / 真实仓零残留。"""

    def test_release_assert_failure_prints_stderr_and_marks_slot_dead(
            self, tmp_path, monkeypatch, capsys):
        proj, target = _write_project(tmp_path)
        target_rel = "calc.gd"
        slot = shadowpool.acquire_slot(str(proj), "linux", [target_rel],
                                       wiring_hash="w1")

        def broken_restore(project_root, slot_root, rel):
            raise shadow.SlotProvisionError(
                f"slot return assert failed: {rel} differs after restore")

        # 归还断言失败注入点 = restore_target 本体（gqt 写纪律是「临时文件
        # + os.replace」原子替换——就地写穿透 hardlink 共享 inode 的模拟
        # 会连真实仓一起改写，不属于 gqt 可能产生的失败面）。
        monkeypatch.setattr(shadow, "restore_target", broken_restore)
        problems = shadowpool.release_slot(slot)
        captured = capsys.readouterr()
        assert problems, "归还断言失败必须显式返回问题"
        assert "FAILED" in captured.err, "stderr 必须显式暴露归还失败"
        # warm 记录标死：path 清空 → 下次获取按 missing tree 重建（linux
        # fresh 通道无池记录，直接断言真实仓未被污染）
        assert target.read_text() == "func add(a, b):\n\treturn a + b\n"

    def test_baseline_timeout_abort_contract_leaves_repo_pristine(
            self, tmp_path, monkeypatch):
        # baseline TimeoutExpired → 中止契约 JSON 返回；真实仓文件保持原码。
        proj, target = _write_project(tmp_path)
        before = target.read_text()

        def timeout_run(root, timeout_s=60, tests_glob=None):
            import subprocess
            raise subprocess.TimeoutExpired(cmd="godot", timeout=timeout_s)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", timeout_run)
        result = run_mutation(str(target), str(proj), budget=2, timeout_s=60)
        assert result["summary"]["run_error"] is True
        assert target.read_text() == before, "中止契约路径真实仓必须零写入"

    def test_baseline_launch_failure_abort_contract_leaves_repo_pristine(
            self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
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

    def test_no_mutation_residue_outside_qa_cache(self, tmp_path, monkeypatch):
        """W 矩阵门：`.qa-cache` 外零残留——run 后真实仓新增文件只允许在
        `.qa-cache` 内（影子槽在 linux 通道位于 /tmp，不落仓内）。"""
        proj, target = _write_project(tmp_path)
        before = {p.name for p in proj.iterdir()}
        monkeypatch.setattr(
            mrunner, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass [passed]"))
        run_mutation(str(target), str(proj), budget=2)
        after = {p.name for p in proj.iterdir()}
        new_names = after - before
        assert all(n == ".qa-cache" for n in new_names), \
            f"only .qa-cache may appear: {new_names}"
