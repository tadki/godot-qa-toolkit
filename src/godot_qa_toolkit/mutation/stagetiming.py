"""SEE-1564 §SPEC-202 分段计时仪表（stdout 单 JSON 只加不改）。

阶段清单（STAGE_KEYS）：test_scan / prewarm / baseline / per_mutant /
worker_boot / slot_sync / prefilter / total——同一阶段多次发生（多分片/
多文件）按求和累计，披露值为累计墙钟秒（round 3）。

披露层级与语义：
- 单位级（run_mutation 的 summary.stage_timings）：该单元亲自执行的阶段
  （prewarm 只在并行父层发生，单元恒 0.0）；baseline = 本 run 消费的
  baseline GUT 耗时（含复用父层快照的 elapsed——占比归因口径）。
- 运行级（run_mutation_files 信封 stage_timings）：父层编排阶段
  （test_scan/prewarm/baseline/prefilter/total）+ 各分片执行阶段
  （per_mutant/worker_boot/slot_sync）求和并入。
- rss_peak_mb：本进程 VmHWM 高水位（Linux /proc，kB→MB；不可得 = 0.0
  诚实缺省）——godot 子进程内存不计入（QA 战役由 bench 侧采样补齐）。
- disk_delta_bytes：.qa-cache + ext4 影子农场树字节数 before/after；
  仅运行级信封与单文件 summary 提供（并行分片各自测量会重复走树且
  语义归 run——单文件投影见 executor 侧注入）。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path

STAGE_KEYS = ("test_scan", "prewarm", "baseline", "per_mutant",
              "worker_boot", "slot_sync", "prefilter", "total")

# 分片执行阶段——分片 summary 携带、运行级并入的唯一键集（编排阶段父层
# 已自测：单元同名值语义不同，如单元 baseline=快照耗时 vs 父层=预跑墙钟，
# 并入会双计）。
EXEC_STAGE_KEYS = ("per_mutant", "worker_boot", "slot_sync")


class StageClock:
    """阶段耗时累加器（调用栈内局部对象，无共享态、无锁）。"""

    def __init__(self) -> None:
        self._acc: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str):
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.add(name, time.monotonic() - t0)

    def add(self, name: str, seconds: float) -> None:
        if name not in STAGE_KEYS:
            raise ValueError(f"unknown stage key: {name}")
        self._acc[name] = self._acc.get(name, 0.0) + max(0.0, float(seconds))

    def disclosure(self) -> dict:
        return {k: round(self._acc.get(k, 0.0), 3) for k in STAGE_KEYS}


def merge_execution_stages(clock: StageClock, timings: dict | None) -> None:
    """分片执行阶段并入运行级时钟（见 EXEC_STAGE_KEYS 双计说明）。"""
    for k in EXEC_STAGE_KEYS:
        v = (timings or {}).get(k)
        if isinstance(v, (int, float)):
            clock.add(k, float(v))


def self_rss_peak_mb() -> float:
    """本进程 RSS 高水位（VmHWM）。非 Linux / 读取失败 = 0.0（诚实缺省）。"""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        return 0.0
    return 0.0


def qa_disk_bytes(project_root: str) -> int:
    """磁盘占用核算：目标项目 .qa-cache + ext4 影子农场树（字节）。

    农场根按 linux 域解析（win64 池根本就在 .qa-cache 内，不会重复计数）；
    任一路径缺失按 0 计——测量面不抛，不干扰主契约。"""
    from .shadow import binary_domain_root, dir_size_bytes
    total = dir_size_bytes(Path(project_root) / ".qa-cache")
    farm = binary_domain_root(project_root, "linux")
    if farm.is_dir():
        total += dir_size_bytes(farm)
    return total
