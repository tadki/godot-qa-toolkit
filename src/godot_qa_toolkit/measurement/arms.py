"""SEE-1594 BTIDY（零行为拆分）：批式臂执行——单臂（§SPEC-052 并行分片、
XDG 隔离、shadow 插桩、门控/对账/bundle）与 gherkin 臂编排。自
orchestrator.py 的 run_measurement 内嵌闭包 `_arm` / `_gherkin_flow`
提升为模块级并按阶段分解为 ≤50 行函数；闭包变量显式参数化，逻辑逐语句
等价。"""

from __future__ import annotations

import json
import re
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
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
from ..coverage.runner import (
    _GUT_RUN_EVIDENCE,
    _looks_like_godot_launch_failure,
)
from ..coverage.union import arm_record_from_bundle
from ..gherkin.run import GherkinRunError, replay_check
from ..gherkin.addon import (AddonError, compile_preflight,
                             normalize_feature_key,
                             run_validator_preflight)
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
    partial 语义），仅有其它失败仍照旧 fail-closed。

    SEE-1623 M10：返回追加 (collected_total, executed_stream)——真实
    收集脚本数与"任一 rep 出现 GUT 测试流证据"（零执行证据门证据）。"""
    gut_ok = True
    run_error = False
    timed_out = False
    arm_failures: set[str] = set()
    arm_fail_pairs: set[tuple[str, str]] = set()
    timeout_excluded: list[dict] = []
    all_keys: list[str] = []
    collected_total = 0
    executed_stream = False
    for r in results:
        shard = _scan_shard_row(r)
        timed_out = timed_out or shard["timed_out"]
        gut_ok = gut_ok and shard["gut_ok"]
        run_error = run_error or shard["run_error"]
        arm_fail_pairs |= shard["fail_pairs"]
        arm_failures |= shard["failures"]
        all_keys += shard["keys"]
        collected_total += shard["collected"]
        executed_stream = executed_stream or shard["stream"]
        if shard["timed_out"]:
            timeout_excluded.append({"shard": shard["shard_id"],
                                     "keys": shard["keys"],
                                     "reason": "shard_timeout"})
    return _finalize_shard_scan(gut_ok, run_error, timed_out, arm_failures,
                                arm_fail_pairs, timeout_excluded, all_keys,
                                collected_total, executed_stream)


def _plan_collection_evidence(plan: dict) -> tuple:
    """SEE-1623 M10：分片计划的收集证据 → (collected, keys)。

    `collected` = arms_stage 注入的真实 test_*.gd 脚本数（可能缺省 0）；
    `keys` 保持既有语义——gut_full 为 `-gdir` 目录组 token（仅作超时
    排除粒度），gherkin 按名。"""
    keys = ([str(k) for k in (plan.get("keys") or [])]
            or [a for a in (plan.get("args") or [])
                if str(a).startswith("-gdir=")])
    return int(plan.get("collected") or 0), keys


_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def _shard_stream_evidence(r: dict) -> bool:
    """SEE-1623 M10：任一非超时 rep 输出含 GUT 测试流证据。

    GUT 在带色输出下测试流行以 ANSI SGR 前缀开头（如
    `\\x1b[0m* test_add`），`^` 锚定会失配——先剥 ANSI 再判定。"""
    return any(
        _GUT_RUN_EVIDENCE.search(
            _ANSI_SGR_RE.sub("", rep.get("out") or ""))
        for rep in (r.get("reps") or []) if not rep.get("timed_out"))


def _scan_shard_row(r: dict) -> dict:
    """§SPEC-TIMEOUT-ADAPT：单分片行扫描（keys 解析 + 逐 rep 归类）。

    SEE-1623 M10：追加真实收集证据——`collected`（分片计划中的
    test_*.gd 脚本数，来自 arms_stage 的同一发现口径）与 `stream`
    （任一非超时 rep 的输出含 GUT 测试流证据）。目录 token 不再冒充
    "非空测试键集"。"""
    plan = r.get("plan") or {}
    collected, keys = _plan_collection_evidence(plan)
    shard_id = str(plan.get("keys") or plan.get("suite_dir")
                   or ";".join(keys) or "")[:120]
    shard = _scan_shard(r, keys, shard_id)
    shard["keys"] = keys
    shard["collected"] = collected
    shard["stream"] = _shard_stream_evidence(r)
    return shard


def _finalize_shard_scan(gut_ok: bool, run_error: bool, timed_out: bool,
                         arm_failures: set, arm_fail_pairs: set,
                         timeout_excluded: "list[dict]",
                         all_keys: "list[str]",
                         collected_total: int = 0,
                         executed_stream: bool = False) -> tuple:
    """§SPEC-TIMEOUT-ADAPT：排除后无可用测试 → 整体拒绝。"""
    excluded_keys = {k for e in timeout_excluded for k in e["keys"]}
    usable = [k for k in all_keys if k not in excluded_keys]
    if timed_out and not usable:
        gut_ok = False
        run_error = True
    return (gut_ok, run_error, timed_out, arm_failures, arm_fail_pairs,
            timeout_excluded, all_keys, collected_total, executed_stream)

def _zero_evidence_gate(name: str, *, arm_valid: bool, invalid_reason,
                        invalid_evidence, gut_ok: bool, run_error: bool,
                        collected_total: int, executed_stream: bool,
                        timeout_excluded) -> tuple:
    """SEE-1623 M10：gut_full 的零执行证据门（真实管线）。

    - 收集 0 个测试脚本（tests 目录存在但零测试，F3）→ `empty_test_face`；
    - 收集 >0 但全部 rep 无 GUT 测试流（引擎 rc=0 零输出等）→
      `no_execution_evidence`（零执行证据不得判 valid 臂）。
    仅当臂当前 valid、无其它具名 invalid 原因且无超时排除时生效；
    gherkin 臂不参与（其空面语义由 gherkin 管线自理）。"""
    if (name != "gut_full" or not arm_valid or invalid_reason is not None
            or timeout_excluded):
        return arm_valid, invalid_reason, invalid_evidence, gut_ok, run_error
    if collected_total == 0:
        reason = "empty_test_face"
        note = ("selection collected 0 test scripts — no execution "
                "evidence is possible for this face; add tests or fix "
                "the tests glob/recipe")
    elif not executed_stream:
        reason = "no_execution_evidence"
        note = ("engine produced no GUT run stream for the collected "
                f"{collected_total} test script(s) (e.g. rc=0 with empty "
                "engine output) — execution cannot be attested; check the "
                "godot binary and engine stdout")
    else:
        return arm_valid, invalid_reason, invalid_evidence, gut_ok, run_error
    return (False, reason,
            {"note": note, "all_causes": [reason],
             "collected_scripts": int(collected_total)},
            False, True)


_BASELINE_TOP_N = 10  # 具名披露 top-N（与预检路径同口径）


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


def _lazy_absorption_meta(lazy_pairs: set, baseline_meta: dict) -> dict:
    """SEE-1623 M11：懒吸收与预检路径（status=partial + EXCLUDED by name）
    披露对齐（自 _lazy_baseline_merge 原样搬移）——被基线吸收的红测试具名
    在场，不再只有「only NEW failures gate」一句话而无名单。"""
    meta = {**baseline_meta, "status": "partial",
            "excluded_tests": sorted(
                f"{s}::{t}" for (s, t) in lazy_pairs)}
    named = ", ".join(sorted(
        f"{s}::{t}" for (s, t) in lazy_pairs)[:_BASELINE_TOP_N]
        or [""])
    print(f"measure: lazy baseline ABSORBED {len(lazy_pairs)} "
          f"pre-existing failing pair(s) EXCLUDED by name "
          f"(top-{min(len(lazy_pairs), _BASELINE_TOP_N)}: {named}); "
          f"baseline marked partial", file=sys.stderr)
    return meta


def _new_pairs_after_absorption(arm_fail_pairs: set, declared_scoped: set,
                                effective_baseline: set) -> set:
    """§SPEC-064a：懒基线吸收后的新增失败重算（自 _lazy_baseline_merge
    原样搬移）——归因实例被基线/声明覆盖时，其未归因回显随之吸收。"""
    covered_attributed_names = {
        t for (s, t) in arm_fail_pairs
        if s and ((s, t) in effective_baseline
                  or (s, t) in declared_scoped)}

    def _covered(p: tuple[str, str]) -> bool:
        if p in declared_scoped or p in effective_baseline:
            return True
        return p[0] == "" and p[1] in covered_attributed_names

    return {p for p in arm_fail_pairs if not _covered(p)}


def _classify_suspected_intolerance(new_pairs: set) -> set:
    """SEE-1623 H5：懒基线实测证明「未插桩通过、插桩失败」的失败 → 疑似
    插桩不耐受车道（自 _lazy_baseline_merge 原样搬移）。身份规范化复用
    M8 单一出处 canonical_failure_pairs（同一失败的双形态只保留归因形态，
    车道计数不翻倍），并随 stderr 披露 --intolerant-tests 声明草稿。"""
    from .orchestrator import canonical_failure_pairs

    suspected = canonical_failure_pairs(new_pairs)
    draft = _suspected_draft(suspected)
    print(f"measure: {len(suspected)} failing pair(s) classified "
          f"suspected_instrumentation_intolerance (pass "
          f"uninstrumented baseline, fail instrumented) — routed "
          f"out of new_failures; declaration draft for "
          f"--intolerant-tests: {json.dumps(draft, ensure_ascii=False)}",
          file=sys.stderr)
    return suspected


def _lazy_baseline_merge(name: str, root: Path, recipe: dict,
                         timeout_s: int, arm_fail_pairs: set,
                         declared_scoped: set, effective_baseline: set,
                         new_pairs: set,
                         baseline_failures,
                         baseline_meta: dict | None) -> tuple:
    """§SPEC-064a 懒获取（成本裁定）：绿臂零额外 GUT；观测到失败且未
    显式提供基线时才跑一次同面无插桩 GUT 取基线并复核。获取失败 → 具名
    stderr + 臂保持 invalid（fail-loud，不按空基线静默转绿）。
    gherkin 臂不得继承 gut_full 的懒基线（其失败保持可见）。

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
        if lazy_pairs:
            baseline_meta = _lazy_absorption_meta(lazy_pairs, baseline_meta)
        new_pairs = _new_pairs_after_absorption(
            arm_fail_pairs, declared_scoped, effective_baseline)
        print(f"measure: lazy baseline {baseline_meta['count']} "
              f"failing pairs acquired uninstrumented "
              f"({baseline_meta['wall_s']}s) — only NEW failures "
              f"gate the arm", file=sys.stderr)
        if new_pairs:
            suspected = _classify_suspected_intolerance(new_pairs)
            new_pairs = set()
    return effective_baseline, baseline_meta, new_pairs, suspected

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
                _cleanup_suite_dir(Path(r["plan"]["suite_dir"]), root)
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
    # uuid 生成，写入每个 shadow 的 addons/（根树不被本臂触碰）。
    # SEE-1631：双臂同权——归属开启时 gherkin 臂与 gut_full 同用 attr 钩子。
    flusher_gd, pre_hook_gd = _hook_templates(test_attribution, run_uuid)
    shadow_hook_files = [(_FLUSHER_RELPATH, flusher_gd),
                         (_PRE_HOOK_RELPATH, pre_hook_gd)]
    if attribution_causal:
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
        (gut_ok, run_error, timed_out, arm_failures, arm_fail_pairs,
         timeout_excluded, all_keys, collected_total,
         executed_stream) = _arm_fail_scan(results)
    finally:
        _arm_restore(root, started, backup_dir)
    return (results, gut_ok, run_error, timed_out, wall_s, xdg_homes,
            arm_failures, arm_fail_pairs, timeout_excluded, all_keys,
            collected_total, executed_stream)
