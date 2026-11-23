"""SEE-1594 BTIDY（零行为拆分）：批式臂执行——单臂（§SPEC-052 并行分片、
XDG 隔离、shadow 插桩、门控/对账/bundle）与 acceptance 臂编排。自
orchestrator.py 的 run_measurement 内嵌闭包 `_arm` / `_acceptance_flow`
提升为模块级并按阶段分解为 ≤50 行函数；闭包变量显式参数化，逻辑逐语句
等价。"""

from __future__ import annotations

import json
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
from ..gherkin.run import AcceptRunError, replay_check
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



def _scan_shard(r: dict, keys: list[str], shard_id: str) -> dict:
    """§SPEC-TIMEOUT-ADAPT：单分片扫描（超时→按名排除标记；rc!=0 无可
    解析失败名→fail-closed）。"""
    from .orchestrator import _parse_failing_pairs

    out = {"timed_out": False, "gut_ok": True, "run_error": False,
           "fail_pairs": set(), "failures": set(),
           "shard_id": shard_id}
    for rep in r.get("reps") or []:
        if rep["timed_out"]:
            out["timed_out"] = True
            continue
        pairs = _parse_failing_pairs(rep["out"])
        out["fail_pairs"] |= pairs
        out["failures"] |= {t for (_s, t) in pairs}
        if rep["rc"] != 0:
            if _looks_like_godot_launch_failure(rep["out"]) or not pairs:
                out["gut_ok"] = False
                out["run_error"] = True
    return out


def _arm_fail_scan(results: list[dict]) -> tuple:
    """§SPEC-063a/c：失败身份解析为 (script, test) 对；任一 rep rc!=0
    且无可解析失败名（含启动失败）→ 不可归因失败 fail-closed 门控，
    不得出分。

    §SPEC-TIMEOUT-ADAPT：分片超时**不再置 run_error**——该分片计划内
    测试按名收进 timeout_excluded（reason=shard_timeout，PLANFIRST
    partial 语义），仅有其它失败仍照旧 fail-closed。返回 (gut_ok,
    run_error, timed_out, arm_failures, arm_fail_pairs, timeout_excluded,
    all_keys)。"""
    gut_ok = True
    run_error = False
    timed_out = False
    arm_failures: set[str] = set()
    arm_fail_pairs: set[tuple[str, str]] = set()
    timeout_excluded: list[dict] = []
    all_keys: list[str] = []
    for r in results:
        shard = _scan_shard_row(r)
        timed_out = timed_out or shard["timed_out"]
        gut_ok = gut_ok and shard["gut_ok"]
        run_error = run_error or shard["run_error"]
        arm_fail_pairs |= shard["fail_pairs"]
        arm_failures |= shard["failures"]
        all_keys += shard["keys"]
        if shard["timed_out"]:
            timeout_excluded.append({"shard": shard["shard_id"],
                                     "keys": shard["keys"],
                                     "reason": "shard_timeout"})
    return _finalize_shard_scan(gut_ok, run_error, timed_out, arm_failures,
                                arm_fail_pairs, timeout_excluded, all_keys)


def _scan_shard_row(r: dict) -> dict:
    """§SPEC-TIMEOUT-ADAPT：单分片行扫描（keys 解析 + 逐 rep 归类）。"""
    plan = r.get("plan") or {}
    # gut_full 分片计划为 -gdir 组（keys=None）——排除粒度按 dir 组；
    # acceptance 分片有 keys 列表按名排除
    keys = ([str(k) for k in (plan.get("keys") or [])]
            or [a for a in (plan.get("args") or [])
                if str(a).startswith("-gdir=")])
    shard_id = str(plan.get("keys") or plan.get("suite_dir")
                   or ";".join(keys) or "")[:120]
    shard = _scan_shard(r, keys, shard_id)
    shard["keys"] = keys
    return shard


def _finalize_shard_scan(gut_ok: bool, run_error: bool, timed_out: bool,
                         arm_failures: set, arm_fail_pairs: set,
                         timeout_excluded: "list[dict]",
                         all_keys: "list[str]") -> tuple:
    """§SPEC-TIMEOUT-ADAPT：排除后无可用测试 → 整体拒绝。"""
    excluded_keys = {k for e in timeout_excluded for k in e["keys"]}
    usable = [k for k in all_keys if k not in excluded_keys]
    if timed_out and not usable:
        gut_ok = False
        run_error = True
    return (gut_ok, run_error, timed_out, arm_failures, arm_fail_pairs,
            timeout_excluded, all_keys)

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

