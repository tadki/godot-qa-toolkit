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
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from ..source_io import anchor_targets_to_project_root
from . import cost_priors, shadow
from .ops import _collect_mutations
from .reporting import (SURVIVOR_DETAIL_KEYS, SURVIVORS_CAP, _mutant_record,
                        _valid_mutants)
from .coverlink import (COVERAGE_PREFILTER_MISS_STATES,
                        COVERAGE_PREFILTER_SOURCES,
                        MAP_PATH_UNRESOLVED, apply_coverage_prefilter,
                        coverage_prefilter_summary, selection_map_path)
from .identity import certificate_valid, identity_pack
from .baseline import (_BASELINE_FAILURE_REASONS, _baseline_key,
                       _run_baseline_snapshot)
from .progress import new_run_id
from .resume2 import ResumeStoreV2
from .diffengine import budget_on_retry, plan_diff, untouched_ranges
from .scopemanifest import build_manifest
from .stagetiming import (StageClock, merge_execution_stages, qa_disk_bytes,
                          reconcile,
                          self_rss_peak_mb)
from .runner import (_auto_scan_tests_glob_meta, _resolve_auto_glob,
                     _res_path_of, run_mutation)

# SEE-1594 BTIDY：规划/合并簇物理迁移至 planning/merging 模块（零行为拆分）；
# 再导出保持既有 executor.* 引用面与 monkeypatch seam（替身钉死契约）。
from .planning import (  # noqa: F401
    _apply_coverage_prefilter,
    _attach_chunk_selection,
    _auto_scan_tests_glob,
    _ensure_shared_baseline,
    _fn_group_chunks,
    _fid_of_line,
    _line_group_chunks,
    _plan_file_chunks,
    _precompute_baselines,
    _prepare_baseline_root,
    _provision_farm_slot,
    _slice_retry_ids,
    _warn_serial_fallback,
    _warn_site_scale,
)
from .merging import (  # noqa: F401
    _accumulate_chunk,
    _attention_details,
    _chunk_worker,
    _file_summary,
    _merge_results,
    _order_results,
    _see1550_disclosures,
    _truncation_disclosure,
)

# 进程池工厂（可注入替身做单测）
_worker_pool = ProcessPoolExecutor

# §SPEC-203 测量专用 gated 旋钮：缺省 = master 现行为（等价钉子钉死），
# set 时生效并在 stdout 披露（knobs_disclosure）。
ENV_JOBS_CAP = "GQT_MUTATION_JOBS_CAP"
ENV_BASELINE_IN_FARM = "GQT_MUTATION_BASELINE_IN_FARM"

def _serial_fast_path(f, project_root, budget, timeout_s, dry_run,
                      tests_glob, binary_kind, timing_store,
                      mutation_warning, timeout_factor, reuse_coverage,
                      identity, worker_channel=None,
                      select_mode: str = "full",
                      break_on_baseline_failure: bool = False,
                      kill_bail: bool | None = None,
                      mutate_all: bool = False,
                      jobs: int | None = None) -> dict:
    """--jobs 1 单文件串行快路径（SEE-1413；形态与串行路径一致 jobs=1）。
    SEE-1610 D3：补传 mutate_all（此前静默丢弃 → 差分复用照旧）。"""
    r = run_mutation(f, project_root, budget=budget, timeout_s=timeout_s,
                     dry_run=dry_run, tests_glob=tests_glob,
                     binary_kind=binary_kind, timing_store=timing_store,
                     mutation_warning=mutation_warning,
                     timeout_factor=timeout_factor,
                     reuse_coverage=reuse_coverage, identity=identity,
                     worker_channel=worker_channel, select_mode=select_mode,
                     break_on_baseline_failure=break_on_baseline_failure,
                     kill_bail=kill_bail, mutate_all=mutate_all, jobs=jobs)
    r["summary"].setdefault("jobs", 1)
    return {"tool": "mutation", "ok": r["ok"], "jobs": 1, "results": [r]}

def _dry_run_list(files, project_root, budget, timeout_s, tests_glob,
                  binary_kind, mutation_warning, timeout_factor,
                  reuse_coverage, identity, worker_channel=None,
                  select_mode: str = "full",
                  jobs: int | None = None) -> dict:
    """多文件 dry-run 清点（零 GUT/零预热/零 baseline）。"""
    results = [
        run_mutation(f, project_root, budget=budget, timeout_s=timeout_s,
                     dry_run=True, tests_glob=tests_glob,
                     binary_kind=binary_kind,
                     mutation_warning=mutation_warning,
                     timeout_factor=timeout_factor,
                     reuse_coverage=reuse_coverage, identity=identity,
                     worker_channel=worker_channel, select_mode=select_mode,
                     jobs=jobs)
        for f in files
    ]
    for result in results:
        result["summary"].setdefault("jobs", 1)
    return {"tool": "mutation", "ok": all(r["ok"] for r in results),
            "jobs": 1, "results": results}

