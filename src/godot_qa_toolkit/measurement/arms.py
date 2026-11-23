"""SEE-1594 BTIDY（零行为拆分）：批式臂执行——单臂（§SPEC-052 并行分片、
XDG 隔离、shadow 插桩、门控/对账/bundle）与 gherkin 臂编排。自
orchestrator.py 的 run_measurement 内嵌闭包 `_arm` / `_gherkin_flow`
提升为模块级并按阶段分解为 ≤50 行函数；闭包变量显式参数化，逻辑逐语句
等价。"""

from __future__ import annotations

import json
import re
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..coverage.evidence import (
    build_bundle,
    build_run_record,
    clear_sink,
    make_run_uuid,
    update_run_record,
    write_bundle,
    write_run_record,
)
from ..coverage.runner import _looks_like_godot_launch_failure
from ..coverage.union import arm_record_from_bundle
from ..gherkin.run import GherkinRunError, replay_check
from ..gherkin.addon import AddonError, run_preflight
from ..gherkin.suite import (
    SuiteError,
    _cleanup_suite_dir,
    _snapshot_main,
    compile_keys_to_suite,
    create_suite_dir,
    reconcile,
)
from .backup import MeasurementError, _backup_base, batch_sink_name, file_key8
from .crapbuild import _cause_tail, _pairs_sha256  # noqa: F401
from .arms_stage import (  # noqa: F401 — 迁移绑定保持既有补丁面
    _arm_inject_root,
    _arm_plans,
    _arm_restore,
    _arm_stage_created,
    _stage_arm_files,
)

from .probe_templates import (
    _ATTR_CTX_RELPATH,
    _FLUSHER_RELPATH,
    _PRE_HOOK_RELPATH,
)
from .shadowio import (
    _cleanup_shadow,
    _prepare_shadow,
    _shard_groups,
)

def _orch(name: str):
    """运行时经 orchestrator 模块命名空间解析共享 helper——保持既有
    monkeypatch 点（orch._run_one_gut / orch._read_hits_merged）的调用
    链不变（原内嵌闭包经 run_measurement 模块全局解析，语义等价）。"""
    import godot_qa_toolkit.measurement.orchestrator as _o

    return getattr(_o, name)
from .attribution import _collect_attribution



def _instrument_shadow(shadow: Path, inst_by_rel: dict[str, str],
                       shadow_hook_files: list[tuple[str, str]]) -> None:
    """P2 双臂并行：插桩写入分片 shadow 而非项目根——两臂可并发执行
    而不互踩 uuid。"""
    for rel, inst in inst_by_rel.items():
        target = shadow / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(inst, encoding="utf-8")
    for relpath, text in shadow_hook_files:
        hp = shadow / relpath
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(text, encoding="utf-8")

def _shard_run_reps(root: Path, shadow: Path, args: list[str],
                    run_uuid: str, name: str, idx: int, reps: int,
                    timeout_s: int, adaptive_reps: bool, prev_union,
                    sink_by_rel: dict, manifest_by_rel: dict,
                    shard_log_dir: str | None,
                    xdg_homes: list[str]) -> "tuple[list[dict], frozenset]":
    """单分片逐 rep 执行 GUT（独立 XDG user:// + 运行时 PID sink）；
    adaptive_reps 时分片局部累计并集停止增长即提前锁定（判据与
    §SPEC-062C 一致：∪(1..k)==∪(1..N)；无全局 barrier）。"""
    rep_records: list[dict] = []
    for rep in range(reps):
        xdg = tempfile.mkdtemp(
            prefix=f"gqt-shard-{run_uuid[:8]}-{idx}-r{rep}-")
        # §SPEC-060e：mkdtemp 成功即注册进闭包共享的
        # xdg_homes——任何异常路径都由外层 finally 清理
        xdg_homes.append(xdg)
        env = dict(os.environ)
        env["XDG_DATA_HOME"] = xdg
        try:
            r, _w = _orch("_run_one_gut")(shadow, args, timeout_s,
                                 env=env, gate_root=root)
            out = (r.stdout or "") + "\n" + (r.stderr or "")
            rc = r.returncode
            rep_timed_out = False
        except MeasurementError as e:
            te = getattr(e, "__cause__", None)
            out = str(e) + "\n" + _cause_tail(te)
            rc = None
            rep_timed_out = True
        if shard_log_dir:
            log_dir = Path(shard_log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir /
             f"{name}-shard{idx}-rep{rep}.log").write_text(
                out, encoding="utf-8")
        rep_records.append(
            {"xdg": xdg, "rc": rc, "out": out,
             "timed_out": rep_timed_out})
        if adaptive_reps:
            shard_union: set[int] = set(prev_union or ())
            for rel in sink_by_rel:
                hits, _ = _orch("_read_hits_merged")(
                    root, run_uuid, rel, manifest_by_rel[rel],
                    [xdg], consume=False)
                shard_union |= set(hits)
            if rep >= 1 and frozenset(shard_union) == prev_union:
                break
            prev_union = frozenset(shard_union)
    return rep_records, prev_union

def _run_shard_leg(ip, *, name: str, plans: list[dict],
                   root: Path, run_uuid: str, reps: int, timeout_s: int,
                   adaptive_reps: bool, prev_union, sink_by_rel: dict,
                   manifest_by_rel: dict, shadow_instrument: bool,
                   inst_by_rel: dict[str, str],
                   shadow_hook_files: list[tuple[str, str]],
                   shard_log_dir: str | None,
                   xdg_homes: list[str]) -> dict:
    """单分片：shadow 准备（rep 间复用）→ 逐 rep GUT → 还原清理。"""

    from .probe_templates import _PRE_HOOK_RELPATH

    idx, plan = ip
    args = list(plan["args"]) + [
        "-gpre_run_script=res://" + _PRE_HOOK_RELPATH]
    shadow = None
    shadow_disclosure: dict = {}
    _t_shard = time.monotonic()
    print(f"measure: arm={name} shard={idx + 1}/{len(plans)} "
          f"rep=0/{reps} start", file=sys.stderr)
    try:
        # §SPEC-056：分片独立 project shadow（rep 间复用）
        shadow, shadow_disclosure = _prepare_shadow(
            root, run_uuid, idx)
        if shadow_instrument:
            _instrument_shadow(shadow, inst_by_rel, shadow_hook_files)
        rep_records, _pu = _shard_run_reps(
            root, shadow, args, run_uuid, name, idx, reps, timeout_s,
            adaptive_reps, prev_union, sink_by_rel, manifest_by_rel,
            shard_log_dir, xdg_homes)
    finally:
        _cleanup_shadow(shadow)
    # §SPEC-071 H1：shadow symlink 消解行为显式披露（解引用/悬空移除）
    print(f"measure: arm={name} shard={idx + 1}/{len(plans)} "
          f"done s={round(time.monotonic() - _t_shard, 1)}",
          file=sys.stderr)
    return {"plan": plan, "reps": rep_records,
            "shadow_disclosure": shadow_disclosure}



