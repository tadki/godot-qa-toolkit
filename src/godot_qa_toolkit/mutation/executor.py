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
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import shadow
from .inject import godot_command
from .ops import _collect_mutations
from .reporting import (SURVIVOR_DETAIL_KEYS, SURVIVORS_CAP, _mutant_record,
                        _valid_mutants)
from .coverlink import load_instrumentable_lines, load_reusable_coverage
from .identity import certificate_valid, disclose_superseded, identity_pack
from .baseline import (_BASELINE_FAILURE_REASONS, _baseline_key,
                       _run_baseline_snapshot)
from .progress import new_run_id
from .resume2 import ResumeStoreV2
from .diffengine import budget_on_retry, plan_diff, untouched_ranges
from .scopemanifest import build_manifest
from .stagetiming import (StageClock, merge_execution_stages, qa_disk_bytes,
                          self_rss_peak_mb)
from .runner import run_mutation

# 进程池工厂（可注入替身做单测）
_worker_pool = ProcessPoolExecutor

# 预热 godot import 缓存；幂等（--import 不产生写副作用差异）
_PREWARM_TIMEOUT_S = 300

# §SPEC-203 测量专用 gated 旋钮：缺省 = master 现行为（等价钉子钉死），
# set 时生效并在 stdout 披露（knobs_disclosure）。
ENV_JOBS_CAP = "GQT_MUTATION_JOBS_CAP"
ENV_BASELINE_IN_FARM = "GQT_MUTATION_BASELINE_IN_FARM"


def _serial_fast_path(f, project_root, budget, timeout_s, dry_run,
                      tests_glob, resume, binary_kind, timing_store,
                      mutation_warning, timeout_factor, reuse_coverage,
                      identity, worker_channel=None) -> dict:
    """--jobs 1 单文件串行快路径（SEE-1413；形态与串行路径一致 jobs=1）。"""
    r = run_mutation(f, project_root, budget=budget, timeout_s=timeout_s,
                     dry_run=dry_run, tests_glob=tests_glob, resume=resume,
                     binary_kind=binary_kind, timing_store=timing_store,
                     mutation_warning=mutation_warning,
                     timeout_factor=timeout_factor,
                     reuse_coverage=reuse_coverage, identity=identity,
                     worker_channel=worker_channel)
    r["summary"].setdefault("jobs", 1)
    return {"tool": "mutation", "ok": r["ok"], "jobs": 1, "results": [r]}


def _dry_run_list(files, project_root, budget, timeout_s, tests_glob,
                  binary_kind, mutation_warning, timeout_factor,
                  reuse_coverage, identity, worker_channel=None) -> dict:
    """多文件 dry-run 清点（零 GUT/零预热/零 baseline）。"""
    results = [
        run_mutation(f, project_root, budget=budget, timeout_s=timeout_s,
                     dry_run=True, tests_glob=tests_glob,
                     binary_kind=binary_kind,
                     mutation_warning=mutation_warning,
                     timeout_factor=timeout_factor,
                     reuse_coverage=reuse_coverage, identity=identity,
                     worker_channel=worker_channel)
        for f in files
    ]
    for result in results:
        result["summary"].setdefault("jobs", 1)
    return {"tool": "mutation", "ok": all(r["ok"] for r in results),
            "jobs": 1, "results": results}


def _detect_binary_kind() -> str:
    from .inject import detect_binary_kind
    return detect_binary_kind()


def _disclose_superseded_stores(project_root: str) -> None:
    """旧 store 一次性失效披露（OPS see1550/1 bump；不读不写不删旧条目）。"""
    root = Path(project_root) / ".qa-cache"
    disclose_superseded(project_root,
                        root / "gqt-mutation-resume-v2",
                        root / "gqt-coverage")