def _detect_binary_kind() -> str:
    from .inject import detect_binary_kind
    return detect_binary_kind()

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

def effective_jobs(jobs: int | None) -> int:
    """真跑编排的 effective jobs（§SPEC-PLANFIELD-A dry-run derived 披露
    同源）：显式 1 → 1（单文件/串行 gate 快路径语义），否则 default_jobs
    钳制。与 _run_mutation_files_impl 编排分支逐位同判。"""
    return 1 if jobs == 1 else default_jobs(jobs)

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

def prewarm_import(project_root: str) -> dict:
    """并发路径预热：导入幂等，消除 worker 首轮 .godot 缓存竞争。

    非 Godot 项目目录（无 project.godot，如单测 toy fixture）跳过——
    godot --import 会退化为全盘资源扫描挂住。预热失败（含超时）降级为
    告警继续——预热是优化项，其失败不得打破统一 JSON 契约（LOW-5）。

    SEE-1571 §SPEC-509：预热走新鲜度证明流（mutation/prewarm_proof.py，
    §SPEC-507 定案设计经质量门全闭）——默认启用，输入与导入产物双内容
    摘要与证书逐位一致才跳过导入；`GQT_PREWARM_SENTINEL=0|false|no` 为
    逃生门（始终导入）；fresh-skip 时 stderr 披露 proof_id 与双摘要前缀，
    重导入披露具体原因。
    """
    if not os.path.isfile(os.path.join(project_root, "project.godot")):
        return {"decision": "skipped_non_project", "import_calls": 0,
                "reason": "no_project_godot"}
    from . import prewarm_proof
    return prewarm_proof.prewarm_with_proof(project_root)

def _guard_baseline(root: str, project_root: str) -> tuple[bool, str]:
    """SEE-1571 §SPEC-507 §4：baseline 执行根双绑定（缺证 = 守卫不激活
    并披露；失配时调用方必须保持 baseline 计数 0 并走 MUST_REBUILD）。"""
    from . import prewarm_proof
    ok, reason = prewarm_proof.guard_baseline_root(root, project_root)
    if ok and reason.startswith("guard inactive"):
        print(f"mutation: baseline double-binding inactive ({reason})",
              file=sys.stderr)
    return ok, reason

