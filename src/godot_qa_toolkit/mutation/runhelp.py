"""SEE-1594 BTIDY（零行为拆分）：mutation runner 支撑簇——glob/选面推导、
预算协商、执行收尾工具与逐 mutant 循环（baseline/槽/双腿）。自 runner.py
原行段物理迁移，逻辑逐语句等价。

**patch seam 纪律**：`_run_gut_on_project` 被测试钉在 `runner.*`（冷腿消费）
——`_run_cold_legs` 运行时经 runner 命名空间解析；自适应 timeout 全局
`_adaptive_timeout` 的消费方（`_run_single_mutant` / `_exec_finalize`）经
模块属性读取，保持「写入后同值可见」语义。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..timing import project_relative
from . import beacon
from . import shadow
from . import shadowpool
from .diffengine import apply_timeout_streaks, make_entry, streak_for
from .godot_gate import GlobalGodotGate
from .identity import INJECT_SCHEME, identity_pack
from .identity import identity_pack
from .inject import detect_binary_kind
from .ops import Mutant
from .inject import forget_temp, godot_command, make_user_dir, mutant_env
from .gutrun import (_looks_like_godot_launch_failure, _parse_failing_tests)
from .ops import _apply_mutation, _collect_mutations
from .reporting import (_mutation_report, _record_invalid_mutants,
                        _run_error_result, _valid_mutants)
from .planner import dry_run_estimate
from .progress import make_reporter
from .inject import register_temp
from .gutrun import GUT_SCRIPT_RES_PATH
from ..timing import suggested_action, suggested_action_kind
from .shadow import SlotProvisionError
from .reporting import _mutant_record
from .resume2 import ResumeStoreV2
from .scopemanifest import build_manifest
from .stagetiming import StageClock
from ..timing import explicit_timing_meta, resolve_budget

_TIMEOUT_SCALE = 2.0
from .identity import killer_certificate, suspected_orphan
from .worker_channel import (WORKER_SCHEME, build_worker_disclosure,
                             degraded_worker_disclosure, resolve_exec_scheme)

class _BaselineLaunchFailure(Exception):
    """baseline run 命中 godot 启动失败——mutant 数据不可信，run_mutation 层转中止契约。"""

    def __init__(self, tail: str):
        self.tail = tail
        super().__init__(tail[:200])



def _auto_scan_tests_glob(file_path: str, project_root: str,
                          select_mode: str = "full") -> str | None:
    """SPEC-001：目标 → 选面 glob（SEE-1581：运行时映射；full=显式全脸）。

    SEE-1581：自动选面唯一出处委托 _auto_scan_tests_glob_meta（运行时
    真值映射；A10 后静态强度选面已退役）。
    """
    from godot_qa_toolkit.mutation import runner as _r

    glob, _meta = _r._auto_scan_tests_glob_meta(file_path, project_root,
                                                select_mode=select_mode)
    return glob


def _resolve_auto_glob(tests_glob: str | None, file_path: str,
                       project_root: str,
                       selection_meta: dict,
                       select_mode: str = "full") -> tuple[str | None, dict]:
    """显式 `--tests` 直过（选测未发生，披露 meta 原样保留）；否则自动选测。

    §SPEC-613 判据唯一出处的守门形态：并行规划/串行门共用，禁止按 CLI
    参数推断选测是否发生。select_mode: line/fn/file（严格真值选面，不可用
    即 fail-loud）| full（显式审计全脸）。

    SEE-1587：显式 `--tests` 表示用户接管测试面——在 line/fn 模式下必须
    披露「未使用行级选面」（source=explicit_tests），不得静默。"""
    if tests_glob:
        if not selection_meta.get("source") \
                and str(select_mode).lower() in ("line", "fn"):
            selection_meta = {
                "source": "explicit_tests",
                "reason": "explicit --tests overrides line-level selection",
                "tests": [], "zero_ref": False, "mapping": None,
                "ambiguous_names": [],
            }
        return tests_glob, selection_meta
    from godot_qa_toolkit.mutation import runner as _r

    return _r._auto_scan_tests_glob_meta(file_path, project_root,
                                         select_mode=select_mode)


def _auto_scan_tests_glob_meta(file_path: str,
                               project_root: str,
                               select_mode: str = "full"
                               ) -> tuple[str | None, dict]:
    """SEE-1581：选面改运行时真值映射（A09 后静态归属/静态选面零消费者）。

    - 新鲜映射（recipe_hash + 目标文件 sha256 绑定）→ 精确测试脚本集，
      tests_glob 指向选择目录（`.qa-cache/gqt-attr-select/<id>/`）；
    - 映射说零测试 → zero_ref=True（零引用短路以真值为准）；
    - fn/file 面：无映射 / 过期 / 歧义 → resolve_selection fail-loud 拒跑
      （唯 `--select full` 显式审计面 tests_glob=None，不再派生静态 glob）。
    meta 由调用方挂入 summary.test_selection（source/reason/mapping 披露）。
    """
    from .truth_select import MutationSelectionError, resolve_selection
    if (select_mode or "fn").lower() == "full":
        return None, {"source": "full_face", "reason": "explicit_full_face",
                      "tests": [], "zero_ref": False, "mapping": None,
                      "ambiguous_names": []}
    # Owner 定稿：映射不可用 → fail-loud 拒跑（全脸仅显式审计开关）
    sel = resolve_selection(project_root, file_path)
    meta = {"source": sel["source"], "reason": sel["reason"],
            "mapping": sel.get("mapping"), "tests": sel.get("tests", []),
            "zero_ref": sel.get("zero_ref", False),
            "ambiguous_names": sel.get("ambiguous_names", [])}
    if sel["source"] == "runtime_mapping" and not sel.get("zero_ref"):
        print(f"mutation: runtime-mapping selection for {file_path} -> "
              f"{len(sel['tests'])} test script(s)", file=sys.stderr)
    elif sel.get("zero_ref"):
        print(f"mutation: runtime mapping says no test touches {file_path}",
              file=sys.stderr)
    # fn/file 不可用即 fail-loud（上方 resolve_selection 已抛），无静默
    # 回退路径；explicit full 腿的 tests_glob=None 是显式审计语义
    return sel["tests_glob"], meta


def _exec_prep_results(path, project_root, original_src, mutants, executed_list,
                       reused, results_uncovered, scope_hashes, disclosures,
                       budget, store2, header_hash, retry_ids, total_sites) -> tuple:
    """复用/invalid/uncovered 记录重建 + manifest 索引层先行写（中断安全；
    worker 注入路径不写——父层合并后单点写，规避并发竞态）。
    返回 (results, invalid_count)。"""
    # 复用记录重建（全签名已验）——串行路径在此落报告面；worker 路径的
    # reused 为空（父层合并后单点注入，避免分片重复计数）。
    results = _rebuild_reused_records(reused, mutants, original_src)
    # _record_invalid_mutants 返回新列表——与 reused/uncovered 记录拼接而非覆盖。
    # O2：invalid_mutant（语法预检剔除）从未进入 GUT 执行——执行面以
    # _valid_mutants 过滤后的可执行集合为准。
    invalid_results, invalid_count = _record_invalid_mutants(
        original_src, executed_list, None)
    results = results + results_uncovered + invalid_results
    # O2 键在场性守卫：executed 只在【披露语义在场】时出现——在场时以
    # actually_executed 覆写计划面值（Revy 实机 20 vs mutants 19 口径漂移）。
    if "executed" in disclosures:
        disclosures["executed"] = len(_valid_mutants(original_src,
                                                     executed_list))
    if retry_ids is None:
        store2.write_manifest(header_hash, scope_hashes,
                              ResumeStoreV2._sha1_text(original_src),
                              project_relative(project_root, str(path)))
    return results, invalid_count


def _exec_finalize(path, results, raw_pairs, prior_entries, scope_hashes,
                   store2, timing_store, baseline_reused, baseline_elapsed,
                   timing_meta, reused, invalid_count, budget, scoped,
                   disclosures, slot_info, original_src, clock) -> dict:
    """D5 streak 落定（权威口径）：streak/pinned 转化 + 毒 run 修正——
    循环内乐观落盘已保证中断安全，毒窗口时改写条目为未递增 streak。"""
    from godot_qa_toolkit.mutation import runner as _r

    clock.add("baseline", baseline_elapsed)
    streaks = apply_timeout_streaks(raw_pairs, prior_entries, scope_hashes,
                                    time.time())
    results.extend(streaks.records)
    if streaks.suspended:
        for e in streaks.entries:
            store2.record_entry(e)
        print("mutation: timeout streak suspended: suspected infra run",
              file=sys.stderr)
    _record_timing_sample(timing_store, baseline_reused, baseline_elapsed,
                          timing_meta)
    return _mutation_report(str(path), *_tally(results), invalid_count,
                            budget, results, scoped, baseline_reused,
                            timing_meta,
                            len(reused) if reused else None,
                            disclosures=disclosures or None,
                            pinned_count=len(streaks.pinned_ids),
                            slot_info=slot_info, original_src=original_src,
                            adaptive_timeout_s=_r._adaptive_timeout,
                            stage_timings=clock.disclosure())


def _sm_inject(m, slot, target_rel, target_res, original_src, project_root,
               path, auto_name) -> tuple:
    """mutant 注入影子槽（临时文件 + os.replace，引擎启动前落盘）+ beacon
    配置与进程 env 准备。返回 (mutated_src, cfg_path, user_dir)。"""
    mutated_src = _apply_mutation(original_src, m)
    if target_res == "":
        target_res = _res_path_of(str(path), str(Path(project_root)))
    if target_rel == "":
        target_rel = project_relative(project_root, str(path))
    try:
        mutant_sha = shadow.inject_mutant(slot.root, target_rel, mutated_src)
    except (OSError, SlotProvisionError) as e:
        return _mutant_record(
            m, "run_error",
            f"disk injection failed for {target_res}: {e}",
            src=original_src), None, None
    cfg_path = beacon.write_beacon_cfg(slot.root, target_res, mutant_sha,
                                       auto_name)
    register_temp(cfg_path)
    from godot_qa_toolkit.mutation import runner as _r

    user_dir = _r.make_user_dir()
    env_extra, _ = mutant_env(str(cfg_path), user_dir)
    return mutated_src, cfg_path, user_dir


def _sm_classify(m, rc, tail, baseline_failures, baseline_dirty, original_src,
                 ownership_fn, project_root) -> dict:
    """beacon 四级失败分类 + kill 判定 + L4 GUT 运行证据门 + killed 证书。"""
    # 四级 beacon 失败分类：load-null/parse → invalid_mutant；
    # 哈希不符/实例不同源/基础设施 → run_error（§SPEC-003）。
    beacon_verdict = beacon.classify_beacon_failure(rc, tail)
    if beacon_verdict is not None:
        return _mutant_record(
            m, beacon_verdict,
            f"beacon failed ({beacon_verdict}): {tail.strip()[:200]}",
            src=original_src)
    # classify 保持 5 参签名（interrupt 令牌测试以同签名 fake 替换）；
    # line_text 统一在消费侧补——所有 verdict 记录同构（SEE-1416）。
    from godot_qa_toolkit.mutation import runner as _r

    verdict = _r._classify_mutant_run(m, rc, tail, baseline_failures,
                                      baseline_dirty)
    verdict = {**verdict, "line_text": _r.line_text_of(original_src, m.line)}
    # L4 GUT 运行证据门：无运行流不得 killed/survived（§SPEC-003）。
    if verdict["verdict"] in ("killed", "survived") \
            and not _r._GUT_RUN_EVIDENCE.search(tail):
        return _mutant_record(
            m, "run_error",
            f"no GUT run stream evidence for "
            f"{m.kind} {m.original}→{m.mutated} — engine output "
            f"lacks test-run markers: {tail.strip()[:200]}",
            src=original_src)
    _attach_killed_certificate(verdict, tail, baseline_failures,
                               ownership_fn, project_root)
    return verdict


def _res_path_of(file_path: str, project_root: str) -> str:
    real = Path(file_path).resolve().as_posix()
    root = Path(project_root).resolve().as_posix()
    rel = real[len(root):].lstrip("/") if real.startswith(root) \
        else Path(file_path).name
    return "res://" + rel


def adaptive_timeout_for(baseline_elapsed: float, timeout_s: int,
                         factor: float | None = None) -> int:
    """§SPEC-1420-02：adaptive = max(timeout_s, baseline×F+1)。

    factor None → 模块默认 _TIMEOUT_SCALE（2.0，L5 定案"默认不动"）；
    显式 factor 只放大不收紧（低于显式 timeout 的派生值被显式值覆盖）。
    """
    k = _TIMEOUT_SCALE if factor is None else float(factor)
    return max(timeout_s, int(baseline_elapsed * k) + 1)


def _tally(results: list[dict]) -> tuple[int, int, int, int, int]:
    """results（含复用/invalid/streak 终态记录）→ 五档计数。"""
    tally = {"killed": 0, "survived": 0, "timeout": 0, "run_error": 0,
             "suspect": 0}
    for r in results:
        if r["verdict"] in tally:
            tally[r["verdict"]] += 1
    return (tally["killed"], tally["survived"], tally["timeout"],
            tally["run_error"], tally["suspect"])


def _record_timing_sample(timing_store, baseline_reused: bool,
                          baseline_elapsed: float, timing_meta: dict) -> None:
    """baseline 永远实测——只有亲自跑过 baseline 才采样（worker 不重复计数）。
    报告字段反映本轮采样后的 store 状态（samples_n/baseline_p90 为观察字段）。"""
    if timing_store is None or baseline_reused:
        return
    timing_store.record(baseline_elapsed)
    fresh = timing_store.read()
    if fresh:
        timing_meta["samples_n"] = fresh["samples_n"]
        timing_meta["baseline_p90"] = fresh["p90"]


def _negotiate_budget(timing_store, timeout_s: int,
                      precomputed_baseline: dict | None
                      ) -> tuple[int, dict]:
    """SEE-1356 L1 (§SPEC-L1-01)：预算协商。缓存只喂预算锚——baseline 永远
    实测（判定语义）；worker 分片（precomputed_baseline 携带者）跳过
    store 读写（executor 单进程拥有协商与采样）。"""
    timing_meta = explicit_timing_meta()
    if timing_store is None or precomputed_baseline is not None:
        return timeout_s, timing_meta
    hist = timing_store.read()
    anchor = hist["last"] if hist else None
    effective_timeout, timing_meta["timing_source"] = resolve_budget(
        timeout_s, anchor)
    timing_meta["samples_n"] = hist["samples_n"] if hist else 0
    timing_meta["baseline_p90"] = hist["p90"] if hist else None
    return effective_timeout, timing_meta


def _rebuild_reused_records(reused: dict, mutants: list,
                            original_src: str) -> list[dict]:
    """复用条目 → 报告记录重建（全签名已在 plan 阶段验证）。"""
    by_id_all = {m.stable_id: m for m in mutants}
    results: list[dict] = []
    for sid, entry in reused.items():
        m = by_id_all.get(sid)
        if m is not None:
            results.append(_mutant_record(m, entry["verdict"],
                                          entry.get("reason", ""),
                                          src=original_src))
    return results


def _orphan_disclosure(project_root: str, target_res: str) -> bool:
    """unreachable 不进 verdict、不 skip——静态零引用只做疑似孤儿披露。"""
    try:
        return suspected_orphan(project_root, target_res)
    except OSError as e:
        print(f"orphan scan failed for {target_res}: {e}", file=sys.stderr)
        return False


def _attach_killed_certificate(verdict: dict, tail: str,
                               baseline_failures: set[str], ownership_fn,
                               project_root: str) -> None:
    """killed 证书附挂（SEE-1581）：完整失败集 + 运行时归属。

    归属/闭包不确定（owner=None）→ 不附证书 → 下轮保守重跑；fail-safe
    只偏向重跑。"""
    if verdict["verdict"] != "killed" or ownership_fn is None:
        return
    owner = ownership_fn()
    if owner is None:
        return
    new_failures = _parse_failing_tests(tail) - baseline_failures
    cert = killer_certificate(
        new_failures, owner, project_root,
        ownership_source=getattr(owner, "source", "runtime_mapping"))
    if cert is not None:
        verdict["killed_by"] = cert


def _caliper_lines(mutants: list, select_ids: "list[str] | None",
                   select_lines: "list[int] | set[int] | None") -> set[int]:
    """卡尺选中位点的实际行集合（--mutation id 先解析到行；--lines 求交）。

    未命中任何位点的请求（stale id / 越界行）不在此处失败——交由
    planner._plan_selection 的显式错误契约统一收口（单一失败出处）。"""
    if select_ids:
        wanted = set(select_ids)
        return {m.line for m in mutants if m.stable_id in wanted}
    lines = {int(l) for l in (select_lines or [])}
    return {m.line for m in mutants if m.line in lines}


def _loop_slot(project_root, path, kind, identity, tests_glob,
               worker_channel, targeted_only, clock) -> tuple:
    """身份包/影子槽获取/路由解析与机制披露。返回 (slot, idpack, auto_name,
    target_rel, target_res, exec_scheme, slot_info, t_slot)。"""
    # patch seam：resolve_exec_scheme / time 被测试钉在 runner.*（原闭包经
    # run_measurement/run_mutation 模块全局解析）——运行时经 runner 命名
    # 空间解析，补丁点调用链不变。
    from godot_qa_toolkit.mutation import runner as _r

    idpack = identity if identity is not None else \
        identity_pack(project_root, kind)
    target_rel = project_relative(project_root, str(path))
    target_res = _res_path_of(str(path), str(Path(project_root)))
    auto_name = beacon.autoload_name_for(project_root, target_res)
    t_slot = _r.time.monotonic()
    with clock.stage("farm_provision"):
        slot = shadowpool.acquire_slot(
            project_root, kind, [target_rel],
            idpack.get("boot_wiring_hash", ""),
            targeted_only=targeted_only,
            required_globs=[tests_glob] if tests_glob else None)
    # R-DW6 默认启用实测轮：身份与执行同源解析——store file_key 与实际
    # 执行通道一致，杜绝 scheme 漂移性 store-miss。
    with clock.stage("route_scan"):
        exec_scheme_resolved, route_resolved = _r.resolve_exec_scheme(
            project_root, target_res, worker_channel)
    slot_info: dict = {}
    slot_info["exec_scheme"] = exec_scheme_resolved
    slot_info["worker_channel"] = build_worker_disclosure(
        project_root, target_res, worker_channel, exec_scheme_resolved,
        route_resolved)
    slot_info["farm"] = dict(getattr(slot, "farm", {}) or {})
    return (slot, idpack, auto_name, target_rel, target_res,
            exec_scheme_resolved, slot_info, t_slot)
