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
from .inject import detect_binary_kind
from .ops import Mutant
from .inject import forget_temp, godot_command, make_user_dir, mutant_env
from .gutrun import (_looks_like_godot_launch_failure, _parse_failing_tests)
from .ops import _apply_mutation, _collect_mutations
from .reporting import _run_error_result, _valid_mutants
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
    glob, _meta = _auto_scan_tests_glob_meta(file_path, project_root,
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
    return _auto_scan_tests_glob_meta(file_path, project_root,
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


def _run_mutant_loop_with_aborts(path, project_root, original_src, backup,
                                 executed_list, tests_glob, timeout_s,
                                 precomputed_baseline=None,
                                 store2=None, prior_entries=None,
                                 scopes=None,
                                 timeout_factor: float | None = None,
                                 reporter=None, identity=None,
                                 targeted_only: bool = False,
                                 worker_channel=None,
                                 clock: StageClock | None = None):
    """baseline 中止形态转统一 JSON（timeout / godot 启动失败）；否则返回 raw。"""
    try:
        from godot_qa_toolkit.mutation import runner as _r2
        return _r2._run_mutant_loop(
            path, project_root, original_src, backup,
            _valid_mutants(original_src, executed_list), tests_glob, timeout_s,
            precomputed_baseline, store2, prior_entries, scopes,
            timeout_factor, reporter=reporter, identity=identity,
            targeted_only=targeted_only, worker_channel=worker_channel,
            clock=clock,
        )
    except subprocess.TimeoutExpired:
        # SEE-1356 L1 (§SPEC-L1-03)：预算超时附机器可执行的补救指令。
        return _r2._run_error_result(
            str(path),
            f"baseline GUT timed out after {timeout_s}s — mutation data untrustworthy",
            f"baseline GUT timed out after {timeout_s}s (project's baseline run exceeds "
            f"the timeout — raise --timeout or check why tests are this slow)",
            hint=suggested_action(timeout_s),
            action_kind=suggested_action_kind(timeout_s),
        )
    except _BaselineLaunchFailure as e:
        return _r2._run_error_result(
            str(path),
            "godot launch failed on baseline — mutation data untrustworthy",
            f"godot launch failed on baseline run: {e.tail.strip()[:200]}",
        )
    except SlotProvisionError as e:
        # 影子槽制备失败（.gitignore fail-loud / 磁盘不足）——数据不可信，
        # 中止契约转统一 JSON（与 baseline 启动失败同档）。
        return _r2._run_error_result(
            str(path),
            "shadow slot provisioning failed — mutation data untrustworthy",
            f"shadow slot provisioning failed: {e}",
        )


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


def _run_baseline(project_root: str, tests_glob: str | None, timeout_s: int
                  ) -> tuple[int, str, float]:
    """跑 baseline 并计时；TimeoutExpired 由调用方按中止契约处理。"""
    from godot_qa_toolkit.mutation import runner as _r

    t0 = time.monotonic()
    rc, tail = _r._run_gut_on_project(project_root, timeout_s=timeout_s,
                                      tests_glob=tests_glob)
    return rc, tail, time.monotonic() - t0


def _run_cold_legs(path, project_root, original_src, backup, mutants,
                   tests_glob, baseline_failures, baseline_dirty, slot,
                   auto_name, target_rel, target_res, ownership_fn, reporter,
                   store2, prior_entries, scopes):
    """冷通道（M0）逐 mutant 执行循环（默认路径，行为与既有逐位不变）。"""
    from godot_qa_toolkit.mutation import runner as _r

    raw_pairs: list[tuple] = []
    for seq, m in enumerate(mutants):
        t_mutant = time.monotonic()
        verdict = _r._run_single_mutant(
            m, path, project_root, original_src, backup, tests_glob, seq,
            baseline_failures, baseline_dirty, slot=slot,
            auto_name=auto_name, ownership_fn=ownership_fn,
            target_rel=target_rel, target_res=target_res)
        raw_pairs.append((m, verdict))
        # SEE-1525 进度面唯一计数点：人眼进度行 + 机器可读分片。
        if reporter is not None:
            reporter.record(m, verdict, time.monotonic() - t_mutant)
        # 中断安全增量落盘：乐观 streak（毒窗口收尾修正）。
        # SEE-1405 发现三毒条目防护由 resume2.record_entry 承接
        # （启动失败类 run_error 拒收——verdict 零信息量）。
        if store2 is not None:
            prior = prior_entries.get(m.stable_id)
            streak, v2_verdict = streak_for(prior, verdict["verdict"])
            store2.record_entry(make_entry(m, verdict, v2_verdict, streak,
                                           scopes or {}, time.time()))
    return raw_pairs


def _loop_baseline(workdir, path, project_root, tests_glob, timeout_s,
                   precomputed_baseline, timeout_factor) -> tuple:
    """workdir 备份 + baseline 实测/复用 + 自适应 timeout 锚 + 启动失败守卫。
    baseline 永远实测（判定语义锚）；快照 elapsed 只喂预算锚。"""
    backup_path = os.path.join(workdir, "original.gd")
    shutil.copy2(str(path), backup_path)
    if precomputed_baseline is not None:
        baseline_rc = int(precomputed_baseline["rc"])
        baseline_tail = str(precomputed_baseline["tail"])
        baseline_elapsed = float(precomputed_baseline["elapsed"])
        baseline_reused = True
    else:
        # Revy QA retest 实证：baseline 必然花最久——timeout 漏网按 run_error
        # JSON 契约归一（与 per-mutant 循环同款处理）。
        from godot_qa_toolkit.mutation import runner as _r

        baseline_rc, baseline_tail, baseline_elapsed = _r._run_baseline(
            project_root, tests_glob, timeout_s)
        baseline_reused = False
    # SPEC-009 自适应 timeout：per-mutant 上限 = baseline 耗时 × F（只放大）。
    global _adaptive_timeout
    _adaptive_timeout = adaptive_timeout_for(baseline_elapsed, timeout_s,
                                             timeout_factor)
    if baseline_rc != 0 and _looks_like_godot_launch_failure(baseline_tail):
        raise _BaselineLaunchFailure(baseline_tail)
    baseline_failures = _parse_failing_tests(baseline_tail)
    return baseline_failures, bool(baseline_failures), baseline_reused, \
        baseline_elapsed


def _loop_slot(project_root, path, kind, identity, tests_glob,
               worker_channel, targeted_only, clock) -> tuple:
    """身份包/影子槽获取/路由解析与机制披露。返回 (slot, idpack, auto_name,
    target_rel, target_res, exec_scheme, route, slot_info_partial)。"""
    idpack = identity if identity is not None else \
        identity_pack(project_root, kind)
    target_rel = project_relative(project_root, str(path))
    target_res = _res_path_of(str(path), str(Path(project_root)))
    auto_name = beacon.autoload_name_for(project_root, target_res)
    t_slot = time.monotonic()
    with clock.stage("farm_provision"):
        slot = shadowpool.acquire_slot(
            project_root, kind, [target_rel],
            idpack.get("boot_wiring_hash", ""),
            targeted_only=targeted_only,
            required_globs=[tests_glob] if tests_glob else None)
    # R-DW6 默认启用实测轮：身份与执行同源解析（resolve_exec_scheme 统一
    # 路由排除 + gut.gd 直驱可用性）——coverage/resume store file_key 与
    # 实际执行通道一致，杜绝 scheme 漂移性 store-miss。
    with clock.stage("route_scan"):
        exec_scheme_resolved, route_resolved = resolve_exec_scheme(
            project_root, target_res, worker_channel)
    slot_info: dict = {}
    slot_info["exec_scheme"] = exec_scheme_resolved
    slot_info["worker_channel"] = build_worker_disclosure(
        project_root, target_res, worker_channel, exec_scheme_resolved,
        route_resolved)
    slot_info["farm"] = dict(getattr(slot, "farm", {}) or {})
    return (slot, idpack, auto_name, target_rel, target_res,
            exec_scheme_resolved, route_resolved, slot_info, t_slot)


def _loop_legs(path, project_root, original_src, backup, mutants, tests_glob,
               baseline_failures, baseline_dirty, slot, auto_name, target_rel,
               target_res, kind, idpack, exec_scheme_resolved, worker_channel,
               reporter, store2, prior_entries, scopes, clock, slot_info,
               t_slot) -> list:
    """beacon 播种 → worker/冷腿执行 → 槽归还断言与 slot_sync 计时。"""
    ownership_holder: list = []

    def _ownership():
        if not ownership_holder:
            from .truth_select import ownership_for
            ownership_holder.append(
                ownership_for(str(project_root), str(path)))
        return ownership_holder[0]

    use_worker = exec_scheme_resolved == WORKER_SCHEME
    timings_out: dict = {}
    try:
        beacon.seed_beacon_script(slot.root)
        if use_worker:
            from .worker_channel import execute_worker_branch
            with clock.stage("per_mutant"):
                raw_pairs, fatal_events = execute_worker_branch(
                    path, project_root, original_src, mutants, tests_glob,
                    baseline_failures, baseline_dirty, slot, auto_name,
                    target_rel, target_res, kind, reporter, store2,
                    prior_entries, scopes, _ownership,
                    timings_out=timings_out)
            clock.add("worker_boot",
                      timings_out.get("worker_boot_s", 0.0))
            if fatal_events:
                slot_info["worker_fatal_events"] = fatal_events
                slot_info["worker_channel"] = degraded_worker_disclosure(
                    slot_info.get("worker_channel") or {}, fatal_events)
        else:
            with clock.stage("per_mutant"):
                raw_pairs = _run_cold_legs(
                    path, project_root, original_src, backup, mutants,
                    tests_glob, baseline_failures, baseline_dirty, slot,
                    auto_name, target_rel, target_res, _ownership, reporter,
                    store2, prior_entries, scopes)
    finally:
        release_problems = shadowpool.release_slot(slot)
        # §SPEC-202 slot_sync = 槽获取→归还断言全程；槽位固定成本 = 其与
        # per_mutant 之差（§SPEC-210 LOW-1 口径修正）。
        clock.add("slot_sync", time.monotonic() - t_slot)
        if release_problems:
            slot_info["release_problems"] = release_problems
    return raw_pairs


def _run_mutant_loop(
    path, project_root: str, original_src: str, backup: str,
    mutants: list[Mutant], tests_glob: str | None, timeout_s: int,
    precomputed_baseline: dict | None = None,
    store2=None, prior_entries: dict | None = None, scopes: dict | None = None,
    timeout_factor: float | None = None, reporter=None,
    identity: dict | None = None, targeted_only: bool = False,
    worker_channel=None, clock: StageClock | None = None,
) -> tuple[list, bool, float, dict]:
    """逐 mutant 跑 GUT 并分类判定；返回 (raw_pairs, baseline_reused,
    baseline_elapsed, slot_info)。verdict 记录不在此处进 results——D5 streak
    是窗口级判定，由调用方 apply_timeout_streaks 落定后统一扩展。"""
    with tempfile.TemporaryDirectory() as workdir:
        (baseline_failures, baseline_dirty, baseline_reused,
         baseline_elapsed) = _loop_baseline(workdir, path, project_root,
                                            tests_glob, timeout_s,
                                            precomputed_baseline,
                                            timeout_factor)
        clock = clock or StageClock()
        (slot, idpack, auto_name, target_rel, target_res, scheme, _route,
         slot_info, t_slot) = _loop_slot(
            project_root, path, detect_binary_kind(), identity, tests_glob,
            worker_channel, targeted_only, clock)
        raw_pairs = _loop_legs(
            path, project_root, original_src, backup, mutants, tests_glob,
            baseline_failures, baseline_dirty, slot, auto_name, target_rel,
            target_res, detect_binary_kind(), idpack, scheme, worker_channel,
            reporter, store2, prior_entries, scopes, clock, slot_info, t_slot)
        slot_info["inject_scheme"] = idpack.get("inject_scheme", INJECT_SCHEME)
        slot_info["slot_mode"] = slot.mode
        slot_info["freshness"] = list(slot.freshness)
        if any(v["verdict"] == "survived" for _, v in raw_pairs):
            slot_info["suspected_orphan"] = _orphan_disclosure(
                project_root, target_res)
    return raw_pairs, baseline_reused, baseline_elapsed, slot_info



# per-mutant 自适应上限（baseline 耗时决定；runner 消费方经模块属性读取）。
_adaptive_timeout = 0