def run_mutation_files(
    files: list[str],
    project_root: str,
    budget: int = 50,
    timeout_s: int = 600,
    dry_run: bool = False,
    tests_glob: str | None = None,
    jobs: int | None = None,
    binary_kind: str | None = None,
    timing_store=None,
    mutation_warning: int | None = None,
    timeout_factor: float | None = None,
    mutate_all: bool = False,
    reuse_coverage: bool = True,
    worker_channel: bool | None = None,
    select_mode: str = "full",
    break_on_baseline_failure: bool = False,
    kill_bail: bool | None = None,
) -> dict:
    """多文件 / 多 mutant 编排入口（§SPEC-202 运行级测量面挂载点）。

    dry-run（SPEC-009 零 GUT 契约）信封不加测量字段；常规路径在信封挂
    stage_timings / rss_peak_mb / disk_delta_bytes（只加不改），旋钮 set
    时挂 knobs——单文件 flat CLI 契约的信封键随 unwrap 丢失，单文件时
    投影进 summary（多文件语义归 run，逐文件不投影）。
    """
    # §SPEC-PLANFIRST：默认 run 的第一步 = plan（静态穷查 ≤10s）——
    # error 级问题即停并列问题（不跑任何重活）；无问题自动进入 prepare。
    from .plancheck import static_plan
    plan = static_plan(project_root, files[0] if files else None, tests_glob)
    plan_errors = [p for p in plan["problems"] if p["severity"] == "error"]
    print(f"mutation: plan {len(plan['problems'])} problem(s) "
          f"({len(plan_errors)} error) in {plan['elapsed_s']}s"
          + (" — BLOCKED" if (plan_errors and not dry_run) else ""),
          file=sys.stderr)
    if plan_errors and not dry_run:
        return {"tool": "mutation", "ok": False, "error": "plan_blocked",
                "plan": plan,
                "results": [{"file": f, "ok": False,
                             "summary": {"error": "plan_blocked",
                                         "plan": plan}}
                            for f in files]}
    if dry_run:
        return _run_mutation_files_impl(
            files, project_root, budget=budget, timeout_s=timeout_s,
            dry_run=dry_run, tests_glob=tests_glob, jobs=jobs,
            binary_kind=binary_kind, timing_store=timing_store,
            mutation_warning=mutation_warning,
            timeout_factor=timeout_factor, mutate_all=mutate_all,
            reuse_coverage=reuse_coverage, worker_channel=worker_channel,
            select_mode=select_mode,
            break_on_baseline_failure=break_on_baseline_failure,
            kill_bail=kill_bail)
    # §SPEC-PLANFIRST：prepare 启动披露（缺失前置将由既有惰性路径补齐，
    # 每步具名失败/独立超时保留）
    print("mutation: prepare filling missing preconditions "
          "(import -> map -> baseline) — see [[plan]] warnings above",
          file=sys.stderr)
    clock = StageClock()
    knob_state: dict = {}
    disk_before = qa_disk_bytes(project_root)
    t0 = time.monotonic()
    envelope = _run_mutation_files_impl(
        files, project_root, budget=budget, timeout_s=timeout_s,
        dry_run=dry_run, tests_glob=tests_glob, jobs=jobs,
        binary_kind=binary_kind, timing_store=timing_store,
        mutation_warning=mutation_warning, timeout_factor=timeout_factor,
        mutate_all=mutate_all, reuse_coverage=reuse_coverage,
        worker_channel=worker_channel, clock=clock, knob_state=knob_state,
        select_mode=select_mode,
        break_on_baseline_failure=break_on_baseline_failure,
        kill_bail=kill_bail)
    return _finalize_run_envelope(envelope, files, project_root, clock,
                                  knob_state, t0, disk_before)

def _finalize_run_envelope(envelope: dict, files: list[str],
                           project_root: str, clock: StageClock,
                           knob_state: dict, t0: float,
                           disk_before: int) -> dict:
    """常规路径信封装饰（§SPEC-202 只加不改）：总耗时与执行阶段合并、
    系统测量字段挂载、单文件投影、旋钮披露、E6-5 成本先验收尾写。"""
    wall_s = time.monotonic() - t0
    clock.add("total", wall_s)
    for r in envelope.get("results", []):
        merge_execution_stages(
            clock, (r.get("summary") or {}).get("stage_timings"))
    envelope["stage_timings"] = clock.disclosure()
    prewarm_info = envelope.get("prewarm") or {}
    envelope["stage_reconciliation"] = reconcile(
        envelope["stage_timings"], wall_s,
        prewarm_import_s=float(prewarm_info.get("import_wall_s") or 0.0),
        prewarm_decision=prewarm_info.get("decision"))
    envelope["rss_peak_mb"] = self_rss_peak_mb()
    envelope["disk_delta_bytes"] = qa_disk_bytes(project_root) - disk_before
    disc = knobs_disclosure(knob_state.get("baseline_in_farm"))
    if disc:
        envelope["knobs"] = disc
    _project_single_file(envelope, files, project_root)
    return envelope

def _project_single_file(envelope: dict, files: list[str],
                         project_root: str) -> None:
    """单文件 flat CLI 契约投影：信封键随 unwrap 丢失——运行级测量字段
    （磁盘增量、旋钮披露）语义上就是该文件的 run，投影进唯一文件的
    summary；并执行 E6-5 成本先验收尾写（§SPEC-502，单写者，对齐
    manifest 收尾单点写惯例；0 值保留旧先验合并——串行轮测不到
    prewarm/baseline 阶段，写 0 不得覆盖既有测量，消除逐轮翻转；
    短路/零执行轮无新信息，record 内跳过）。"""
    if not (len(files) == 1 and envelope.get("results")):
        return
    results = envelope["results"]
    results[0]["summary"]["disk_delta_bytes"] = \
        envelope["disk_delta_bytes"]
    if envelope.get("knobs"):
        results[0]["summary"]["knobs"] = envelope["knobs"]
    if envelope.get("prewarm"):
        results[0]["summary"]["prewarm"] = envelope["prewarm"]
    if envelope.get("stage_reconciliation"):
        results[0]["summary"]["stage_reconciliation"] = \
            envelope["stage_reconciliation"]
    summary = results[0].get("summary") or {}
    cost_priors.record(project_root, envelope.get("stage_timings") or {},
                       envelope.get("jobs") or 0,
                       int(summary.get("executed")
                           or summary.get("mutants") or 0))

