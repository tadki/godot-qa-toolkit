"""SEE-1321 mutation 编排：文件级并发 + 同文件 per-mutant 并发（SPEC-003/004/008）。

--jobs N（默认/上限 max(2, cpu//2)）：
- 不同目标文件并发评估（SPEC-003 文件级路径）；
- 内存注入路径下同文件 per-mutant 并发放开（Atlas 裁决 2026-09-19：内存注入
  实证磁盘零竞态，取消「同文件多 worker」对注入路径的禁令——复用 per-mutant
  mutant_slice 分片；磁盘改写兜底路径若被引入仍严禁同文件多 worker）。
- 并发前预热 `godot --headless --import`（幂等）。
- 同次运行内 baseline 复用：同一 tests_glob 只冷启动跑一次，快照传各 worker；
  禁止跨 mtime 复用（快照不落盘、进程结束即失效）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor

from .ops import _collect_mutations
from .runner import run_mutation

# 进程池工厂（可注入替身做单测）
_worker_pool = ProcessPoolExecutor

# 预热 godot import 缓存；幂等（--import 不产生写副作用差异）
_PREWARM_TIMEOUT_S = 300


def default_jobs(jobs: int | None) -> int:
    """jobs 钳制：缺省给上限；显式值取 min(jobs, 上限) 且不低于 2。"""
    cap = max(2, (os.cpu_count() or 2) // 2)
    if jobs is None:
        return cap
    return max(2, min(jobs, cap))


def _group_by_file(files: list[str]) -> list[str]:
    if len(set(files)) != len(files):
        raise ValueError(
            "duplicate mutation target files are forbidden — repeated CLI "
            f"paths of one file would race deduplication: {files}")
    return list(dict.fromkeys(files))


def prewarm_import(project_root: str) -> None:
    """并发路径预热：导入幂等，消除 worker 首轮 .godot 缓存竞争。

    非 Godot 项目目录（无 project.godot，如单测 toy fixture）跳过——
    godot --import 会退化为全盘资源扫描挂住。预热失败（含超时）降级为
    告警继续——预热是优化项，其失败不得打破统一 JSON 契约（LOW-5）。
    """
    if not os.path.isfile(os.path.join(project_root, "project.godot")):
        return
    try:
        subprocess.run(
            ["godot", "--headless", "--import", "--path", project_root],
            capture_output=True, text=True, timeout=_PREWARM_TIMEOUT_S,
            cwd=project_root,
        )
    except subprocess.TimeoutExpired:
        print(f"prewarm import timed out after {_PREWARM_TIMEOUT_S}s for "
              f"{project_root} — continuing without prewarm", file=sys.stderr)
    except OSError as e:
        print(f"prewarm import failed for {project_root}: {e} — continuing "
              f"without prewarm", file=sys.stderr)


def run_mutation_files(
    files: list[str],
    project_root: str,
    budget: int = 50,
    timeout_s: int = 60,
    dry_run: bool = False,
    tests_glob: str | None = None,
    jobs: int | None = None,
) -> dict:
    """多文件 / 多 mutant 编排：统一 JSON 契约摘要输出。

    dry_run（SPEC-009）：只清点不跑 GUT——预热、baseline、worker mutant 轮
    全部短路（Refacty 移交缺陷：此前 dry_run 不进 worker 与父层路径会
    真实跑 GUT，违反零 GUT 调用契约；硬化组 test_dry_run_* 实证据）。
    报告完备性（SPEC-016）：收集异常文件以 error 契约进 results、零变异
    文件逐文件出 no_sites 记录——任何输入文件都不得从报告中消失。
    """
    files = _group_by_file(files)
    if len(files) == 1 and (jobs is None or jobs <= 1):
        r = run_mutation(files[0], project_root, budget=budget,
                         timeout_s=timeout_s, dry_run=dry_run,
                         tests_glob=tests_glob)
        r["summary"].setdefault("jobs", 1)
        return {"tool": "mutation", "ok": r["ok"], "jobs": 1,
                "results": [r]}

    if dry_run:
        results = [
            run_mutation(f, project_root, budget=budget,
                         timeout_s=timeout_s, dry_run=True,
                         tests_glob=tests_glob)
            for f in files
        ]
        return {"tool": "mutation",
                "ok": all(r["ok"] for r in results),
                "jobs": 1,
                "results": results}

    jobs = default_jobs(jobs)
    prewarm_import(project_root)
    chunks, settled = _build_chunks(files, project_root, budget, timeout_s,
                                    tests_glob, jobs)
    chunk_results = []
    if chunks:
        with _worker_pool(max_workers=jobs) as pool:
            chunk_results = list(pool.map(_chunk_worker, chunks))
    merged = _merge_results(chunks, chunk_results, jobs)
    # settled（异常/零变异文件）按输入顺序并入 results——任何输入文件都留痕
    results = _order_results(files, merged.pop("results"), settled)
    return {**merged, "ok": all(r["ok"] for r in results),
            "results": results}


def _order_results(files: list[str], merged: list[dict],
                   settled: list[dict]) -> list[dict]:
    """按输入文件顺序重排（merged 已按 chunk 序，settled 按收集序）。"""
    by_file: dict[str, dict] = {}
    for r in merged + settled:
        by_file[r["summary"]["file"]] = r
    return [by_file[f] for f in files if f in by_file]


def _settled_error(file: str, err) -> dict:
    """收集异常（如不可解析 GDScript）的定终态 error 契约——
    与 runner._setup_error_result 同构（单文件路径的既定形态）。"""
    return {
        "tool": "mutation",
        "ok": False,
        "summary": {"file": file, "mutants": 0, "error": str(err)},
        "failures": [{"reason": f"mutation setup failed: {err}"}],
    }


def _settled_no_sites(file: str, scoped: bool) -> dict:
    """零变异文件的定终态 no_sites 契约——与 runner._no_sites_result 同构。"""
    return {
        "tool": "mutation",
        "ok": True,
        "summary": {"file": file, "mutants": 0, "killed": 0, "survived": 0,
                    "timeout": 0, "kill_rate": 0.0, "note": "no mutation sites",
                    "scoped": bool(scoped)},
        "failures": [],
    }


def _chunk_worker(payload: dict) -> dict:
    """worker 进程入口：消费共享 baseline 与分片区间跑单文件 mutation。

    baseline 快照缺失（冷启动失败 / 脏 baseline）时退回串行单任务
    （serial=True，无 slice、worker 自跑 baseline）。
    """
    if payload.get("serial"):
        return run_mutation(
            payload["file"], payload["project_root"], budget=payload["budget"],
            timeout_s=payload["timeout_s"], tests_glob=payload["tests_glob"])
    return run_mutation(
        payload["file"], payload["project_root"], budget=payload["budget"],
        timeout_s=payload["timeout_s"], tests_glob=payload["tests_glob"],
        precomputed_baseline=payload["baseline"],
        mutant_slice=(payload["start"], payload["stop"]),
    )


def _merge_results(chunks: list[dict], chunk_results: list[dict],
                   jobs: int) -> dict:
    """按文件合并分片契约：计数求和、failures 拼接、kill_rate 重算。"""
    per_file: dict[str, dict] = {}
    for chunk, cr in zip(chunks, chunk_results):
        file = chunk["file"]
        agg = per_file.setdefault(file, {
            "file": file, "mutants": 0, "killed": 0, "survived": 0,
            "suspect": 0, "timeout": 0, "run_errors": 0,
            "invalid_mutants": 0, "failures": [], "scoped": None,
            "budget": None, "baseline_reused": None,
        })
        s = cr["summary"]
        for k in ("mutants", "killed", "survived", "suspect",
                  "timeout", "run_errors", "invalid_mutants"):
            agg[k] += s.get(k, 0)
        agg["failures"].extend(cr.get("failures", []))
        agg["scoped"] = bool(s.get("scoped"))
        agg["budget"] = s.get("budget")
        agg["baseline_reused"] = bool(s.get("baseline_reused"))
        t = agg["mutants"]
        agg["kill_rate"] = round(agg["killed"] / t, 4) if t else 0.0
    results = []
    for agg in per_file.values():
        ok = (agg["survived"] == 0 and agg["timeout"] == 0
              and agg["run_errors"] == 0 and agg["suspect"] == 0)
        results.append({
            "tool": "mutation",
            "ok": ok,
            "summary": {
                "file": agg["file"],
                "mutants": agg["mutants"],
                "killed": agg["killed"],
                "survived": agg["survived"],
                "suspect": agg["suspect"],
                "timeout": agg["timeout"],
                "run_errors": agg["run_errors"],
                "invalid_mutants": agg["invalid_mutants"],
                "kill_rate": agg["kill_rate"],
                "budget": agg["budget"],
                "scoped": agg["scoped"],
                "baseline_reused": agg["baseline_reused"],
            },
            "failures": agg["failures"],
        })
    return {
        "tool": "mutation",
        "ok": all(r["ok"] for r in results),
        "jobs": len(chunks),
        "failures": [f for r in results for f in r.get("failures", [])],
        "results": results,
    }


def _build_chunks(files: list[str], project_root: str, budget: int,
                  timeout_s: int, tests_glob: str | None,
                  jobs: int) -> tuple[list[dict], list[dict]]:
    """父层：per-file 收集 mutant 总数 → 分片；同样 glob 共享一份 baseline。

    返回 (chunks, settled)：settled 是收集异常（error 契约）与零变异
    （no_sites 契约）的定终态文件——SPEC-016：任何输入文件都不得从报告
    消失（此前 except 静默丢弃 + 零 chunk 早退只报 files[0] 是报告失真）。
    """
    baselines = _precompute_baselines(files, project_root, timeout_s,
                                      tests_glob)
    chunks: list[dict] = []
    settled: list[dict] = []
    for f in files:
        try:
            src = open(f, encoding="utf-8").read()
            total = min(len(_collect_mutations(src, f)), budget)
        except Exception as e:
            settled.append(_settled_error(f, e))
            continue
        glob = tests_glob or _auto_scan_tests_glob(f, project_root)
        if total == 0:
            settled.append(_settled_no_sites(f, bool(glob)))
            continue
        baseline = baselines.get(glob)
        if baseline is None:
            # baseline 快照失败（冷启动漂移/脏 baseline）→ 该文件整卷退回
            # 串行单任务（worker 自跑 baseline）；分片保证必须锚在共享快照上
            chunks.append({
                "file": f, "project_root": project_root, "budget": budget,
                "timeout_s": timeout_s, "tests_glob": glob, "serial": True,
            })
            continue
        n_chunks = min(jobs, total)
        size = (total + n_chunks - 1) // n_chunks
        for i in range(n_chunks):
            start, stop = i * size, min((i + 1) * size, total)
            if start >= stop:
                continue
            chunks.append({
                "file": f,
                "project_root": project_root,
                "budget": budget,
                "timeout_s": timeout_s,
                "tests_glob": glob,
                "start": start,
                "stop": stop,
                "baseline": baselines.get(glob),
            })
    return chunks, settled


def _precompute_baselines(files, project_root, timeout_s, tests_glob):
    """并发分派【之前】单进程冷启动 baseline（SPEC-005 锚点）。

    复用条件（SPEC-008）：同一 tests_glob（显式相同 / 全部退回全量 /
    自动扫描推导出同一目录）跨文件共享一份；代码状态 = 目标文件此刻都
    是仓库原始态，快照不落盘，禁止跨 mtime 复用。
    """
    from .runner import _auto_scan_tests_glob

    globs = {}
    for f in files:
        g = tests_glob if tests_glob else _auto_scan_tests_glob(f, project_root)
        globs[f] = g
    baselines: dict = {}
    for g in set(globs.values()):
        if g is None:
            continue
        baseline = _run_baseline_snapshot(project_root, g, timeout_s)
        if baseline and baseline.get("rc") == 0:
            baselines[g] = baseline
    return baselines


def _auto_scan_tests_glob(file_path: str, project_root: str) -> str | None:
    """父层推导 scoped glob（与 worker 侧 run_mutation 内推导同口径）。"""
    from .runner import _auto_scan_tests_glob as _runner_scan
    return _runner_scan(file_path, project_root)


def _run_baseline_snapshot(project_root: str, tests_glob: str | None,
                           timeout_s: int) -> dict | None:
    from .runner import _run_baseline

    try:
        rc, tail, elapsed = _run_baseline(project_root, tests_glob, timeout_s)
    except Exception:
        return None
    if rc != 0:
        return None
    return {"rc": rc, "tail": tail, "elapsed": elapsed}
