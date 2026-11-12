"""Mutation runner: source-level GDScript mutants + kill/survive/timeout verdicts.

SEE-1268 自研（gdtoolkit lark AST 定位 → 源码 span 替换 → 跑 GUT 看该 mutant
是否被杀死）。kill rate 永不进验收证据（owner-order L5 条文）。

P0' 判定原则落点：每个 mutant 输出机器可判的 killed/survived/timeout 布尔 +
结构化 failures；exit 0=全部杀死 1=有存活/超时。

SEE-1312 增强：变异点扩展（三元/守卫/第二梯队算子/常量）、span 替换定位、
--dry-run/--tests/自适应 timeout 成本控制、invalid_mutant 预检与 suspect 标签。
"""

from __future__ import annotations

import os
import shutil
import signal  # noqa: F401  # 测试经 mrunner.signal monkeypatch signal.signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 变异算子机制在 ops.py（runner <800 行约束拆分）；re-export 保持既有
# import / monkeypatch 名称空间稳定。
from .ops import (  # noqa: F401
    Mutant,
    _apply_arm_swap,
    _apply_mutation,
    _apply_mutation_fallback,
    _collect_mutations,
    _cond_span,
    _line_offsets,
    _span_mutants_for,
    _span_token_ops,
    _split_invalid_mutants,
    _subtree_has_mutable_node,
    _ternary_mutants,
    _token_mutants_for,
    _token_pos,
    _tree_depth_walk,
    mk_guard_not,
)

from .inject import (forget_temp, godot_command,
                     detect_binary_kind,
                     godot_project_path as _unified_godot_project_path,
                     make_user_dir, mutant_env, register_temp)
from . import beacon
from . import shadow
from . import shadowpool
from .shadow import SlotProvisionError
from .identity import (COLD_SCHEME, INJECT_SCHEME, TestOwnershipIndex,
                       WORKER_SCHEME, identity_pack, killer_certificate,
                       suspected_orphan)
from .channel_router import CHANNEL_WORKER, route_target
from .stagetiming import StageClock, self_rss_peak_mb
from .worker_channel import (identity_for_target, resolve_exec_scheme,
                             run_worker_legs, worker_enabled)
# 统一 mutant 明细记录形态（SEE-1416 扩列）、结果塑形契约与统一报告构建
# （SEE-1550 tidy 迁入）在 reporting.py（<800 行纪律拆分先例）；调用方留在
# runner，by-value import 经本命名空间解析——既有 monkeypatch seam 零变化。
from .reporting import (
    _attention_note, _classify_mutant_run, _mutant_record,
    _no_sites_result, _record_invalid_mutants, _require_baseline_snapshot,
    _run_error_result,
    _setup_error_result,
    _mutation_report,
    _valid_mutants,
    classify_worker_leg, line_text_of,)
from .godot_gate import GlobalGodotGate
from .diffengine import apply_timeout_streaks, make_entry, streak_for
from .planner import dry_run_estimate, plan_execution
from .progress import make_reporter
from .resume2 import ResumeStoreV2
from .scopemanifest import build_manifest
from ..timing import (BUDGET_CAP_S, explicit_timing_meta, project_relative,
                      resolve_budget, suggested_action)


# GUT 执行域判定原语在 gutrun.py（SEE-1420 <800 行纪律拆分）；re-export
# 保持既有 import / monkeypatch 名称空间稳定。
from .gutrun import (  # noqa: F401,E402
    GUT_SCRIPT_RES_PATH,
    _GUT_RUN_EVIDENCE,
    _looks_like_godot_launch_failure,
    _parse_failing_tests,
)


def _godot_project_path(project_root: str) -> str:
    """--path 语义随 detect_binary_kind() 同源（§SPEC-1525-002）：委托
    inject.godot_project_path 统一原语——win64 通道 /mnt/ 转 Windows 形式
    （行为与 af728f5 逐字节一致）、linux 通道原生不转换，转换子进程行为
    与 coverage 共用一份，勿在此模块再分叉。"""
    return _unified_godot_project_path(project_root)