def _zero_ref_terminal(file: str, total: int,
                       selection_meta: dict) -> dict | None:
    """SEE-1581：仅当新鲜映射明确判零测试（zero_ref）才短路。

    None 不再是零引用判据——无新鲜映射时 resolve_selection fail-loud；
    仅 `--select full` 显式审计面才以 None 继续。"""
    if total <= 0 or not selection_meta.get("zero_ref"):
        return None
    from .testscan import disclose_zero_ref, selection_disclosure, \
        zero_ref_short_circuit_result
    disclose_zero_ref(file)
    result = zero_ref_short_circuit_result(file)
    # 双披露与常规轮同源：`summary.zero_ref_short_circuit` 之外，
    # `test_selection.zero_ref` 及来源继续在场（L2 键名一致性）。
    result["summary"].update(selection_disclosure(selection_meta))
    result["summary"]["selection_mode"] = selection_meta.get(
        "selection_mode") or "line"
    return {"tool": "mutation", "ok": True, "jobs": 1, "results": [result]}

def _gate_collect_total(files: list[str], budget: int) -> int:
    """门内位点收集：目标缺失与串行路径同款 FileNotFoundError；收集异常
    交给 _build_chunks 定终态（返回 -1，error 契约）。"""
    if not Path(files[0]).is_file():
        raise FileNotFoundError(f"mutation target not found: {files[0]}")
    try:
        src = open(files[0], encoding="utf-8").read()
        return min(len(_collect_mutations(src, files[0])), budget)
    except Exception:
        return -1

def _gate_zero_ref(files, project_root, tests_glob, dry_run, total,
                   select_mode) -> tuple:
    """E2（§SPEC-501）零引用短路：派生 glob 零命中（非显式 --tests）且
    有可执行位点时，以披露记录替代全量 GUT 退路；门在 prewarm 之前。
    串行与并行共用 testscan 判据与记录形态——判据唯一出处 = 派生 glob。
    返回 (tests_glob, selection_meta, terminal_or_None)。"""
    selection_meta: dict = {}
    if tests_glob is None and not dry_run:
        derived, selection_meta = _auto_scan_tests_glob_meta(
            files[0], project_root, select_mode=select_mode)
        if derived is None:
            terminal = _zero_ref_terminal(files[0], total, selection_meta)
            if terminal is not None:
                return tests_glob, selection_meta, terminal
        else:
            tests_glob = derived
    return tests_glob, selection_meta, None

def _gate_serial_dispatch(files, project_root, *, budget, timeout_s,
                          tests_glob, jobs, total, binary_kind, timing_store,
                          mutation_warning, timeout_factor, mutate_all,
                          worker_channel, select_mode,
                          selection_meta,
                          break_on_baseline_failure: bool = False,
                          kill_bail: bool | None = None) -> dict | None:
    """E6-5（§SPEC-502）：成本感知仲裁，仍锚定在 prewarm/baseline 之前
    （退串行判定前置纪律不回退）。total > jobs 才需仲裁；无先验/先验
    不可用 = 并行缺省（KOL 量级不误降级）。"""
    cost_serial = (total > jobs
                   and cost_priors.prefers_serial(project_root, total,
                                                  jobs))
    if total == 0 or 0 < total <= jobs or cost_serial:
        r = run_mutation(files[0], project_root, budget=budget,
                         timeout_s=timeout_s, dry_run=False,
                         tests_glob=tests_glob,
                         binary_kind=binary_kind,
                         timing_store=timing_store,
                         mutation_warning=mutation_warning,
                         timeout_factor=timeout_factor,
                         mutate_all=mutate_all,
                         worker_channel=worker_channel,
                         select_mode=select_mode,
                         # 与门内扫描同源：避免二次扫描并把 zero_ref 语义
                         # 在改用例路径上丢失（SEE-1589 A-L2 实测）。
                         selection_meta=selection_meta,
                         break_on_baseline_failure=break_on_baseline_failure,
                         kill_bail=kill_bail)
        r["summary"].setdefault("jobs", 1)
        from .testscan import selection_disclosure
        r["summary"].update(selection_disclosure(selection_meta))
        return {"tool": "mutation", "ok": r["ok"], "jobs": 1,
                "results": [r]}
    return None

