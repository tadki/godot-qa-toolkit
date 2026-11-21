"""SEE-1550 §SPEC-020 第二批 ③——per-slot 常驻 GUT 直驱 worker
（exec_scheme=worker-reset/1，定案 A1–A9 照案实施）。

形态：每 run 进程启动一次；mutant 影盘写入时序与 M0 完全一致（Python 侧
`os.replace`）；引擎侧常驻循环逐腿执行 R-C 硬序：

    L1 磁盘读回哈希 → IGNORE 原位刷码（CACHE_MODE_IGNORE 强制读盘）→
    REPLACE 落缓存（CACHE_MODE_REPLACE）→ 逐闭包 reload（硬序，全 ok 才
    实例化）→ autoload 交叠防御 → fresh Gut 直驱（gut.gd test_scripts，
    E0 通路，await end_run）→ 腿末状态回报（loaded-source 哈希 / user://
    树 / 连接指纹）

reload 原语实证（本批 /tmp 探针）：`GDScript.reload()` 只重编译缓存文本、
不读盘（rc=0 假阳性）；单步 `CACHE_MODE_REPLACE` 被路径缓存挡住（同秒
写不可见）；**IGNORE 读盘 + REPLACE 落缓存两步**后 fresh `load()` 见新码
——R-C 的「逐闭包脚本 reload」就落在这两步上。

会话致命（定案硬规则，测试钉住）：闭包任一 reload 失败（parse/缺失，
rc≠0）/ loaded-source 哈希失配 / autoload 交叠 / 腿挂死 → 不再执行后续
mutant → respawn + 自上次 canary 窗口整批作废 + 冷重跑 + stderr 披露；
禁止静默改道或 IGNORE 后继续。

A9 默认启用（SEE-1550 default-on 翻转，护栏全保留）：eligible 目标自动走
worker，显式 flag 最高优先，env `GQT_MUTATION_WORKER=0|false|no` 为关断
逃生门（冷通道回退）；定案红线：baseline 永远实测走冷通道。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import shadow
from .channel_router import CHANNEL_WORKER, route_target
from .godot_gate import GlobalGodotGate
from .identity import COLD_SCHEME, WORKER_SCHEME

ENV_WORKER = "GQT_MUTATION_WORKER"
ENV_CANARY_EVERY = "GQT_WORKER_CANARY_EVERY"
DEFAULT_CANARY_EVERY = 25
from .shadow import SlotProvisionError
from .worker_session import (
    HOST_SCRIPT_NAME,
    WORKER_DIRNAME,
    WORKER_HOST_GD,
    WorkerLegResult,
    WorkerSession,
    _SessionState,  # noqa: F401 — WorkerSession 补丁钉在 wc_mod 命名空间
)



def worker_enabled(flag: bool | None = None) -> bool:
    """默认启用（Owner 裁决实测轮）：eligible 目标自动走 worker，全部自动
    降级护栏（parity/漂移/canary 否证/会话致命整批作废/两连失败冷降级）
    保留。显式 flag 仍最高优先；env `GQT_MUTATION_WORKER=0|false|no` 为
    关断逃生门（冷通道回退），其余取值不再需要——默认即启用。"""
    if flag is not None:
        return flag
    return os.environ.get(ENV_WORKER, "").strip() not in ("0", "false", "no")


def canary_every() -> int:
    raw = os.environ.get(ENV_CANARY_EVERY, "")
    try:
        v = int(raw)
        return v if v > 0 else DEFAULT_CANARY_EVERY
    except (TypeError, ValueError):
        return DEFAULT_CANARY_EVERY


def gut_direct_drive_available(project_root: str) -> bool:
    """直驱宿主可用性（R-DW6 默认启用实测轮）：常驻 worker 需要**真实**
    GUT addon（gut.gd）——仅 gut_cmdln.gd 存根的项目（如 BDD toy fixture）
    结构性不可用，必须回冷（禁止 parity 腿盲跑挂死）。"""
    return os.path.isfile(os.path.join(project_root, "addons", "gut",
                                       "gut.gd"))


class RouteDriftError(Exception):
    """SEE-1606 §SPEC-P1：下游重解析与父层/权威 route 漂移（fail-loud）。

    权威 scheme 来自父层身份/file_key（planning 注入）或单元 setup 首次
    解析；任何下游比对点不一致即本异常——由调用方转具名 route_drift
    统一 JSON（零 baseline、零错误 file_key 缓存/证书写入）。"""

    def __init__(self, target_res: str, expected: str, actual: str,
                 where: str):
        self.target_res = target_res
        self.expected = expected
        self.actual = actual
        self.where = where
        super().__init__(
            f"route_drift at {where}: {target_res} "
            f"authoritative={expected} resolved={actual}")


def resolve_exec_scheme(project_root: str, target_res: str,
                        worker_flag: bool | None = None
                        ) -> tuple[str, object]:
    """(exec_scheme, route)：默认启用下 eligible 且直驱可用才 worker-reset/1；
    路由排除（autoload/接线链/GUT 域/场景持有）或 gut.gd 缺失 → 冷通道。
    身份与执行必须同源解析（coverage/resume store file_key 一致性）。"""
    if not worker_enabled(worker_flag):
        return COLD_SCHEME, None
    route = route_target(project_root, target_res)
    if route.channel == CHANNEL_WORKER:
        if gut_direct_drive_available(project_root):
            return WORKER_SCHEME, route
        print("mutation worker: GUT addon gut.gd not found — resident "
              "direct-drive unavailable, using the cold channel "
              "(disclosed, not silently)", file=sys.stderr)
    return COLD_SCHEME, route


def build_worker_disclosure(project_root: str, target_res: str,
                            requested: bool | None, scheme: str,
                            route) -> dict:
    """每轮机器可读通道披露（SEE-1589 B §SPEC-002）。

    `exec_scheme` 单独存在时只能看到结果，看不到「为什么冷」——本块把
    静态路由 reason（含成员上下文）、闭包规模、显式 flag 是否被拒一并
    落盘，禁止无解释静默冷通道。
    """
    from .channel_router import summarize_reasons
    if route is None:
        channel = "not_evaluated"
        reasons: tuple[str, ...] = ()
        closure_size = 0
    else:
        channel = getattr(route, "channel", "unknown")
        reasons = tuple(getattr(route, "reasons", ()) or ())
        closure_size = len(getattr(route, "closure", ()) or ())
    if scheme == WORKER_SCHEME:
        decision, reason = "worker", "worker_reset_session"
    elif channel == "cold":
        decision, reason = "cold_route_excluded", "route_reason_present"
    elif not worker_enabled(requested):
        decision, reason = "cold_worker_disabled", "worker_flag_or_env_off"
    elif not gut_direct_drive_available(project_root):
        decision = "cold_gut_direct_unavailable"
        reason = "gut_addon_missing"
    else:
        decision, reason = "cold_unknown", "unresolved"
    return {
        "requested": requested,
        "route": channel,
        "exec_scheme": scheme,
        "decision": decision,
        "reason": reason,
        "route_reasons": list(reasons),
        "reason_counts": summarize_reasons(reasons),
        "closure_size": closure_size,
        "override_refused": bool(requested) and scheme != WORKER_SCHEME,
    }


def _fatal_reason_code(events: list[str]) -> str:
    """会话致命事件 → 稳定 reason 码（机器可读不可达原因）。"""
    joined = " ".join(events).lower()
    if "user:// tree drift" in joined:
        return "user_state_drift"
    if "connection fingerprint drift" in joined:
        return "connection_drift"
    if "parity" in joined:
        return "parity_mismatch"
    return "session_fatal"


def degraded_worker_disclosure(base: dict, fatal_events: list[str]) -> dict:
    """执行后回填：会话致命 → 该轮实际全部降级冷通道，必须如实披露。

    SEE-1589 B：旧输出在 fatal 后仍报 decision=worker（仅 stderr 有告警），
    真实执行是「1 腿 worker + 其余全冷」。`user://` 树在腿间漂移 = 测试
    写 user:// 状态、常驻进程无法维持逐 mutant 隔离 → 结构性不可达。
    """
    out = dict(base or {})
    if not fatal_events:
        return out
    out.update(
        decision="worker_degraded_cold",
        reason=_fatal_reason_code(list(fatal_events)),
        fatal_events=list(fatal_events),
        equivalence="cold_rerun_all_remaining_legs",
    )
    return out


def identity_for_target(identity: dict | None, project_root: str,
                        target_res: str, worker_flag: bool | None = None,
                        check: bool = False) -> tuple[dict | None, object, str]:
    """按目标解析 scheme 并把 exec_scheme 写入身份包（file_key 隔离面）。

    SEE-1606 §SPEC-P1：`check=True` 时（下游消费点）本地重解析只做漂移
    比对——入包已携带权威 exec_scheme 且与实跑不一致 → 具名 route_drift
    fail-loud（禁止静默覆盖 file_key 面）；`check=False`（规划层首次解析）
    保持既有覆写语义，本次解析即权威。"""
    scheme, route = resolve_exec_scheme(project_root, target_res, worker_flag)
    expected = identity.get("exec_scheme") if isinstance(identity, dict) \
        else None
    if check and expected is not None and expected != scheme:
        raise RouteDriftError(target_res, str(expected), str(scheme),
                              "core_setup")
    if identity is None:
        return None, route, scheme
    return {**identity, "exec_scheme": scheme}, route, scheme


def canary_due(leg_index: int, every: int | None = None) -> bool:
    """三层门时序（synthesis §4）：首 mutant 强制双通道预检 + 周期 renew。"""
    k = canary_every() if every is None else every
    return leg_index == 0 or leg_index % k == 0



# 引擎侧常驻宿主：R-C 硬序逐腿执行 + 文件会合协议。reload 原语 =
# CACHE_MODE_IGNORE（强制读盘，见模块 docstring 的实证）+ REPLACE（落缓存）；
# 腿末回报 loaded-source sha1（静态哈希门）/ user:// 树 / 连接指纹。



def _parity_gate_leg(session, slot, target_res, closure, tests_glob,
                     baseline_failures, parity_inject_fn,
                     tests_scripts: list[str] | None = None):
    """会话前置 parity 门（D-W2）：直驱失败集必须逐位等于冷 baseline 失败集。

    返回 (degrade_all, fatal_event)；披露（stderr）在本函数内完成，事件串
    由调用方汇入 fatal_events。门未启用（参数缺省）直接放行。"""
    if baseline_failures is None or parity_inject_fn is None:
        return False, None
    parity_sha, parity_err = parity_inject_fn()
    if parity_err is not None or session.dead:
        print("mutation worker: PARITY GATE unavailable — degrading "
              "this file to the cold channel (disclosed, not "
              "silently)", file=sys.stderr)
        return True, (f"parity leg unavailable: "
                      f"{parity_err or session.dead_reason}")
    pleg = session.run_leg(-1, target_res, parity_sha, list(closure),
                           tests_glob, tests_scripts=tests_scripts,
                           baseline_failures=sorted(baseline_failures or ()))
    if pleg.fatal:
        print(f"mutation worker: PARITY GATE fatal — "
              f"{pleg.fatal_reason}; degrading this file to the "
              f"cold channel (disclosed, not silently)", file=sys.stderr)
        return True, f"parity leg fatal: {pleg.fatal_reason}"
    if pleg.gut_ran and pleg.failing_tests != baseline_failures:
        print("mutation worker: PARITY MISMATCH — direct-drive "
              "failing set differs from cold baseline on this "
              "suite; degrading this file to the cold channel "
              "(equivalence preserved, disclosed)", file=sys.stderr)
        return True, ("mode parity gate: direct-drive failing set diverges "
                      f"from cold baseline (worker="
                      f"{sorted(pleg.failing_tests)[:4]} "
                      f"cold={sorted(baseline_failures)[:4]})")
    return False, None


def _apply_canary(canary_cold, verdict, seq, session):
    """canary 双通道比对（D-W2 通道资格否证语义）。

    返回 (verdict, mismatch_event, mismatch)。失配时 stderr 披露在本函数
    内完成；窗口改冷权威 + 会话关闭 + 剩余冷降级由调用方执行（编排状态居
    编排层）。"""
    if canary_cold is None:
        return verdict, None, False
    if canary_cold["verdict"] == verdict["verdict"]:
        session.note_canary()
        return verdict, None, False
    print(f"mutation worker: CANARY MISMATCH at leg {seq} "
          f"(cold={canary_cold['verdict']} "
          f"worker={verdict['verdict']}) — channel "
          f"disqualified for this file: window invalidated "
          f"and ALL remaining legs degraded to cold "
          f"(disclosed, equivalence preserved)", file=sys.stderr)
    # canary 失配 = 双通道等价性被实证否证（D-W2 KOL 实测：mutant 才触发
    # 的 autoload 状态污染无法静态预判，且「作废窗口后继续 worker」会让
    # 后续未验证判定静默出货）——窗口内改冷权威 + 剩余全部冷降级。
    event = (f"canary mismatch leg {seq}: cold="
             f"{canary_cold['verdict']} "
             f"worker={verdict['verdict']}")
    return canary_cold, event, True  # 本腿已有冷权威判定


def _legs_ctx(*, slot, kind, tests_glob, leg_timeout_s, timings_out,
              inject_fn, cold_runner, record_fn, classify_fn) -> dict:
    """逐腿编排的可变状态上下文（原闭包自由变量的显式化——零行为）。"""
    return {"raw_pairs": [], "fatal_events": [], "window_start": 0,
            "session_failures": 0, "degrade_all": False, "session": None,
            "slot": slot, "kind": kind, "tests_glob": tests_glob,
            "leg_timeout_s": leg_timeout_s, "timings_out": timings_out,
            "inject_fn": inject_fn, "cold_runner": cold_runner,
            "record_fn": record_fn, "classify_fn": classify_fn,
            "kill_bail": False, "tests_scripts": None,
            "red_pairs": None, "baseline_red_map": None}


def _legs_harvest_boot(c: dict, s) -> None:
    """§SPEC-202：会话 boot 耗时收割（含 respawn 的旧会话——收后清零
    防最终重复计数）。"""
    if c["timings_out"] is None:
        return
    c["timings_out"]["worker_boot_s"] = round(
        c["timings_out"].get("worker_boot_s", 0.0) + s.boot_s, 3)
    s.boot_s = 0.0


def _legs_record(c: dict, m, verdict, elapsed_s=0.0) -> None:
    c["raw_pairs"].append((m, verdict))
    c["record_fn"](m, verdict, elapsed_s)


def _legs_run_cold(c: dict, m, seq) -> tuple:
    t0 = time.monotonic()
    return c["cold_runner"](m, seq), time.monotonic() - t0


def _legs_recast_cold(c: dict, start: int) -> None:
    """整批作废：窗口内 worker 判定全部改冷重跑（权威回归冷通道）。"""
    for idx in range(start, len(c["raw_pairs"])):
        m, _old = c["raw_pairs"][idx]
        verdict, elapsed = _legs_run_cold(c, m, idx)
        c["raw_pairs"][idx] = (m, verdict)
        c["record_fn"](m, verdict, elapsed)


def _legs_new_session(c: dict) -> "WorkerSession":
    return WorkerSession(c["slot"].root, c["slot"].project_root, c["kind"],
                         godot_bin=None, leg_timeout_s=c["leg_timeout_s"])


def _legs_recover_fatal(c: dict, seq, leg, m) -> tuple:
    """会话致命处置：窗口作废冷重跑 + respawn；返回 (verdict, elapsed)。"""
    c["session_failures"] += 1
    print(f"mutation worker: SESSION FATAL at leg {seq} — "
          f"{leg.fatal_reason}; window {c['window_start']}.."
          f"{len(c['raw_pairs'])} invalidated, cold rerun", file=sys.stderr)
    _legs_recast_cold(c, c["window_start"])
    _legs_harvest_boot(c, c["session"])
    c["session"].close()
    c["session"] = _legs_new_session(c)
    c["window_start"] = len(c["raw_pairs"])
    verdict, elapsed = _legs_run_cold(c, m, seq)
    if c["session_failures"] >= 2:
        print("mutation worker: respawn failed twice — "
              "degrading remaining legs to the cold channel "
              "(disclosed, not silently)", file=sys.stderr)
    return verdict, elapsed


def _legs_parity(c: dict, target_res: str, closure, parity_inject_fn) -> None:
    """会话前置 parity 门（D-W2，`_parity_gate_leg`）：直驱 GUT 与 gut_cmdln
    对同一代码必须产出同一失败集；失配 → 披露 + 整文件冷降级（等价性
    守恒）。"""
    c["session"] = _legs_new_session(c)
    try:
        degrade_all, parity_event = _parity_gate_leg(
            c["session"], c["slot"], target_res, closure, c["tests_glob"],
            c["baseline_failures"], parity_inject_fn,
            tests_scripts=(c["tests_scripts"] if c["kill_bail"] else None))
        if parity_event is not None:
            c["fatal_events"].append(parity_event)
        c["degrade_all"] = degrade_all
    except BaseException:
        _legs_harvest_boot(c, c["session"])
        c["session"].close()
        raise


def _legs_drift_disqualify(c: dict, seq, leg) -> None:
    """漂移致命 = 腿间状态污染被实证（D-DVP 扩面实测：与 canary 失配同族）
    → 通道资格否证：窗口冷权威 + 剩余全部冷降级（不再 respawn 后继续
    worker，同 canary 否证语义）。"""
    c["fatal_events"].append(leg.fatal_reason)
    print(f"mutation worker: SESSION FATAL at leg {seq} — "
          f"{leg.fatal_reason}; channel disqualified for this "
          f"file: window invalidated and ALL remaining legs "
          f"degraded to cold (disclosed, equivalence preserved)",
          file=sys.stderr)
    _legs_recast_cold(c, c["window_start"])
    _legs_harvest_boot(c, c["session"])
    c["session"].close()
    c["degrade_all"] = True


def _legs_one(c: dict, seq, m, target_res: str, closure,
              prev_leg_gut_ran: bool) -> bool:
    """单腿状态迁移（inject → canary 决策 → 致命处置 → 判定记录）。
    返回本腿是否为 GUT 执行腿。"""
    mutant_sha, inject_error = c["inject_fn"](m)
    if inject_error is not None:
        _legs_record(c, m, inject_error)
        return prev_leg_gut_ran
    # D-DVP：非 GUT 腿之后的腿，wipe+部分再生的 user:// 态与冷全新目录
    # 不等价——该腿强制双通道 canary（否则产生无证据假 kill）。
    force_canary = not prev_leg_gut_ran
    canary_cold = (c["cold_runner"](m, seq)
                   if (canary_due(seq) or force_canary) else None)
    if c["degrade_all"] or c["session_failures"] >= 2 or c["session"].dead:
        # parity 失配 / 会话不可用（respawn 连续失败）：剩余腿全冷降级
        verdict, elapsed = _legs_run_cold(c, m, seq)
        _legs_record(c, m, verdict, elapsed)
        return prev_leg_gut_ran
    t_leg = time.monotonic()
    leg = c["session"].run_leg(seq, target_res, mutant_sha, list(closure),
                               c["tests_glob"],
                               tests_scripts=(c["tests_scripts"]
                                              if c["kill_bail"] else None),
                               baseline_failures=sorted(
                                   c["baseline_failures"] or ()),
                               skip_red_tests=(c["red_pairs"] or []),
                               allow_tests=(c["allow_tests"] or None))
    leg_elapsed = time.monotonic() - t_leg
    if leg.fatal and leg.state_drift:
        _legs_drift_disqualify(c, seq, leg)
        verdict, elapsed = _legs_run_cold(c, m, seq)
        _legs_record(c, m, verdict, elapsed)
        return prev_leg_gut_ran
    if leg.fatal:
        verdict, elapsed = _legs_recover_fatal(c, seq, leg, m)
        c["fatal_events"].append(leg.fatal_reason)
        _legs_record(c, m, verdict, elapsed)
        return prev_leg_gut_ran
    c["session_failures"] = 0
    verdict = c["classify_fn"](m, leg)
    verdict, canary_event, mismatch = _apply_canary(
        canary_cold, verdict, seq, c["session"])
    if mismatch:
        c["fatal_events"].append(canary_event)
        _legs_recast_cold(c, c["window_start"])
        c["session"].close()
        c["degrade_all"] = True
    _legs_record(c, m, verdict, leg_elapsed)
    return leg.gut_ran

def _legs_mutants(c: dict, mutants, target_res: str, closure) -> None:
    """逐 mutant 编排循环（状态迁移见 _legs_one）。"""
    prev_leg_gut_ran = True  # parity 腿按 GUT 执行腿对待（有其冷 oracle）
    try:
        for seq, m in enumerate(mutants):
            prev_leg_gut_ran = _legs_one(c, seq, m, target_res, closure,
                                         prev_leg_gut_ran)
    finally:
        _legs_harvest_boot(c, c["session"])
        c["session"].close()


def run_worker_legs(mutants, slot, target_rel, target_res, closure, kind,
                    tests_glob, inject_fn, cold_runner, record_fn,
                    classify_fn, leg_timeout_s,
                    baseline_failures=None, parity_inject_fn=None,
                    timings_out: dict | None = None,
                    kill_bail: bool = False,
                    tests_scripts: list[str] | None = None,
                    red_pairs: list[tuple[str, str]] | None = None,
                    baseline_red_map=None,
                    allow_tests: list | None = None):
    """§SPEC-020 W 通道逐腿执行（R-C 硬序 + 门消费 + 会话致命处置）。

    会话致命（闭包任一 reload rc≠0 / loaded-source 哈希门 fire / autoload
    交叠 / 腿挂死）→ 不再执行后续 mutant：respawn + **自上次 canary 窗口
    整批作废 + 冷重跑** + stderr 披露（定案硬规则，禁止静默改道/继续）。
    首腿与每 k 腿双通道 canary：cold oracle 与 worker 判定必须同档，失配
    = 通道资格否证（窗口冷权威 + 剩余全部冷降级，披露）。

    会话前置 parity 门（D-W2，`_parity_gate_leg`）；编排/会话/门居本模块；
    verdict 语义（classify_fn）、冷重跑（cold_runner）、注入（inject_fn）
    与记录（record_fn）由 runner 注入——避免反向依赖。复杂度纪律
    （R-DW5）：门判定下沉 `_parity_gate_leg` / `_apply_canary`，编排循环
    只留状态迁移（分解函数族 `_legs_*`）。
    """
    c = _legs_ctx(slot=slot, kind=kind, tests_glob=tests_glob,
                  leg_timeout_s=leg_timeout_s, timings_out=timings_out,
                  inject_fn=inject_fn, cold_runner=cold_runner,
                  record_fn=record_fn, classify_fn=classify_fn)
    c["baseline_failures"] = baseline_failures
    c["kill_bail"] = kill_bail
    c["tests_scripts"] = tests_scripts
    c["red_pairs"] = red_pairs
    c["baseline_red_map"] = baseline_red_map
    c["allow_tests"] = allow_tests
    _legs_parity(c, target_res, closure, parity_inject_fn)
    _legs_mutants(c, mutants, target_res, closure)
    return c["raw_pairs"], c["fatal_events"]

def _adaptive_timeout_ref():
    """runner 的 per-mutant 自适应上限（模块属性运行时读取）。"""
    from . import runner as _runner

    return _runner._adaptive_timeout


def _wb_inject(m, slot, target_rel, original_src, target_res):
    """mutant 注入影子槽；失败返回 (None, run_error 记录)。"""
    from . import runner as _runner

    try:
        return shadow.inject_mutant(
            slot.root, target_rel,
            _runner._apply_mutation(original_src, m)), None
    except (OSError, SlotProvisionError) as e:
        return None, _runner._mutant_record(
            m, "run_error",
            f"disk injection failed for {target_res}: {e}",
            src=original_src)


def _wb_cold(m, seq, *, path, project_root, original_src, tests_glob,
             baseline_failures, baseline_dirty, slot, auto_name,
             ownership_fn, target_rel, target_res,
             direct_scripts: list[str] | None = None,
             red_pairs: list[tuple[str, str]] | None = None):
    """冷通道单点重跑（runner 注入语义）；kill 即退与红测跳过随主腿生效。"""
    from . import runner as _runner

    return _runner._run_single_mutant(
        m, path, project_root, original_src, "", tests_glob, seq,
        baseline_failures, baseline_dirty, slot=slot,
        auto_name=auto_name, ownership_fn=ownership_fn,
        target_rel=target_rel, target_res=target_res,
        direct_scripts=direct_scripts, red_pairs=red_pairs)


def _wb_parity_inject(slot, target_rel, original_src, target_res):
    """parity 门注入（原文）；失败返回 (None, 原因)。"""
    try:
        return shadow.inject_mutant(slot.root, target_rel, original_src), \
            None
    except (OSError, SlotProvisionError) as e:
        return None, f"parity injection failed for {target_res}: {e}"


def _wb_record_factory(reporter, store2, prior_entries, scopes):
    """记录回调：reporter 进度 + v2 条目增量落盘（乐观 streak）。"""
    def _record(m, verdict, elapsed_s=0.0):
        if reporter is not None:
            reporter.record(m, verdict, elapsed_s)
        if store2 is not None:
            from .diffengine import make_entry, streak_for
            prior = prior_entries.get(m.stable_id)
            streak, v2 = streak_for(prior, verdict["verdict"])
            store2.record_entry(make_entry(m, verdict, v2, streak,
                                           scopes or {}, time.time()))
    return _record


def execute_worker_branch(path, project_root, original_src, mutants,
                          tests_glob, baseline_failures, baseline_dirty,
                          slot, auto_name, target_rel, target_res, kind,
                          reporter, store2, prior_entries, scopes,
                          ownership_fn, timings_out: dict | None = None,
                          kill_bail: bool = False,
                          tests_scripts: list[str] | None = None,
                          red_pairs: list[tuple[str, str]] | None = None,
                          baseline_red_map=None,
                          allow_tests: list | None = None,
                          expected_scheme: str | None = None):
    """§SPEC-020 W 通道接线（薄层）：注入/冷跑/记录/分类回调注入编排层。

    SEE-1606 §SPEC-P1：worker 注入路径的 route_target 重解析只做比对——
    权威 scheme=worker 而实跑 route 判 cold（树/状态在 setup 后漂移）→
    具名 route_drift，不得以漂移后的 closure 继续逐腿。
    runner 域符号在函数体内延迟 import（baseline.py 先例）——避免
    runner ↔ worker_channel 的模块级循环依赖。
    """
    from .reporting import classify_worker_leg

    route = route_target(project_root, target_res)
    if expected_scheme == WORKER_SCHEME and route.channel != CHANNEL_WORKER:
        raise RouteDriftError(target_res, WORKER_SCHEME, str(route.channel),
                              "worker_injection")
    return run_worker_legs(
        mutants, slot, target_rel, target_res, route.closure, kind,
        tests_glob,
        inject_fn=lambda m: _wb_inject(m, slot, target_rel, original_src,
                                       target_res),
        cold_runner=lambda m, seq: _wb_cold(
            m, seq, path=path, project_root=project_root,
            original_src=original_src, tests_glob=tests_glob,
            baseline_failures=baseline_failures, baseline_dirty=baseline_dirty,
            slot=slot, auto_name=auto_name, ownership_fn=ownership_fn,
            target_rel=target_rel, target_res=target_res,
            direct_scripts=(tests_scripts if (kill_bail or
                                              baseline_failures) else None),
            red_pairs=red_pairs),
        record_fn=_wb_record_factory(reporter, store2, prior_entries, scopes),
        classify_fn=lambda m, leg: classify_worker_leg(
            m, leg, baseline_failures, baseline_dirty, ownership_fn,
            project_root, original_src,
            baseline_red_map=baseline_red_map),
        leg_timeout_s=max(60, int(_adaptive_timeout_ref() or 60)),
        allow_tests=allow_tests,
        baseline_failures=set(baseline_failures or ()),
        parity_inject_fn=lambda: _wb_parity_inject(slot, target_rel,
                                                   original_src, target_res),
        timings_out=timings_out,
        kill_bail=kill_bail, tests_scripts=tests_scripts,
        red_pairs=red_pairs, baseline_red_map=baseline_red_map)
