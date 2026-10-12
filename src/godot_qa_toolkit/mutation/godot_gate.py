"""SEE-1400 §SPEC-003 — 跨进程全局 godot 子进程并发闸。

根因（SEE-1397 实测）：worker 池是 per-process 的，操作者并行跑两个 gqt
进程 = 2×worker 个 Godot headless 实例互抢核，单轮时间远超基线。

形态：flock 槽位文件 ``<project>/.qa-cache/gqt-godot-slots/slot_<i>.lock``。
- 容量 env ``GQT_GLOBAL_GODOT_SLOTS``，缺省 = max(2, cpu//2)（与 default_jobs
  上限一致——单进程默认并发恰好占满缺省槽位，零行为差）。
- flock 是进程死亡自动释放的 advisory 锁：无陈锁、无死锁面。
- fcntl 不可用平台（Windows 原生）：告警一次、不阻塞地放行（WSL-first 现实
  下的诚实降级，与仓内既有降级先例一致）。
- 等槽时间不计入 GUT timeout——timeout 语义只管测试进程本身（契约 §6 不变）。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - 原生 Windows 等：降级放行
    fcntl = None

ENV_GLOBAL_SLOTS = "GQT_GLOBAL_GODOT_SLOTS"

# 全忙时的轮询间隔（秒）：等待方 60-180s 量级的 run 面前，50ms 轮询开销可忽略，
# 换取「任一槽释放即进」而不 convoy 在单槽上。
_POLL_INTERVAL_S = 0.05

_warned_no_fcntl = False


def slot_capacity() -> int:
    """槽位容量：env 可调，缺省 = max(2, cpu//2)；非法 env 回落缺省。"""
    default = max(2, (os.cpu_count() or 2) // 2)
    raw = os.environ.get(ENV_GLOBAL_SLOTS, "")
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if v > 0 else default


def slots_dir(project_root: str) -> Path:
    return Path(project_root) / ".qa-cache" / "gqt-godot-slots"


class GlobalGodotGate:
    """with 语义的跨进程槽位闸：进入占一槽，退出释放；flock 缺失则透明放行。"""

    def __init__(self, project_root: str, capacity: int | None = None):
        self.project_root = project_root
        self.capacity = slot_capacity() if capacity is None else int(capacity)
        self._fd = None
        self._slot: int | None = None

    def __enter__(self) -> "GlobalGodotGate":
        global _warned_no_fcntl
        if fcntl is None:
            if not _warned_no_fcntl:
                _warned_no_fcntl = True
                print("godot gate: fcntl unavailable — running WITHOUT the "
                      "cross-process slot limit (GQT_GLOBAL_GODOT_SLOTS "
                      "inactive on this platform)", file=sys.stderr)
            return self
        if self.capacity <= 0:
            return self
        try:
            self._fd, self._slot = self._acquire()
        except OSError as e:
            # 槽位目录建不出/锁失败是环境问题，不是被测对象的失败——
            # 告警一次放行（与 fcntl 缺失同档降级，不阻塞 run）。
            global_warn(f"godot gate acquire failed ({e}) — running without "
                        f"the cross-process slot limit")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._fd is not None and fcntl is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass  # fd.close() 兜底释放（进程内兜底；进程死亡由 OS 兜底）
            finally:
                self._fd.close()
                self._fd = None
                self._slot = None
        return False

    @property
    def held_slot(self) -> int | None:
        return self._slot

    def _acquire(self) -> tuple:
        d = slots_dir(self.project_root)
        try:
            d.mkdir(parents=True, exist_ok=True)
            gitignore = d / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text("*\n", encoding="utf-8")
        except OSError:
            pass  # .gitignore 是卫生项；建不出锁文件时 _open_slot 的 OSError 上抛
        # 快速遍历：任一空槽非阻塞抢占。全部 open 失败 = 环境坏（如只读 fs），
        # 上抛给 __enter__ 降级放行——闸的职责是限流，绝不能变成无限阻塞点。
        while True:
            opened_any = False
            for i in range(self.capacity):
                try:
                    fd = self._open_slot(d, i)
                except OSError:
                    continue
                opened_any = True
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd, i
                except OSError:
                    fd.close()
            if not opened_any:
                raise OSError(
                    f"cannot open any godot slot file under {d}")
            time.sleep(_POLL_INTERVAL_S)

    @staticmethod
    def _open_slot(d: Path, i: int):
        return open(d / f"slot_{i}.lock", "a+b")


def global_warn(message: str) -> None:
    """一次性（每进程）闸降级告警——run 级重复告警只会淹没 stderr。"""
    global _warned_no_fcntl
    if not _warned_no_fcntl:
        _warned_no_fcntl = True
        print(message, file=sys.stderr)


def reset_warn_state() -> None:
    """测试钩子：恢复首告警状态（仅单测使用）。"""
    global _warned_no_fcntl
    _warned_no_fcntl = False