def _single_file_serial_gate(files: list[str], project_root: str, *,
                             budget: int, timeout_s: int,
                             tests_glob: str | None, jobs: int,
                             dry_run: bool,
                             binary_kind: str | None, timing_store,
                             mutation_warning: int | None,
                             timeout_factor: float | None,
                             mutate_all: bool,
                             worker_channel: bool | None,
                             select_mode: str = "full",
                             break_on_baseline_failure: bool = False,
                             kill_bail: bool | None = None) -> tuple[
        dict | None, str | None, dict]:
    """单文件门（判定先于任何 GUT 调用的前置纪律在调用方保持）。

    流程：目标缺失检查 → 位点收集 → E2 零引用短路（§SPEC-501）→
    E6-5 成本仲裁（§SPEC-502）→ 小规模/零位点串行分派。
    返回 (result, tests_glob, selection_meta)：result 非 None 即终态（短路
    或串行分派），None = 并行路径继续；tests_glob 与 §SPEC-613 选测 meta
    原样回传（派生 glob 回填后由并行路径 _build_chunks 同源消费）。
    """
    total = _gate_collect_total(files, budget)
    tests_glob, selection_meta, terminal = _gate_zero_ref(
        files, project_root, tests_glob, dry_run, total, select_mode)
    if terminal is not None:
        return terminal, tests_glob, {}
    # SEE-1587：行级（默认）/fn 分组需要 per-group glob，必须走编排分片
    # 路径（不做单文件串行快路径）；显式 --tests / full 审计模式维持原语义。
    if str(select_mode or "line").lower() in ("line", "fn") \
            and tests_glob is not None and not dry_run:
        return None, tests_glob, selection_meta
    ret = _gate_serial_dispatch(
        files, project_root, budget=budget, timeout_s=timeout_s,
        tests_glob=tests_glob, jobs=jobs, total=total,
        binary_kind=binary_kind, timing_store=timing_store,
        mutation_warning=mutation_warning, timeout_factor=timeout_factor,
        mutate_all=mutate_all, worker_channel=worker_channel,
        select_mode=select_mode, selection_meta=selection_meta,
        break_on_baseline_failure=break_on_baseline_failure,
        kill_bail=kill_bail)
    if ret is not None:
        return ret, tests_glob, selection_meta
    return None, tests_glob, selection_meta

def _impl_fast_path(files, project_root, budget, timeout_s, dry_run,
                    tests_glob, binary_kind, timing_store, mutation_warning,
                    timeout_factor, reuse_coverage, identity,
                    worker_channel, select_mode, jobs,
                    break_on_baseline_failure: bool = False,
                    kill_bail: bool | None = None,
                    mutate_all: bool = False) -> dict | None:
    """显式串行（--jobs 1）与 dry_run 快路径（判定先于 prewarm/baseline）。"""
    if len(files) == 1 and jobs == 1:
        return _serial_fast_path(files[0], project_root, budget, timeout_s,
                                 dry_run, tests_glob, binary_kind,
                                 timing_store, mutation_warning,
                                 timeout_factor, reuse_coverage, identity,
                                 worker_channel=worker_channel,
                                 select_mode=select_mode,
                                 break_on_baseline_failure=
                                 break_on_baseline_failure,
                                 kill_bail=kill_bail,
                                 mutate_all=mutate_all,
                                 jobs=jobs)
    if dry_run:
        return _dry_run_list(files, project_root, budget, timeout_s,
                             tests_glob, binary_kind, mutation_warning,
                             timeout_factor, reuse_coverage, identity,
                             worker_channel=worker_channel,
                             select_mode=select_mode, jobs=jobs)
    return None

def _impl_serial_gate(files, project_root, *, budget, timeout_s, tests_glob,
                      jobs, dry_run, binary_kind, timing_store,
                      mutation_warning, timeout_factor, mutate_all,
                      worker_channel, select_mode, clock,
                      break_on_baseline_failure: bool = False,
                      kill_bail: bool | None = None) -> tuple:
    """SEE-1413 退串行判定：必须在任何 GUT 调用（prewarm / 共享 baseline
    预跑）之前——变异点太少时走并发路径反而比纯串行慢。判定对缺省与
    显式 jobs 一体生效（§SPEC-002：总数 ≤ jobs 即退）；--jobs 1 在快路径
    已拦截。返回 (ret, tests_glob, selection_meta)。"""
    selection_meta: dict = {}
    if len(files) == 1:
        with clock.stage("planning"):
            ret, tests_glob, selection_meta = _single_file_serial_gate(
                files, project_root, budget=budget, timeout_s=timeout_s,
                tests_glob=tests_glob, jobs=jobs, dry_run=dry_run,
                binary_kind=binary_kind,
                timing_store=timing_store, mutation_warning=mutation_warning,
                timeout_factor=timeout_factor, mutate_all=mutate_all,
                worker_channel=worker_channel, select_mode=select_mode,
                break_on_baseline_failure=break_on_baseline_failure,
                kill_bail=kill_bail)
        if ret is not None:
            return ret, tests_glob, selection_meta
    return None, tests_glob, selection_meta