_SUSPECT_INTOL_REASON = ("suspected_instrumentation_intolerance: passes "
                         "uninstrumented baseline, fails instrumented run "
                         "— confirm to exempt or investigate instrumentation")


def _suspected_draft(pairs: set) -> list:
    """SEE-1623 H5：疑似插桩不耐受 → --intolerant-tests 声明草稿
    （与声明配置同构：script 缺省 = bare-name 形态，仅豁免未归因失败）。"""
    out: list = []
    for s, t in sorted(pairs):
        entry = {"name": t, "reason": _SUSPECT_INTOL_REASON}
        if s:
            entry = {"script": s, **entry}
        out.append(entry)
    return out


def _lazy_baseline_merge(name: str, root: Path, recipe: dict,
                         timeout_s: int, arm_fail_pairs: set,
                         declared_scoped: set, effective_baseline: set,
                         new_pairs: set,
                         baseline_failures,
                         baseline_meta: dict | None) -> tuple:
    """§SPEC-064a 懒获取（成本裁定）：绿臂零额外 GUT；观测到失败且未
    显式提供基线时才跑一次同面无插桩 GUT 取基线并复核。获取失败 → 具名
    stderr + 臂保持 invalid（fail-loud，不按空基线静默转绿）。
    acceptance 臂不得继承 gut_full 的懒基线（其失败保持可见）。

    SEE-1623 H5：懒基线实测后仍新增的失败 = 实测证明「未插桩通过、插桩
    失败」——按 suspected_instrumentation_intolerance 迁出 new_failures
    门禁，入具名不耐受车道并附 --intolerant-tests 声明草稿（gut_full
    限定；获取失败无实测证明不分类，fail-loud 语义不弱化）。"""
    from .orchestrator import (
        _acquire_lazy_baseline,
        _gutconfig_face_args,
    )

    suspected: set = set()
    if not (name == "gut_full" and new_pairs
            and baseline_failures is None):
        return effective_baseline, baseline_meta, new_pairs, suspected
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
        if new_pairs:
            # SEE-1623 H5（身份规范化）：同一失败的未归因回显 ("", t) 与
            # 归因形态 (s, t) 并存时只保留归因形态——车道计数不翻倍。
            scripted = {p for p in new_pairs if p[0]}
            scripted_names = {t for _, t in scripted}
            suspected = scripted | {p for p in new_pairs
                                    if not p[0] and p[1] not in scripted_names}
            new_pairs = set()
            draft = _suspected_draft(suspected)
            print(f"measure: {len(suspected)} failing pair(s) classified "
                  f"suspected_instrumentation_intolerance (pass "
                  f"uninstrumented baseline, fail instrumented) — routed "
                  f"out of new_failures; declaration draft for "
                  f"--intolerant-tests: {json.dumps(draft, ensure_ascii=False)}",
                  file=sys.stderr)
    return effective_baseline, baseline_meta, new_pairs, suspected

def _arm_reconcile(root: Path, acceptance: dict | None, results: list[dict],
                   run_error: bool) -> tuple:
    """§SPEC-035 对账：分片组逐 rep 对账（rep 间独立进程独立输出），
    任一 false → 臂 invalid；§SPEC-063b：结构对账失败必须传入 union
    失败哨兵（run_error）——对账失败不得静默出分。suite 目录清理。"""
    reconciliation = None
    arm_valid = not run_error
    invalid_reason = None
    gut_ok_delta = False
    if acceptance is not None and not run_error:
        recs = []
        ok_all = True
        for r in results:
            for rep in r["reps"]:
                rec = reconcile(
                    root, r["plan"]["suite_dir"], r["plan"]["keys"],
                    rep["out"], replay_ok=True,
                    main_snapshot=acceptance["snapshot"])
                recs.append(rec)
                ok_all = ok_all and bool(rec.get("ok"))
        reconciliation = {"ok": ok_all, "shards": recs}
        if not ok_all:
            arm_valid = False
            invalid_reason = "reconciliation_failed"
            gut_ok_delta = True
            run_error = True
    if acceptance is not None:
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
               acceptance, acceptance_repeats: int) -> tuple:
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
    plans = _arm_plans(root, recipe, acceptance, shard_width)
    # §SPEC-062C：仅验收臂 N 次重复；gut_full 面单轮（确定性面）
    reps = max(1, int(acceptance_repeats)) if acceptance is not None else 1
    # P2（双臂并行）：acceptance 臂在 shadow 内插桩——钩子文本按本臂
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
        # concurrency（acceptance per-feature 进程上限）；缺省全并行
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
        (gut_ok, run_error, timed_out, arm_failures, arm_fail_pairs,
         timeout_excluded, all_keys) = _arm_fail_scan(results)
    finally:
        _arm_restore(root, started, backup_dir)
    return (results, gut_ok, run_error, timed_out, wall_s, xdg_homes,
            arm_failures, arm_fail_pairs, timeout_excluded, all_keys)