def _scan_shard_problems(results, timeout_s: int, timeout_source: str):
    """§SPEC-PLANFIRST：分片级问题扫描——(launch, timeouts, unattributed)
    三类明细（超时分片带超时值与来源；不可归因分片带 rc 与输出尾部）。"""
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
              declared_bare, arm_fail_pairs, gut_ok, gherkin,
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
     run_error) = _arm_reconcile(root, gherkin, results, run_error)
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
            gherkin: dict | None = None, shadow_instrument: bool = False,
            adaptive_reps: bool = False, test_attribution: bool = False,
            attribution_causal: bool = False, shard_width: int = 6,
            shard_log_dir: str | None = None, timeout_s: int = 180,
            timeout_s_source: str = "parameter",
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
     arm_failures, arm_fail_pairs, timeout_excluded,
     all_keys, collected_total, executed_stream) = _arm_execute(
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
        gherkin=gherkin, results=results, run_error=run_error,
        timed_out=timed_out)
    # SEE-1623 M10：零执行证据门（真实管线收集计数 + 测试流证据）。
    (arm_valid, invalid_reason, invalid_evidence, gut_ok,
     run_error) = _zero_evidence_gate(
        name, arm_valid=arm_valid, invalid_reason=invalid_reason,
        invalid_evidence=invalid_evidence, gut_ok=gut_ok,
        run_error=run_error, collected_total=collected_total,
        executed_stream=executed_stream,
        timeout_excluded=timeout_excluded)
    doc = _arm_result(
        name, root=root, prepared=prepared, gherkin=gherkin,
        arms_parallel=arms_parallel, evidence_dir=evidence_dir,
        shard_log_dir=shard_log_dir, timeout_s=timeout_s,
        adaptive_reps=adaptive_reps and gherkin is not None,
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
    if name == "gut_full":
        # SEE-1623 M10：真实收集执行面作为臂证据披露（机器可审计）。
        doc["collected_scripts"] = int(collected_total)
        doc["execution_stream"] = bool(executed_stream)
    return doc
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
        # §SPEC-083：归属证据与注册表必须在 xdg 清理前读回（归属臂）。
        # SEE-1631：双臂同权收集；gherkin 臂按编译期 manifest 把生成脚本
        # 身份改写为稳定 feature/scenario 身份。
        identity_rewrite = None
        if name == "gherkin" and gherkin is not None:
            from .attribution import _gherkin_identity_rewrite
            identity_rewrite = _gherkin_identity_rewrite(
                root, gherkin.get("groups") or [],
                gherkin.get("identities") or {})
        attribution = _collect_attribution(
            test_attribution, root, run_uuid, name,
            sink_by_rel, xdg_homes, prepared=prepared,
            identity_rewrite=identity_rewrite)
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

def _arm_result(name: str, *, root: Path, prepared: dict, gherkin,
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
    组内多 key 共享 suite/进程）。keys <= shards 时退化为单 key 组。

    §SPEC-019 HIGH-4：key 一律归一（与编译产物/manifest 同口径）——
    连字符 stem 分组后仍与生成物键一致，对账不崩。"""
    ks = sorted({normalize_feature_key(k) for k in keys})
    n = max(1, int(shards or 1))
    n = min(n, len(ks)) if ks else 1
    groups: list[list[str]] = [[] for _ in range(n)]
    for i, k in enumerate(ks):
        groups[i % n].append(k)
    return [g for g in groups if g]


def _gherkin_error_result(e: Exception, groups: list[dict],
                          root: Path) -> dict:
    """异常路径：回收已建 suite 并组装 invalid 结果。
    §SPEC-025：清理失败 = 残留 = 具名 suite_residue，不得静默。"""
    residue: list[str] = []
    for g in groups:
        try:
            _cleanup_suite_dir(Path(g["suite_dir"]), root)
        except OSError as ce:
            residue.append(f"{g['suite_dir']}: {ce}")
    reason = f"suite_error:{e}"
    if residue:
        reason += f" | suite_residue: {'; '.join(residue)}"
    return {"arm": "gherkin", "run_uuid": None, "wall_s": 0.0,
            "gut_ok": False, "timed_out": False, "state": "invalid",
            "invalid_reason": reason, "process_fallbacks": [],
            "bundles": 0,
            "bundles_data": {}, "reconciliation": None,
            "test_failures": {"observed": [], "observed_pairs": [],
                              "new": [], "new_pairs": [],
                              "instrumentation_intolerant": []},
            "repeats": 0, "convergence": None}


_GHERKIN_INTOL_REASON = (
    "uninstrumented comparison green while attribution-instrumented red "
    "(gherkin arm, SEE-1631 §SPEC-017 suspected instrumentation "
    "intolerance)")


def _script_key(script: str) -> "str | None":
    m = re.search(r"test_(.+)\.gd$", script.replace("\\", "/"))
    return m.group(1) if m else None


def _observed_failed_keys(result: dict, groups: list[dict]) -> list[str]:
    """共享轮观测红 key 集（§SPEC-017 对照腿的对象：只看红的 feature）。"""
    keys = {k for pair in (result.get("test_failures") or {}).get(
        "observed_pairs", []) if (k := _script_key(pair.split("::", 1)[0]))}
    return sorted(k for k in keys
                  if any(k in g["keys"] for g in groups))


def _compile_into(sd: Path, root: Path, chunk: list[str]) -> dict:
    """私有 suite 组编译 + 逐 key 重放校验（目录即选择集）。SEE-1631：
    返回 {feature_key: manifest scenario 归属条目}——suite 目录在读回段
    前被清理，feature/scenario 身份必须在编译期捕获。"""
    out = compile_keys_to_suite(root, chunk, sd)
    for k in chunk:
        replay_check(root, k, suite_dir=sd)
    return {key: (out["manifests"].get(key) or {}).get("attribution") or []
            for key in chunk}


def _finish_gherkin_result(result: dict) -> dict:
    """成功路径收口：process_fallbacks 键保留恒空（§SPEC-017——判断
    "同进程污染"的带归属插桩单例重跑已退役，feature 已全部原子化）。"""
    result["process_fallbacks"] = []
    return result


def _gherkin_uninstrumented_comparison(root: Path, failed_keys: list[str],
                                       timeout_s: int) -> dict:
    """§SPEC-017：红 feature 的未插桩对照（零探针，非整脸）。

    干净树（归属臂 finally 已还原）逐 key 编译私有 suite 并无钩子跑 GUT：
    - 对照绿（无可解析失败对）→ suspected instrumentation intolerance；
    - 对照红（有解析失败对）→ 既有失败；
    - rc!=0 且无可解析失败对 → 取不到证据（臂保持 invalid，不静默转绿）。
    返回 {"green"/"red"/"no_evidence": set, "executions": [...]}。
    编译/预检失败（SuiteError/AddonError/GherkinRunError）→ 全员
    no_evidence（fail-loud 不静默）。"""
    from .orchestrator import _parse_failing_pairs
    executions: list[dict] = []
    green: set[str] = set()
    red: set[str] = set()
    no_evidence: set[str] = set()
    preflight_error: "str | None" = None
    try:
        compile_preflight(root)
    except (AddonError, GherkinRunError) as e:
        preflight_error = str(e)
    for key in failed_keys:
        if preflight_error is not None:
            no_evidence.add(key)
            executions.append({"key": key, "rc": None,
                               "error": f"preflight: {preflight_error}"})
            continue
        suite_dir = root / "gherkin" / "generated" / (
            f".gqt-run-cmp-{uuid.uuid4().hex[:12]}")
        try:
            suite_dir.mkdir(parents=True)
            compile_keys_to_suite(root, [key], suite_dir)
            replay_check(root, key, suite_dir=suite_dir)
            gen_res = ("res://" + suite_dir.joinpath("generated")
                       .relative_to(root).as_posix())
            import tempfile as _tempfile
            xdg = _tempfile.mkdtemp(prefix="gqt-gh-cmp-")
            argv = [f"-gdir={gen_res}"]   # 零探针：无 -gpre_run_script
            from .orchestrator import _run_one_gut
            env = dict(os.environ)
            env["XDG_DATA_HOME"] = xdg
            r, _w = _run_one_gut(root, argv, timeout_s, env=env,
                                 gate_root=root)
            out = (r.stdout or "") + "\n" + (r.stderr or "")
            pairs = _parse_failing_pairs(out)
            executions.append({"key": key, "rc": r.returncode,
                               "pairs": len(pairs), "error": None})
            if r.returncode == 0:
                green.add(key)
            elif pairs:
                red.add(key)
            else:
                no_evidence.add(key)
        except (SuiteError, AddonError, GherkinRunError, OSError) as e:
            no_evidence.add(key)
            executions.append({"key": key, "rc": None, "error": str(e)})
        finally:
            try:
                _cleanup_suite_dir(suite_dir, root)
            except OSError:
                pass
    return {"green": green, "red": red, "no_evidence": no_evidence,
            "executions": executions}


def _gherkin_name_to_key(tf: dict) -> dict:
    """name → key 映射：Run Summary 段的未归因对（空脚本段）按同名归因对
    解析 feature key（归因对在 observed_pairs 中恒在场）。"""
    name_to_key: dict[str, str] = {}
    for pair in tf.get("observed_pairs") or []:
        script, name = pair.split("::", 1)
        k = _script_key(script)
        if k:
            name_to_key.setdefault(name, k)
    return name_to_key


def _route_gherkin_new_pairs(tf: dict, verdict: dict,
                             name_to_key: dict) -> "tuple[list, list, list, list]":
    """new_pairs 按对照结论三路路由（车道形态与 gut_full 一致——sorted
    "s::t" 对字符串，报告聚合块按 set(pairs) 消费；dict 声明草稿走
    suspected_intolerance_draft）。返回 (intolerant, pre_existing,
    kept_new_pairs, draft)。"""
    intolerant = list(tf.get("instrumentation_intolerant") or [])
    pre_existing: list[str] = []
    kept_new_pairs: list[str] = []
    draft: list[dict] = []
    for pair in tf.get("new_pairs") or []:
        script, name = pair.split("::", 1)
        k = _script_key(script) or name_to_key.get(name)
        if k in verdict["green"]:
            intolerant.append(pair)
            draft.append({"script": script, "name": name,
                          "reason": _GHERKIN_INTOL_REASON})
        elif k in verdict["red"]:
            pre_existing.append(pair)
        else:
            kept_new_pairs.append(pair)
    return intolerant, pre_existing, kept_new_pairs, draft


def _apply_gherkin_comparison(result: dict, root: Path,
                              failed_keys: list[str], timeout_s: int) -> None:
    """§SPEC-017：对照结果路由 + 臂状态按真实残集重算（H5 同语义）。

    - 对照绿：失败对迁入具名 `instrumentation_intolerant` 车道 + 声明
      草稿（stderr 披露），退出 new_failures 门禁——臂保持 valid；
    - 对照红：既有失败吸收（`pre_existing_failures` 具名），臂保持
      valid；
    - 取不到证据：失败对保持 new_failures——臂保持 invalid（fail-loud）。
    对账未执行的事实如实标注（executed=False），不虚构对账失败。"""
    verdict = _gherkin_uninstrumented_comparison(root, failed_keys,
                                                 timeout_s)
    result["gherkin_comparison"] = {
        "green": sorted(verdict["green"]), "red": sorted(verdict["red"]),
        "no_evidence": sorted(verdict["no_evidence"]),
        "executions": verdict["executions"]}
    tf = result.get("test_failures") or {}
    name_to_key = _gherkin_name_to_key(tf)
    intolerant, pre_existing, kept_new_pairs, draft = \
        _route_gherkin_new_pairs(tf, verdict, name_to_key)
    tf["instrumentation_intolerant"] = intolerant
    tf["pre_existing_failures"] = pre_existing
    tf["new_pairs"] = kept_new_pairs
    tf["new"] = sorted(p.split("::", 1)[1] for p in kept_new_pairs)
    result["test_failures"] = tf
    kept_new_pairs_set = {tuple(p.split("::", 1)) for p in kept_new_pairs}
    # C3-R2 口径：顶层失败对以 sorted "s::t" 字符串列表承载（JSON 面）
    kept_pairs = sorted(f"{s}::{t}" for (s, t) in kept_new_pairs_set)
    result["arm_fail_pairs"] = kept_pairs
    result["arm_failures"] = sorted({t for (_s, t) in kept_new_pairs_set})
    result["new_pairs"] = kept_pairs
    # 声明草稿 stderr 披露（与 gut_full 懒基线通道同形）
    if draft:
        print(f"measure: gherkin suspected instrumentation intolerance — "
              f"{len(draft)} pair(s) (uninstrumented green, instrumented "
              f"red); declaration draft for --intolerant-tests: "
              f"{json.dumps(draft, ensure_ascii=False)}", file=sys.stderr)
    if not kept_new_pairs:
        # 全部红有对照结论 → 臂保持 valid（吸收/车道），对账未执行如实标注
        result["state"] = "valid"
        result["invalid_reason"] = None
        result["invalid_evidence"] = None
        result["gut_ok"] = True
    else:
        result["state"] = "invalid"
        result["invalid_reason"] = "new_failures"
    result["reconciliation"] = {"ok": None, "executed": False,
                                "skipped_reason": ("new_failures_route"),
                                "shards": []}
    result["suspected_intolerance_draft"] = draft


def run_gherkin_flow(*, root: Path, recipe: dict,
                     gherkin_keys: list[str] | None,
                     gherkin_shards: int,
                     arms_parallel: bool, gherkin_adaptive: bool,
                     arm_kwargs: dict) -> dict:
    """臂 gherkin：纯解析预检 → 私有 suite 按分片分组编译/重放 →
    validator 拉起一次（§SPEC-003）→ 并行扇出 GUT。

    SEE-1624 §SPEC-016 次序：compile/replay 的纯域检查（零 Godot）先行，
    validator（需要 Godot 二进制）在业务扇出之前——缺二进制不得抢占
    域错误，仍保持"业务执行前恰好一次预检"。"""
    keys = list(gherkin_keys) if gherkin_keys else [
        f["key"] for f in recipe["gherkin_face"]["features"]]
    snapshot = _snapshot_main(root)
    groups: list[dict] = []
    cleanup_done = False
    try:
        try:
            compile_preflight(root)
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
        identities: dict[str, list] = {}
        for chunk in _gherkin_groups(keys, gherkin_shards):
            sd = create_suite_dir(root)
            groups.append({"suite_dir": sd, "keys": list(chunk)})
            identities.update(_compile_into(sd, root, chunk))

        # 编译/重放（纯域）全过之后、业务扇出之前：validator 恰好一次。
        try:
            run_validator_preflight(root)
        except AddonError as e:
            raise GherkinRunError(f"gherkin preflight failed: {e}") from e

        result = run_arm("gherkin", root=root, recipe=recipe,
                         gherkin={"groups": groups, "snapshot": snapshot,
                                  "identities": identities},
                         shadow_instrument=arms_parallel,
                         adaptive_reps=gherkin_adaptive,
                         concurrency=max(1, int(gherkin_shards or 1)),
                         **arm_kwargs)

        # SEE-1631 §SPEC-017（Owner 裁决）：归属插桩单例重跑退役——红了
        # 只对红的 feature 做未插桩对照（零探针、非整脸）：对照绿 = 疑似
        # 插桩不耐受（具名车道 + 声明草稿，臂保持 valid，与 gut_full H5
        # 同语义）；对照红 = 既有失败（吸收，具名）；取不到证据 = 臂保持
        # invalid（不静默转绿）。"是否同进程污染"的重跑不再存在
        # （feature 已全部原子化）；process_fallbacks 键保留恒空。
        failed = _observed_failed_keys(result, groups)
        if failed:
            _apply_gherkin_comparison(
                result, root, failed,
                timeout_s=int(arm_kwargs.get("timeout_s") or 180))

        cleanup_done = True
        return _finish_gherkin_result(result)
    except (SuiteError, GherkinRunError, MeasurementError, AddonError) as e:
        # §SPEC-025：异常路径必须连 fallback singleton suite 一并回收
        # （旧实现只清 shared groups，cleanup_done=True 跳过 finally，
        # 第二个 singleton 编译失败时前一个 singleton 目录泄漏）。
        # SEE-1631 §SPEC-007：AddonError（缺根/空 features/未知 key 在
        # 编译期被发现）同样具名转 invalid——不得裸 traceback 逃逸。
        result = _gherkin_error_result(e, groups, root)
        cleanup_done = True
        return result
    finally:
        # D22/D23：兜底清理覆盖**所有退出路径**——run_arm 等未列名异常
        # 冒泡时，本次新建的 suite 目录一并回收（异常照常传播，不吞；
        # 清理自身失败逐项写 stderr，不静默）。已显式清理的路径由
        # cleanup_done 跳过（成功路径 suites 由 run_arm 读回段清理）。
        if not cleanup_done:
            for g in groups:
                try:
                    _cleanup_suite_dir(Path(g["suite_dir"]), root)
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