def _impl_parallel_envelope(files, project_root, budget, timeout_s,
                            tests_glob, jobs, binary_kind,
                            mutation_warning, timeout_factor, mutate_all,
                            reuse_coverage, identity, worker_channel, clock,
                            knob_state, selection_meta, select_mode,
                            prewarm_info, pool_factory,
                            break_on_baseline_failure: bool = False,
                            kill_bail: bool | None = None) -> dict:
    """并行分片执行与多文件信封组装（P1 差分父层单点注入 + settled 留痕
    + SEE-1416 attention 投影）。"""
    chunks, settled, v2ctx = _build_chunks(files, project_root, budget,
                                           timeout_s, tests_glob, jobs,
                                           binary_kind=binary_kind,
                                           mutation_warning=mutation_warning,
                                           timeout_factor=timeout_factor,
                                           mutate_all=mutate_all,
                                           reuse_coverage=reuse_coverage,
                                           identity=identity,
                                           worker_channel=worker_channel,
                                           clock=clock,
                                           knob_state=knob_state,
                                           selection_meta=selection_meta,
                                           select_mode=select_mode,
                                           break_on_baseline_failure=
                                           break_on_baseline_failure,
                                           kill_bail=kill_bail)
    chunk_results = []
    if chunks:
        # pool_factory = 模块变量 `_worker_pool` seam（可被替身注入）
        with pool_factory(max_workers=jobs) as pool:
            chunk_results = list(pool.map(_chunk_worker, chunks))
    merged = _merge_results(chunks, chunk_results, jobs)
    results_merged = merged.pop("results")
    # P1 差分（SEE-1420）：父层单点注入 reused 记录与差分披露，并写
    # manifest 索引层（worker 不写——并发竞态规避，D5 高风险通道裁决）。
    _inject_diff_results(results_merged, v2ctx)
    # settled（异常/零变异文件）按输入顺序并入 results——任何输入文件都留痕
    results = _order_results(files, results_merged, settled)
    _attach_chunk_selection(results, chunks)
    envelope = {**merged, "ok": all(r["ok"] for r in results),
                "results": results, "prewarm": prewarm_info}
    # SEE-1416 §SPEC-1416-3：多文件信封顶层 summary.attention——跨文件
    # 非 killed 明细合并投影（≤5 条，B 记录形态）；单文件 flat 契径不加
    # attention（保持既有形态，兼容判据 §SPEC-1416-3）。
    if len(files) > 1:
        envelope["summary"] = {"attention": _attention_details(results)}
    return envelope

def _impl_prep(files, project_root, binary_kind, clock, knob_state) -> tuple:
    """入口归一：§SPEC-610 相对目标锚定 + 身份包 + 时钟/旋钮态（调用方
    传入优先，缺省兜底）。"""
    files = anchor_targets_to_project_root(_group_by_file(files),
                                           project_root)
    kind = binary_kind or _detect_binary_kind()
    identity = identity_pack(project_root, kind)
    clock = clock or StageClock()
    knob_state = knob_state if knob_state is not None else {}
    return files, kind, identity, clock, knob_state

