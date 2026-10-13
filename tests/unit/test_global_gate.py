"""SEE-1400 §SPEC-003：跨进程 flock 槽位闸单测。

对抗面：容量 env 解析、槽位互斥（线程级——flock 对同进程不同 fd 同样
互斥，flock(2) 明确该语义）、进程内多次进出、fcntl 缺失降级不阻塞、
_run_gut_on_project 签名不变与闸穿透。
"""

import inspect
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import godot_gate
from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.godot_gate import (
    ENV_GLOBAL_SLOTS,
    GlobalGodotGate,
    reset_warn_state,
    slot_capacity,
    slots_dir,
)


@pytest.fixture(autouse=True)
def _restore_warn_state():
    reset_warn_state()
    yield
    reset_warn_state()


class TestSlotCapacity:
    def test_default_matches_default_jobs_ceiling(self, monkeypatch):
        monkeypatch.delenv(ENV_GLOBAL_SLOTS, raising=False)
        monkeypatch.setattr(godot_gate.os, "cpu_count", lambda: 16)
        assert slot_capacity() == 8

    def test_default_floor_two(self, monkeypatch):
        monkeypatch.delenv(ENV_GLOBAL_SLOTS, raising=False)
        monkeypatch.setattr(godot_gate.os, "cpu_count", lambda: 2)
        assert slot_capacity() == 2

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(ENV_GLOBAL_SLOTS, "3")
        assert slot_capacity() == 3

    def test_env_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv(ENV_GLOBAL_SLOTS, "abc")
        assert slot_capacity() >= 2
        monkeypatch.setenv(ENV_GLOBAL_SLOTS, "0")
        assert slot_capacity() >= 2
        monkeypatch.setenv(ENV_GLOBAL_SLOTS, "-5")
        assert slot_capacity() >= 2


@pytest.mark.skipif(godot_gate.fcntl is None,
                    reason="fcntl unavailable on this platform")
class TestFlockGate:
    def test_acquire_release_cycle(self, tmp_path):
        with GlobalGodotGate(str(tmp_path), capacity=2) as g:
            assert g.held_slot is not None
            lock_files = sorted(slots_dir(str(tmp_path)).glob("slot_*.lock"))
            assert len(lock_files) == g.held_slot + 1
        assert g.held_slot is None

    def test_gitignore_self_contained(self, tmp_path):
        with GlobalGodotGate(str(tmp_path), capacity=1):
            pass
        assert (slots_dir(str(tmp_path)) / ".gitignore").read_text() == "*\n"

    def test_same_capacity_second_gate_waits_for_first(self, tmp_path):
        first = GlobalGodotGate(str(tmp_path), capacity=1)
        first.__enter__()
        assert first.held_slot == 0
        second = GlobalGodotGate(str(tmp_path), capacity=1)
        t0 = time.monotonic()
        released = threading.Event()

        def release_later():
            time.sleep(0.15)
            released.set()
            first.__exit__(None, None, None)

        th = threading.Thread(target=release_later)
        th.start()
        second.__enter__()  # 必须阻塞到 first 释放
        waited = time.monotonic() - t0
        th.join()
        assert released.is_set() and waited >= 0.14
        assert second.held_slot == 0
        second.__exit__(None, None, None)

    def test_capacity_two_allows_two(self, tmp_path):
        g1 = GlobalGodotGate(str(tmp_path), capacity=2)
        g2 = GlobalGodotGate(str(tmp_path), capacity=2)
        g1.__enter__()
        g2.__enter__()  # 非阻塞即得——不被 g1 卡住
        assert g1.held_slot != g2.held_slot
        assert sorted((g1.held_slot, g2.held_slot)) == [0, 1]
        g1.__exit__(None, None, None)
        g2.__exit__(None, None, None)

    def test_process_death_release_semantics_via_close(self, tmp_path):
        """fd 关闭（进程死亡等价物——OS 释放 flock）后继者立即可得槽。"""
        import fcntl
        g = GlobalGodotGate(str(tmp_path), capacity=1)
        g.__enter__()
        g._fd.close()  # 模拟进程死亡：不经过 __exit__，锁必须随 fd 消失
        g._fd = None
        g2 = GlobalGodotGate(str(tmp_path), capacity=1)
        g2.__enter__()
        assert g2.held_slot == 0
        g2.__exit__(None, None, None)