def _scan_shard_problems(results, timeout_s: int, timeout_source: str):
    """§SPEC-PLANFIRST：分片级问题扫描——(launch, timeouts, unattributed)
    三类明细（超时分片带超时值与来源；不可归因分片带 rc 与输出尾部）。"""
    from .orchestrator import _parse_failing_pairs

    unattributed: list[dict] = []
    timeouts: list[dict] = []
    launch: list[dict] = []
    for r in results or []:
        ident = (r.get("plan") or {}).get("keys") or \
            (r.get("plan") or {}).get("suite_dir") or \
            f"shard#{(results or []).index(r)}"
        for rep in r.get("reps") or []:
            problem = _classify_rep_problem(rep, ident, timeout_s,
                                            timeout_source)
            if problem is None:
                continue
            kind, rec = problem
            {"timeout": timeouts, "launch": launch,
             "unattributed": unattributed}[kind].append(rec)
    return launch, timeouts, unattributed


def _classify_rep_problem(rep, ident, timeout_s: int, timeout_source: str):
    """单 rep 问题归类：None=正常；("timeout"/"launch"/"unattributed", 记录)。"""
    from .orchestrator import _parse_failing_pairs

    ident = str(ident)[:120]
    if rep.get("timed_out"):
        return "timeout", {"shard": ident, "timeout_s": timeout_s,
                           "timeout_source": timeout_source}
    if rep.get("rc") in (0, None):
        return None
    tail = (rep.get("out") or "").strip().splitlines()[-3:]
    if _looks_like_godot_launch_failure(rep.get("out") or ""):
        return "launch", {"shard": ident, "rc": rep.get("rc"),
                          "tail": tail}
    if not _parse_failing_pairs(rep.get("out") or ""):
        return "unattributed", {"shard": ident, "rc": rep.get("rc"),
                                "tail": tail}
    return None


def _classify_invalid(name: str, *, results, timed_out: bool,
                      run_error: bool, new_pairs: set,
                      effective_baseline: set, timeout_s: int,
                      timeout_source: str,
                      reconciliation_failed: bool) -> "tuple[str, dict | None]":
    """§SPEC-PLANFIRST 证据补强：臂 invalid 的细分原因与证据（不再只有
    通用文案）。优先级：launch_failure > shard_timeout >
    unattributed_failures > new_failures > reconciliation_failed；
    证据块带全部命中类的明细。"""
    launch, timeouts, unattributed = _scan_shard_problems(
        results, timeout_s, timeout_source)
    hits: list[str] = []
    evidence: dict = {}
    if launch:
        hits.append("launch_failure")
        evidence["launch_failures"] = launch
    if timeouts:
        hits.append("shard_timeout")
        evidence["shard_timeouts"] = timeouts
    if unattributed:
        hits.append("unattributed_failures")
        evidence["unattributed_shards"] = unattributed
    if new_pairs:
        hits.append("new_failures")
        evidence["new_pairs"] = sorted(f"{a}::{b}" for a, b in new_pairs)
        evidence["baseline_absorbed"] = len(effective_baseline or set())
    if reconciliation_failed:
        hits.append("reconciliation_failed")
    if not hits:
        return ("run_error", {"note": "arm invalid without a classified "
                              "cause (legacy generic reason)"})
    evidence["all_causes"] = hits
    return hits[0], evidence