def _run_mutation_files_impl(
    files: list[str],
    project_root: str,
    budget: int = 50,
    timeout_s: int = 600,
    dry_run: bool = False,
    tests_glob: str | None = None,
    jobs: int | None = None,
    binary_kind: str | None = None,
    timing_store=None,
    mutation_warning: int | None = None,
    timeout_factor: float | None = None,
    mutate_all: bool = False,
    reuse_coverage: bool = True,
    worker_channel: bool | None = None,
    clock: StageClock | None = None,
    knob_state: dict | None = None,
    select_mode: str = "full",
    break_on_baseline_failure: bool = False,
    kill_bail: bool | None = None,
) -> dict:
    """多文件 / 多 mutant 编排：统一 JSON 契约摘要输出。

    阶段纪律与各语义见 _impl_fast_path / _impl_serial_gate /
    _impl_parallel_envelope 分解函数——本函数只做顺序编排。
    """
    files, _kind, identity, clock, knob_state = _impl_prep(
        files, project_root, binary_kind, clock, knob_state)
    fast = _impl_fast_path(files, project_root, budget, timeout_s, dry_run,
                           tests_glob, binary_kind, timing_store,
                           mutation_warning, timeout_factor, reuse_coverage,
                           identity, worker_channel, select_mode, jobs,
                           break_on_baseline_failure, kill_bail, mutate_all)
    if fast is not None:
        return fast
    jobs = default_jobs(jobs)
    ret, tests_glob, selection_meta = _impl_serial_gate(
        files, project_root, budget=budget, timeout_s=timeout_s,
        tests_glob=tests_glob, jobs=jobs, dry_run=dry_run,
        binary_kind=binary_kind, timing_store=timing_store,
        mutation_warning=mutation_warning, timeout_factor=timeout_factor,
        mutate_all=mutate_all, worker_channel=worker_channel,
        select_mode=select_mode, clock=clock,
        break_on_baseline_failure=break_on_baseline_failure,
        kill_bail=kill_bail)
    if ret is not None:
        return ret
    with clock.stage("prewarm"):
        prewarm_info = prewarm_import(project_root)
    return _impl_parallel_envelope(
        files, project_root, budget, timeout_s, tests_glob, jobs,
        binary_kind, mutation_warning, timeout_factor, mutate_all,
        reuse_coverage, identity, worker_channel, clock, knob_state,
        selection_meta, select_mode, prewarm_info,
        pool_factory=lambda max_workers: _worker_pool(max_workers=max_workers),
        break_on_baseline_failure=break_on_baseline_failure,
        kill_bail=kill_bail)

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

def _settled_all_uncovered(file: str, records: list[dict],
                           total_sites: int) -> dict:
    """SEE-1587：全部位点落在无映射测试行 → 定终态（零执行、uncovered 全
    披露、不进 ok 门禁）——零 chunk 也不得让文件从报告中消失。"""
    return {
        "tool": "mutation",
        "ok": True,
        "summary": {
            "file": file, "mutants": 0, "jobs": 0,
            "killed": 0, "survived": 0, "timeout": 0, "run_errors": 0,
            "suspect": 0, "invalid_mutants": 0, "kill_rate": 0.0,
            "scoped": True, "total_sites": total_sites,
            "uncovered": len(records),
            "note": ("all mutation sites fall on lines without mapped tests "
                     "(line-level selection; uncovered, not executed)"),
        },
        "failures": records,
    }