def _run_gut_on_project(project_root: str, timeout_s: int = 60,
                        tests_glob: str | None = None) -> tuple[int, str]:
    """在项目里 headless 跑 GUT；返回 (exit_code, 输出尾部)。

    -s 用 res:// 路径（绝对路径会让 godot 拒绝加载）；脚本存在性仍由调用方
    校验（文件系统检查），加载失败由 _looks_like_godot_launch_failure 识别。
    tests_glob（SPEC-009）：受影响测试子集（如 res://tests/save/），替代全量
    -gdir=res://tests/ 以压缩单轮成本。
    SEE-1321 SPEC-006/007：子进程 env 注入（XDG_DATA_HOME / GQT_MUTATION_CFG）
    与入口脚本切换经模块级 _ACTIVE_ENV/_ACTIVE_SCRIPT 传递——保持函数签名
    向后兼容（既有令牌测试以 3 参 fake 替换本函数）。
    SEE-1550：mutant 轮的 project_root 是影子槽，跨进程槽位闸必须仍锚真实仓
    （"GlobalGodotGate 锚真实仓"既有语义）——闸锚经模块级 _ACTIVE_GATE_ROOT
    传递（缺省回落 project_root，3 参 fake 契约零变化）。
    §SPEC-015：真实 Godot 项目缺导入缓存时在此 fail-fast（首个 GUT 调用即
    拦截——baseline 之前，绝不放行进逐位点 timeout 级联）。
    """
    shadow.require_import_cache(project_root)
    gut_fs = os.path.join(project_root, "addons", "gut", "gut_cmdln.gd")
    if not os.path.isfile(gut_fs):
        raise FileNotFoundError(f"GUT runner not found: {gut_fs}")
    gdir = tests_glob if tests_glob else "res://tests/"
    # -gtest 接受的是「脚本完整路径列表」，但会在装满所有收集脚本后仍全量跑
    # （GUT 9.6 实测：单脚本 -gtest 展开 251 scripts/2718 tests）——不能作
    # 文件级子集。文件路径改走 -gselect=脚本名（GUT 按 -select_script 子集
    # 过滤收集结果，实测精确 1 script），目录仍走 -gdir（SEE-1316 实测）。
    if gdir.endswith(".gd"):
        script_stem = Path(gdir).stem
        extra = [
            "-gdir=res://tests/",
            f"-gselect={script_stem}",
        ]
    else:
        extra = [f"-gdir={gdir}"]
    env = dict(os.environ)
    if _ACTIVE_ENV:
        env.update(_ACTIVE_ENV)
    # SEE-1400 §SPEC-003：跨进程槽位闸包裹真实 godot 子进程（等槽时间不计
    # timeout——timeout 语义只管测试进程本身）。签名与 3 参 fake 契约不变。
    with GlobalGodotGate(_ACTIVE_GATE_ROOT or project_root):
        r = subprocess.run(
            [godot_command(), "--headless", "--path",
             _godot_project_path(project_root),
             "-s", _ACTIVE_SCRIPT, *extra, "-gexit"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=project_root,
            env=env,
        )
    # 失败测试名解析需要完整运行流（'-*', '* test_x'、[Failed] 行）——2000
    # 字符的窗口在长断言输出下会把失败行挤出窗外导致 kill 判定失真（
    # SEE-1321 实测 pricing_resolver or→and 29 项新失败被吞成 survived）。
    # SPEC-017（§SPEC-016 实证）：注入宿主的 printerr 全落 stderr——GUT 运行
    # 流落 stdout，而 GQT_INJECT_FAIL 只落 stderr。旧逻辑 stdout 非空时丢弃
    # stderr，rc==3 注入失败在 stdout 里只有 73 字节引擎 banner，
    # "GQT_INJECT_FAIL in tail" 守卫永不命中 → 注入失败被吞成 survived
    # （12/17 假 survived）。修复：tail = 运行流 + stderr 尾部拼接，两者都
    # 进判定窗口；注入失败与 GUT 运行流互斥出现，拼接不会互相挤出。
    tail = (r.stdout[-200000:] if r.stdout else "") + "\n--- gqt: stderr ---\n" + r.stderr[-10000:]
    return r.returncode, tail


# 子进程环境/入口脚本的活动状态（mutant 循环内动态切换，见 _run_single_mutant）
_ACTIVE_ENV: dict | None = None
_ACTIVE_SCRIPT: str = GUT_SCRIPT_RES_PATH
# 槽位闸锚（SEE-1550）：mutant 轮指向真实仓根（闸必须跨 worker 全局计数，
# 不能锚到各 worker 各自的影子槽）；baseline 轮为 None → 回落 project_root。
_ACTIVE_GATE_ROOT: str | None = None


# 自适应 timeout 倍数：per-mutant 上限 = baseline 实测耗时 × 该倍数。
_TIMEOUT_SCALE = 2.0


def adaptive_timeout_for(baseline_elapsed: float, timeout_s: int,
                         factor: float | None = None) -> int:
    """§SPEC-1420-02：adaptive = max(timeout_s, baseline×F+1)。

    factor None → 模块默认 _TIMEOUT_SCALE（2.0，L5 定案"默认不动"）；
    显式 factor 只放大不收紧（低于显式 timeout 的派生值被显式值覆盖）。
    """
    k = _TIMEOUT_SCALE if factor is None else float(factor)
    return max(timeout_s, int(baseline_elapsed * k) + 1)



def run_mutation(    file_path: str,
    project_root: str,
    budget: int = 50,
    timeout_s: int = 60,
    dry_run: bool = False,
    tests_glob: str | None = None,
    precomputed_baseline: dict | None = None,
    mutant_slice: tuple[int, int] | None = None,
    timing_store=None,
    resume: bool = False,
    binary_kind: str | None = None,
    mutation_warning: int | None = None,
    timeout_factor: float | None = None,
    mutate_all: bool = False,
    retry_ids: list[str] | None = None,
    resume2_store=None,
    select_ids: list[str] | None = None,
    select_lines: list[int] | None = None,
    reuse_coverage: bool = True,
    progress: dict | None = None,
    identity: dict | None = None,
    worker_channel: bool | None = None,
) -> dict:
    """单文件 mutation 入口（§SPEC-202 单位级测量面挂载点）。

    dry-run / 零位点 / setup 错误契约保持既有 summary 形态（零键增量）；
    完整报告路径在 summary 挂 stage_timings（8 阶段全键）+ rss_peak_mb
    （self VmHWM）——total 在包装层补记（覆盖核心体全程墙钟）。
    """
    t0 = time.monotonic()
    result = _run_mutation_core(
        file_path, project_root, budget=budget, timeout_s=timeout_s,
        dry_run=dry_run, tests_glob=tests_glob,
        precomputed_baseline=precomputed_baseline,
        mutant_slice=mutant_slice, timing_store=timing_store,
        resume=resume, binary_kind=binary_kind,
        mutation_warning=mutation_warning, timeout_factor=timeout_factor,
        mutate_all=mutate_all, retry_ids=retry_ids,
        resume2_store=resume2_store, select_ids=select_ids,
        select_lines=select_lines, reuse_coverage=reuse_coverage,
        progress=progress, identity=identity, worker_channel=worker_channel)
    s = result.get("summary") if isinstance(result, dict) else None
    if isinstance(s, dict) and "stage_timings" in s:
        s["stage_timings"]["total"] = round(time.monotonic() - t0, 3)
        s["rss_peak_mb"] = self_rss_peak_mb()
    return result


def _run_mutation_core(file_path: str,
    project_root: str,
    budget: int = 50,
    timeout_s: int = 60,
    dry_run: bool = False,
    tests_glob: str | None = None,
    precomputed_baseline: dict | None = None,
    mutant_slice: tuple[int, int] | None = None,
    timing_store=None,
    resume: bool = False,
    binary_kind: str | None = None,
    mutation_warning: int | None = None,
    timeout_factor: float | None = None,
    mutate_all: bool = False,
    retry_ids: list[str] | None = None,
    resume2_store=None,
    select_ids: list[str] | None = None,
    select_lines: list[int] | None = None,
    reuse_coverage: bool = True,
    progress: dict | None = None,
    identity: dict | None = None,
    worker_channel: bool | None = None,
) -> dict:
    """对单个 .gd 文件做变异测试并出统一 JSON 契约。

    budget 控制 mutant 上限（mutation 风暴防御——100 变异 × GUT 全套 = 时间爆炸）。
    kill 判定 = 失败测试名集合相对 baseline 的差集（mutant 引入的新失败），
    而非 exit code——pre-existing failure 不算 killed。
    dry_run（SPEC-009）：只清点变异点不跑 GUT——CI 快闸与成本评估入口。
    tests_glob（SPEC-009）：受影响测试子集，passed to _run_gut_on_project。
    precomputed_baseline（SEE-1321 SPEC-008）：同次运行内共享的 baseline
    快照 {rc, tail, elapsed}——单进程冷启动产物，自适应 timeout 锚点。
    mutant_slice（SEE-1321 SPEC-003 修订）：per-mutant 并发 worker 的分片，
    只跑 [start, stop) 的 mutants；必须携带共享 baseline（worker 各自跑
    baseline 会 N 倍放大启动税且并发期耗时污染 timeout 锚）。
    resume（SEE-1400 §SPEC-002）：v1 遗留开关。SEE-1420 起差分**默认开启**
    （manifest 在场即差分，定案 D4）；--mutate-all 显式退路；本 flag 仅保留
    summary.resumed 键在场性（无复用时为 0），语义已被差分吸收。
    mutate_all（§SPEC-1420-07）：--mutate-all 显式全量——忽略旧 manifest，
    全位点执行（仍落盘 v2）。
    retry_ids（并发 worker 注入面）：父层（executor）已做差分规划与预算
    截断，worker 只执行这些 stable_id；注入时 worker 不再自建 plan、
    不写 manifest 索引层（父层单点写，规避并发竞态）。
    mutation_warning（§SPEC-1420-01）：按截断前全量位点数计数的告警阈值
    （默认 50）——超阈值 stderr 告警 + summary.attention 建议拆分。
    timeout_factor（§SPEC-1420-02）：覆盖 _TIMEOUT_SCALE 的自适应倍数
    （默认 None=2.0 不动）；两者均不进任何 resume key（定案 D6 负面清单）。
    progress（SEE-1525）：父层注入的进度上下文 {run_id,total,worker}；缺省
    自建（串行 / 单点寻址路径）。进度只写 stderr 与 `.qa-cache`，绝不影响
    stdout 单 JSON 契约。
    reuse_coverage（SEE-1550 L1）：coverage prefilter **默认自动启用**
    （store fresh ∧ 身份包全匹配才生效，任一漂移禁用并披露；uncovered 不进
    ok 门禁、不跑 GUT）；--no-reuse-coverage 显式关断。
    identity（SEE-1550）：缓存身份包（executor 父层计算后随 chunk 注入，
    保证多 worker 同 key）；缺省自建（含 godot --version，按 path+mtime 缓存）。
    注入机制 = 磁盘影子注入（disk-farm/1，终案 §3）：mutant 经 os.replace
    写影子内目标 `.gd`，引擎启动前从磁盘读到变异体。
    返回 {"tool":"mutation", "ok", "summary", "failures"}；ok=True 表示
    所有 mutant 均被测试杀死。
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"mutation target not found: {file_path}")

    _require_baseline_snapshot(mutant_slice, retry_ids, precomputed_baseline)

    clock = StageClock()
    if tests_glob is None:
        with clock.stage("test_scan"):
            tests_glob = _auto_scan_tests_glob(str(path), project_root)
    scoped = tests_glob is not None

    original_src = path.read_text(encoding="utf-8")
    backup = original_src  # 永不改写原文件之外的任何东西（kill rate 证据隔离）

    kind = binary_kind or detect_binary_kind()
    target_res_early = _res_path_of(str(path), str(project_root))
    # SEE-1550 §SPEC-020：exec_scheme 按目标路由入身份包（worker 显式启用且
    # 路由判 worker 才 worker-reset/1；A9 默认冷）。parent 传入的 identity
    # 用同源解析覆盖 scheme——父子两侧 file_key 一致。
    identity, _route, exec_scheme = identity_for_target(
        identity, project_root, target_res_early, worker_channel)
    if identity is None:
        identity = identity_pack(project_root, kind, exec_scheme=exec_scheme)

    try:
        raw_mutants = _collect_mutations(original_src, str(path))
    except Exception as e:
        return _setup_error_result(str(path), e)

    # P1 manifest 标定（定案 D1/D2/D3 + L6）：scope 提取 → no-mutate 过滤
    # → stable id。total_sites 按过滤后位点数计（无位点 scope 不进 manifest）。
    header_hash, spans, mutants, scope_hashes = build_manifest(
        original_src, str(path), raw_mutants)
    total_sites = len(mutants)
    attention = _attention_note(mutation_warning, total_sites, str(path))

    if dry_run:
        return dry_run_estimate(
            str(path), mutants, budget, scoped, total_sites,
            project_root, tests_glob, binary_kind, resume2_store,
            mutate_all, header_hash, scope_hashes, attention=attention,
            identity=identity)

    if not mutants:
        return _no_sites_result(str(path), scoped)

    # P1 差分（定案 D4）：串行路径自建 plan；worker 注入路径只执行 retry_ids；
    # 显式寻址（--mutation/--lines 卡尺）强制新执行，绕过差分复用。
    store2 = resume2_store if resume2_store is not None else ResumeStoreV2(
        project_root, str(path), tests_glob, binary_kind=binary_kind,
        identity=identity)
    prior_entries, reused, untouched, disclosures, executed_list, \
        results_uncovered = plan_execution(
            path, project_root, mutants, header_hash, scope_hashes, budget,
            tests_glob, binary_kind, resume2_store, mutate_all, retry_ids,
            select_ids, select_lines, reuse_coverage, attention,
            mutant_slice, original_src, identity=identity, clock=clock)

    # budget 吃光且无可报告复用——零 mutant 不得触发 baseline GUT（硬化
    # 反例实证：白付一轮 108s baseline）。
    if not executed_list and not reused:
        return _no_sites_result(str(path), scoped)
    if total_sites > budget:
        disclosures["total_sites"] = total_sites
        # O2：此处 executed 仍为计划面预估——_execute_planned 在 invalid
        # 预检后以 actually_executed 覆写（实际执行语义的唯一出处）。
        disclosures.setdefault("executed", len(executed_list))

    return _execute_planned(
        path, project_root, original_src, backup, mutants, executed_list,
        reused, results_uncovered, prior_entries, scope_hashes, disclosures,
        budget, tests_glob, timing_store, timeout_s, precomputed_baseline,
        store2, timeout_factor, header_hash, retry_ids, resume, scoped,
        total_sites, progress=progress, identity=identity,
        targeted_only=bool(select_ids or select_lines),
        worker_channel=worker_channel, clock=clock)


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


def _execute_planned(path, project_root, original_src, backup, mutants,
                     executed_list, reused, results_uncovered, prior_entries,
                     scope_hashes, disclosures, budget, tests_glob,
                     timing_store, timeout_s, precomputed_baseline, store2,
                     timeout_factor, header_hash, retry_ids, resume, scoped,
                     total_sites, progress=None, identity=None,
                     targeted_only=False, worker_channel=None,
                     clock: StageClock | None = None):
    """执行规划面 → D5 streak 落定 → 统一 JSON 报告（run_mutation 的执行尾）。"""
    clock = clock or StageClock()
    if not executed_list and not reused:
        return _no_sites_result(str(path), scoped)
    if total_sites > budget:
        disclosures["total_sites"] = total_sites
        disclosures.setdefault("executed", len(executed_list))

    # 复用记录重建（全签名已验）——串行路径在此落报告面；worker 路径的
    # reused 为空（父层合并后单点注入，避免分片重复计数）。
    results = _rebuild_reused_records(reused, mutants, original_src)
    # _record_invalid_mutants 返回新列表——与 reused/uncovered 记录拼接而非覆盖。
    # O2：invalid_mutant（语法预检剔除）从未进入 GUT 执行——执行面以
    # _valid_mutants 过滤后的可执行集合为准，summary.executed 报实际执行数
    # 而非计划面（Revy 实机 20 vs mutants 19 实证口径漂移）。
    invalid_results, invalid_count = _record_invalid_mutants(
        original_src, executed_list, None)
    results = results + results_uncovered + invalid_results
    # O2 键在场性守卫：executed 只在【披露语义在场】（差分/寻址/截断披露）
    # 时出现——无披露语义的常规 run 保持既有 summary 形态（零键增量）；
    # 在场时以 actually_executed（invalid 预检剔除后的真实 GUT 执行数）
    # 覆写计划面值（Revy 实机 20 vs mutants 19 实证口径漂移）。
    if "executed" in disclosures:
        disclosures["executed"] = len(_valid_mutants(original_src,
                                                     executed_list))
    # P1：manifest 索引层先行写入（中断安全——索引在场 + 条目增量，下轮
    # 差分对已执行子集正确复用，未落盘位点按 new_site 重跑，保守正确）。
    # worker 注入路径不写（父层合并后单点写，规避并发竞态）。
    if retry_ids is None:
        store2.write_manifest(header_hash, scope_hashes,
                              ResumeStoreV2._sha1_text(original_src),
                              project_relative(project_root, str(path)))

    effective_timeout, timing_meta = _negotiate_budget(
        timing_store, timeout_s, precomputed_baseline)

    # SEE-1525 进度面：分母 = 本 worker 真实 GUT 执行面（invalid 预检剔除后）；
    # 并行分片由父层注入全局分母（progress["total"]，含 invalid 位点），此时
    # done 从本 worker 已判定的 invalid 数起算（判定在 make_reporter 内），
    # 双路径都不会停在 [n-1/n]。
    reporter = make_reporter(project_root, str(path), progress,
                             len(executed_list) - invalid_count,
                             invalid_count)

    # 磁盘影子注入（SEE-1550 M0）：目标文件写入全部发生在影子槽内，真实仓
    # 目标文件零写入——中断/硬杀不可能留变异残留进 git（守卫语义由槽归还
    # 断言与下轮定向三向承接，不再需要真实仓恢复守卫）。
    outcome = _run_mutant_loop_with_aborts(
        path, project_root, original_src, backup, executed_list,
        tests_glob, effective_timeout, precomputed_baseline,
        store2, prior_entries, scope_hashes, timeout_factor,
        reporter=reporter, identity=identity, targeted_only=targeted_only,
        worker_channel=worker_channel, clock=clock)
    if isinstance(outcome, dict):
        return outcome  # baseline 中止契约（timeout / launch failure）

    raw_pairs, baseline_reused, baseline_elapsed, slot_info = outcome
    # §SPEC-202：本 run 消费的 baseline GUT 耗时入账（复用父层快照时
    # elapsed 同样入账——占比归因口径：成本付过就该可见）。
    clock.add("baseline", baseline_elapsed)
    resumed_count = len(reused)

    # D5 落定（权威口径）：streak/pinned 转化 + 毒 run 修正——循环内乐观
    # 落盘已保证中断安全，此处毒窗口时改写条目为未递增 streak。
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
                            resumed_count if (resumed_count or resume) else None,
                            disclosures=disclosures or None,
                            pinned_count=len(streaks.pinned_ids),
                            slot_info=slot_info, original_src=original_src,
                            adaptive_timeout_s=_adaptive_timeout,
                            stage_timings=clock.disclosure())


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


def _auto_scan_tests_glob(file_path: str, project_root: str) -> str | None:
    """SPEC-001：目标 → 扫描 tests/ 引用子集；零命中退回全量（scoped=False）。

    SEE-1405 §SPEC-004：project_root 显式传给 derive_tests_glob——内部
    换源 gdself 正向索引（间接消费闭包可见）；扫描失败诚实退全量。
    """
    try:
        from .testscan import derive_tests_glob
        return derive_tests_glob(_res_path_of(file_path, project_root),
                                 Path(project_root) / "tests",
                                 project_root=project_root)
    except Exception as e:
        print(f"auto scan failed, falling back to full suite: {e}",
              file=sys.stderr)
        return None


def _res_path_of(file_path: str, project_root: str) -> str:
    real = Path(file_path).resolve().as_posix()
    root = Path(project_root).resolve().as_posix()
    rel = real[len(root):].lstrip("/") if real.startswith(root) \
        else Path(file_path).name
    return "res://" + rel


def _tally(results: list[dict]) -> tuple[int, int, int, int, int]:
    """results（含复用/invalid/streak 终态记录）→ 五档计数。"""
    tally = {"killed": 0, "survived": 0, "timeout": 0, "run_error": 0,
             "suspect": 0}
    for r in results:
        if r["verdict"] in tally:
            tally[r["verdict"]] += 1
    return (tally["killed"], tally["survived"], tally["timeout"],
            tally["run_error"], tally["suspect"])


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
        return _run_mutant_loop(
            path, project_root, original_src, backup,
            _valid_mutants(original_src, executed_list), tests_glob, timeout_s,
            precomputed_baseline, store2, prior_entries, scopes,
            timeout_factor, reporter=reporter, identity=identity,
            targeted_only=targeted_only, worker_channel=worker_channel,
            clock=clock,
        )
    except subprocess.TimeoutExpired:
        # SEE-1356 L1 (§SPEC-L1-03)：预算超时附机器可执行的补救指令。
        return _run_error_result(
            str(path),
            f"baseline GUT timed out after {timeout_s}s — mutation data untrustworthy",
            f"baseline GUT timed out after {timeout_s}s (project's baseline run exceeds "
            f"the timeout — raise --timeout or check why tests are this slow)",
            hint=suggested_action(BUDGET_CAP_S),
        )
    except _BaselineLaunchFailure as e:
        return _run_error_result(
            str(path),
            "godot launch failed on baseline — mutation data untrustworthy",
            f"godot launch failed on baseline run: {e.tail.strip()[:200]}",
        )
    except SlotProvisionError as e:
        # 影子槽制备失败（.gitignore fail-loud / 磁盘不足）——数据不可信，
        # 中止契约转统一 JSON（与 baseline 启动失败同档）。
        return _run_error_result(
            str(path),
            "shadow slot provisioning failed — mutation data untrustworthy",
            f"shadow slot provisioning failed: {e}",
        )


# per-mutant 自适应上限（baseline 耗时决定，_run_mutant_loop 内写入；统一
# 报告构建已迁 reporting.py，经参数传入读取时刻同值）。
_adaptive_timeout = 0


def _run_baseline(project_root: str, tests_glob: str | None, timeout_s: int
                  ) -> tuple[int, str, float]:
    """跑 baseline 并计时；TimeoutExpired 由调用方按中止契约处理。"""
    t0 = time.monotonic()
    rc, tail = _run_gut_on_project(project_root, timeout_s=timeout_s, tests_glob=tests_glob)
    return rc, tail, time.monotonic() - t0


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
    baseline_elapsed, slot_info)。

    mutants = 已过差分/预算的【执行面】（串行：budget_on_retry 产物；worker：
    父层注入的 retry_ids 子集）。verdict 记录不在此处进 results——D5 streak
    是窗口级判定，由调用方 apply_timeout_streaks 落定后统一扩展。
    条目增量落盘（中断安全）：每 verdict 立即以乐观 streak 写 v2 条目；
    毒 run 守卫在窗口收尾修正（apply_timeout_streaks + 调用方改写）。
    baseline 永远实测（判定语义锚）；快照 elapsed 只喂预算锚。
    timeout_factor（§SPEC-1420-02）：None = 模块默认 _TIMEOUT_SCALE。
    注入 = 磁盘影子槽（SEE-1550 M0）：每 worker 一个槽，mutant 经
    os.replace 写影子内目标，真实仓零写入；槽归还原并断言。
    """
    global _adaptive_timeout
    raw_pairs: list[tuple] = []
    prior_entries = prior_entries or {}
    slot_info: dict = {}

    with tempfile.TemporaryDirectory() as workdir:
        backup_path = os.path.join(workdir, "original.gd")
        shutil.copy2(str(path), backup_path)

        # Baseline：原文件的 GUT 结果（失败测试名集合）。pre-existing 失败
        # 不属于任何 mutant——kill 判定只看相对 baseline 的【新增】失败。
        if precomputed_baseline is not None:
            baseline_rc = int(precomputed_baseline["rc"])
            baseline_tail = str(precomputed_baseline["tail"])
            baseline_elapsed = float(precomputed_baseline["elapsed"])
            baseline_reused = True
        else:
            # Revy QA retest 实证：baseline 必然花最久（实测项目 ~108s），默认
            # --timeout 60 下 TimeoutExpired 漏网成原始 traceback（无统一 JSON）——
            # 与 per-mutant 循环的 timeout 同款处理：归 run_error JSON 契约。
            baseline_rc, baseline_tail, baseline_elapsed = _run_baseline(
                project_root, tests_glob, timeout_s)
            baseline_reused = False
        # SPEC-009 自适应 timeout：per-mutant 上限 = baseline 耗时 × F（§SPEC-
        # 1420-02：F 可由 --timeout-factor 覆盖；不降低于显式 timeout——只放大）。
        _adaptive_timeout = adaptive_timeout_for(baseline_elapsed, timeout_s,
                                                 timeout_factor)

        if baseline_rc != 0 and _looks_like_godot_launch_failure(baseline_tail):
            raise _BaselineLaunchFailure(baseline_tail)

        baseline_failures = _parse_failing_tests(baseline_tail)
        baseline_dirty = bool(baseline_failures)

        kind = detect_binary_kind()
        idpack = identity if identity is not None else \
            identity_pack(project_root, kind)
        target_rel = project_relative(project_root, str(path))
        target_res = _res_path_of(str(path), str(Path(project_root)))
        auto_name = beacon.autoload_name_for(project_root, target_res)
        clock = clock or StageClock()
        t_slot = time.monotonic()
        slot = shadowpool.acquire_slot(project_root, kind, [target_rel],
                                       idpack.get("boot_wiring_hash", ""),
                                       targeted_only=targeted_only)
        # killed 证书的归属索引懒构建（零 killed 的 run 零支付）
        ownership_holder: list = []

        def _ownership() -> TestOwnershipIndex:
            if not ownership_holder:
                ownership_holder.append(TestOwnershipIndex(project_root))
            return ownership_holder[0]

        # R-DW6 默认启用实测轮：身份与执行同源解析（resolve_exec_scheme 统一
        # 路由排除 + gut.gd 直驱可用性）——coverage/resume store file_key 与
        # 实际执行通道一致，杜绝 scheme 漂移性 store-miss。
        exec_scheme_resolved, _route_resolved = resolve_exec_scheme(
            project_root, target_res, worker_channel)
        use_worker = exec_scheme_resolved == WORKER_SCHEME
        slot_info["exec_scheme"] = exec_scheme_resolved
        try:
            beacon.seed_beacon_script(slot.root)
            timings_out: dict = {}
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
            else:
                with clock.stage("per_mutant"):
                    raw_pairs = _run_cold_legs(
                        path, project_root, original_src, backup, mutants,
                        tests_glob, baseline_failures, baseline_dirty, slot,
                        auto_name, target_rel, target_res, _ownership, reporter,
                        store2, prior_entries, scopes)
        finally:
            release_problems = shadowpool.release_slot(slot)
            # §SPEC-202 slot_sync = 槽获取（含 fresh 制备/warm 同步）+ 归还
            # 断言全程——槽位固定成本的唯一出处。
            clock.add("slot_sync", time.monotonic() - t_slot)
            if release_problems:
                slot_info["release_problems"] = release_problems

        slot_info["inject_scheme"] = idpack.get("inject_scheme", INJECT_SCHEME)
        slot_info["slot_mode"] = slot.mode
        slot_info["freshness"] = list(slot.freshness)
        if any(v["verdict"] == "survived" for _, v in raw_pairs):
            slot_info["suspected_orphan"] = _orphan_disclosure(
                project_root, target_res)

    return raw_pairs, baseline_reused, baseline_elapsed, slot_info


def _run_cold_legs(path, project_root, original_src, backup, mutants,
                   tests_glob, baseline_failures, baseline_dirty, slot,
                   auto_name, target_rel, target_res, ownership_fn, reporter,
                   store2, prior_entries, scopes):
    """冷通道（M0）逐 mutant 执行循环（默认路径，行为与既有逐位不变）。"""
    raw_pairs: list[tuple] = []
    for seq, m in enumerate(mutants):
        t_mutant = time.monotonic()
        verdict = _run_single_mutant(
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


def _orphan_disclosure(project_root: str, target_res: str) -> bool:
    """unreachable 不进 verdict、不 skip——静态零引用只做疑似孤儿披露。"""
    try:
        return suspected_orphan(project_root, target_res)
    except OSError as e:
        print(f"orphan scan failed for {target_res}: {e}", file=sys.stderr)
        return False


def _run_single_mutant(m: Mutant, path, project_root: str, original_src: str,
                       backup: str, tests_glob: str | None, seq: int,
                       baseline_failures: set[str], baseline_dirty: bool,
                       slot=None, auto_name: str = "",
                       ownership_fn=None, target_rel: str = "",
                       target_res: str = "") -> dict:
    """磁盘影子注入 → 跑 GUT → 分类；真实仓目标文件全程零写入（SEE-1550）。

    - 注入：mutant 经「临时文件 + os.replace」写影子槽内目标 `.gd`（引擎
      启动前落盘）——每 mutant 一次 replace，无恢复步，下一个 mutant 直接
      覆盖上一个；写失败上抛转 run_error（绝不让引擎读到原码假 survived）。
    - 四级 beacon（beacon.py）：磁盘哈希 / load 编译 / autoload 同源 /
      GUT 运行证据——任何一级失败都不得产出 killed/survived。
    """
    global _ACTIVE_ENV, _ACTIVE_SCRIPT
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
            src=original_src)
    cfg_path = beacon.write_beacon_cfg(slot.root, target_res, mutant_sha,
                                       auto_name)
    register_temp(cfg_path)
    user_dir = make_user_dir()
    env_extra, _ = mutant_env(str(cfg_path), user_dir)
    _ACTIVE_ENV = env_extra
    _ACTIVE_SCRIPT = beacon.beacon_script_res(slot.root)
    _ACTIVE_GATE_ROOT = project_root
    try:
        try:
            rc, tail = _run_gut_on_project(
                str(slot.root), timeout_s=_adaptive_timeout,
                tests_glob=tests_glob)
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
            verdict = _classify_mutant_run(m, rc, tail, baseline_failures,
                                           baseline_dirty)
            verdict = {**verdict, "line_text": line_text_of(original_src, m.line)}
            # L4 GUT 运行证据门：无运行流不得 killed/survived（§SPEC-003）。
            if verdict["verdict"] in ("killed", "survived") \
                    and not _GUT_RUN_EVIDENCE.search(tail):
                return _mutant_record(
                    m, "run_error",
                    f"no GUT run stream evidence for "
                    f"{m.kind} {m.original}→{m.mutated} — engine output "
                    f"lacks test-run markers: {tail.strip()[:200]}",
                    src=original_src)
            # killed 证书（完整失败集；归属/闭包不确定 → 不附 → 下轮保守
            # 重跑，fail-safe 只偏向重跑）。
            if verdict["verdict"] == "killed" and ownership_fn is not None:
                new_failures = _parse_failing_tests(tail) - baseline_failures
                cert = killer_certificate(new_failures, ownership_fn(),
                                          project_root)
                if cert is not None:
                    verdict["killed_by"] = cert
        except subprocess.TimeoutExpired:
            verdict = _mutant_record(m, "timeout",
                                     f"GUT timed out after {m.kind} {m.original}→{m.mutated}",
                                     src=original_src)
    finally:
        _ACTIVE_ENV = None
        _ACTIVE_SCRIPT = GUT_SCRIPT_RES_PATH
        _ACTIVE_GATE_ROOT = None
        Path(cfg_path).unlink(missing_ok=True)
        forget_temp(cfg_path)
        if user_dir:
            shutil.rmtree(user_dir, ignore_errors=True)
    return verdict


class _BaselineLaunchFailure(Exception):
    """baseline run 命中 godot 启动失败——mutant 数据不可信，run_mutation 层转中止契约。"""

    def __init__(self, tail: str):
        self.tail = tail
        super().__init__(tail[:200])