def _arm_fail_scan(results: list[dict]) -> tuple:
    """§SPEC-063a/c：失败身份解析为 (script, test) 对；任一 rep rc!=0
    且无可解析失败名（含启动失败）→ 不可归因失败 fail-closed 门控，
    不得出分。返回 (gut_ok, run_error, timed_out, arm_failures,
    arm_fail_pairs)。"""
    from .orchestrator import _parse_failing_pairs

    gut_ok = True
    run_error = False
    timed_out = False
    arm_failures: set[str] = set()
    arm_fail_pairs: set[tuple[str, str]] = set()
    for r in results:
        for rep in r["reps"]:
            if rep["timed_out"]:
                gut_ok = False
                run_error = True
                timed_out = True
                continue
            pairs = _parse_failing_pairs(rep["out"])
            arm_fail_pairs |= pairs
            arm_failures |= {t for (_s, t) in pairs}
            if rep["rc"] != 0:
                if _looks_like_godot_launch_failure(rep["out"]) \
                        or not pairs:
                    gut_ok = False
                    run_error = True
    return gut_ok, run_error, timed_out, arm_failures, arm_fail_pairs

def _gate_ctx(baseline_failures, baseline_pairs: set,
              arm_fail_pairs: set, declared_scoped: set,
              declared_bare: set) -> tuple:
    """§SPEC-060a/064a 门控上下文：臂内生效基线 = 显式参数（或空）；
    Summary 段 ("", t) 是同 run 内已归因失败 (s, t) 的回显（同一失败的
    第二种披露形态）——其归因实例已被基线吸收或声明豁免时随同覆盖。"""
    def _exempt(p: tuple[str, str]) -> bool:
        if p in declared_scoped:
            return True
        return p[0] == "" and p[1] in declared_bare

    effective_baseline = set(baseline_pairs)
    baseline_meta: dict | None = None
    if baseline_failures is not None:
        baseline_meta = {"method": "explicit_param",
                         "count": len(effective_baseline),
                         "pairs": sorted([list(p) for p in effective_baseline]),
                         "sha256": _pairs_sha256(effective_baseline)}
    covered_attributed_names = {
        t for (s, t) in arm_fail_pairs
        if s and ((s, t) in effective_baseline
                  or (s, t) in declared_scoped)}

    def _covered(p: tuple[str, str]) -> bool:
        if _exempt(p) or p in effective_baseline:
            return True
        return p[0] == "" and p[1] in covered_attributed_names

    new_pairs = {p for p in arm_fail_pairs if not _covered(p)}
    exempt_pairs = {p for p in arm_fail_pairs if _exempt(p)}
    return effective_baseline, baseline_meta, new_pairs, exempt_pairs

def _lazy_baseline_merge(name: str, root: Path, recipe: dict,
                         timeout_s: int, arm_fail_pairs: set,
                         declared_scoped: set, effective_baseline: set,
                         new_pairs: set,
                         baseline_failures,
                         baseline_meta: dict | None) -> tuple:
    """§SPEC-064a 懒获取（成本裁定）：绿臂零额外 GUT；观测到失败且未
    显式提供基线时才跑一次同面无插桩 GUT 取基线并复核。获取失败 → 具名
    stderr + 臂保持 invalid（fail-loud，不按空基线静默转绿）。
    gherkin 臂不得继承 gut_full 的懒基线（其失败保持可见）。"""
    from .orchestrator import (
        _acquire_lazy_baseline,
        _gutconfig_face_args,
    )

    if not (name == "gut_full" and new_pairs
            and baseline_failures is None):
        return effective_baseline, baseline_meta, new_pairs
    try:
        lazy_pairs, baseline_meta = _acquire_lazy_baseline(
            root, _gutconfig_face_args(recipe), timeout_s)
    except MeasurementError as e:
        print(f"measure: {e} — observed failures stay unabsorbed; "
              f"arm {name} invalid (fail-loud)", file=sys.stderr)
        baseline_meta = None  # 懒获取失败：基线披露缺席（fail-loud 已声明）
    else:
        effective_baseline = effective_baseline | set(lazy_pairs)
        covered_attributed_names = {
            t for (s, t) in arm_fail_pairs
            if s and ((s, t) in effective_baseline
                      or (s, t) in declared_scoped)}

        def _covered(p: tuple[str, str]) -> bool:
            if p in declared_scoped or p in effective_baseline:
                return True
            return p[0] == "" and p[1] in covered_attributed_names

        new_pairs = {p for p in arm_fail_pairs if not _covered(p)}
        print(f"measure: lazy baseline {baseline_meta['count']} "
              f"failing pairs acquired uninstrumented "
              f"({baseline_meta['wall_s']}s) — only NEW failures "
              f"gate the arm", file=sys.stderr)
    return effective_baseline, baseline_meta, new_pairs