def _build_chunks(files: list[str], project_root: str, budget: int,
                  timeout_s: int, tests_glob: str | None, jobs: int,
                  binary_kind: str | None = None,
                  mutation_warning: int | None = None,
                  timeout_factor: float | None = None,
                  mutate_all: bool = False,
                  reuse_coverage: bool = True,
                  identity: dict | None = None,
                  worker_channel: bool | None = None,
                  clock: StageClock | None = None,
                  knob_state: dict | None = None,
                  selection_meta: dict | None = None,
                  select_mode: str = "full",
                  break_on_baseline_failure: bool = False,
                  kill_bail: bool | None = None,
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
    # A19（SEE-1594 S5）：baseline 共享判定必须使用实际生效的 select_mode
    # （缺省 full 会把自动选面 glob 判成 None → 多文件 baseline 不共享 →
    # chunk 退串行）。
    baselines, baseline_failures = _precompute_baselines(
        files, project_root, timeout_s, tests_glob, clock=clock,
        knob_state=knob_state, select_mode=select_mode)
    run_id = new_run_id()
    chunks: list[dict] = []
    settled: list[dict] = []
    v2ctx: dict = {}
    for f in files:
        file_chunks, file_ctx, file_settled = _plan_file_chunks(
            f, project_root, budget, timeout_s, tests_glob, jobs,
            binary_kind, mutation_warning, timeout_factor, mutate_all,
            reuse_coverage, baselines, baseline_failures, run_id,
            identity=identity, worker_channel=worker_channel, clock=clock,
            auto_selection=selection_meta, select_mode=select_mode,
            break_on_baseline_failure=break_on_baseline_failure,
            kill_bail=kill_bail)
        chunks.extend(file_chunks)
        if file_ctx is not None:
            v2ctx[f] = file_ctx
        if file_settled is not None:
            settled.append(file_settled)
    # SEE-1606 P2/P3：开关随每个分片穿透到 worker（单点盖章，避免各分片
    # 构造函数各写一遍）。
    for chunk in chunks:
        chunk["break_on_baseline_failure"] = break_on_baseline_failure
        chunk["kill_bail"] = kill_bail
    return chunks, settled, v2ctx

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
        # SEE-1615 §SPEC-PERF2：等价剪枝披露（父层单点注入 per-file summary）
        eq_disc = ctx.get("equivalent_pruned")
        if eq_disc and eq_disc.get("enabled"):
            r.setdefault("summary", {})["equivalent_pruned"] = eq_disc
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
        # §SPEC-609（SEE-1576 R-4）：failures[] 与常规路径 reporting 口径一致
        # （verdict != killed 才进）；killed 复用记录经 summary 计数 +
        # diff_skipped 呈现，不回放进 failures。
        if rec["verdict"] != "killed":
            r["failures"].append(rec)
        if rec["verdict"] in _VERDICT_SUMMARY_KEY:
            s[_VERDICT_SUMMARY_KEY[rec["verdict"]]] += 1
        s["mutants"] += 1
    s["resumed"] = len(ctx["plan"].reused)

def _inject_selection_block(s: dict, ctx: dict) -> None:
    """§SPEC-613：自动选测披露随报告透出（显式 --tests 时缺省缺席）；
    selection_mode / 并发上限披露按组数修正。"""
    from .testscan import (concurrency_disclosure, resolve_selection_mode,
                           selection_disclosure)
    s.update(selection_disclosure(ctx["test_selection"]))
    if ctx.get("selection_mode"):
        s["selection_mode"] = resolve_selection_mode(
            ctx["selection_mode"], ctx.get("test_selection"))
        groups = len(ctx.get("selection_groups") or []) or None
        cap = concurrency_disclosure(ctx["selection_mode"], groups,
                                     ctx.get("requested_jobs"))
        if cap:
            s.update(cap)
        elif ctx.get("selection_groups"):
            s["selection_groups"] = list(ctx["selection_groups"])

def _inject_uncovered(r: dict, s: dict, ctx: dict) -> None:
    """§SPEC-1420-12（P2）：uncovered 位点不执行、不进 ok 门禁，failures
    显式标记（计数与覆盖执行面在 summary.coverage_prefilter 披露）。"""
    uncovered = ctx.get("uncovered") or []
    pf_source = ctx.get("coverage_prefilter_source")
    if uncovered:
        reasons = ctx.get("uncovered_reasons") or {}
        for m in uncovered:
            r["failures"].append(_mutant_record(
                m, "uncovered",
                reasons.get(m.stable_id)
                or f"line {m.line} not covered by tests (reused coverage "
                   f"prefilter; site never executed)", src=ctx["src"]))
        # A08：summary.coverage_prefilter 唯一 writer（coverlink）
        s["coverage_prefilter"] = coverage_prefilter_summary(
            pf_source or None, len(uncovered), len(ctx["executed"]))
    elif isinstance(pf_source, str) and pf_source in COVERAGE_PREFILTER_SOURCES:
        # SEE-1587：prefilter 成功生效（零 uncovered）也必须披露来源——
        # 「默认路径不再常态 store miss」的证据面；miss 态不在此新增键。
        s["coverage_prefilter"] = coverage_prefilter_summary(
            pf_source, 0, len(ctx["executed"]))

def _inject_diff_disclosure(s: dict, ctx: dict) -> None:
    """§SPEC-1420-07（D6）+ O3 键集对齐：差分在场与 --mutate-all 两径披露
    同一键集（--mutate-all 全量面也可见：executed == retry_total、
    diff_skipped 空）。"""
    plan = ctx["plan"]
    if plan.diff_active or plan.mutate_all:
        s["diff_skipped"] = sorted(plan.reused)
        s["retry_total"] = len(plan.executed)
        s.setdefault("executed",
                     len(_valid_mutants(ctx["src"], ctx["executed"])))
        s["untouched_ranges"] = untouched_ranges(ctx["untouched"])

def _inject_one(r: dict, ctx: dict) -> None:
    """单文件收尾注入：reused/uncovered 记录 + 差分与覆盖披露 + gate 重算。"""
    plan = ctx["plan"]
    s = r["summary"]
    _inject_selection_block(s, ctx)
    if plan.reused:
        _inject_reused(r, ctx)
    _inject_uncovered(r, s, ctx)
    _inject_diff_disclosure(s, ctx)
    total = s["mutants"]
    s["kill_rate"] = round(s["killed"] / total, 4) if total else 0.0
    r["ok"] = (s["survived"] == 0 and s["timeout"] == 0
               and s["run_errors"] == 0 and s["suspect"] == 0)

