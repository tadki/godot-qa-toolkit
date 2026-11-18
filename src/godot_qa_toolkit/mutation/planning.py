"""SEE-1594 BTIDY（零行为拆分）：mutation 父层规划——单文件规划（收集
→ 差分 → prefilter → budget → 分片）、行级/fn 分组、baseline 预跑与
farm 槽制备。自 executor.py 原行段物理迁移；`_plan_file_chunks`、
`_line_group_chunks`、`_precompute_baselines` 分解为 ≤50 行阶段函数。

**patch seam 纪律**：被测试钉在 `executor.*` 上的名字（`run_mutation` /
`budget_on_retry` / `_apply_coverage_prefilter` / `_run_baseline_snapshot`
/ `_baseline_key` / `_BASELINE_FAILURE_REASONS` / `baseline_in_farm_requested`
/ settled 契约）一律经 `_x()` 运行时从 executor 模块命名空间解析——调用
链与拆分前逐点等价。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .stagetiming import StageClock
from .scopemanifest import build_manifest
from .resume2 import ResumeStoreV2
from .diffengine import plan_diff
from .identity import certificate_valid
from .reporting import _mutant_record
from .coverlink import MAP_PATH_UNRESOLVED, apply_coverage_prefilter
from .runner import _resolve_auto_glob, _res_path_of
from .coverlink import selection_map_path


def _x(name: str):
    """运行时经 executor 模块命名空间解析共享 helper（patch seam 保持）。"""
    import godot_qa_toolkit.mutation.executor as _e

    return getattr(_e, name)


def _warn_site_scale(f: str, raw_total: int,
                     mutation_warning: int | None) -> str | None:
    """§SPEC-1420-01：位点数超告警线 → 父层发一次告警，返回提示文本。

    按截断前全量位点数判定（budget 截断不掩盖规模问题）；未超线返回 None。"""
    if mutation_warning is None or raw_total <= mutation_warning:
        return None
    print(f"mutation-warning: {raw_total} mutation sites exceed "
          f"--mutation-warning {mutation_warning} in {f} — consider "
          f"splitting this file", file=sys.stderr)
    return (f"{raw_total} mutation sites exceed "
            f"--mutation-warning {mutation_warning} — consider "
            f"splitting this file")


def _ensure_shared_baseline(project_root: str, glob: str | None, jobs: int,
                            timeout_s: int, baselines: dict) -> dict | None:
    """SEE-1581：显式全脸审计面（`--select full`、无 scoped glob）也需要父层共享 baseline 快照。

    否则每文件退单 worker 串行、per-mutant 分片并行丢失（旧默认恒有
    scoped glob，此入口不存在）。一次全脸 baseline 换取 N 路分片。"""
    baseline = baselines.get(glob)
    if baseline is None and glob is None and jobs > 1:
        baseline = _x("_run_baseline_snapshot")(project_root, None,
                                                timeout_s)
        if baseline and baseline.get("rc") == 0:
            baselines[None] = baseline
        else:
            baseline = None
    return baseline
def _plan_collect(f: str) -> tuple:
    """收集变异位点；异常按 error 契约定终态。返回
    (src, collected, raw_total, settled_or_None)。"""
    try:
        src = open(f, encoding="utf-8").read()
        collected = _x("_collect_mutations")(src, f)
        return src, collected, len(collected), None
    except Exception as e:
        return None, None, None, _x("_settled_error")(f, e)


def _plan_resolve_gate(tests_glob, f, project_root, selection_meta,
                       select_mode, clock, raw_total, budget,
                       mutation_warning):
    """规模告警 → 选面解析 → no_sites / zero_ref 终态门。
    返回 (glob, selection_meta, attention, settled_or_None)。"""
    # §SPEC-1420-01：告警由父层发一次（并发 worker 不重复）——按截断前
    # 全量位点数判定，budget 截断不掩盖规模问题。
    attention = _warn_site_scale(f, raw_total, mutation_warning)
    selection_meta = dict(selection_meta or {})
    with clock.stage("test_scan"):
        glob, selection_meta = _resolve_auto_glob(tests_glob, f,
                                                  project_root,
                                                  selection_meta,
                                                  select_mode=select_mode)
    # 负/零 budget 与零位点同态：无执行面即 no_sites（jobs=0 契约），
    # 不得触发 baseline/prewarm。
    if raw_total <= 0 or budget <= 0:
        return glob, selection_meta, attention, _x("_settled_no_sites")(
            f, bool(glob))
    # E2（§SPEC-501）+ SEE-1581：自动面 glob=None 且 fresh 映射判零测试
    # （zero_ref）→ settled 披露记录；显式审计面 → tests_glob None 即
    # 全量面继续。
    if glob is None and selection_meta.get("zero_ref"):
        from .testscan import disclose_zero_ref, zero_ref_short_circuit_result
        disclose_zero_ref(f)
        return glob, selection_meta, attention, zero_ref_short_circuit_result(f)
    return glob, selection_meta, attention, None


def _plan_diff_phase(src, collected, f, project_root, identity,
                     worker_channel, mutate_all, glob,
                     binary_kind) -> tuple:
    """manifest 构建 + 身份注入（D-1 同通道同键）+ resume store 载入 +
    killed 证书差分门。返回 (header_hash, mutants, scope_hashes, identity,
    store2, plan)。"""
    header_hash, spans, mutants, scope_hashes = build_manifest(
        src, f, collected)
    from .worker_channel import identity_for_target
    identity, _route, _scheme = identity_for_target(
        identity, project_root, _res_path_of(f, project_root), worker_channel)
    store2 = ResumeStoreV2(project_root, f, glob, binary_kind=binary_kind,
                           identity=identity)
    old_manifest = store2.load_manifest()
    entries = store2.load_entries()
    # killed 证书复用门（SEE-1550 §SPEC-007）：定向校验 killer 闭包
    plan = plan_diff(header_hash, scope_hashes, old_manifest, mutants,
                     entries, mutate_all=mutate_all,
                     cert_validator=lambda kb: certificate_valid(kb,
                                                                 project_root))
    return header_hash, mutants, scope_hashes, identity, store2, plan


def _plan_budget_phase(plan, f, project_root, glob, binary_kind,
                       reuse_coverage, identity, selection_meta, clock,
                       timeout_s, budget, jobs, baselines) -> tuple:
    """prefilter（P2）→ budget 截断 → 共享 baseline 取用。
    返回 (uncovered, executed_list, untouched, baseline, disclosures)。"""
    prefilter_disclosures: dict = {}
    with clock.stage("prefilter"):
        uncovered_mutants, retry_pool = _x("_apply_coverage_prefilter")(
            plan, f, project_root, glob, binary_kind, reuse_coverage,
            identity=identity,
            tests_scripts=selection_meta.get("tests") or None,
            disclosures=prefilter_disclosures,
            map_path=selection_map_path(selection_meta))
    executed_list, untouched = _x("budget_on_retry")(retry_pool, budget)
    baseline = _ensure_shared_baseline(project_root, glob, jobs, timeout_s,
                                       baselines)
    return (uncovered_mutants, executed_list, untouched, baseline,
            prefilter_disclosures)


def _plan_serial_finish(f, project_root, budget, timeout_s, glob,
                        binary_kind, timeout_factor, mutate_all, baseline,
                        run_id, reuse_coverage, identity, worker_channel,
                        select_mode, selection_meta):
    """差分后零执行（全 reused / budget=0）：串行路径收尾（零 mutant
    GUT + 1 baseline 锚）。父层已预跑的共享 baseline 快照必须注入
    （SEE-1420 QA 实证：不注入则 run_mutation「永远实测」再跑一轮）。"""
    r = _x("run_mutation")(
        f, project_root, budget=budget,
        timeout_s=timeout_s, tests_glob=glob,
        binary_kind=binary_kind,
        mutation_warning=None, timeout_factor=timeout_factor,
        mutate_all=mutate_all,
        precomputed_baseline=baseline,
        progress={"run_id": run_id, "worker": "serial",
                  "total": 0},
        reuse_coverage=reuse_coverage,
        identity=identity,
        worker_channel=worker_channel,
        select_mode=select_mode)
    r["summary"].setdefault("jobs", 1)
    from .testscan import selection_disclosure
    r["summary"].update(selection_disclosure(selection_meta))
    return r


def _plan_serial_chunk(f, project_root, budget, timeout_s, glob, binary_kind,
                       timeout_factor, mutate_all, selection_meta,
                       select_mode, raw_total, attention, run_id,
                       executed_list, identity, worker_channel) -> dict:
    """baseline 快照失败 → 该文件整卷退回串行单任务（worker 自跑
    baseline 并自建差分报告——父层不注入，防重复计数）。"""
    return {
        "file": f, "project_root": project_root, "budget": budget,
        "timeout_s": timeout_s, "tests_glob": glob, "serial": True,
        "selection_meta": selection_meta, "selection_mode": select_mode,
        "binary_kind": binary_kind,
        "timeout_factor": timeout_factor, "total_sites": raw_total,
        "attention": attention, "mutate_all": mutate_all,
        "progress": {"run_id": run_id, "worker": "serial",
                     "total": len(executed_list)},
        "identity": identity,
        "worker_channel": worker_channel,
    }


def _plan_grouped_chunks(f, project_root, select_mode, glob, binary_kind,
                         executed_list, src, baseline, timeout_factor,
                         timeout_s, budget, mutate_all, attention, run_id,
                         identity, worker_channel, selection_meta,
                         raw_total, ctx) -> tuple:
    """SEE-1587：行级（默认）按实际位点的 frozenset(line→tests) 签名分组，
    同签名共享一个 glob；fn 为显式校准视图（按包围函数分组）。基线仍用
    文件级并集面——既有失败集是超集，killed 判定语义不变。
    返回 (chunks, ctx, settled_or_None)。"""
    if select_mode == "line":
        chunks, groups, no_mapped = _line_group_chunks(
            f, project_root, glob, binary_kind, executed_list,
            selection_meta, baseline, timeout_factor, timeout_s,
            budget, mutate_all, attention, run_id=run_id,
            identity=identity, worker_channel=worker_channel,
            total_sites=raw_total)
        # 无映射测试行的位点：默认路径按 uncovered 口径标记（不执行、
        # 不进 ok 门禁），与 prefilter uncovered 记录同一形态合并；
        # 原因按来源区分（行级无映射 ≠ prefilter 未覆盖）。
        ctx["uncovered"] = list(ctx.get("uncovered") or []) + no_mapped
        ctx.setdefault("uncovered_reasons", {}).update({
            m.stable_id: (f"line {m.line} has no mapped tests "
                          f"(line-level selection; site not executed)")
            for m in no_mapped})
        ctx["selection_groups"] = groups
        if not chunks:
            # 全部位点落在无映射行：零 chunk 也必须留文件痕迹（报告
            # 完备性——任何输入文件都不得从报告中消失）；ctx 不落盘
            # （零执行面无 reused/披露可注入，原语义返回 None）。
            return [], None, _x("_settled_all_uncovered")(
                f, [_mutant_record(
                    m, "uncovered",
                    f"line {m.line} has no mapped tests (line-level "
                    f"selection; site not executed)", src=src)
                    for m in no_mapped], raw_total)
        return chunks, ctx, None
    chunks, groups = _fn_group_chunks(
        f, project_root, glob, binary_kind, executed_list, src,
        baseline, timeout_factor, timeout_s, budget, mutate_all,
        attention, run_id=run_id, identity=identity,
        worker_channel=worker_channel, selection_meta=selection_meta,
        total_sites=raw_total)
    ctx["selection_groups"] = groups
    return chunks, ctx, None


def _plan_make_ctx(plan, executed_list, untouched, header_hash, scope_hashes,
                   store2, src, mutants, uncovered_mutants, selection_meta,
                   select_mode, jobs, prefilter_disclosures) -> dict:
    """v2ctx 组装（差分收尾注入与合并层的共享上下文）。"""
    return {
        "plan": plan, "executed": executed_list, "untouched": untouched,
        "header_hash": header_hash, "scope_hashes": scope_hashes,
        "store2": store2, "src": src, "mutants": mutants,
        "uncovered": uncovered_mutants, "test_selection": selection_meta,
        "selection_mode": select_mode, "requested_jobs": jobs,
        "coverage_prefilter_source": prefilter_disclosures.get(
            "coverage_prefilter"),
    }


def _plan_file_chunks(f, project_root, budget, timeout_s, tests_glob, jobs,
                      binary_kind, mutation_warning, timeout_factor,
                      mutate_all, reuse_coverage, baselines,
                      baseline_failures=None, run_id=None,
                      identity: dict | None = None,
                      worker_channel: bool | None = None,
                      clock: StageClock | None = None,
                      auto_selection: dict | None = None,
                      select_mode: str = "full"):
    """单文件规划：收集 → 差分 plan（killed 证书门）→ P2 覆盖过滤 →
    budget → 分片（返回 chunks, v2ctx_or_None, settled_or_None）。"""
    clock = clock or StageClock()
    src, collected, raw_total, settled = _plan_collect(f)
    if settled is not None:
        return [], None, settled
    glob, selection_meta, attention, gate_settled = _plan_resolve_gate(
        tests_glob, f, project_root, auto_selection or {}, select_mode,
        clock, raw_total, budget, mutation_warning)
    if gate_settled is not None:
        return [], None, gate_settled
    (header_hash, mutants, scope_hashes, identity, store2,
     plan) = _plan_diff_phase(src, collected, f, project_root, identity,
                              worker_channel, mutate_all, glob, binary_kind)
    (uncovered_mutants, executed_list, untouched, baseline,
     prefilter_disclosures) = _plan_budget_phase(
        plan, f, project_root, glob, binary_kind, reuse_coverage, identity,
        selection_meta, clock, timeout_s, budget, jobs, baselines)
    if not executed_list:
        return [], None, _plan_serial_finish(
            f, project_root, budget, timeout_s, glob, binary_kind,
            timeout_factor, mutate_all, baseline, run_id, reuse_coverage,
            identity, worker_channel, select_mode, selection_meta)
    if baseline is None:
        # baseline 快照失败 → 整卷退串行单任务（SEE-1525 显式降级告警）。
        if glob is not None:
            _warn_serial_fallback(f, (baseline_failures or {}).get(glob))
        return [_plan_serial_chunk(
            f, project_root, budget, timeout_s, glob, binary_kind,
            timeout_factor, mutate_all, selection_meta, select_mode,
            raw_total, attention, run_id, executed_list, identity,
            worker_channel)], None, None
    ctx = _plan_make_ctx(plan, executed_list, untouched, header_hash,
                         scope_hashes, store2, src, mutants,
                         uncovered_mutants, selection_meta, select_mode,
                         jobs, prefilter_disclosures)
    if select_mode in ("line", "fn") and glob is not None \
            and selection_meta.get("source") == "runtime_mapping":
        return _plan_grouped_chunks(
            f, project_root, select_mode, glob, binary_kind, executed_list,
            src, baseline, timeout_factor, timeout_s, budget, mutate_all,
            attention, run_id, identity, worker_channel, selection_meta,
            raw_total, ctx)
    chunks = _slice_retry_ids(f, project_root, budget, timeout_s, glob,
                              binary_kind, [m.stable_id for m in executed_list],
                              baseline, timeout_factor, raw_total, attention,
                              jobs, run_id=run_id, identity=identity,
                              worker_channel=worker_channel,
                              select_mode=select_mode)
    return chunks, ctx, None

def _fid_of_line(registry: dict, line: int) -> str | None:
    for entry in (registry or {}).get("entries") or []:
        if not entry.get("instrumentable"):
            continue
        start, end = entry.get("start_line"), entry.get("end_line")
        if isinstance(start, int) and isinstance(end, int) \
                and start <= line <= end:
            return entry["fid"]
    return None


def _fn_group_chunks(f, project_root, file_glob, binary_kind, executed_list,
                     src, baseline, timeout_factor, timeout_s, budget,
                     mutate_all, attention, *, run_id, identity,
                     worker_channel, selection_meta, total_sites):
    """按函数分组出 chunk：每 chunk 独立选择目录 glob，基线共享文件级面。

    函数不在映射（unknown/新函数）→ 文件级回退并披露（source=
    file_fallback）；组间由 worker 池并发复用 warm 槽。"""
    from ..registry.functions import build_file_registry
    from . import truth_select

    rel = _res_path_of(f, project_root).removeprefix("res://")
    registry = build_file_registry(src, file_label=rel)
    groups: dict[str | None, list] = {}
    for m in executed_list:
        fid = _fid_of_line(registry, getattr(m, "line", -1))
        groups.setdefault(fid, []).append(m)
    file_selection = {
        "source": "runtime_mapping", "zero_ref": False,
        "tests": selection_meta.get("tests") or [],
        "tests_glob": file_glob,
        "mapping": selection_meta.get("mapping"),
    }
    chunks: list[dict] = []
    meta: list[dict] = []
    for fid, mutants in sorted(groups.items(), key=lambda kv: str(kv[0])):
        try:
            sel = truth_select.resolve_function_selection(
                project_root, f, fid or "", file_selection=file_selection)
        except truth_select.MutationSelectionError:
            raise
        group_glob = sel.get("tests_glob") or file_glob
        source = ("file_fallback" if fid is None else sel.get("source"))
        reason = ("module_scope" if fid is None else sel.get("reason"))
        meta.append({"function_id": fid, "source": source, "reason": reason,
                     "tests": len(sel.get("tests") or []),
                     "mutants": len(mutants)})
        chunks.append({
            "file": f, "project_root": project_root, "budget": budget,
            "timeout_s": timeout_s, "tests_glob": group_glob,
            "binary_kind": binary_kind,
            "timeout_factor": timeout_factor, "total_sites": total_sites,
            "attention": attention, "mutate_all": mutate_all,
            "retry_ids": [m.stable_id for m in mutants],
            "baseline": baseline,
            "progress": {"run_id": run_id, "worker": f"fn:{fid or 'module'}",
                         "total": len(executed_list)},
            "identity": identity, "worker_channel": worker_channel,
        })
    return chunks, meta

def _line_map_sets(f, project_root, selection_meta, executed_list) -> tuple:
    """S2.3 守卫（runtime_mapping 必携带 resolved map path——缺失 = 具名
    fail-loud，绝不回退默认路径重猜）+ 映射装载 + 行/名双表。"""
    from . import truth_select

    mapping_path = (selection_meta.get("mapping") or {}).get("path")
    if selection_meta.get("source") == "runtime_mapping" and not mapping_path:
        raise truth_select.MutationSelectionError(
            "selection_metadata_inconsistent",
            "line_group_chunks: source=runtime_mapping without a resolved "
            "map path — refusing to re-guess the default attribution map")
    rel = _res_path_of(f, project_root).removeprefix("res://")
    try:
        doc = truth_select.load_map(
            mapping_path) if mapping_path else truth_select.load_map(
                truth_select.map_path(str(Path(project_root).resolve())))
    except Exception as e:  # noqa: BLE001 — 不可用即 fail-loud（不静默回退）
        raise truth_select.MutationSelectionError(
            "line_selection_map_unusable", str(e)) from e
    by_line = truth_select._line_script_sets(
        doc, rel, {getattr(m, "line", -1) for m in executed_list})
    by_name = truth_select._test_scripts_by_name(
        str(Path(project_root).resolve()))
    return by_line, by_name


def _line_partitions(executed_list: list, by_line: dict) -> tuple:
    """位点按行测试集签名分组；无映射行不静默丢弃（uncovered 口径返回）。"""
    groups: dict[frozenset, list] = {}
    no_mapped: list = []          # Mutant 对象（与 prefilter uncovered 同形态）
    for m in executed_list:
        names = frozenset(by_line.get(getattr(m, "line", -1)) or ())
        if not names:
            no_mapped.append(m)
            continue
        groups.setdefault(names, []).append(m)
    return groups, no_mapped


def _line_chunk_for(f, project_root, file_glob, binary_kind, names, mutants,
                    *, baseline, timeout_factor, timeout_s, budget,
                    mutate_all, attention, run_id, identity, worker_channel,
                    total_sites, selection_meta, by_name, seq,
                    executed_list) -> tuple:
    """单签名组 → 物化选择目录 glob → chunk。脚本漂移（行事实指向的脚本
    不在 tests 树）按 uncovered 口径返回（与无映射行同形态）。"""
    from . import truth_select

    scripts, ambiguous = truth_select._resolve_script_paths(by_name,
                                                            names)
    if ambiguous:
        raise truth_select.MutationSelectionError(
            "ambiguous_test_names", str(sorted(ambiguous)[:5]))
    if not scripts:
        return None, mutants, None
    try:
        group_glob = truth_select.materialize_selection(
            str(Path(project_root).resolve()), scripts)
    except OSError as e:
        raise truth_select.MutationSelectionError(
            "selection_materialize_failed", str(e)) from e
    meta = {"signature": sorted(names), "tests": len(scripts),
            "mutants": len(mutants)}
    chunk = {
        "file": f, "project_root": project_root, "budget": budget,
        "timeout_s": timeout_s, "tests_glob": group_glob,
        "binary_kind": binary_kind,
        "timeout_factor": timeout_factor, "total_sites": total_sites,
        "attention": attention, "mutate_all": mutate_all,
        "retry_ids": [m.stable_id for m in mutants],
        "baseline": baseline,
        # SEE-1587：worker 侧 prefilter 投影需要该 glob 实际承载的测试
        # 脚本集（组内签名），不用文件级并集冒充组面。
        "selection_meta": {**selection_meta, "tests": scripts,
                           "source": "runtime_mapping",
                           "reason": "line_mapping_fresh"},
        "selection_mode": "line",
        "progress": {"run_id": run_id,
                     "worker": f"line:{seq}",
                     "total": len(executed_list)},
        "identity": identity, "worker_channel": worker_channel,
    }
    return chunk, meta, None


def _line_group_chunks(f, project_root, file_glob, binary_kind, executed_list,
                       selection_meta, baseline, timeout_factor,
                       timeout_s, budget, mutate_all, attention, *, run_id,
                       identity, worker_channel, total_sites):
    """SEE-1587 行级分组：按位点所在行的测试集签名 `frozenset(line→tests)`
    分组，同签名共享一个物化选择目录 glob（组数 = 不同测试集合数）。

    返回 (chunks, group_meta, uncovered_records)。"""
    by_line, by_name = _line_map_sets(f, project_root, selection_meta,
                                      executed_list)
    groups, no_mapped = _line_partitions(executed_list, by_line)
    chunks: list[dict] = []
    meta: list[dict] = []
    for names, mutants in sorted(groups.items(),
                                 key=lambda kv: (len(kv[0]), sorted(kv[0]))):
        chunk, m, extra = _line_chunk_for(
            f, project_root, file_glob, binary_kind, names, mutants,
            baseline=baseline, timeout_factor=timeout_factor,
            timeout_s=timeout_s, budget=budget, mutate_all=mutate_all,
            attention=attention, run_id=run_id, identity=identity,
            worker_channel=worker_channel, total_sites=total_sites,
            selection_meta=selection_meta, by_name=by_name,
            seq=len(meta) + 1, executed_list=executed_list)
        if chunk is None:
            no_mapped.extend(extra)
            continue
        chunks.append(chunk)
        meta.append(m)
    return chunks, meta, no_mapped


def _attach_chunk_selection(results: list[dict], chunks: list[dict]) -> None:
    """串行回退/无 ctx 的 chunk 也要带选面披露（观测面不因降级而丢失）。"""
    from .testscan import selection_disclosure
    by_file = {r.get("summary", {}).get("file"): r for r in results}
    for ch in chunks:
        meta = ch.get("selection_meta")
        r = by_file.get(ch.get("file"))
        if not meta or r is None:
            continue
        r["summary"].update(selection_disclosure(meta))
        if ch.get("selection_mode"):
            r["summary"].setdefault("selection_mode", ch["selection_mode"])


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
                              reuse_coverage, identity=None,
                              tests_scripts=None, disclosures=None,
                              map_path=MAP_PATH_UNRESOLVED):
    """§SPEC-1420-12（P2）+ SEE-1550 L1 + SEE-1587 + SEE-1594 S2：coverage
    前置过滤先于 budget（预算只花在 covered 位点）；uncovered 全部显式标记
    （父层合并时注入报告）。

    数据源统一经 coverlink.resolve_coverage_for_prefilter（/2 投影 store →
    行级主真值现场投影；map_path = selection 已解析映射路径，A06），两层
    全失才回退不过滤。
    D3 修复：过滤锚 = covered ∧ instrumentable——不可插桩行上的位点留在
    执行面（与 planner._apply_coverage_prefilter 同语义）；可插桩集不可得
    = 不过滤（宁多跑不假阴性）。"""
    # A07（SEE-1594 S3）：应用逻辑单一 helper 在 coverlink，本函数只保留
    # 形参适配；D3 锚（执行面 = covered ∨ 不可插桩）/miss 披露口径与
    # planner 路径同源。
    return apply_coverage_prefilter(
        plan.executed, f, project_root, glob, tests_scripts, binary_kind,
        identity, reuse_coverage, disclosures, map_path)


def _slice_retry_ids(f, project_root, budget, timeout_s, glob, binary_kind,
                     ids, baseline, timeout_factor, raw_total,
                     attention, jobs, run_id=None, identity=None,
                     worker_channel=None, select_mode: str = "full"):
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
            # resume 已退役（A13）：worker payload 不再携带

            "timeout_factor": timeout_factor,
            "total_sites": raw_total,
            "attention": attention,
            "progress": {"run_id": run_id, "worker": f"w{i}",
                         "total": len(ids)},
            "identity": identity,
            "worker_channel": worker_channel,
            "selection_mode": select_mode,
        })
    return chunks

def _prepare_baseline_root(project_root: str, knob_state: dict | None,
                           clock: StageClock,
                           globs: dict) -> tuple[str, object, tuple | None]:
    """baseline 执行根制备（§SPEC-210 farm 槽 opt-in / §SPEC-507 §4 双绑定）。

    制备段整体计入 clock 的 "baseline" stage。返回 (farm_root, farm_slot,
    veto)：veto 非 None 时为 (baselines, failures) 终态——真实仓重签证后
    仍漂移，baselines 收缴为 0（MUST_REBUILD 纪律），调用方直接返回。
    """
    farm_root, farm_slot = project_root, None
    with clock.stage("baseline"):
        if knob_state is not None and _x(
                "baseline_in_farm_requested")():
            farm_root, farm_slot = _provision_farm_slot(
                project_root, knob_state)
        else:
            # §SPEC-507 §4：真实仓 baseline 前双绑定（缺证 = 守卫不激活，
            # 如实披露；失配 → MUST_REBUILD 重签证一次）。
            ok, reason = _x("_guard_baseline")(farm_root, project_root)
            if not ok:
                print(f"mutation: real-repo baseline drift ({reason}) — "
                      f"reissuing prewarm proof before baseline",
                      file=sys.stderr)
                from . import prewarm_proof
                prewarm_proof.reissue_after_drift(project_root)
                ok, reason = _x("_guard_baseline")(farm_root, project_root)
                if not ok:
                    print(f"mutation: real-repo proof still drifted "
                          f"({reason}) — baselines withheld (count=0)",
                          file=sys.stderr)
                    return farm_root, farm_slot, ({},
                                                  {g: f"prewarm proof drift "
                                                      f"persists: {reason}"
                                                   for g in set(globs.values())
                                                   if g is not None})
    return farm_root, farm_slot, None


def _provision_farm_slot(project_root: str,
                         knob_state: dict) -> tuple[str, object]:
    """§SPEC-210 opt-in farm 槽制备 + §SPEC-507 §4 执行根双绑定。

    复制后、任何 baseline 启动前重算 input+output 双摘要；失配废弃重建
    一次（MUST_REBUILD），仍失配退回真实仓；制备失败同样退回真实仓
    （D5 权威不动，降级 stderr 告警）。返回 (farm_root, farm_slot)。
    """
    from .shadow import SlotProvisionError, create_slot

    try:
        root, _prov = create_slot(project_root, "linux")
        farm_root, farm_slot = str(root), root
        ok, reason = _x("_guard_baseline")(farm_root, project_root)
        if not ok:
            print(f"mutation: baseline-in-farm root drift "
                  f"({reason}) — rebuilding slot once",
                  file=sys.stderr)
            shutil.rmtree(farm_slot, ignore_errors=True)
            root, _prov = create_slot(project_root, "linux")
            farm_root, farm_slot = str(root), root
            ok, reason = _x("_guard_baseline")(farm_root, project_root)
        if not ok:
            print(f"mutation: baseline-in-farm root still "
                  f"drifted ({reason}) — baseline falls back to "
                  f"the real repo", file=sys.stderr)
            return project_root, None
        knob_state["baseline_in_farm"] = True
        return farm_root, farm_slot
    except SlotProvisionError as e:
        knob_state["baseline_in_farm"] = False
        print(f"mutation: baseline-in-farm requested but slot "
              f"provisioning failed ({e}) — baseline runs on the "
              f"real repo (D5 authority unchanged)",
              file=sys.stderr)
        return project_root, None


def _auto_scan_tests_glob(file_path: str, project_root: str,
                          select_mode: str = "full") -> str | None:
    """父层推导 scoped glob（与 worker 侧 run_mutation 内推导同口径）。"""
    from .runner import _auto_scan_tests_glob as _runner_scan
    return _runner_scan(file_path, project_root)


def _baseline_globs(files, project_root, tests_glob, clock: StageClock,
                    select_mode: str) -> dict:
    """每文件 baseline 共享 glob 判定（显式相同 / 全量 / 自动扫描推导）；
    单文件选面不可用 → glob=None 跳过预跑——逐文件的 fail-loud/settled
    归属后续 _plan_file_chunks。"""
    from .runner import _auto_scan_tests_glob

    globs = {}
    for f in files:
        with clock.stage("test_scan"):
            if tests_glob:
                g = tests_glob
            else:
                # A19：baseline 共享判定使用实际生效 select_mode。
                from .truth_select import MutationSelectionError
                try:
                    g = _auto_scan_tests_glob(f, project_root,
                                              select_mode=select_mode)
                except MutationSelectionError as e:
                    print(f"baseline pre-run: selection unavailable for "
                          f"{f} ({e}) — skipping shared baseline for this "
                          f"file", file=sys.stderr)
                    g = None
        globs[f] = g
    return globs


def _precompute_baselines(files, project_root, timeout_s, tests_glob,
                          clock: StageClock | None = None,
                          knob_state: dict | None = None,
                          select_mode: str = "full"):
    """并发分派【之前】单进程冷启动 baseline（SPEC-005 锚点）。

    复用条件（SPEC-008）：同一 tests_glob 跨文件共享一份；代码状态 =
    目标文件此刻都是仓库原始态，快照不落盘，禁止跨 mtime 复用。

    §SPEC-203（opt-in）：GQT_MUTATION_BASELINE_IN_FARM 置位时 baseline 在
    ext4 影子槽副本内实测——缺省与制备失败都落在真实仓（D5 权威不动）。
    返回 (baselines, failures)：failures 只含**尝试过且失败**的 glob →
    原因摘要；glob=None（全量面未预跑）不在其中。
    """
    clock = clock or StageClock()
    globs = _baseline_globs(files, project_root, tests_glob, clock,
                            select_mode)
    baselines: dict = {}
    failures: dict[str, str] = {}
    farm_slot = None
    try:
        # §SPEC-210 MED-1：stage 粒度单计——制备段与逐 glob 快照段各自成
        # 一段；制备开销（opt-in 时）仍计入 baseline 阶段总额，占比归因
        # 口径不变。
        farm_root, farm_slot, veto = _x("_prepare_baseline_root")(
            project_root, knob_state, clock, globs)
        if veto is not None:
            return veto
        for g in set(globs.values()):
            if g is None:
                continue
            with clock.stage("baseline"):
                baseline = _x("_run_baseline_snapshot")(farm_root, g,
                                                        timeout_s)
            if baseline and baseline.get("rc") == 0:
                baselines[g] = baseline
            else:
                failures[g] = _x("_BASELINE_FAILURE_REASONS").pop(
                    _x("_baseline_key")(farm_root, g),
                    "no baseline snapshot returned")
    finally:
        if farm_slot is not None:
            shutil.rmtree(farm_slot, ignore_errors=True)
    return baselines, failures