def _arm_reconcile(root: Path, gherkin: dict | None, results: list[dict],
                   run_error: bool) -> tuple:
    """§SPEC-035 对账：分片组逐 rep 对账（rep 间独立进程独立输出），
    任一 false → 臂 invalid；§SPEC-063b：结构对账失败必须传入 union
    失败哨兵（run_error）——对账失败不得静默出分。suite 目录清理。"""
    reconciliation = None
    arm_valid = not run_error
    invalid_reason = None
    gut_ok_delta = False
    if gherkin is not None and not run_error:
        recs = []
        ok_all = True
        for r in results:
            for rep in r["reps"]:
                rec = reconcile(
                    root, r["plan"]["suite_dir"], r["plan"]["keys"],
                    rep["out"], replay_ok=True,
                    main_snapshot=gherkin["snapshot"])
                recs.append(rec)
                ok_all = ok_all and bool(rec.get("ok"))
        reconciliation = {"ok": ok_all, "shards": recs}
        if not ok_all:
            arm_valid = False
            invalid_reason = "reconciliation_failed"
            gut_ok_delta = True
            run_error = True
    if gherkin is not None:
        for r in results:
            if r["plan"].get("suite_dir") is not None:
                _cleanup_suite_dir(Path(r["plan"]["suite_dir"]))
    return (reconciliation, arm_valid, invalid_reason, gut_ok_delta,
            run_error)

def _bundle_with_evidence(prepared_item: dict, rel: str, name: str,
                          run_uuid: str, hits_lines: list,
                          probes_fired: int, gut_ok: bool, run_error: bool,
                          timed_out: bool, vacuous: bool,
                          evidence_dir) -> dict:
    """单文件 bundle 构建与 evidence 落盘（批式同臂共享 run_uuid——
    evidence 文件名必须带 per-file 后缀防互相覆盖，R3 实证缺陷）。"""
    bundle = build_bundle(
        run_uuid=run_uuid, file_path="res://" + rel,
        gd_source=prepared_item["src"], tests_glob=None,
        registry=prepared_item["registry"],
        probe_manifest=prepared_item["probe_manifest"],
        hits_lines=hits_lines, probes_fired=probes_fired,
        gut_ok=gut_ok, run_error=run_error, timeout=timed_out,
        zero_tests=False, vacuous=vacuous, arm=name)
    if evidence_dir is not None:
        suffix = f"-{file_key8(rel)}"
        record = build_run_record(
            run_uuid=run_uuid, file_path="res://" + rel,
            gd_source=prepared_item["src"], tests_glob=None,
            probe_manifest=prepared_item["probe_manifest"], arm=name)
        # §SPEC-070 FR-5：不再写入 sink_path——批式 sink 在分片
        # XDG 下且读回即消费，指向 evidence_dir/<sink> 是不存在
        # 位置的虚假路径
        write_run_record(evidence_dir, record, name_suffix=suffix)
        write_bundle(evidence_dir, bundle, name_suffix=suffix)
        update_run_record(evidence_dir, run_uuid, name_suffix=suffix,
                          status="bundle_written")
    return bundle

def _arm_hit_bundles(name: str, prepared: dict, sink_by_rel: dict,
                     manifest_by_rel: dict, xdg_homes: list[str],
                     root: Path, run_uuid: str, gut_ok: bool,
                     run_error: bool, timed_out: bool,
                     evidence_dir, shard_log_dir: str | None) -> dict:
    """逐文件 sink 读回 → 块入口命中展开（§SPEC-055/070 FR-4 行覆盖近似，
    在报告 coverage_semantics 显式披露）→ bundle。"""
    bundles: dict[str, dict] = {}
    for rel, sink in sink_by_rel.items():
        item = prepared[rel]
        retain = (Path(shard_log_dir) / f"{name}-sinks"
                  if shard_log_dir else None)
        hits, probes_fired = _orch("_read_hits_merged")(
            root, run_uuid, rel, manifest_by_rel[rel], xdg_homes,
            retain_dir=retain)
        block_map = prepared[rel].get("block_map") or {}
        if block_map:
            # §SPEC-055/070 FR-4：块入口命中展开为块内全部语句起始行
            # ——这是行覆盖近似而非逐语句精确测量：块内后继语句被
            # 运行期错误中止时仍会被计为已覆盖。该近似在报告
            # coverage_semantics 显式披露，不冒充精确
            # statement_start_line。
            hits = {ln for start in hits
                    for ln in block_map.get(start, [start])}
        bundles[rel] = _bundle_with_evidence(
            item, rel, name, run_uuid, sorted(hits), probes_fired,
            gut_ok, run_error, timed_out, False, evidence_dir)
        clear_sink(None, str(root), sink)
    return bundles

def _arm_vacuous_bundles(name: str, prepared: dict, vacuous_rels: list[str],
                         run_uuid: str, gut_ok: bool, run_error: bool,
                         timed_out: bool, evidence_dir) -> dict:
    """§SPEC-042：零可执行行 → 显式 vacuous 车道（bundle 落盘、
    union 按 §SPEC-036 严格语义仍 invalid，reason=vacuous）。"""
    bundles: dict[str, dict] = {}
    for rel in vacuous_rels:
        bundles[rel] = _bundle_with_evidence(
            prepared[rel], rel, name, run_uuid, [], 0,
            gut_ok, run_error, timed_out, True, evidence_dir)
    return bundles


def _arm_stage(name: str, *, root: Path, prepared: dict, recipe: dict,
               test_attribution: bool, attribution_causal: bool,
               shadow_instrument: bool, shard_width: int,
               gherkin, gherkin_repeats: int) -> tuple:
    """臂前置：uuid/备份/装载/pg 检查/created 清单/分片计划/钩子文本。"""
    from .instrument import _hook_templates
    from .probe_templates import (
        _ATTR_CTX_GD,
        _FLUSHER_RELPATH,
        _PRE_HOOK_RELPATH,
        _ATTR_CTX_RELPATH,
    )

    run_uuid = make_run_uuid()
    backup_dir = _backup_base() / run_uuid
    backup_dir.mkdir(mode=0o700)
    (started, sink_by_rel, manifest_by_rel, instrumented_files,
     inst_by_rel, vacuous_rels) = _stage_arm_files(
        name, prepared, run_uuid, test_attribution)
    started["project_root"] = str(root)
    _arm_stage_created(root, started, name, attribution_causal,
                       shadow_instrument, backup_dir)
    plans = _arm_plans(root, recipe, gherkin, shard_width)
    # §SPEC-062C：仅验收臂 N 次重复；gut_full 面单轮（确定性面）
    reps = max(1, int(gherkin_repeats)) if gherkin is not None else 1
    # P2（双臂并行）：gherkin 臂在 shadow 内插桩——钩子文本按本臂
    # uuid 生成，写入每个 shadow 的 addons/（根树不被本臂触碰）
    flusher_gd, pre_hook_gd = _hook_templates(
        test_attribution and name == "gut_full", run_uuid)
    shadow_hook_files = [(_FLUSHER_RELPATH, flusher_gd),
                         (_PRE_HOOK_RELPATH, pre_hook_gd)]
    if attribution_causal and name == "gut_full":
        shadow_hook_files.append((_ATTR_CTX_RELPATH, _ATTR_CTX_GD))
    return {"run_uuid": run_uuid, "backup_dir": backup_dir, "started": started,
            "sink_by_rel": sink_by_rel, "manifest_by_rel": manifest_by_rel,
            "instrumented_files": instrumented_files, "inst_by_rel": inst_by_rel,
            "vacuous_rels": vacuous_rels, "plans": plans, "reps": reps,
            "shadow_hook_files": shadow_hook_files,
            "attribution_causal": attribution_causal}
