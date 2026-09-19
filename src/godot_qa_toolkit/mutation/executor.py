"""SEE-1321 多文件 mutation 编排：文件级并发 + baseline 复用（SPEC-003/004/008）。

- --jobs N（默认/上限 max(2, cpu//2)）按不同目标文件并发评估；同文件多 worker
  严禁（runner 判定依赖的目标文件原始内容语义，互踩风险防御性拒绝）。
- 并发前预热 `godot --headless --import`（幂等，仅并发路径执行）。
- 同次运行内 baseline 复用：同一 tests_glob 只在编排层单进程冷启动跑一次，
  结果传给各 worker——代码状态同一时刻、同一份，禁止跨 mtime 复用。
"""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ProcessPoolExecutor

from concurrent.futures import Executor

from .runner import run_mutation

# 进程池工厂（可注入替身做单测）
_worker_pool = ProcessPoolExecutor

# 预热 godot import 缓存；幂等（--import 不产生写副作用差异）
_PREWARM_TIMEOUT_S = 300


def default_jobs(jobs: int | None) -> int:
    """jobs 钳制：默认 max(2, cpu//2)；显式超上限收敛到上限（防内存放大）。"""
    cap = max(2, (os.cpu_count() or 2) // 2)
    if jobs is None:
        return cap
    return max(2, min(jobs, cap))


def _group_by_file(files: list[str]) -> list[str]:
    if len(set(files)) != len(files):
        raise ValueError(
            "duplicate mutation target files are forbidden — same-file "
            f"concurrency would corrupt verdicts: {files}")
    return list(dict.fromkeys(files))


def prewarm_import(project_root: str) -> None:
    """并发路径预热：导入幂等，消除 worker 首轮 .godot 缓存竞争。"""
    subprocess.run(
        ["godot", "--headless", "--import", "--path", project_root],
        capture_output=True, text=True, timeout=_PREWARM_TIMEOUT_S,
        cwd=project_root,
    )


def run_mutation_files(
    files: list[str],
    project_root: str,
    budget: int = 50,
    timeout_s: int = 60,
    dry_run: bool = False,
    tests_glob: str | None = None,
    jobs: int | None = None,
) -> dict:
    """多文件 mutation：单文件路径内部代理 run_mutation，多文件并发编排。

    返回 {"tool":"mutation", "ok", "jobs", "results": [单文件契约...]}。
    """
    files = _group_by_file(files)
    if len(files) == 1:
        r = run_mutation(files[0], project_root, budget=budget,
                         timeout_s=timeout_s, dry_run=dry_run,
                         tests_glob=tests_glob)
        r["summary"].setdefault("jobs", 1)
        return {"tool": "mutation", "ok": r["ok"], "jobs": 1,
                "results": [r]}

    jobs = default_jobs(jobs)
    prewarm_import(project_root)
    baselines = _precompute_baselines(files, project_root, budget, timeout_s,
                                      tests_glob, dry_run)
    payloads = [_worker_payload(f, project_root, budget, timeout_s,
                                tests_glob, dry_run, baselines)
                for f in files]
    with _worker_pool(max_workers=jobs) as pool:
        results = list(pool.map(_file_worker, payloads))
    return {
        "tool": "mutation",
        "ok": all(r["ok"] for r in results),
        "jobs": len(results),
        "failures": [f for r in results for f in r.get("failures", [])],
        "results": results,
    }


def _file_worker(payload: dict) -> dict:
    """worker 进程入口：消费预计算 baseline 跑单文件 mutation。"""
    return run_mutation(
        payload["file"], payload["project_root"], budget=payload["budget"],
        timeout_s=payload["timeout_s"], tests_glob=payload["tests_glob"],
        precomputed_baseline=payload["baseline"],
    )


_file_worker_before = _file_worker


def _worker_payload(file_path, project_root, budget, timeout_s, tests_glob,
                    dry_run, baselines) -> dict:
    key = _baseline_key(file_path, tests_glob)
    return {
        "file": file_path,
        "project_root": project_root,
        "budget": budget,
        "timeout_s": timeout_s,
        "tests_glob": tests_glob,
        "dry_run": dry_run,
        "baseline": baselines.get(key),
    }


def _baseline_key(file_path: str, tests_glob: str | None) -> tuple:
    # 同一显式 glob 的多文件共享 baseline；自动扫描 glob 逐文件不同（各文件
    # 的 staged glob 未必一致）→ 各自独立 baseline，禁止错误共享
    return (os.path.getmtime(file_path) if not tests_glob else 0, tests_glob)


def _precompute_baselines(files, project_root, budget, timeout_s, tests_glob,
                          dry_run):
    """同次运行内、并发分派【之前】单进程冷启动 baseline（SPEC-005 锚点）。

    复用条件（SPEC-008）：显式统一 tests_glob（跨文件同一套测试）时共享一份；
    代码状态 = 所有目标文件此刻都是仓库原始态，一跑即逝不复用、禁止
    跨 mtime 复用（baseline 快照不落盘，进程结束即失效）。
    """
    if not tests_glob:
        return {}
    return {(0, tests_glob): _run_baseline_snapshot(project_root, tests_glob,
                                                    timeout_s)}


def _run_baseline_snapshot(project_root: str, tests_glob: str | None,
                           timeout_s: int) -> dict | None:
    from .runner import _run_baseline, _BaselineLaunchFailure

    try:
        rc, tail, elapsed = _run_baseline(project_root, tests_glob, timeout_s)
    except Exception:
        return None
    if rc != 0:
        return None
    return {"rc": rc, "tail": tail, "elapsed": elapsed}