class TestGateDegradation:
    def test_fcntl_missing_degrades_without_blocking(self, tmp_path, capsys,
                                                     monkeypatch):
        monkeypatch.setattr(godot_gate, "fcntl", None)
        with GlobalGodotGate(str(tmp_path), capacity=1) as g:
            assert g.held_slot is None  # 未占槽但放行
        assert "fcntl unavailable" in capsys.readouterr().err

    def test_zero_capacity_is_transparent(self, tmp_path):
        with GlobalGodotGate(str(tmp_path), capacity=0) as g:
            assert g.held_slot is None

    def test_acquire_oserror_degrades_with_single_warning(
            self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(GlobalGodotGate, "_acquire",
                            lambda self: (_ for _ in ()).throw(
                                OSError("no fs locks")))
        with GlobalGodotGate(str(tmp_path), capacity=1) as g:
            assert g.held_slot is None
        with GlobalGodotGate(str(tmp_path), capacity=1):
            pass
        err = capsys.readouterr().err
        assert err.count("godot gate acquire failed") == 1  # 每进程一次


@pytest.mark.skipif(godot_gate.fcntl is None,
                    reason="fcntl unavailable on this platform")
class TestGateInternals:
    def test_exit_survives_flock_un_oserror(self, tmp_path, monkeypatch):
        import fcntl as real_fcntl
        g = GlobalGodotGate(str(tmp_path), capacity=1)
        g.__enter__()
        calls = {"n": 0}

        def flaky_flock(fd, op):
            calls["n"] += 1
            if op == real_fcntl.LOCK_UN:
                raise OSError("unlock raced")
            return real_fcntl.flock(fd, op)

        monkeypatch.setattr(godot_gate.fcntl, "flock", flaky_flock)
        g.__exit__(None, None, None)  # 不抛出；fd.close() 兜底释放
        assert g.held_slot is None
        assert calls["n"] == 1

    def test_mkdir_oserror_is_hygiene_only(self, tmp_path, monkeypatch):
        d = slots_dir(str(tmp_path))
        d.mkdir(parents=True)
        (d / "slot_0.lock").touch()
        monkeypatch.setattr(Path, "mkdir",
                            lambda self, *a, **k: (_ for _ in ()).throw(
                                OSError("ro fs")))
        # 槽位目录已存在时 mkdir 失败只是卫生项——照常占槽
        with GlobalGodotGate(str(tmp_path), capacity=1) as g:
            assert g.held_slot == 0

    def test_all_slots_unopenable_degrades_not_blocks(
            self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(GlobalGodotGate, "_open_slot",
                            staticmethod(lambda d, i: (_ for _ in ()).throw(
                                OSError("fs gone"))))
        with GlobalGodotGate(str(tmp_path), capacity=2) as g:
            assert g.held_slot is None  # 降级放行而非死等
        assert "godot gate acquire failed" in capsys.readouterr().err

    def test_open_slot_oserror_skips_to_next(self, tmp_path, monkeypatch):
        real_open = GlobalGodotGate._open_slot

        def flaky_open(d, i):
            if i == 0:
                raise OSError("slot0 transiently unopenable")
            return real_open(d, i)

        monkeypatch.setattr(GlobalGodotGate, "_open_slot",
                            staticmethod(flaky_open))
        with GlobalGodotGate(str(tmp_path), capacity=2) as g:
            assert g.held_slot == 1


class TestGateAtGutExit:
    """真实 _run_gut_on_project 必须经闸（签名不变、3 参契约不变）。"""

    def _write_project(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        return proj

    def test_run_gut_acquires_and_releases_slot(self, tmp_path, monkeypatch):
        proj = self._write_project(tmp_path)
        observed = {"slots": []}
        orig_enter = GlobalGodotGate.__enter__

        def spy_enter(self):
            orig_enter(self)
            observed["slots"].append(self.held_slot)
            return self

        class FakeProc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        monkeypatch.setattr(GlobalGodotGate, "__enter__", spy_enter)
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeProc())
        rc, tail = mrunner._run_gut_on_project(str(proj))
        assert rc == 0
        assert observed["slots"]  # 确实过了闸
        # 释放：再开一槽必须立刻可得（不留陈锁）
        g = GlobalGodotGate(str(proj))
        g.__enter__()
        assert g.held_slot is not None
        g.__exit__(None, None, None)

    def test_signature_unchanged(self):
        sig = inspect.signature(mrunner._run_gut_on_project)
        assert list(sig.parameters) == ["project_root", "timeout_s",
                                        "tests_glob"]


# ---------------------------------------------------------------------------
# SEE-1400 hardener 对抗注入面 3：槽位饥饿——slots=1 下多 worker 真实并发
# 峰值必须探测为 1（不是「看起来对」，是跨进程探测到的峰值）；等槽时间
# 不得进入 timeout 语义
# ---------------------------------------------------------------------------


@pytest.mark.skipif(godot_gate.fcntl is None,
                    reason="fcntl unavailable on this platform")
class TestSlotStarvationHardening:
    def test_cross_process_peak_concurrency_is_one(self, tmp_path):
        """两个真实子进程各持 gate 睡眠：父进程高频探测槽位锁，探测期内
        每次锁探测只应见到「忙」（1 个持有者）——flock 互斥下峰值 >1 在
        slots=1 时物理不可能，此处钉死的是闸确实被子进程真实使用而非
        仅在测试进程内生效。"""
        import fcntl
        ready = tmp_path / "probe_ready.flag"
        # 启动栅栏（CI 时序缺陷修复）：子进程抢到锁后写 ready 标记——父进程
        # 见标记才开始探测窗口，冷启动（CI 首跑解释器/imports 慢 ~1s）不再
        # 错过持锁窗口。持锁窗 2s，探测对齐后必然落在窗口内。
        child_src = (
            "import os, sys, time\n"
            f"sys.path.insert(0, {str(Path(__file__).parent.parent.parent / 'src')!r})\n"
            "from godot_qa_toolkit.mutation.godot_gate import GlobalGodotGate\n"
            "with GlobalGodotGate(os.environ['PROBE_ROOT'], capacity=1):\n"
            f"    open({str(ready)!r}, 'a').close()\n"
            "    time.sleep(2.0)\n"
        )
        (tmp_path / "child.py").write_text(child_src)
        env = dict(os.environ, PROBE_ROOT=str(tmp_path))
        lock_path = slots_dir(str(tmp_path)) / "slot_0.lock"
        # 先让两个子进程都启动（各自 __enter__ 会建槽位目录）
        p1 = subprocess.Popen([sys.executable, str(tmp_path / "child.py")],
                              env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        p2 = subprocess.Popen([sys.executable, str(tmp_path / "child.py")],
                              env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        try:
            # 栅栏：等任一子进程真实持锁（ready 标记出现），才开探测窗。
            # 超时上限远大于冷启动开销；等待期子进程锁已持有，不算探测样本。
            fence_deadline = time.monotonic() + 15.0
            while time.monotonic() < fence_deadline:
                if ready.exists():
                    break
                if p1.poll() is not None and p2.poll() is not None:
                    break  # 双双早退（异常）——交由下方 rc 断言显式暴露
                time.sleep(0.02)
            else:
                raise AssertionError(
                    "probe fence timeout: no child acquired the gate within "
                    "15s (startup barrier never saw the ready flag)")
            busy = acquired = 0
            # 探测窗覆盖栅栏后剩余持锁时间（2s）；窗口开始时锁必然被持有
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline and (p1.poll() is None
                                                   or p2.poll() is None):
                if not lock_path.exists():
                    time.sleep(0.02)
                    continue
                try:
                    fd = open(lock_path, "a+b")
                except OSError:
                    time.sleep(0.02)
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired += 1
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    busy += 1  # 锁被某个子进程持有（互斥下至多 1 个）
                finally:
                    fd.close()
                time.sleep(0.01)
            assert (p1.wait(), p2.wait()) == (0, 0)
            # 栅栏保证探测窗起于持锁中：busy 必然 > 0（若闸没在子进程内
            # 生效，ready 永不出现，栅栏断言已先失败——本断言是冗余保险）
            assert busy > 0, "探测期内未观察到任何子进程持锁——闸未生效"
            # 我们自己从父进程能在窗口内抢到锁，说明任一时刻至多一个持有者
            assert acquired > 0
        finally:
            for p in (p1, p2):
                if p.poll() is None:
                    p.kill()
            for p in (p1, p2):
                p.wait()

    def test_wait_time_not_counted_into_timeout_argument(self, tmp_path):
        """等槽发生在 _run_gut_on_project 的 with 行——timeout 参数原样传给
        subprocess.run，闸不修改它。钉住 signature 语义：gate 层无 timeout
        参数。"""
        import inspect
        sig = inspect.signature(mrunner._run_gut_on_project)
        assert "timeout_s" in sig.parameters
        gate_sig = inspect.signature(godot_gate.GlobalGodotGate.__init__)
        assert "timeout" not in gate_sig.parameters