def _arm_gate(name: str, *, root: Path, recipe: dict, timeout_s: int,
              baseline_failures, baseline_pairs, declared_scoped,
              declared_bare, arm_fail_pairs, gut_ok, acceptance,
              results, run_error, timed_out: bool = False,
              timeout_excluded: "list[dict] | None" = None,
              all_keys: "list[str] | None" = None,
              timeout_s_source: str = "parameter") -> tuple:
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
    (effective_baseline, baseline_meta, new_pairs, suspected_pairs) = \
        _lazy_baseline_merge(
            name, root, recipe, timeout_s, arm_fail_pairs, declared_scoped,
            effective_baseline, new_pairs, baseline_failures,
            baseline_meta)
    # SEE-1623 H5：疑似不耐受对入具名车道（observed 非空），不进
    # new_failures 门禁；草稿随臂文档出报告块。
    exempt_pairs = set(exempt_pairs) | suspected_pairs
    suspected_draft = _suspected_draft(suspected_pairs) if suspected_pairs \
        else None
    excluded: list[dict] = []
    # §SPEC-TIMEOUT-ADAPT：超时分片按名排除（partial），非 run_error——
    # 仅排除后无可用测试才整体拒绝（判定在 _arm_fail_scan）
    if timeout_excluded:
        excluded += [dict(e, timeout_s=timeout_s,
                          timeout_s_source=timeout_s_source)
                     for e in timeout_excluded]
        print(f"measure: shard_timeout — "
              f"{sum(len(e.get('keys') or []) for e in timeout_excluded)} "
              f"test(s) EXCLUDED by name; mapping marked partial",
              file=sys.stderr)
    # §SPEC-TIMEOUT-ADAPT：EXPLICIT_PARTIAL_CONTINUE 接线（中断报告
    # 选项 c）——显式 env 下新增失败按名排除继续（partial），不中断
    if new_pairs and os.environ.get(
            "GQT_MEASUREMENT_EXPLICIT_PARTIAL_CONTINUE", "").strip() \
            not in ("", "0", "false"):
        excluded.append({"reason": "explicit_partial_continue",
                         "keys": sorted(f"{a}::{b}" for a, b in new_pairs)})
        print(f"measure: EXPLICIT_PARTIAL_CONTINUE — {len(new_pairs)} new "
              f"failing pair(s) EXCLUDED by name; mapping marked partial",
              file=sys.stderr)
        new_pairs = set()
    if new_pairs:
        gut_ok = False
        run_error = True
    (reconciliation, arm_valid, invalid_reason, gut_ok_delta,
     run_error) = _arm_reconcile(root, acceptance, results, run_error)
    gut_ok = gut_ok and not gut_ok_delta
    # §SPEC-PLANFIRST 证据补强：invalid 时细分原因 + 证据块（不再只有
    # 通用「new failures gate the build」文案）
    invalid_evidence = None
    if not arm_valid:
        invalid_reason, invalid_evidence = _classify_invalid(
            name, results=results, timed_out=timed_out, run_error=run_error,
            new_pairs=new_pairs, effective_baseline=effective_baseline,
            timeout_s=timeout_s,
            timeout_source=f"parameter timeout_s={timeout_s}s",
            reconciliation_failed=(invalid_reason
                                   == "reconciliation_failed"))
    return (gut_ok, run_error, baseline_meta, new_pairs, exempt_pairs,
            reconciliation, arm_valid, invalid_reason, invalid_evidence,
            excluded, suspected_draft)