def _arm_fanout(name: str, *, root: Path, run_uuid: str, plans: list,
                reps: int, timeout_s: int, adaptive_reps: bool,
                sink_by_rel: dict, manifest_by_rel: dict,
                shadow_instrument: bool, inst_by_rel: dict,
                shadow_hook_files: list, shard_log_dir: str | None,
                xdg_homes: list[str],
                concurrency: int | None = None) -> list[dict]:
    """分片并行扇出（ThreadPoolExecutor：每分片独立进程 + XDG 隔离）。

    §SPEC-060e：worker 生命周期异常终止（如 shadow 构建失败）→ 已创建的
    XDG 立即清理；with-block 退出已等待全部在飞分片，闭包注册表在此刻
    已完整。"""
    results: list[dict] = []
    try:
        # concurrency（gherkin per-feature 进程上限）；缺省全并行
        workers = (max(1, min(int(concurrency), len(plans)))
                   if concurrency else max(1, len(plans)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(
                lambda ip: _run_shard_leg(
                    ip, name=name, plans=plans,
                    root=root, run_uuid=run_uuid,
                    reps=reps, timeout_s=timeout_s,
                    adaptive_reps=adaptive_reps, prev_union=None,
                    sink_by_rel=sink_by_rel,
                    manifest_by_rel=manifest_by_rel,
                    shadow_instrument=shadow_instrument,
                    inst_by_rel=inst_by_rel,
                    shadow_hook_files=shadow_hook_files,
                    shard_log_dir=shard_log_dir,
                    xdg_homes=xdg_homes),
                enumerate(plans)))
    except BaseException:
        for xdg in xdg_homes:
            shutil.rmtree(xdg, ignore_errors=True)
        xdg_homes.clear()
        raise
    return results

def _arm_execute(name: str, *, root: Path, shadow_instrument: bool,
                 timeout_s: int, adaptive_reps: bool, shard_log_dir: str | None,
                 stage: dict, concurrency: int | None = None) -> tuple:
    """执行段：根树注入 → 分片扇出 → XDG 异步清理 → 失败扫描。"""
    run_uuid = stage["run_uuid"]
    backup_dir = stage["backup_dir"]
    started = stage["started"]
    sink_by_rel = stage["sink_by_rel"]
    manifest_by_rel = stage["manifest_by_rel"]
    inst_by_rel = stage["inst_by_rel"]
    plans = stage["plans"]
    reps = stage["reps"]
    shadow_hook_files = stage["shadow_hook_files"]
    causal = stage["attribution_causal"]
    xdg_homes: list[str] = []
    t_arm = time.monotonic()
    try:
        if not shadow_instrument:
            _arm_inject_root(root, stage["instrumented_files"],
                             shadow_hook_files, causal, name)
        print(f"measure: arm={name} start shards={len(plans)} "
              f"reps={reps}", file=sys.stderr)
        results = _arm_fanout(
            name, root=root, run_uuid=run_uuid, plans=plans, reps=reps,
            timeout_s=timeout_s, adaptive_reps=adaptive_reps,
            sink_by_rel=sink_by_rel, manifest_by_rel=manifest_by_rel,
            shadow_instrument=shadow_instrument, inst_by_rel=inst_by_rel,
            shadow_hook_files=shadow_hook_files,
            shard_log_dir=shard_log_dir, xdg_homes=xdg_homes,
            concurrency=concurrency)
        wall_s = time.monotonic() - t_arm
        gut_ok, run_error, timed_out, arm_failures, arm_fail_pairs = \
            _arm_fail_scan(results)
    finally:
        _arm_restore(root, started, backup_dir)
    return (results, gut_ok, run_error, timed_out, wall_s, xdg_homes,
            arm_failures, arm_fail_pairs)
def _arm_gate(name: str, *, root: Path, recipe: dict, timeout_s: int,
              baseline_failures, baseline_pairs, declared_scoped,
              declared_bare, arm_fail_pairs, gut_ok, gherkin,
              results, run_error) -> tuple:
    """§SPEC-060a/061b/063c 门控（树已还原=无插桩；懒基线获取在此执行）：
    新增失败门控 + 具名排除按 (script, test) 身份吸收 + reconcile。
    返回 (gut_ok, run_error, baseline_meta, new_pairs, exempt_pairs,
    reconciliation, arm_valid, invalid_reason)。"""
    declared_scoped = declared_scoped or set()
    declared_bare = declared_bare or set()
    baseline_pairs = baseline_pairs or set()
    (effective_baseline, baseline_meta, new_pairs, exempt_pairs) = \
        _gate_ctx(baseline_failures, baseline_pairs, arm_fail_pairs,
                  declared_scoped, declared_bare)
    (effective_baseline, baseline_meta, new_pairs) = _lazy_baseline_merge(
        name, root, recipe, timeout_s, arm_fail_pairs, declared_scoped,
        effective_baseline, new_pairs, baseline_failures,
        baseline_meta)
    if new_pairs:
        gut_ok = False
        run_error = True
    (reconciliation, arm_valid, invalid_reason, gut_ok_delta,
     run_error) = _arm_reconcile(root, gherkin, results, run_error)
    gut_ok = gut_ok and not gut_ok_delta
    return (gut_ok, run_error, baseline_meta, new_pairs, exempt_pairs,
            reconciliation, arm_valid, invalid_reason)

def run_arm(name: str, *, root: Path, prepared: dict, recipe: dict,
            gherkin: dict | None = None, shadow_instrument: bool = False,
            adaptive_reps: bool = False, test_attribution: bool = False,
            attribution_causal: bool = False, shard_width: int = 6,
            shard_log_dir: str | None = None, timeout_s: int = 180,
            gherkin_repeats: int = 3,
            baseline_failures: "set[tuple[str, str]] | None" = None,
            baseline_pairs: "set[tuple[str, str]] | None" = None,
            declared_scoped: "set[tuple[str, str]] | None" = None,
            declared_bare: "set[str] | None" = None,
            arms_parallel: bool = False, evidence_dir=None,
            concurrency: int | None = None) -> dict:
    """一条批式臂（§SPEC-052 并行分片）：装载 → 分片扇出 → 门控 → 读回
    → vacuous 车道。shadow_instrument/adaptive_reps 语义见各分解函数。"""
    stage = _arm_stage(
        name, root=root, prepared=prepared, recipe=recipe,
        test_attribution=test_attribution,
        attribution_causal=attribution_causal,
        shadow_instrument=shadow_instrument, shard_width=shard_width,
        gherkin=gherkin, gherkin_repeats=gherkin_repeats)
    run_uuid, plans, reps = stage["run_uuid"], stage["plans"], stage["reps"]
    sink_by_rel, manifest_by_rel = stage["sink_by_rel"], stage["manifest_by_rel"]
    vacuous_rels = stage["vacuous_rels"]
    (results, gut_ok, run_error, timed_out, wall_s, xdg_homes,
     arm_failures, arm_fail_pairs) = _arm_execute(
        name, root=root, shadow_instrument=shadow_instrument,
        timeout_s=timeout_s, adaptive_reps=adaptive_reps,
        shard_log_dir=shard_log_dir, stage=stage,
        concurrency=concurrency)
    (gut_ok, run_error, baseline_meta, new_pairs, exempt_pairs,
     reconciliation, arm_valid, invalid_reason) = _arm_gate(
        name, root=root, recipe=recipe, timeout_s=timeout_s,
        baseline_failures=baseline_failures, baseline_pairs=baseline_pairs,
        declared_scoped=declared_scoped, declared_bare=declared_bare,
        arm_fail_pairs=arm_fail_pairs, gut_ok=gut_ok,
        gherkin=gherkin, results=results, run_error=run_error)
    return _arm_result(
        name, root=root, prepared=prepared, gherkin=gherkin,
        arms_parallel=arms_parallel, evidence_dir=evidence_dir,
        shard_log_dir=shard_log_dir, timeout_s=timeout_s,
        adaptive_reps=adaptive_reps and gherkin is not None,
        test_attribution=test_attribution, results=results,
        run_uuid=run_uuid, wall_s=wall_s, gut_ok=gut_ok,
        run_error=run_error, timed_out=timed_out,
        arm_failures=arm_failures, arm_fail_pairs=arm_fail_pairs,
        new_pairs=new_pairs, exempt_pairs=exempt_pairs,
        baseline_meta=baseline_meta, reconciliation=reconciliation,
        arm_valid=arm_valid, invalid_reason=invalid_reason, reps=reps,
        plans=plans, sink_by_rel=sink_by_rel,
        manifest_by_rel=manifest_by_rel, xdg_homes=xdg_homes,
        vacuous_rels=vacuous_rels)
def _arm_readback(name: str, *, root: Path, prepared: dict, gherkin,
                  test_attribution: bool, results, run_uuid: str, reps: int,
                  sink_by_rel: dict, manifest_by_rel: dict,
                  xdg_homes: list[str], gut_ok: bool, run_error: bool,
                  timed_out: bool, vacuous_rels: list, evidence_dir,
                  shard_log_dir: str | None) -> tuple:
    """读回段：convergence/bundles/attribution（xdg 清理前读回归属）→
    vacuous 车道并入。返回 (convergence, attribution, bundles)。"""
    convergence = None
    attribution = None
    bundles: dict[str, dict] = {}
    try:
        if gherkin is not None and reps > 1:
            # §SPEC-062C：逐轮行集并集收敛轮数（rep 间独立进程，
            # 并集对轮序不敏感）
            from .orchestrator import _gherkin_convergence
            convergence = _gherkin_convergence(
                root, run_uuid, sink_by_rel, manifest_by_rel,
                results, reps)
        bundles = _arm_hit_bundles(
            name, prepared, sink_by_rel, manifest_by_rel, xdg_homes,
            root, run_uuid, gut_ok, run_error, timed_out,
            evidence_dir, shard_log_dir)
        # §SPEC-083：归属证据与注册表必须在 xdg 清理前读回（仅归属臂）
        attribution = _collect_attribution(
            test_attribution and name == "gut_full", root, run_uuid, name,
            sink_by_rel, xdg_homes, prepared=prepared)
    finally:
        for xdg in xdg_homes:
            shutil.rmtree(xdg, ignore_errors=True)
    bundles.update(_arm_vacuous_bundles(
        name, prepared, vacuous_rels, run_uuid, gut_ok, run_error,
        timed_out, evidence_dir))
    return convergence, attribution, bundles

def _arm_doc(name: str, run_uuid: str, wall_s: float, gut_ok: bool,
             timed_out: bool, arm_valid: bool, invalid_reason, run_error: bool,
             baseline_meta, arm_failures, arm_fail_pairs, new_pairs,
             exempt_pairs, reps: int, convergence, adaptive_rounds_used,
             arms_parallel: bool, attribution, bundles: dict,
             reconciliation, plans: list, results) -> dict:
    """结果文档组装（键序 = 结果 JSON 契约）。"""
    return {
        "arm": name, "run_uuid": run_uuid, "wall_s": round(wall_s, 3),
        "gut_ok": gut_ok, "timed_out": timed_out,
        "state": ("valid" if arm_valid else "invalid"),
        "invalid_reason": (invalid_reason
                           if invalid_reason is not None
                           else ("timeout" if timed_out
                                 else ("run_error" if run_error else None))),
        "baseline_failures_meta": baseline_meta,
        "test_failures": {"observed": sorted(arm_failures),
                          "observed_pairs": sorted(
                              f"{s}::{t}" for (s, t) in arm_fail_pairs),
                          "new": sorted({t for (_s, t) in new_pairs}),
                          "new_pairs": sorted(
                              f"{s}::{t}" for (s, t) in new_pairs),
                          "instrumentation_intolerant": sorted(
                              f"{s}::{t}" for (s, t) in exempt_pairs)},
        # §SPEC-062C：验收臂 N 次重复与收敛披露（gut_full 恒单轮）
        "repeats": reps,
        "convergence": convergence,
        "adaptive_rounds_used": adaptive_rounds_used,
        "arms_parallel": arms_parallel,
        "attribution": attribution,
        "bundles": len(bundles), "bundles_data": bundles,
        "reconciliation": reconciliation,
        "shards": len(plans),
        "shadowed": True,
        # §SPEC-071 H1：shadow symlink 消解计数显式披露
        "shadow_disclosure": {
            "symlinks_dereferenced": sum(
                r.get("shadow_disclosure", {}).get(
                    "symlinks_dereferenced", 0) for r in results),
            "dangling_links_removed": sum(
                r.get("shadow_disclosure", {}).get(
                    "dangling_links_removed", 0) for r in results),
        },
    }

def _arm_result(name: str, *, root: Path, prepared: dict, gherkin,
                arms_parallel: bool, evidence_dir, shard_log_dir: str | None,
                timeout_s: int, adaptive_reps: bool, test_attribution: bool,
                results, run_uuid: str, wall_s: float, gut_ok: bool,
                run_error: bool, timed_out: bool, arm_failures,
                arm_fail_pairs, new_pairs, exempt_pairs, baseline_meta,
                reconciliation, arm_valid: bool, invalid_reason, reps: int,
                plans: list, sink_by_rel: dict, manifest_by_rel: dict,
                xdg_homes: list[str], vacuous_rels: list) -> dict:
    """读回段：convergence/bundles/attribution（xdg 清理前读回归属）→
    vacuous 车道 → 结果文档组装（键序 = 结果 JSON 契约）。"""
    convergence, attribution, bundles = _arm_readback(
        name, root=root, prepared=prepared, gherkin=gherkin,
        test_attribution=test_attribution, results=results,
        run_uuid=run_uuid, reps=reps, sink_by_rel=sink_by_rel,
        manifest_by_rel=manifest_by_rel, xdg_homes=xdg_homes, gut_ok=gut_ok,
        run_error=run_error, timed_out=timed_out, vacuous_rels=vacuous_rels,
        evidence_dir=evidence_dir, shard_log_dir=shard_log_dir)
    print(f"measure: arm={name} done state="
          f"{'valid' if arm_valid else 'invalid'} "
          f"wall_s={round(wall_s, 1)}", file=sys.stderr)
    adaptive_rounds_used = max(
        (len(r["reps"]) for r in results), default=0) if adaptive_reps else None
    return _arm_doc(
        name, run_uuid, wall_s, gut_ok, timed_out, arm_valid, invalid_reason,
        run_error, baseline_meta, arm_failures, arm_fail_pairs, new_pairs,
        exempt_pairs, reps, convergence, adaptive_rounds_used, arms_parallel,
        attribution, bundles, reconciliation, plans, results)
def _ensure_gherkin_harness(root: Path) -> None:
    """SEE-1624 §SPEC-002：契约位在场断言（单根裁决下的连带简化）。

    生成测试加载位 `res://addons/gqt_gherkin/harness.gd` 是 addon 单根
    的受控源文件（KOL M1 后随 addon 入库）；缺失即布局错误 fail-loud，
    不再从旧路径复制（cp 接线退役，无双根/回退/自动复制）。"""
    dest = root / "addons" / "gqt_gherkin" / "harness.gd"
    if not dest.is_file():
        raise MeasurementError(
            f"gherkin harness contract position missing: "
            f"res://addons/gqt_gherkin/harness.gd — addon single-root "
            f"layout required (no cp wiring, no legacy fallback)")


def _gherkin_groups(keys: list[str], shards: int) -> list[list[str]]:
    """§SPEC-005 共享分片：排序去重后均衡切分为 shards 组（组数=分片数，
    组内多 key 共享 suite/进程）。keys <= shards 时退化为单 key 组。"""
    ks = sorted(set(keys))
    n = max(1, int(shards or 1))
    n = min(n, len(ks)) if ks else 1
    groups: list[list[str]] = [[] for _ in range(n)]
    for i, k in enumerate(ks):
        groups[i % n].append(k)
    return [g for g in groups if g]


_FALLBACK_REASON = "shared_red_isolated_green"


def _script_key(script: str) -> str | None:
    m = re.search(r"test_(.+)\.gd$", script.replace("\\", "/"))
    return m.group(1) if m else None


def _reconciliation_failed_keys(result: dict) -> set[str]:
    """对账失败分组的全组 key（对账 ok 或缺失时为空集）。"""
    rec = result.get("reconciliation") or {}
    if not rec or rec.get("ok"):
        return set()
    return {k for sh in rec.get("shards") or [] if not sh.get("ok")
            for k in sh.get("keys") or []}


def _failed_keys(result: dict, groups: list[dict]) -> list[str]:
    """共享轮失败 key 集：观测失败对的脚本归属 + 对账失败分组的全组 key。"""
    keys = {k for pair in (result.get("test_failures") or {}).get(
        "observed_pairs", []) if (k := _script_key(pair.split("::", 1)[0]))}
    keys |= _reconciliation_failed_keys(result)
    return sorted(k for k in keys
                  if any(k in g["keys"] for g in groups))


def _green_script(pair_script: str, ok_keys: set[str]) -> bool:
    return Path(pair_script.replace("\\", "/")).name in {
        f"test_{k}.gd" for k in ok_keys}


def _isolated_green_keys(fb_result: dict, fb_groups: list[dict]
                         ) -> tuple[set[str], bool]:
    """单例隔离轮：返回 (隔离绿 key 集, 对账 ok)。隔离红 key 不列入。"""
    fb_rec_ok = bool((fb_result.get("reconciliation") or {}).get("ok", True))
    ok_keys: set[str] = set()
    for g in fb_groups:
        k = g["keys"][0]
        script = f"test_{k}.gd"
        red = [pr for pr in (fb_result.get("test_failures") or {}).get(
            "observed_pairs", [])
            if pr.split("::", 1)[0].endswith(script)]
        if not red and fb_rec_ok:
            ok_keys.add(k)
    return ok_keys, fb_rec_ok


def _keep_pairs(pairs, keep) -> set:
    """按 (script, test) 二元组保留谓词为真的失败对。"""
    return {pr for pr in pairs if keep(f"{pr[0]}::{pr[1]}")}


def _keep_pair_strings(pairs, keep) -> list[str]:
    """按 "script::test" 字符串保留谓词为真的失败对（排序）。"""
    return sorted(pr for pr in pairs if keep(pr))


def _drop_green_failures(result: dict, ok_keys: set[str]) -> None:
    """共享轮红对移除隔离绿 key（进程残留伪红由隔离证据推翻）。"""
    def keep(pair: str) -> bool:
        return not _green_script(pair.split("::", 1)[0], ok_keys)

    # SEE-1619 C3-R2：合并后键面必须 JSON 可序列化——set 形态会使
    # cli.py 写报告时 TypeError 崩溃（A/A 三轮 rc=1 的根因）。以
    # sorted "s::t" 字符串列表承载（与 test_failures 块同形）。
    kept_pairs = _keep_pairs(
        result.get("arm_fail_pairs") or set(), keep)
    result["arm_fail_pairs"] = sorted(
        f"{s}::{t}" for (s, t) in kept_pairs)
    result["arm_failures"] = sorted({t for (_s, t) in kept_pairs})
    result["new_pairs"] = sorted(
        f"{s}::{t}" for (s, t) in _keep_pairs(
            result.get("new_pairs") or set(), keep))
    tf = result.get("test_failures") or {}
    tf["observed_pairs"] = _keep_pair_strings(
        tf.get("observed_pairs", []), keep)
    tf["observed"] = sorted(
        pr.split("::", 1)[1] for pr in tf["observed_pairs"])
    tf["new_pairs"] = _keep_pair_strings(tf.get("new_pairs", []), keep)
    tf["new"] = sorted(pr.split("::", 1)[1] for pr in tf["new_pairs"])
    result["test_failures"] = tf


def _merged_state(rec_ok: bool, remaining_fail: bool
                  ) -> tuple[str, str | None]:
    """合并后状态与 invalid_reason（valid 时 reason 为 None）。"""
    if rec_ok and not remaining_fail:
        return "valid", None
    if not rec_ok:
        return "invalid", "reconciliation_failed"
    return "invalid", "isolated_red_not_fallback"


def _merge_reconciliation(result: dict, fb_result: dict, fb_rec_ok: bool,
                          ok_keys: set[str]) -> None:
    """两轮 reconciliation 分项并列；gut_ok/state 按合并后残集重算。"""
    shared_rec = result.get("reconciliation") or {}
    fb_rec = fb_result.get("reconciliation") or {}
    rec_ok = bool(shared_rec.get("ok", True)) and fb_rec_ok
    merged_shards = list(shared_rec.get("shards") or []) + [
        {**sh, "round": "fallback_singleton"}
        for sh in (fb_rec.get("shards") or [])]
    result["reconciliation"] = {
        "ok": rec_ok,
        "shards": merged_shards,
        "fallback_round": {"ok": fb_rec_ok,
                           "keys": sorted(ok_keys)},
    }
    remaining_fail = bool(result["arm_fail_pairs"]) or bool(
        result["new_pairs"])
    result["gut_ok"] = not remaining_fail
    result["state"], result["invalid_reason"] = _merged_state(
        rec_ok, remaining_fail)
    result["wall_s"] = round(
        float(result.get("wall_s") or 0.0)
        + float(fb_result.get("wall_s") or 0.0), 3)


def _merge_fallback_round(result: dict, fb_result: dict,
                          fb_groups: list[dict]) -> dict:
    """§SPEC-006 共享轮 → 单例隔离轮合并。

    隔离绿 key：共享轮红对移除（进程残留伪红由隔离证据推翻）、process_
    fallbacks 具名；隔离红 key：共享红保持、不列入降级（不得包装为有效
    降级）。bundle/收敛/reps 保留共享轮证据（覆盖测量以共享轮为准）；
    reconciliation 两轮分项并列披露；gut_ok/state 按合并后残集重算。"""
    ok_keys, fb_rec_ok = _isolated_green_keys(fb_result, fb_groups)
    _drop_green_failures(result, ok_keys)
    _merge_reconciliation(result, fb_result, fb_rec_ok, ok_keys)
    return result


def _fallback_disclosure(result: dict, fb_groups: list[dict]) -> list[dict]:
    """§SPEC-006 process_fallbacks：按 key 排序、每项恰含 key/reason。
    披露即合并轮的隔离绿判定（fallback_round.keys）；隔离红 key 不列入
    （不得包装为有效降级）；无降级轮为空数组。"""
    if not fb_groups:
        return []
    rec = result.get("reconciliation") or {}
    fb_round = rec.get("fallback_round") or {}
    if not bool(fb_round.get("ok", True)):
        return []
    fb_keys = {g["keys"][0] for g in fb_groups}
    return sorted(
        ({"key": k, "reason": _FALLBACK_REASON}
         for k in set(fb_round.get("keys") or []) & fb_keys),
        key=lambda f: f["key"])


def _compile_into(sd: Path, root: Path, chunk: list[str]) -> None:
    """私有 suite 组编译 + 逐 key 重放校验（目录即选择集）。"""
    compile_keys_to_suite(root, chunk, sd)
    for k in chunk:
        replay_check(root, k, suite_dir=sd)


def _finish_gherkin_result(result: dict, fb_groups: list[dict]) -> dict:
    """成功路径收口：process_fallbacks 具名披露（harness 契约位是受控
    源文件，SEE-1624 后无临时接线面，故不再有 residue 车道）。"""
    result["process_fallbacks"] = _fallback_disclosure(result, fb_groups)
    return result


def _gherkin_error_result(e: Exception, groups: list[dict]) -> dict:
    """异常路径：回收已建 suite 并组装 invalid 结果（预检失败/编译失败/
    重放失败统一具名，无 fallback、无残留）。"""
    for g in groups:
        _cleanup_suite_dir(Path(g["suite_dir"]))
    return {"arm": "gherkin", "run_uuid": None, "wall_s": 0.0,
            "gut_ok": False, "timed_out": False, "state": "invalid",
            "invalid_reason": f"suite_error:{e}", "process_fallbacks": [],
            "bundles": 0,
            "bundles_data": {}, "reconciliation": None,
            "test_failures": {"observed": [], "observed_pairs": [],
                              "new": [], "new_pairs": [],
                              "instrumentation_intolerant": []},
            "repeats": 0, "convergence": None}


def run_gherkin_flow(*, root: Path, recipe: dict,
                     gherkin_keys: list[str] | None,
                     gherkin_shards: int,
                     arms_parallel: bool, gherkin_adaptive: bool,
                     arm_kwargs: dict) -> dict:
    """臂 gherkin：预检（addon validator 一次，§SPEC-003）→ 私有 suite
    按分片分组编译/重放 → 并行扇出 GUT。"""
    keys = list(gherkin_keys) if gherkin_keys else [
        f["key"] for f in recipe["gherkin_face"]["features"]]
    snapshot = _snapshot_main(root)
    groups: list[dict] = []
    cleanup_done = False
    try:
        try:
            run_preflight(root)
        except AddonError as e:
            # measure 绕过 CLI 的核心路径同样被预检守门：失败具名 invalid
            # 且无 suite 残留（由 except 分支统一回收）。
            raise GherkinRunError(f"gherkin preflight failed: {e}") from e
        _ensure_gherkin_harness(root)
        # §SPEC-005：恢复 gherkin_shards 共享分片——44 feature 已过
        # gherkin-feature-state-isolation 原子化门（SEE-1619 C1），
        # 同进程共享 suite 不再有跨 feature 残留（R1 槽位缝 + 零写入判据
        # 闭环）。shards = 共享组数，并发上限同步收敛到组数；扇出 =
        # len(groups) × reps（非 keys × reps）。
        for chunk in _gherkin_groups(keys, gherkin_shards):
            sd = create_suite_dir(root)
            groups.append({"suite_dir": sd, "keys": list(chunk)})
            _compile_into(sd, root, chunk)

        result = run_arm("gherkin", root=root, recipe=recipe,
                         gherkin={"groups": groups, "snapshot": snapshot},
                         shadow_instrument=arms_parallel,
                         adaptive_reps=gherkin_adaptive,
                         concurrency=max(1, int(gherkin_shards or 1)),
                         **arm_kwargs)

        # §SPEC-006：临时具名降级——仅当某 feature 当次未通过共享校验时
        # singleton 隔离复跑；隔离绿 = process_fallbacks 具名披露，隔离
        # 红 = 保持红（不得包装为有效降级）。全原子时为空数组。
        failed = _failed_keys(result, groups)
        fb_groups: list[dict] = []
        if failed:
            for k in failed:
                sd = create_suite_dir(root)
                fb_groups.append({"suite_dir": sd, "keys": [k]})
                _compile_into(sd, root, [k])
            # §SPEC-005/006（C2-R1）：降级轮与共享轮共用同一并发上限——
            # 多 key 失败时不得按失败数重新放开扇出（OOM 风险回潮）。
            fb_result = run_arm(
                "gherkin", root=root, recipe=recipe,
                gherkin={"groups": fb_groups, "snapshot": snapshot},
                shadow_instrument=arms_parallel,
                adaptive_reps=gherkin_adaptive,
                concurrency=max(1, int(gherkin_shards or 1)), **arm_kwargs)
            result = _merge_fallback_round(result, fb_result, fb_groups)

        cleanup_done = True
        return _finish_gherkin_result(result, fb_groups)
    except (SuiteError, GherkinRunError, MeasurementError) as e:
        result = _gherkin_error_result(e, groups)
        cleanup_done = True
        return result
    finally:
        # D22/D23：兜底清理覆盖**所有退出路径**——run_arm 等未列名异常
        # 冒泡时，本次新建的 **suite 目录**一并回收（异常照常传播，不吞；
        # 清理自身失败逐项写 stderr，不静默）。已显式清理的路径由
        # cleanup_done 跳过（成功路径 suites 由 run_arm 读回段清理）。
        if not cleanup_done:
            for g in groups + (fb_groups if "fb_groups" in locals() else []):
                try:
                    _cleanup_suite_dir(Path(g["suite_dir"]))
                except OSError as e:
                    print(f"measure: gherkin suite cleanup failed "
                          f"({g['suite_dir']}): {e}", file=sys.stderr)

def execute_arms(*, root: Path, recipe: dict, arms_parallel: bool,
                 gherkin_keys: list[str] | None,
                 gherkin_shards: int, gherkin_adaptive: bool,
                 arm_kwargs: dict) -> tuple[dict, dict]:
    """双臂执行：arms_parallel 时 gherkin 臂在 shadow 内插桩（根树只被
    gut 臂触碰），否则串行。"""
    if arms_parallel:
        with ThreadPoolExecutor(max_workers=2) as pex:
            fut_a = pex.submit(run_arm, "gut_full", root=root,
                               recipe=recipe, **arm_kwargs)
            fut_b = pex.submit(run_gherkin_flow, root=root,
                               recipe=recipe,
                               gherkin_keys=gherkin_keys,
                               gherkin_shards=gherkin_shards,
                               arms_parallel=arms_parallel,
                               gherkin_adaptive=gherkin_adaptive,
                               arm_kwargs=arm_kwargs)
            arm_a = fut_a.result()
            arm_b = fut_b.result()
    else:
        # 臂 gut_full：.gutconfig 面（无 -gdir——GUT 缺省读配置，lab 实测锚定）
        arm_a = run_arm("gut_full", root=root, recipe=recipe, **arm_kwargs)
        arm_b = run_gherkin_flow(
            root=root, recipe=recipe, gherkin_keys=gherkin_keys,
            gherkin_shards=gherkin_shards,
            arms_parallel=arms_parallel,
            gherkin_adaptive=gherkin_adaptive,
            arm_kwargs=arm_kwargs)
    return arm_a, arm_b