def jobs_cap() -> int:
    """jobs 并发上限（§SPEC-203：GQT_MUTATION_JOBS_CAP 覆写，钳制保留）。

    缺省 = max(2, cpu//2)（master 公式逐位不变）；env 覆写值同样钳下限 2
    （cap>=2 不变式）；无效值 stderr 告警后回退默认（不静默）。"""
    raw = os.environ.get(ENV_JOBS_CAP, "")
    if raw.strip() == "":
        return max(2, (os.cpu_count() or 2) // 2)
    try:
        return max(2, int(raw))
    except ValueError:
        print(f"mutation: invalid {ENV_JOBS_CAP}={raw!r} — falling back to "
              f"the default cap", file=sys.stderr)
        return max(2, (os.cpu_count() or 2) // 2)


def default_jobs(jobs: int | None) -> int:
    """jobs 钳制：缺省给上限；显式值取 min(jobs, 上限) 且不低于 2。"""
    cap = jobs_cap()
    if jobs is None:
        return cap
    return max(2, min(jobs, cap))


def baseline_in_farm_requested() -> bool:
    """§SPEC-203：baseline 移入 ext4 影子槽（opt-in，缺省 OFF——D5「真实仓
    baseline 权威」默认路径不动）。真值解析与 full_census_requested 同款。"""
    return os.environ.get(ENV_BASELINE_IN_FARM, "").strip() \
        not in ("", "0", "false")


def knobs_disclosure(baseline_in_farm_effective: bool | None = None) -> dict:
    """旋钮 set 时才披露（缺省全 unset = {}，stdout 逐位不变）。

    baseline_in_farm 披露【实际生效值】：制备失败降级回真实仓时为 False
    （降级本身已 stderr 告警，此处数据面如实登记）。"""
    disc: dict = {}
    if os.environ.get(ENV_JOBS_CAP, "").strip() != "":
        disc["jobs_cap"] = jobs_cap()
    if os.environ.get(shadow.ENV_POOL_MAX_SLOTS, "").strip() != "":
        disc["pool_max_slots"] = shadow.pool_max_slots()
    if baseline_in_farm_requested():
        disc["baseline_in_farm"] = bool(baseline_in_farm_effective)
    return disc


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
        # §SPEC-1525-002：预热启动点同源 godot_command()——原字面量 "godot"
        # 使显式 env（Linux ELF）下预热仍走 PATH 首匹配，判定与预热双引擎。
        # --path 保持原样直传（既有语义：win64 下预热失败本就降级放行，
        # 未设变量路径不得在此引入新转换）。
        subprocess.run(
            [godot_command(), "--headless", "--import", "--path", project_root],
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
    resume: bool = False,
    binary_kind: str | None = None,
    timing_store=None,
    mutation_warning: int | None = None,
    timeout_factor: float | None = None,
    mutate_all: bool = False,
    reuse_coverage: bool = True,
    worker_channel: bool | None = None,
) -> dict:
    """多文件 / 多 mutant 编排入口（§SPEC-202 运行级测量面挂载点）。

    dry-run（SPEC-009 零 GUT 契约）信封不加测量字段；常规路径在信封挂
    stage_timings / rss_peak_mb / disk_delta_bytes（只加不改），旋钮 set
    时挂 knobs——单文件 flat CLI 契约的信封键随 unwrap 丢失，单文件时
    投影进 summary（多文件语义归 run，逐文件不投影）。
    """
    if dry_run:
        return _run_mutation_files_impl(
            files, project_root, budget=budget, timeout_s=timeout_s,
            dry_run=dry_run, tests_glob=tests_glob, jobs=jobs, resume=resume,
            binary_kind=binary_kind, timing_store=timing_store,
            mutation_warning=mutation_warning,
            timeout_factor=timeout_factor, mutate_all=mutate_all,
            reuse_coverage=reuse_coverage, worker_channel=worker_channel)
    clock = StageClock()
    knob_state: dict = {}
    disk_before = qa_disk_bytes(project_root)
    t0 = time.monotonic()
    envelope = _run_mutation_files_impl(
        files, project_root, budget=budget, timeout_s=timeout_s,
        dry_run=dry_run, tests_glob=tests_glob, jobs=jobs, resume=resume,
        binary_kind=binary_kind, timing_store=timing_store,
        mutation_warning=mutation_warning, timeout_factor=timeout_factor,
        mutate_all=mutate_all, reuse_coverage=reuse_coverage,
        worker_channel=worker_channel, clock=clock, knob_state=knob_state)
    clock.add("total", time.monotonic() - t0)
    for r in envelope.get("results", []):
        merge_execution_stages(
            clock, (r.get("summary") or {}).get("stage_timings"))
    envelope["stage_timings"] = clock.disclosure()
    envelope["rss_peak_mb"] = self_rss_peak_mb()
    envelope["disk_delta_bytes"] = qa_disk_bytes(project_root) - disk_before
    # 单文件 flat CLI 契约：信封键随 unwrap 丢失——运行级测量字段（磁盘
    # 增量、旋钮披露）语义上就是该文件的 run，投影进唯一文件的 summary。
    results = envelope.get("results") or []
    if len(files) == 1 and results:
        results[0]["summary"]["disk_delta_bytes"] = \
            envelope["disk_delta_bytes"]
    disc = knobs_disclosure(knob_state.get("baseline_in_farm"))
    if disc:
        envelope["knobs"] = disc
        if len(files) == 1 and results:
            results[0]["summary"]["knobs"] = disc
    return envelope


def _run_mutation_files_impl(
    files: list[str],
    project_root: str,
    budget: int = 50,
    timeout_s: int = 60,
    dry_run: bool = False,
    tests_glob: str | None = None,
    jobs: int | None = None,
    resume: bool = False,
    binary_kind: str | None = None,
    timing_store=None,
    mutation_warning: int | None = None,
    timeout_factor: float | None = None,
    mutate_all: bool = False,
    reuse_coverage: bool = True,
    worker_channel: bool | None = None,
    clock: StageClock | None = None,
    knob_state: dict | None = None,
) -> dict:
    """多文件 / 多 mutant 编排：统一 JSON 契约摘要输出。

    dry_run（SPEC-009）：只清点不跑 GUT——预热、baseline、worker mutant 轮
    全部短路（Refacty 移交缺陷：此前 dry_run 不进 worker 与父层路径会
    真实跑 GUT，违反零 GUT 调用契约；硬化组 test_dry_run_* 实证据）。
    报告完备性（SPEC-016）：收集异常文件以 error 契约进 results、零变异
    文件逐文件出 no_sites 记录——任何输入文件都不得从报告中消失。
    resume（SEE-1400 §SPEC-002）：穿线到单文件路径与各 chunk worker——
    worker 各管各 seq 分片，per-mutant 落盘天然无跨 worker 竞态。
    SEE-1413：单文件 jobs>1 时变异点总数 ≤ jobs 自动退串行（判定在收集
    后、prewarm/baseline 之前——少变异场景共享 baseline 预跑的固定开销
    会倒挂），summary.jobs=1 与显式 --jobs 1 同形态。
    SEE-1550：身份包在父层计算一次并随 chunk 注入（多 worker 同 key）；
    reuse_coverage 默认自动启用（store fresh ∧ 身份包全匹配才生效）。
    """
    files = _group_by_file(files)
    kind = binary_kind or _detect_binary_kind()
    identity = identity_pack(project_root, kind)
    clock = clock or StageClock()
    knob_state = knob_state if knob_state is not None else {}
    _disclose_superseded_stores(project_root)
    # SEE-1413：--jobs 1（显式串行）走单文件串行快路径；缺省时默认并行
    # 上限几乎必然 >1，不需要在这里走快路径。
    if len(files) == 1 and jobs == 1:
        return _serial_fast_path(files[0], project_root, budget, timeout_s,
                                 dry_run, tests_glob, resume, binary_kind,
                                 timing_store, mutation_warning,
                                 timeout_factor, reuse_coverage, identity,
                                 worker_channel=worker_channel)
    if dry_run:
        return _dry_run_list(files, project_root, budget, timeout_s,
                             tests_glob, binary_kind, mutation_warning,
                             timeout_factor, reuse_coverage, identity,
                             worker_channel=worker_channel)

    jobs = default_jobs(jobs)
    # SEE-1413 退串行判定：必须在任何 GUT 调用（prewarm / 共享 baseline
    # 预跑）之前——变异点太少时走并发路径反而比纯串行慢。判定对缺省与
    # 显式 jobs 一体生效（§SPEC-002：总数 ≤ jobs 即退）；--jobs 1 在上方
    # 串行快路径已拦截。
    if len(files) == 1:
        # 目标缺失：与串行路径同款 FileNotFoundError（CLI 层
        # failures[0].reason 含 "not found" 的既定 caller 契约）。
        if not Path(files[0]).is_file():
            raise FileNotFoundError(f"mutation target not found: {files[0]}")
        try:
            src = open(files[0], encoding="utf-8").read()
            total = min(len(_collect_mutations(src, files[0])), budget)
        except Exception:
            total = -1  # 收集异常交给 _build_chunks 定终态（error 契约）
        if total == 0 or 0 < total <= jobs:
            r = run_mutation(files[0], project_root, budget=budget,
                             timeout_s=timeout_s, dry_run=False,
                             tests_glob=tests_glob, resume=resume,
                             binary_kind=binary_kind,
                             timing_store=timing_store,
                             mutation_warning=mutation_warning,
                             timeout_factor=timeout_factor,
                             mutate_all=mutate_all,
                             worker_channel=worker_channel)
            r["summary"].setdefault("jobs", 1)
            return {"tool": "mutation", "ok": r["ok"], "jobs": 1,
                    "results": [r]}

    with clock.stage("prewarm"):
        prewarm_import(project_root)
    chunks, settled, v2ctx = _build_chunks(files, project_root, budget,
                                           timeout_s, tests_glob, jobs,
                                           resume, binary_kind=binary_kind,
                                           mutation_warning=mutation_warning,
                                           timeout_factor=timeout_factor,
                                           mutate_all=mutate_all,
                                           reuse_coverage=reuse_coverage,
                                           identity=identity,
                                           worker_channel=worker_channel,
                                           clock=clock,
                                           knob_state=knob_state)
    chunk_results = []
    if chunks:
        with _worker_pool(max_workers=jobs) as pool:
            chunk_results = list(pool.map(_chunk_worker, chunks))
    merged = _merge_results(chunks, chunk_results, jobs)
    results_merged = merged.pop("results")
    # P1 差分（SEE-1420）：父层单点注入 reused 记录与差分披露，并写
    # manifest 索引层（worker 不写——并发竞态规避，D5 高风险通道裁决）。
    _inject_diff_results(results_merged, v2ctx)
    # settled（异常/零变异文件）按输入顺序并入 results——任何输入文件都留痕
    results = _order_results(files, results_merged, settled)
    envelope = {**merged, "ok": all(r["ok"] for r in results),
                "results": results}
    # SEE-1416 §SPEC-1416-3：多文件信封顶层 summary.attention——跨文件
    # 非 killed 明细合并投影（≤5 条，B 记录形态）；单文件 flat 契径不加
    # attention（保持既有形态，兼容判据 §SPEC-1416-3）。
    if len(files) > 1:
        envelope["summary"] = {"attention": _attention_details(results)}
    return envelope


def _attention_details(results: list[dict]) -> list[dict]:
    """跨文件非 killed 明细合并（≤5 条）：只取带完整 mutant 字段的记录，
    error/no_sites 契约的 reason-only 失败项不进投影。"""
    details: list[dict] = []
    for r in results:
        for f in r.get("failures", []):
            if "verdict" in f:
                details.append({k: f[k] for k in SURVIVOR_DETAIL_KEYS
                                if k in f})
            if len(details) >= SURVIVORS_CAP:
                return details
    return details


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
        "summary": {"file": file, "mutants": 0, "jobs": 0, "error": str(err)},
        "failures": [{"reason": f"mutation setup failed: {err}"}],
    }


def _settled_no_sites(file: str, scoped: bool) -> dict:
    """零变异文件的定终态 no_sites 契约——与 runner._no_sites_result 同构。"""
    return {
        "tool": "mutation",
        "ok": True,
        "summary": {"file": file, "mutants": 0, "jobs": 0,
                    "killed": 0, "survived": 0,
                    "timeout": 0, "kill_rate": 0.0, "note": "no mutation sites",
                    "scoped": bool(scoped)},
        "failures": [],
    }


def _chunk_worker(payload: dict) -> dict:
    """worker 进程入口：消费共享 baseline 与父层差分规划的 retry_ids 分片。

    baseline 快照缺失（冷启动失败 / 脏 baseline）时退回串行单任务
    （serial=True，无 retry_ids、worker 自跑 baseline 并自建差分）。
    v2 条目由 run_mutation 内增量落盘——各 worker 只写各自 retry_ids 的
    条目文件（分片零重叠），manifest 索引层由父层收尾单点写。
    timeout_factor（§SPEC-1420-02）随 payload 穿线。
    identity（SEE-1550）：父层身份包随 payload 注入——多 worker 同 key。
    """
    resume = bool(payload.get("resume"))
    binary_kind = payload.get("binary_kind")
    timeout_factor = payload.get("timeout_factor")
    identity = payload.get("identity")
    if payload.get("serial"):
        return run_mutation(
            payload["file"], payload["project_root"], budget=payload["budget"],
            timeout_s=payload["timeout_s"], tests_glob=payload["tests_glob"],
            resume=resume, binary_kind=binary_kind,
            timeout_factor=timeout_factor,
            mutate_all=bool(payload.get("mutate_all")),
            progress=payload.get("progress"),
            identity=identity,
            worker_channel=payload.get("worker_channel"))
    return run_mutation(
        payload["file"], payload["project_root"], budget=payload["budget"],
        timeout_s=payload["timeout_s"], tests_glob=payload["tests_glob"],
        precomputed_baseline=payload["baseline"],
        retry_ids=payload["retry_ids"],
        resume=resume, binary_kind=binary_kind,
        timeout_factor=timeout_factor,
        progress=payload.get("progress"),
        identity=identity,
        worker_channel=payload.get("worker_channel"),
    )


def _accumulate_chunk(agg: dict, cr: dict, passthrough_keys: tuple[str, ...]) -> None:
    """把单个 chunk 累加进文件级聚合。"""
    s = cr["summary"]
    agg["jobs"] += 1
    agg["ok"] = agg["ok"] and bool(cr.get("ok", True))
    for k in passthrough_keys:
        if k in s:
            agg[k] = s[k]
    for k in ("mutants", "killed", "survived", "suspect",
              "timeout", "run_errors", "invalid_mutants", "resumed",
              "timeout_pinned"):
        agg[k] += s.get(k, 0)
    # O2（SEE-1420 qa 复验）：executed = 实际执行语义——worker 层已按
    # invalid 预检覆写，逐分片求和（父层不得回填计划面值）。
    if s.get("executed") is not None:
        agg["executed"] = (agg["executed"] or 0) + s["executed"]
    # SEE-1550 只加不改披露面：机制标签（passthrough）+ 槽位/新鲜度/killed
    # 证书/疑似孤儿（跨分片拼接——各分片槽位结论与证书并集）。
    agg["freshness"] += list(s.get("freshness") or [])
    agg["killed_by_certificates"] += list(s.get("killed_by_certificates") or [])
    agg["suspected_orphans"] += list(s.get("suspected_orphans") or [])
    # §SPEC-202（只加不改）：跨分片阶段耗时求和 + RSS 峰值取最大（分片级
    # VmHWM 是各 worker 进程自己的高水位——文件级取 max 为其诚实上界）。
    for k, v in (s.get("stage_timings") or {}).items():
        if isinstance(v, (int, float)):
            agg["stage_timings"][k] = agg["stage_timings"].get(k, 0.0) + v
    rss = s.get("rss_peak_mb")
    if isinstance(rss, (int, float)):
        agg["rss_peak_mb"] = max(agg["rss_peak_mb"], float(rss))
    agg["failures"].extend(cr.get("failures", []))
    agg["scoped"] = bool(s.get("scoped"))
    agg["budget"] = s.get("budget")
    agg["baseline_reused"] = bool(s.get("baseline_reused"))
    total = agg["mutants"]
    agg["kill_rate"] = round(agg["killed"] / total, 4) if total else 0.0


def _merge_results(chunks: list[dict], chunk_results: list[dict],
                   jobs: int) -> dict:
    """按文件合并分片契约：计数求和、failures 拼接、kill_rate 重算。

    resumed（§SPEC-010）：各 chunk 的 resumed 求和进聚合 summary——resume
    语义下同文件多 chunk 各 skip 各的（分片不重叠），求和即该文件总跳过
    数，与单文件扁平路径口径一致；无 resume 语义的 chunk 按缺省 0 计，
    报告形态保持与单文件路径同构。
    """
    passthrough_keys = (
        "adaptive_timeout_s", "timing_source", "samples_n", "baseline_p90",
        "run_error", "error", "inject_scheme", "slot_mode", "exec_scheme",
    )
    per_file: dict[str, dict] = {}
    resumed_present = any("resumed" in cr["summary"] for cr in chunk_results)
    for chunk, cr in zip(chunks, chunk_results):
        file = chunk["file"]
        agg = per_file.setdefault(file, {
            "file": file, "mutants": 0, "killed": 0, "survived": 0,
            "suspect": 0, "timeout": 0, "run_errors": 0,
            "invalid_mutants": 0, "resumed": 0, "timeout_pinned": 0,
            "executed": None,
            "jobs": 0, "ok": True, "failures": [], "scoped": None,
            "budget": None, "baseline_reused": None,
            "total_sites": None, "attention": None,
            "freshness": [], "killed_by_certificates": [],
            "suspected_orphans": [],
            "stage_timings": {}, "rss_peak_mb": 0.0,
        })
        _accumulate_chunk(agg, cr, passthrough_keys)
        # §SPEC-1420-01：截断面 N/M 披露的父层聚合锚（截断前全量位点数）
        if agg["total_sites"] is None:
            agg["total_sites"] = chunk.get("total_sites")
        if agg.get("attention") is None:
            agg["attention"] = chunk.get("attention")
    results = [_file_summary(agg, passthrough_keys, resumed_present)
               for agg in per_file.values()]
    return {
        "tool": "mutation",
        "ok": all(r["ok"] for r in results),
        "jobs": len(chunks),
        "failures": [f for r in results for f in r.get("failures", [])],
        "results": results,
    }


def _truncation_disclosure(agg: dict) -> dict:
    """§SPEC-1420-01 截断面披露（total_sites > budget 时在场）。"""
    if agg["total_sites"] is None or agg["budget"] is None \
            or agg["total_sites"] <= agg["budget"]:
        return {}
    executed = (agg["executed"] if agg["executed"] is not None
                else min(agg["total_sites"], agg["budget"]))
    return {"total_sites": agg["total_sites"], "executed": executed}


def _see1550_disclosures(agg: dict) -> dict:
    """SEE-1550 只加不改披露面（跨分片拼接的槽位/新鲜度/证书/孤儿）。"""
    out: dict = {}
    if agg.get("freshness"):
        out["freshness"] = agg["freshness"]
    if agg.get("killed_by_certificates"):
        out["killed_by_certificates"] = agg["killed_by_certificates"]
    if agg.get("suspected_orphans"):
        out["suspected_orphans"] = agg["suspected_orphans"]
    return out


def _file_summary(agg: dict, passthrough_keys: tuple[str, ...],
                  resumed_present: bool) -> dict:
    """聚合态 → 单文件 result（summary 形态唯一出处，<800/复杂度纪律拆分）。"""
    ok = (agg["ok"] and agg["survived"] == 0 and agg["timeout"] == 0
          and agg["run_errors"] == 0 and agg["suspect"] == 0)
    total = agg["mutants"]
    kill_rate = round(agg["killed"] / total, 4) if total else 0.0
    summary = {
        "file": agg["file"],
        "jobs": agg["jobs"],
        "mutants": agg["mutants"],
        "killed": agg["killed"],
        "survived": agg["survived"],
        "suspect": agg["suspect"],
        "timeout": agg["timeout"],
        "run_errors": agg["run_errors"],
        "invalid_mutants": agg["invalid_mutants"],
        "kill_rate": kill_rate,
        "budget": agg["budget"],
        "scoped": agg["scoped"],
        "baseline_reused": agg["baseline_reused"],
        **({"timeout_pinned": agg["timeout_pinned"]}
           if agg["timeout_pinned"] else {}),
        **(_truncation_disclosure(agg)),
        **({"attention": agg["attention"]}
           if agg.get("attention") else {}),
        **{k: agg[k] for k in passthrough_keys if k in agg},
        **_see1550_disclosures(agg),
        **({"resumed": agg["resumed"]} if resumed_present else {}),
        "stage_timings": {k: round(v, 3)
                          for k, v in sorted(agg["stage_timings"].items())},
        "rss_peak_mb": round(agg["rss_peak_mb"], 1),
    }
    return {
        "tool": "mutation",
        "ok": ok,
        "summary": summary,
        "failures": agg["failures"],
    }


def _build_chunks(files: list[str], project_root: str, budget: int,
                  timeout_s: int, tests_glob: str | None,
                  jobs: int, resume: bool = False,
                  binary_kind: str | None = None,
                  mutation_warning: int | None = None,
                  timeout_factor: float | None = None,
                  mutate_all: bool = False,
                  reuse_coverage: bool = True,
                  identity: dict | None = None,
                  worker_channel: bool | None = None,
                  clock: StageClock | None = None,
                  knob_state: dict | None = None,
                  ) -> tuple[list[dict], list[dict], dict]:
    """父层：per-file 收集 → 差分规划 → 分片；同样 glob 共享一份 baseline。

    返回 (chunks, settled, v2ctx)。P1 差分规划在父层单点完成（D4/D6）：
    plan_diff + budget_on_retry 后按 retry 位点分片，worker 只执行注入的
    retry_ids——分片间零重叠，per-id 条目写天然无竞态。v2ctx 供合并层注入
    reused 记录/披露并单点写 manifest（D5 高风险通道：并发下 manifest 索引
    层只允许收尾方写）。
    settled 是收集异常（error 契约）与零变异（no_sites 契约）的定终态文件
    ——SPEC-016：任何输入文件都不得从报告消失。
    run_id（SEE-1525）是本 run 的进度面标识，随 chunk 穿透到各 worker（同一
    run 的 per-worker 分片聚合在同一个目录下）。
    """
    baselines, baseline_failures = _precompute_baselines(
        files, project_root, timeout_s, tests_glob, clock=clock,
        knob_state=knob_state)
    run_id = new_run_id()
    chunks: list[dict] = []
    settled: list[dict] = []
    v2ctx: dict = {}
    for f in files:
        file_chunks, file_ctx, file_settled = _plan_file_chunks(
            f, project_root, budget, timeout_s, tests_glob, jobs, resume,
            binary_kind, mutation_warning, timeout_factor, mutate_all,
            reuse_coverage, baselines, baseline_failures, run_id,
            identity=identity, worker_channel=worker_channel, clock=clock)
        chunks.extend(file_chunks)
        if file_ctx is not None:
            v2ctx[f] = file_ctx
        if file_settled is not None:
            settled.append(file_settled)
    return chunks, settled, v2ctx


def _plan_file_chunks(f, project_root, budget, timeout_s, tests_glob, jobs,
                      resume, binary_kind, mutation_warning, timeout_factor,
                      mutate_all, reuse_coverage, baselines,
                      baseline_failures=None, run_id=None,
                      identity: dict | None = None,
                      worker_channel: bool | None = None,
                      clock: StageClock | None = None):
    """单文件规划：收集 → 差分 plan（killed 证书门）→ P2 覆盖过滤 →
    budget → 分片。

    返回 (chunks_for_file, v2ctx_entry_or_None, settled_or_None)。
    """
    clock = clock or StageClock()
    try:
        src = open(f, encoding="utf-8").read()
        collected = _collect_mutations(src, f)
        raw_total = len(collected)
    except Exception as e:
        return [], None, _settled_error(f, e)
    # §SPEC-1420-01：告警由父层发一次（并发 worker 不重复）——按截断前
    # 全量位点数判定，budget 截断不掩盖规模问题。
    attention = None
    if mutation_warning is not None and raw_total > mutation_warning:
        attention = (f"{raw_total} mutation sites exceed "
                     f"--mutation-warning {mutation_warning} — consider "
                     f"splitting this file")
        print(f"mutation-warning: {raw_total} mutation sites exceed "
              f"--mutation-warning {mutation_warning} in {f} — consider "
              f"splitting this file", file=sys.stderr)
    with clock.stage("test_scan"):
        glob = tests_glob or _auto_scan_tests_glob(f, project_root)
    # 负/零 budget 与零位点同态：无执行面即 no_sites（jobs=0 契约），
    # 不得触发 baseline/prewarm。
    if raw_total <= 0 or budget <= 0:
        return [], None, _settled_no_sites(f, bool(glob))
    header_hash, spans, mutants, scope_hashes = build_manifest(src, f, collected)
    store2 = ResumeStoreV2(project_root, f, glob, binary_kind=binary_kind,
                           identity=identity)
    store2.disclose_v1_superseded()
    old_manifest = store2.load_manifest()
    entries = store2.load_entries()
    # killed 证书复用门（SEE-1550 §SPEC-007）：定向校验 killer 闭包
    plan = plan_diff(header_hash, scope_hashes, old_manifest, mutants,
                     entries, mutate_all=mutate_all,
                     cert_validator=lambda kb: certificate_valid(kb,
                                                                 project_root))
    with clock.stage("prefilter"):
        uncovered_mutants, retry_pool = _apply_coverage_prefilter(
            plan, f, project_root, glob, binary_kind, reuse_coverage,
            identity=identity)
    executed_list, untouched = budget_on_retry(retry_pool, budget)
    baseline = baselines.get(glob)
    if not executed_list:
        # 差分后零执行（全 reused / budget=0）：串行路径收尾（零 mutant
        # GUT + 1 baseline 锚），报告由 run_mutation 自建。父层已预跑的
        # 共享 baseline 快照必须注入（SEE-1420 QA 实证：不注入则
        # run_mutation「永远实测」再跑一轮 baseline——B 场景 GUT 调用
        # 2≠1，白付一次全套 GUT）。
        r = run_mutation(f, project_root, budget=budget,
                         timeout_s=timeout_s, tests_glob=glob,
                         resume=False, binary_kind=binary_kind,
                         mutation_warning=None, timeout_factor=timeout_factor,
                         mutate_all=mutate_all,
                         precomputed_baseline=baseline,
                         progress={"run_id": run_id, "worker": "serial",
                                   "total": 0},
                         reuse_coverage=reuse_coverage,
                         identity=identity,
                         worker_channel=worker_channel)
        r["summary"].setdefault("jobs", 1)
        return [], None, r
    ids = [m.stable_id for m in executed_list]
    if baseline is None:
        # baseline 快照失败（冷启动漂移/脏 baseline）→ 该文件整卷退回
        # 串行单任务（worker 自跑 baseline 并自建差分报告——父层不注入，
        # 防重复计数）。
        # SEE-1525：此前该降级零提示——执行人只看到「这个文件莫名在串行跑」。
        # 现显式告警（目标文件 + 失败原因摘要 + falls back to serial），
        # 不改行为、不失败；未尝试预跑（glob=None 的全量面）不在此列。
        if glob is not None:
            _warn_serial_fallback(f, (baseline_failures or {}).get(glob))
        chunk = {
            "file": f, "project_root": project_root, "budget": budget,
            "timeout_s": timeout_s, "tests_glob": glob, "serial": True,
            "resume": resume, "binary_kind": binary_kind,
            "timeout_factor": timeout_factor, "total_sites": raw_total,
            "attention": attention, "mutate_all": mutate_all,
            "progress": {"run_id": run_id, "worker": "serial",
                         "total": len(executed_list)},
            "identity": identity,
            "worker_channel": worker_channel,
        }
        return [chunk], None, None
    ctx = {
        "plan": plan, "executed": executed_list, "untouched": untouched,
        "header_hash": header_hash, "scope_hashes": scope_hashes,
        "store2": store2, "src": src, "mutants": mutants,
        "uncovered": uncovered_mutants,
    }
    chunks = _slice_retry_ids(f, project_root, budget, timeout_s, glob,
                              binary_kind, ids, baseline, resume,
                              timeout_factor, raw_total, attention, jobs,
                              run_id=run_id, identity=identity,
                              worker_channel=worker_channel)
    return chunks, ctx, None


def _warn_serial_fallback(file: str, reason: str | None) -> None:
    """基线快照失败 → 整文件退单 worker 串行：stderr 显式告警（不失败）。

    风格对齐既有 `prewarm import failed … — continuing`：只披露降级与其
    原因摘要，判定行为零变化（进度与失败语义都不受影响）。
    """
    detail = reason or "no baseline snapshot returned"
    print(f"mutation: baseline snapshot failed for {file}: {detail} — this "
          f"file falls back to serial single-worker execution (continuing)",
          file=sys.stderr)


def _apply_coverage_prefilter(plan, f, project_root, glob, binary_kind,
                              reuse_coverage, identity=None):
    """§SPEC-1420-12（P2）+ SEE-1550 L1：coverage 前置过滤先于 budget（预算只
    花在 covered 位点）；uncovered 全部显式标记（父层合并时注入报告）。

    store fresh ∧ 身份包全匹配才生效，任一漂移禁用并披露。
    D3 修复：过滤锚 = covered ∧ instrumentable——不可插桩行上的位点留在
    执行面（与 planner._apply_coverage_prefilter 同语义）；可插桩集不可得
    = 不过滤（宁多跑不假阴性）。"""
    if not reuse_coverage:
        return [], plan.executed
    cov, cov_reason = load_reusable_coverage(project_root, f, glob,
                                             binary_kind, identity=identity)
    if cov is None:
        print(f"mutation: coverage store miss ({cov_reason}) for {f} "
              f"— running unfiltered", file=sys.stderr)
        return [], plan.executed
    inst, inst_reason = load_instrumentable_lines(project_root, f, glob,
                                                  binary_kind,
                                                  identity=identity)
    if inst is None:
        print(f"mutation: instrumentable face unavailable ({inst_reason}) "
              f"for {f} — running unfiltered", file=sys.stderr)
        return [], plan.executed
    uncovered = [m for m in plan.executed
                 if m.line not in cov and m.line in inst]
    # D3 锚统一（SEE-1420 qa 复验 FAIL 实证）：执行面 = covered ∨ 不可插桩
    # ——此前本函数 uncovered 行已换新锚、pool 行仍 covered-only，导致不
    # 可插桩行位点被静默丢弃（不执行也不标记，比修复前更不可见）。
    return uncovered, [m for m in plan.executed
                       if m.line in cov or m.line not in inst]


def _slice_retry_ids(f, project_root, budget, timeout_s, glob, binary_kind,
                     ids, baseline, resume, timeout_factor, raw_total,
                     attention, jobs, run_id=None, identity=None,
                     worker_channel=None):
    """retry 位点保序分片（拒轮转——分片间零重叠，per-id 条目写无竞态）。

    progress（SEE-1525）：各分片共用同一 run_id 与**全局分母** `len(ids)`，
    worker 槽位名进进度行尾与分片文件名——每分片只写自己的进度分片，聚合
    在读侧完成（无共享写者，故无并行写竞争）。
    """
    n_chunks = min(jobs, len(ids))
    size = (len(ids) + n_chunks - 1) // n_chunks
    chunks = []
    for i in range(n_chunks):
        slice_ids = ids[i * size:(i + 1) * size]
        if not slice_ids:
            continue
        chunks.append({
            "file": f,
            "project_root": project_root,
            "budget": budget,
            "timeout_s": timeout_s,
            "tests_glob": glob,
            "binary_kind": binary_kind,
            "retry_ids": slice_ids,
            "baseline": baseline,
            "resume": resume,
            "timeout_factor": timeout_factor,
            "total_sites": raw_total,
            "attention": attention,
            "progress": {"run_id": run_id, "worker": f"w{i}",
                         "total": len(ids)},
            "identity": identity,
            "worker_channel": worker_channel,
        })
    return chunks


# 基线预跑快照原语（SEE-1550 tidy）在 baseline.py（<800 行纪律拆分先例）；
# 再导出保持既有 executor.* monkeypatch seam（替身钉死契约）与旁路表共享
# dict 对象身份零变化。


def _precompute_baselines(files, project_root, timeout_s, tests_glob,
                          clock: StageClock | None = None,
                          knob_state: dict | None = None):
    """并发分派【之前】单进程冷启动 baseline（SPEC-005 锚点）。

    复用条件（SPEC-008）：同一 tests_glob（显式相同 / 全部退回全量 /
    自动扫描推导出同一目录）跨文件共享一份；代码状态 = 目标文件此刻都
    是仓库原始态，快照不落盘，禁止跨 mtime 复用。

    §SPEC-203（opt-in）：GQT_MUTATION_BASELINE_IN_FARM 置位时 baseline 在
    ext4 影子槽副本内实测（measurement-only；fresh 槽用完即弃、不进 warm
    池）——缺省与制备失败都落在真实仓（D5 权威不动，降级 stderr 告警）。
    生效与否经 knob_state["baseline_in_farm"] 回传 stdout 披露。

    返回 (baselines, failures)：failures 只含**尝试过且失败**的 glob →
    原因摘要（SEE-1525 降级告警用）；glob=None（全量面未预跑）不在其中。
    """
    from .runner import _auto_scan_tests_glob
    from .shadow import SlotProvisionError, create_slot

    clock = clock or StageClock()
    globs = {}
    for f in files:
        with clock.stage("test_scan"):
            g = tests_glob if tests_glob \
                else _auto_scan_tests_glob(f, project_root)
        globs[f] = g
    farm_root, farm_slot = project_root, None
    baselines: dict = {}
    failures: dict[str, str] = {}
    try:
        # baseline 阶段含 farm 槽制备成本（opt-in 时）——占比归因口径：
        # 这笔开销是 baseline-in-farm 路线自身的成本，必须可见。
        with clock.stage("baseline"):
            if knob_state is not None and baseline_in_farm_requested():
                try:
                    root, _prov = create_slot(project_root, "linux")
                    farm_root, farm_slot = str(root), root
                    knob_state["baseline_in_farm"] = True
                except SlotProvisionError as e:
                    knob_state["baseline_in_farm"] = False
                    print(f"mutation: baseline-in-farm requested but slot "
                          f"provisioning failed ({e}) — baseline runs on the "
                          f"real repo (D5 authority unchanged)",
                          file=sys.stderr)
            for g in set(globs.values()):
                if g is None:
                    continue
                with clock.stage("baseline"):
                    baseline = _run_baseline_snapshot(farm_root, g, timeout_s)
                if baseline and baseline.get("rc") == 0:
                    baselines[g] = baseline
                else:
                    failures[g] = _BASELINE_FAILURE_REASONS.pop(
                        _baseline_key(farm_root, g),
                        "no baseline snapshot returned")
    finally:
        if farm_slot is not None:
            shutil.rmtree(farm_slot, ignore_errors=True)
    return baselines, failures


def _auto_scan_tests_glob(file_path: str, project_root: str) -> str | None:
    """父层推导 scoped glob（与 worker 侧 run_mutation 内推导同口径）。"""
    from .runner import _auto_scan_tests_glob as _runner_scan
    return _runner_scan(file_path, project_root)


def _inject_diff_results(results: list[dict], v2ctx: dict) -> None:
    """P1 差分收尾（父层单点）：reused 记录注入 + 披露字段 + manifest 落盘。

    只处理走 retry_ids 分片的文件（v2ctx 键集）；串行 fallback / executed 空
    的文件由 run_mutation 自建完整报告（已含 reused），不得重复注入。
    """
    if not v2ctx:
        return
    from ..timing import project_relative

    for r in results:
        ctx = v2ctx.get(r.get("summary", {}).get("file"))
        if ctx is None:
            continue
        _inject_one(r, ctx)
        # manifest 索引层单点写（收尾方唯一写者——并发竞态规避）
        ctx["store2"].write_manifest(
            ctx["header_hash"], ctx["scope_hashes"],
            ResumeStoreV2._sha1_text(ctx["src"]),
            project_relative(ctx["store2"].project_root,
                             r["summary"]["file"]))


_VERDICT_SUMMARY_KEY = {"killed": "killed", "survived": "survived",
                        "timeout": "timeout", "run_error": "run_errors",
                        "suspect": "suspect"}


def _inject_reused(r: dict, ctx: dict) -> None:
    """reused 条目 → 报告记录重建 + 计数并入（全签名已在 plan 阶段验证）。"""
    s = r["summary"]
    for sid, entry in ctx["plan"].reused.items():
        m = next((x for x in ctx["mutants"] if x.stable_id == sid), None)
        if m is None:
            continue
        rec = _mutant_record(m, entry["verdict"],
                             entry.get("reason", ""), src=ctx["src"])
        r["failures"].append(rec)
        if rec["verdict"] in _VERDICT_SUMMARY_KEY:
            s[_VERDICT_SUMMARY_KEY[rec["verdict"]]] += 1
        s["mutants"] += 1
    s["resumed"] = len(ctx["plan"].reused)


def _inject_one(r: dict, ctx: dict) -> None:
    """单文件收尾注入：reused/uncovered 记录 + 差分与覆盖披露 + gate 重算。"""
    plan = ctx["plan"]
    s = r["summary"]
    if plan.reused:
        _inject_reused(r, ctx)
    # §SPEC-1420-12（P2）：uncovered 位点不执行、不进 ok 门禁，failures
    # 显式标记（计数与覆盖执行面在 summary.coverage_prefilter 披露）。
    uncovered = ctx.get("uncovered") or []
    if uncovered:
        for m in uncovered:
            r["failures"].append(_mutant_record(
                m, "uncovered",
                f"line {m.line} not covered by tests (reused coverage "
                f"prefilter; site never executed)", src=ctx["src"]))
        s["coverage_prefilter"] = {"uncovered": len(uncovered),
                                   "covered_executed": len(ctx["executed"])}
    if plan.diff_active or plan.mutate_all:
        # §SPEC-1420-07（D6）+ O3 键集对齐：差分在场与 --mutate-all 两径披露
        # 同一键集（--mutate-all 全量面也可见：executed == retry_total、
        # diff_skipped 空）。
        s["diff_skipped"] = sorted(plan.reused)
        s["retry_total"] = len(plan.executed)
        s.setdefault("executed",
                     len(_valid_mutants(ctx["src"], ctx["executed"])))
        s["untouched_ranges"] = untouched_ranges(ctx["untouched"])
    total = s["mutants"]
    s["kill_rate"] = round(s["killed"] / total, 4) if total else 0.0
    r["ok"] = (s["survived"] == 0 and s["timeout"] == 0
               and s["run_errors"] == 0 and s["suspect"] == 0)