def run_arm(name: str, *, root: Path, prepared: dict, recipe: dict,
            acceptance: dict | None = None, shadow_instrument: bool = False,
            adaptive_reps: bool = False, test_attribution: bool = False,
            attribution_causal: bool = False, shard_width: int = 6,
            shard_log_dir: str | None = None, timeout_s: int = 180,
            timeout_s_source: str = "parameter",
            acceptance_repeats: int = 3,
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
        acceptance=acceptance, acceptance_repeats=acceptance_repeats)
    run_uuid, plans, reps = stage["run_uuid"], stage["plans"], stage["reps"]
    sink_by_rel, manifest_by_rel = stage["sink_by_rel"], stage["manifest_by_rel"]
    vacuous_rels = stage["vacuous_rels"]
    (results, gut_ok, run_error, timed_out, wall_s, xdg_homes,
     arm_failures, arm_fail_pairs, timeout_excluded,
     all_keys) = _arm_execute(
        name, root=root, shadow_instrument=shadow_instrument,
        timeout_s=timeout_s, adaptive_reps=adaptive_reps,
        shard_log_dir=shard_log_dir, stage=stage,
        concurrency=concurrency)
    (gut_ok, run_error, baseline_meta, new_pairs, exempt_pairs,
     reconciliation, arm_valid, invalid_reason, invalid_evidence,
     timeout_excluded, suspected_draft) = _arm_gate(
        name, root=root, recipe=recipe, timeout_s=timeout_s,
        baseline_failures=baseline_failures, baseline_pairs=baseline_pairs,
        declared_scoped=declared_scoped, declared_bare=declared_bare,
        arm_fail_pairs=arm_fail_pairs, gut_ok=gut_ok,
        acceptance=acceptance, results=results, run_error=run_error,
        timed_out=timed_out)
    return _arm_result(
        name, root=root, prepared=prepared, acceptance=acceptance,
        arms_parallel=arms_parallel, evidence_dir=evidence_dir,
        shard_log_dir=shard_log_dir, timeout_s=timeout_s,
        adaptive_reps=adaptive_reps and acceptance is not None,
        test_attribution=test_attribution, results=results,
        run_uuid=run_uuid, wall_s=wall_s, gut_ok=gut_ok,
        run_error=run_error, timed_out=timed_out,
        invalid_evidence=invalid_evidence,
        arm_failures=arm_failures, arm_fail_pairs=arm_fail_pairs,
        new_pairs=new_pairs, exempt_pairs=exempt_pairs,
        baseline_meta=baseline_meta, reconciliation=reconciliation,
        arm_valid=arm_valid, invalid_reason=invalid_reason, reps=reps,
        plans=plans, sink_by_rel=sink_by_rel,
        manifest_by_rel=manifest_by_rel, xdg_homes=xdg_homes,
        vacuous_rels=vacuous_rels,
        suspected_draft=suspected_draft)
def _arm_readback(name: str, *, root: Path, prepared: dict, acceptance,
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
        if acceptance is not None and reps > 1:
            # §SPEC-062C：逐轮行集并集收敛轮数（rep 间独立进程，
            # 并集对轮序不敏感）
            from .orchestrator import _acceptance_convergence
            convergence = _acceptance_convergence(
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

def _arm_state_block(arm_valid: bool, run_error: bool, invalid_reason,
                     timed_out: bool, timeout_excluded,
                     invalid_evidence=None) -> dict:
    """§SPEC-TIMEOUT-ADAPT：state/invalid_reason/evidence/excluded 块。

    仅超时排除（partial，无其它 invalid 原因）→ 臂仍 valid，partial
    语义由 excluded_tests 披露承载；invalid_reason 不再回退 shard_timeout
    （排除已按名披露，不虚构 reason）。"""
    only_timeout = (timed_out and not run_error
                    and invalid_reason is None and timeout_excluded)
    return {
        "state": ("valid" if (arm_valid or only_timeout) else "invalid"),
        "invalid_reason": (invalid_reason
                           if invalid_reason is not None
                           else (None if only_timeout
                                 else ("run_error" if run_error else None))),
        "invalid_evidence": invalid_evidence,
        "excluded_tests": timeout_excluded or [],
    }


def _arm_doc(name: str, run_uuid: str, wall_s: float, gut_ok: bool,
             timed_out: bool, arm_valid: bool, invalid_reason, run_error: bool,
             baseline_meta, arm_failures, arm_fail_pairs, new_pairs,
             exempt_pairs, reps: int, convergence, adaptive_rounds_used,
             arms_parallel: bool, attribution, bundles: dict,
             reconciliation, plans: list, results,
             invalid_evidence: "dict | None" = None,
             timeout_excluded: "list[dict] | None" = None) -> dict:
    """结果文档组装（键序 = 结果 JSON 契约）。"""
    return {
        "arm": name, "run_uuid": run_uuid, "wall_s": round(wall_s, 3),
        "gut_ok": gut_ok, "timed_out": timed_out,
        **_arm_state_block(arm_valid, run_error, invalid_reason, timed_out,
                           timeout_excluded, invalid_evidence),
        "invalid_evidence": invalid_evidence,
        "excluded_tests": timeout_excluded or [],
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

def _arm_result(name: str, *, root: Path, prepared: dict, acceptance,
                arms_parallel: bool, evidence_dir, shard_log_dir: str | None,
                timeout_s: int, adaptive_reps: bool, test_attribution: bool,
                results, run_uuid: str, wall_s: float, gut_ok: bool,
                run_error: bool, timed_out: bool, arm_failures,
                arm_fail_pairs, new_pairs, exempt_pairs, baseline_meta,
                reconciliation, arm_valid: bool, invalid_reason, reps: int,
                plans: list, sink_by_rel: dict, manifest_by_rel: dict,
                xdg_homes: list[str], vacuous_rels: list,
                invalid_evidence: "dict | None" = None,
                timeout_excluded: "list[dict] | None" = None,
                suspected_draft: "list | None" = None) -> dict:
    """读回段：convergence/bundles/attribution（xdg 清理前读回归属）→
    vacuous 车道 → 结果文档组装（键序 = 结果 JSON 契约）。"""
    convergence, attribution, bundles = _arm_readback(
        name, root=root, prepared=prepared, acceptance=acceptance,
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
    doc = _arm_doc(
        name, run_uuid, wall_s, gut_ok, timed_out, arm_valid, invalid_reason,
        run_error, baseline_meta, arm_failures, arm_fail_pairs, new_pairs,
        exempt_pairs, reps, convergence, adaptive_rounds_used, arms_parallel,
        attribution, bundles, reconciliation, plans, results,
        invalid_evidence=invalid_evidence,
        timeout_excluded=timeout_excluded)
    if suspected_draft:
        # SEE-1623 H5：声明草稿随臂文档出报告块（空 = 键缺席，零契约噪音）
        doc["suspected_intolerance_draft"] = suspected_draft
    return doc


def _ensure_acceptance_harness(root: Path) -> Path | None:
    """§SPEC-011/D20：CI 同款 harness 幂等接线。

    项目侧契约位 `res://addons/gqt_accept/harness.gd` 通常被
    .gitignore 忽略（CI acceptance 流水线每次 cp 接线）。measure 的
    acceptance 臂此前跳过该前置 → 生成测试 `_make_harness()` 全部
    fail_test("acceptance harness unavailable")，全量面 177 红。
    契约位缺失且项目提供源时临时复制；返回本次新建路径供 finally 移除
    （生产树零残留契约）。已存在则保持不动（幂等）。
    """
    dest = root / "addons" / "gqt_accept" / "harness.gd"
    if dest.exists():
        return None
    src = root / "acceptance" / "harness" / "harness.gd"
    if not src.is_file():
        return None
    # D21：接线失败不得裸抛/留半成品——本次新建的部分产物就地回收后
    # fail-loud，由调用方把该臂降级为 invalid
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    except OSError as e:
        err = _cleanup_acceptance_harness(dest)
        raise MeasurementError(
            f"acceptance harness wiring failed: {e}"
            + (f" | {err}" if err else "")) from e
    return dest


def _cleanup_acceptance_harness(dest: Path | None) -> str | None:
    """移除本次新建的契约位；返回错误描述（None = 干净）。

    D21：清理失败不得静默——调用方必须把该臂降级为 invalid 并披露
    残留路径，禁止带残留报 valid（违背生产树零残留契约）。"""
    if dest is None:
        return None
    try:
        dest.unlink(missing_ok=True)
        if dest.parent.is_dir() and not any(dest.parent.iterdir()):
            dest.parent.rmdir()
    except OSError as e:
        return f"harness cleanup failed: {e} (residue: {dest})"
    return None


def run_acceptance_flow(*, root: Path, recipe: dict,
                        acceptance_keys: list[str] | None,
                        acceptance_shards: int,
                        arms_parallel: bool, acceptance_adaptive: bool,
                        arm_kwargs: dict) -> dict:
    """臂 acceptance：私有 suite 按分片分组编译/重放 → 并行扇出 GUT。"""
    keys = list(acceptance_keys) if acceptance_keys else [
        f["key"] for f in recipe["acceptance_face"]["features"]]
    snapshot = _snapshot_main(root)
    groups: list[dict] = []
    harness_dest: Path | None = None
    cleanup_done = False
    try:
        harness_dest = _ensure_acceptance_harness(root)
        # §SPEC-011/D20：**每 feature 独立 suite/进程**——与官方 CI
        # per-key compile+run 语义对齐。同进程连续跑多 feature 会让
        # 前序 feature 的全局状态残留进入后续 feature（实测 v5 golden
        # L2 payload 10470B > 4096B 预算；单 feature 进程绿）。
        # acceptance_shards 语义降为并发上限（进程数钳制），不再合并
        # feature 到同一 GUT 进程。
        for k in keys:
            sd = create_suite_dir(root)
            groups.append({"suite_dir": sd, "keys": [k]})
            compile_keys_to_suite(root, [k], sd)
            replay_check(root, k, suite_dir=sd)

        result = run_arm("acceptance", root=root, recipe=recipe,
                         acceptance={"groups": groups, "snapshot": snapshot},
                         shadow_instrument=arms_parallel,
                         adaptive_reps=acceptance_adaptive,
                         concurrency=max(1, int(acceptance_shards or 1)),
                         **arm_kwargs)
        # D21：成功路径显式回收并核对——清理失败 = 残留 = 不得报 valid
        cleanup_done = True
        cleanup_err = _cleanup_acceptance_harness(harness_dest)
        if cleanup_err:
            return {**result, "state": "invalid", "gut_ok": False,
                    "invalid_reason": f"harness_residue: {cleanup_err}"}
        return result
    except (SuiteError, AcceptRunError, MeasurementError) as e:
        for g in groups:
            _cleanup_suite_dir(Path(g["suite_dir"]))
        reason = f"suite_error:{e}"
        cleanup_done = True
        cleanup_err = _cleanup_acceptance_harness(harness_dest)
        if cleanup_err:
            reason += f" | {cleanup_err}"
        return {"arm": "acceptance", "run_uuid": None, "wall_s": 0.0,
                "gut_ok": False, "timed_out": False, "state": "invalid",
                "invalid_reason": reason, "bundles": 0,
                "bundles_data": {}, "reconciliation": None,
                "test_failures": {"observed": [], "observed_pairs": [],
                                  "new": [], "new_pairs": [],
                                  "instrumentation_intolerant": []},
                "repeats": 0, "convergence": None}
    finally:
        # D22/D23：兜底清理覆盖**所有退出路径**——run_arm 等未列名异常
        # 冒泡时，本次新建的 **suite 目录与 harness 契约位**一并回收
        # （异常照常传播，不吞；清理自身失败逐项写 stderr，不静默）。
        # 已显式清理的路径由 cleanup_done 跳过（成功路径 suites 由
        # run_arm 读回段清理）。
        if not cleanup_done:
            for g in groups:
                try:
                    _cleanup_suite_dir(Path(g["suite_dir"]))
                except OSError as e:
                    print(f"measure: acceptance suite cleanup failed "
                          f"({g['suite_dir']}): {e}", file=sys.stderr)
            residue_err = _cleanup_acceptance_harness(harness_dest)
            if residue_err:
                print(f"measure: acceptance {residue_err}",
                      file=sys.stderr)

def execute_arms(*, root: Path, recipe: dict, arms_parallel: bool,
                 acceptance_keys: list[str] | None,
                 acceptance_shards: int, acceptance_adaptive: bool,
                 arm_kwargs: dict) -> tuple[dict, dict]:
    """双臂执行：arms_parallel 时 acceptance 在 shadow 内插桩（根树只被
    gut 臂触碰），否则串行。"""
    if arms_parallel:
        with ThreadPoolExecutor(max_workers=2) as pex:
            fut_a = pex.submit(run_arm, "gut_full", root=root,
                               recipe=recipe, **arm_kwargs)
            fut_b = pex.submit(run_acceptance_flow, root=root,
                               recipe=recipe,
                               acceptance_keys=acceptance_keys,
                               acceptance_shards=acceptance_shards,
                               arms_parallel=arms_parallel,
                               acceptance_adaptive=acceptance_adaptive,
                               arm_kwargs=arm_kwargs)
            arm_a = fut_a.result()
            arm_b = fut_b.result()
    else:
        # 臂 gut_full：.gutconfig 面（无 -gdir——GUT 缺省读配置，lab 实测锚定）
        arm_a = run_arm("gut_full", root=root, recipe=recipe, **arm_kwargs)
        arm_b = run_acceptance_flow(
            root=root, recipe=recipe, acceptance_keys=acceptance_keys,
            acceptance_shards=acceptance_shards,
            arms_parallel=arms_parallel,
            acceptance_adaptive=acceptance_adaptive,
            arm_kwargs=arm_kwargs)
    return arm_a, arm_b
