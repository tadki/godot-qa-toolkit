"""gqt measure run 双源批式编排（§SPEC-037，measurement-report/2）。

批式臂（measurement 的唯一可行形态，设计 §2.1）：目标全集一次插桩 → 每臂
1 次 GUT（gut_full 走 .gutconfig 面 + acceptance 私有目录一次）→ 逐文件
sink（`qa-coverage-hits-{run_uuid}-{file_key8}`）读回 → bundle →
coverage-union/1 逐文件行集并集。两臂共用同一 registry/probe manifest
（同源码、同 manifest），run UUID 每臂独立。

在树插桩：备份目录 per-uid 私有 /tmp/gqt-canonical-backups-<uid>/<run_uuid>/
（started-record + 逐文件原文；0700、ownership 校验、拒 symlink），finally
恢复 + 清理；启动时回收同项目陈旧备份（崩溃恢复，恢复前不信任任何 sink；
恢复写入经 pinned-root 逐组件 O_NOFOLLOW dirfd 走廊限定 project root 内）。
KOL 生产树零残留由「备份+恢复+对账后清理」保证。

certified 边界（§2.4）：一律 evidence_level="exploratory"、
certification.status="not_certified"——单轮 exploratory，WP5 拥有双轮认证。
"""

from __future__ import annotations
from ..digest import sha256_text

import hashlib
import json
import math
import os
import re
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from ..gherkin.run import AcceptRunError, replay_check
from ..gherkin.suite import (
    SuiteError,
    _cleanup_suite_dir,
    _snapshot_main,
    compile_keys_to_suite,
    create_suite_dir,
    reconcile,
)
from ..coverage.evidence import (
    build_bundle,
    build_run_record,
    clear_sink,
    evidence_dir_for,
    make_run_uuid,
    update_run_record,
    write_bundle,
    write_run_record,
)
from ..coverage.runner import (
    _instrument,
    _instrument_blocks,
    _looks_like_godot_launch_failure,
    _read_hits,
    _rewrite_call_sites,
)
from ..coverage.union import arm_record_from_bundle, union_file
from ..mutation.godot_gate import GlobalGodotGate
from ..mutation.inject import BinaryConfigError, godot_command, spawn_godot
from .recipe import verify_recipe
from .shadowio import (
    _run_one_gut,
    _shard_groups,
    _prepare_shadow,
    _cleanup_shadow,
    _read_hits_merged,
    _read_attr_merged,
    _read_registries,
)
from .backup import (
    MeasurementError,
    file_key8,
    _rel_within_root,
    batch_sink_name,
    _backup_base,
    _recover_stale_backups,
)
from .probe_templates import (  # noqa: F401 — 模板常量属插桩面，orchestrator 流程仍消费
    _AUTOLOAD_NAME,
    _FLUSHER_GD,
    _FLUSHER_RELPATH,
    _PRE_HOOK_RELPATH,
    _ATTR_CTX_RELPATH,
    _ATTR_CTX_GD,
    _patch_autoload,
)

from .crapbuild import (
    _SEM_STATEMENT,
    _cause_tail,
    _pairs_sha256,
    _attach_limitation_reasons,
    _crap_records_for_report,
    _load_baseline_report,
    _unstable_keys,
    _FAIL_LANES,
)
from .instrument import (
    _algo_used,
    _hook_templates,
    _inject_toolchain,
    _prepare_target_file,
)
from .arms import execute_arms
from .attribution import (
    ATTRIBUTION_ALGO_TAG,
    _collect_attribution,
    _attribution_section,
    _tests_face_map,
)
REPORT_VERSION = "measurement-report/2"
# SEE-1587：行级主真值 schema——行事实（per-test covered line sets）是唯一
# 主数据，file/function 视图退为派生投影；旧 /5 产物 fail-loud 拒绝（无
# 兼容别名，消费方 load 即报 schema 不符）。
CERTIFICATION = {
    "status": "not_certified",
    "reason": "single round; WP5 owns two-round certification",
}

def _parse_baseline_failures(baseline_failures) -> "set[tuple[str, str]]":
    """§SPEC-064b：基线失败集升级为 (script, test) 身份对——同名跨脚本不
    互相吸收、未归因仅匹配未归因；裸名字符串身份歧义 → fail-closed 拒绝。"""
    baseline_pairs: set[tuple[str, str]] = set()
    for entry in (baseline_failures or ()):
        if (isinstance(entry, tuple) and len(entry) == 2
                and all(isinstance(x, str) for x in entry)):
            baseline_pairs.add((entry[0], entry[1]))
        else:
            raise MeasurementError(
                f"baseline failure entry must be a (script, test) pair, "
                f"got {entry!r} — bare names are identity-ambiguous and "
                f"rejected (§SPEC-064b)")
    return baseline_pairs

def _req_str(obj: dict, key: str, kind: str) -> str:
    """§SPEC-065：配置字段统一类型校验——非 str 或空白即 MeasurementError
    fail-fast（非 str 会在匹配/附着层失配静默失效，如 file=123）。"""
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        raise MeasurementError(
            f"{kind} field {key!r} must be a non-empty string, "
            f"got {v!r} (§SPEC-065)")
    return v

def _parse_intolerant_config(cfg: dict) -> tuple:
    """§SPEC-065（R19）：intolerant 声明解析与统一校验——顶层键显式
    null / 非 list 与声明字段类型错同治（"键缺省" 与 "键值为 null" 是
    两种语义，null 不得静默降级为缺省）。返回 (intolerant_tests,
    declared_scoped, declared_bare, coverage_limitations)。"""
    tests_decl = cfg.get("tests", [])
    if not isinstance(tests_decl, list):
        raise MeasurementError(
            "intolerant 'tests' must be a list, "
            f"got {tests_decl!r} (§SPEC-065)")
    limits_decl = cfg.get("coverage_limitations", [])
    if not isinstance(limits_decl, list):
        raise MeasurementError(
            "intolerant 'coverage_limitations' must be a list, "
            f"got {limits_decl!r} (§SPEC-065)")
    # §SPEC-063e/065：声明 name/reason 统一非空 str 校验——
    # 空论证旁路 = 静默排除的捷径，禁止
    intolerant_tests = []
    for t in tests_decl:
        if not isinstance(t, dict):
            raise MeasurementError(
                "intolerant declaration must be an object, "
                f"got {t!r} (§SPEC-065)")
        entry = {"name": _req_str(t, "name", "intolerant declaration"),
                 "reason": _req_str(t, "reason", "intolerant declaration")}
        # §SPEC-065（R19）：script 键存在性语义——键缺省 = 合法 bare-name
        # 声明；键一旦提供（含显式 null）必须通过非空 str 校验，不得静默
        # 降级改变豁免身份范围
        if "script" in t:
            entry["script"] = _req_str(t, "script",
                                       "intolerant declaration")
        intolerant_tests.append(entry)
    # §SPEC-063c：排除声明按 (script, test) 身份匹配——无 script 声明仅
    # 豁免未归因失败（Run Summary 截断形态），同名跨脚本不合并豁免
    declared_scoped = {(t["script"], t["name"]) for t in intolerant_tests
                       if t.get("script")}
    declared_bare = {t["name"] for t in intolerant_tests if not t.get("script")}
    # §SPEC-064c/065：coverage_limitations file/reason 统一非空 str 校验
    # （盲点披露不得无提示消失或静默失配）
    coverage_limitations = []
    for l in limits_decl:
        if not isinstance(l, dict):
            raise MeasurementError(
                "coverage limitation must be an object, "
                f"got {l!r} (§SPEC-065)")
        coverage_limitations.append(
            {"file": _req_str(l, "file", "coverage limitation"),
             "reason": _req_str(l, "reason", "coverage limitation")})
    return intolerant_tests, declared_scoped, declared_bare, \
        coverage_limitations

def _prepare_targets(root: Path, target_files: list[str], *, recipe_hash: str,
                     base_algo: str, algo_used: str, block_compress: bool,
                     test_attribution: bool,
                     attribution_causal: bool) -> dict:
    """目标全集：registry + probe manifest + 插桩模板由 _prepare_target_file
    逐文件准备（§SPEC-030 单次 parse 纪律 / §SPEC-054 静态缓存 /
    方向②③ 两臂分离模板）；parse 失败逐文件隔离进 unmeasured。"""
    prepared: dict[str, dict] = {}
    unmeasured: list[dict] = []
    cache_hits = 0
    cache_misses = 0
    causal_sites: list[dict] = []
    for path_str in target_files:
        p = Path(path_str)
        if not p.is_file():
            unmeasured.append({"file": path_str, "reason": "target_missing"})
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            unmeasured.append({"file": path_str,
                               "reason": f"read_error:{e}"})
            continue
        rel = _rel_within_root(str(p.resolve()), root)
        outcome = _prepare_target_file(
            root, rel, src, str(p.resolve()), path_str,
            recipe_hash=recipe_hash, base_algo=base_algo, algo_used=algo_used,
            block_compress=block_compress, test_attribution=test_attribution,
            attribution_causal=attribution_causal)
        if outcome["unmeasured"] is not None:
            unmeasured.append(outcome["unmeasured"])
        if outcome["prepared"] is None:
            continue
        prepared[rel] = outcome["prepared"]
        cache_hits += outcome["cache_hits"]
        cache_misses += outcome["cache_misses"]
        if outcome["causal_site"] is not None:
            causal_sites.append(outcome["causal_site"])
    return {"prepared": prepared, "unmeasured": unmeasured,
            "cache_hits": cache_hits, "cache_misses": cache_misses,
            "causal_sites": causal_sites}

def _scope_complete(scope_roots: list[str] | None,
                    target_files: list[str]) -> bool:
    """§SPEC-053：subset 不得标 comparable——目标集未覆盖 scope_roots 下
    全部 .gd 时，union/评分的 comparable 一律降为 false 并给原因。"""
    if not scope_roots:
        return True
    scope_files = {str(x) for sr in scope_roots
                   for x in Path(sr).rglob("*.gd") if x.is_file()}
    return scope_files <= {str(Path(tf)) for tf in target_files}

def _build_unions(arm_a: dict, arm_b: dict, recipe_hash: str) -> list:
    """逐文件 union（coverage-union/1，禁平均）。"""
    unions = []
    for rel in sorted(set(arm_a["bundles_data"])
                      & set(arm_b["bundles_data"])):
        a = arm_record_from_bundle(arm_a["bundles_data"][rel],
                                   recipe_hash=recipe_hash,
                                   wall_s=arm_a["wall_s"])
        b = arm_record_from_bundle(arm_b["bundles_data"][rel],
                                   recipe_hash=recipe_hash,
                                   wall_s=arm_b["wall_s"])
        unions.append(union_file(a, b, canonical_recipe_hash=recipe_hash))
    return unions

def _gate_comparable(unions: list, arm_a: dict, arm_b: dict,
                     scope_complete: bool) -> tuple:
    """§SPEC-060a/053：任一臂出现相对基线的新增失败 / subset 目标 →
    comparable=false 并具名原因。返回 (gated_arms, comparable_reason)。"""
    comparable_reason = None
    gated_arms = {
        arm["arm"]: arm.get("test_failures") or {"observed": [], "new": []}
        for arm in (arm_a, arm_b)}
    if any(v["new"] for v in gated_arms.values()):
        comparable_reason = "new_test_failures"
        for u in unions:
            u["comparable"] = False
            u["comparable_reason"] = "new_test_failures"
    if not scope_complete and comparable_reason is None:
        comparable_reason = "subset_targets"
        for u in unions:
            u["comparable"] = False
            u["comparable_reason"] = "subset_targets"
    return gated_arms, comparable_reason

def _measurable_stats(unions: list) -> tuple:
    """§SPEC-043 measurable 子集披露：vacuous / no_probe_fired 文件归
    unmeasurable（本就无可测数据），其余 union 无效仍属 measurable 失败
    （超时/契约违约等），禁止静默排除——measurable + unmeasurable == 全集。"""
    unmeasurable: list[dict] = []
    measurable_files = 0
    measurable_valid = 0
    for u in unions:
        if u["valid"]:
            measurable_files += 1
            measurable_valid += 1
            continue
        reason = u.get("invalid_reason") or ""
        if "no_probe_fired" in reason or "vacuous" in reason:
            unmeasurable.append({"file": u["file"], "reason": reason})
        else:
            measurable_files += 1
    return unmeasurable, measurable_files, measurable_valid

def _report_failures_block(gated_arms: dict, arm_a: dict) -> dict:
    """§SPEC-060a：双臂失败集逐名披露（observed 全集 + new 相对基线）；
    baseline_count = gut_full 臂生效基线（显式提供或懒获取）。"""
    return {
        "baseline_count": ((arm_a.get("baseline_failures_meta") or {})
                           .get("count") or 0),
        "gated": any(v["new"] for v in gated_arms.values()),
        "arms": gated_arms,
    }

def _report_intolerant_block(intolerant_tests: list, arm_a: dict,
                             arm_b: dict) -> dict:
    """§SPEC-061b/063c：instrumentation-intolerant 具名排除披露——按
    (script, test) 身份匹配旁路门控，逐名给理由，并作为覆盖率盲点
    声明（非静默跳过；同名跨脚本不合并豁免）。"""
    return {
        "declared": [{"name": t["name"],
                      "reason": str(t.get("reason") or ""),
                      **({"script": str(t["script"])}
                         if t.get("script") else {})}
                     for t in intolerant_tests],
        "observed": sorted(
            set((arm_a.get("test_failures") or {})
                .get("instrumentation_intolerant") or [])
            | set((arm_b.get("test_failures") or {})
                  .get("instrumentation_intolerant") or [])),
        "coverage_blind_spots": [t["name"] for t in intolerant_tests],
        "note": ("declared tests bypass the failure-gating comparison "
                 "only by (script, test) identity — a declaration "
                 "without script matches only unattributed failures, "
                 "and same-named tests in undeclared scripts are NOT "
                 "exempted; they cannot attest behavior under "
                 "instrumentation, so they are disclosed here and as "
                 "coverage blind spots — not silently skipped"),
    }

def _report_union_block(unions: list, comparable_reason: str | None,
                        scope_complete: bool, unmeasurable: list,
                        measurable_files: int,
                        measurable_valid: int) -> dict:
    """报告 union 块（valid/可测子集/不可测披露）。"""
    return {
        "valid": bool(unions) and all(u["valid"] for u in unions),
        "files": len(unions),
        "valid_files": sum(1 for u in unions if u["valid"]),
        "method": "line_union",
        "measurable": {
            "files": measurable_files,
            "valid": measurable_valid,
            "all_valid": (bool(measurable_files)
                          and measurable_valid == measurable_files),
        },
        "unmeasurable": unmeasurable,
    }

def _report_crap_block(crap_records: list, crap_lanes: dict,
                       crap_version: str, scope_complete: bool,
                       comparable_reason: str | None,
                       stability_baseline: str | None) -> dict:
    """报告 crap 块。§SPEC-070 FR-4/R26 + §SPEC-071 L2：顶层语义从
    **最终 records** 实测统计（含 ghost 回放对账后的形态），record/
    crap_line/counts 三处一致；两形态并存 → mixed。"""
    return {
        "version": crap_version,
        "evidence_level": "exploratory",
        "coverage_semantics_counts": {
            s: sum(1 for r in crap_records
                   if r["coverage_semantics"] == s)
            for s in {r["coverage_semantics"]
                      for r in crap_records}},
        "coverage_semantics": (
            "mixed" if len({r["coverage_semantics"]
                            for r in crap_records}) > 1
            else (crap_records[0]["coverage_semantics"]
                  if crap_records else _SEM_STATEMENT)),
        "path_coverage": "not_measured",
        "assertion_effectiveness": "not_measured",
        "method": "coverage-union/1 + func-registry",
        "records": crap_records,
        "summary": {
            "functions": len(crap_records),
            "scored": sum(1 for r in crap_records
                          if r["score_status"] == "scored"),
            "lanes": crap_lanes,
        },
        "comparable": scope_complete and comparable_reason is None,
        "comparable_reason": comparable_reason,
        "stability": {
            "baseline": stability_baseline,
            # §SPEC-060g：按函数实数计数（键集含 __file_vanished__
            # 哨兵，直接 len 会把整文件消失算成 1 个函数）
            "unstable_functions": sum(
                1 for r in crap_records
                if r.get("score_status") == "unstable"),
        },
    }

def _report_coverage_semantics_block(algo_used: str, prepared: dict) -> dict:
    """§SPEC-070 FR-4：块压缩近似语义显式披露——不得冒充逐语句精确
    statement_start_line（块内后继语句被运行期错误中止时仍计入覆盖）。"""
    return {
        "algo": algo_used,
        "block_compressed_files": sum(
            1 for item in prepared.values() if item.get("block_map")),
        "statement_files": sum(
            1 for item in prepared.values() if not item.get("block_map")),
        "semantics": (
            "block-entry hit expands to all statement start lines in "
            "the block (line-coverage approximation, not per-statement "
            "exact measurement); a runtime error aborting mid-block can "
            "mark later statements covered"),
    }

def _assemble_report(root: Path, recipe_hash: str, arm_a: dict, arm_b: dict,
                     unions: list, gated_arms: dict,
                     comparable_reason: str | None, scope_complete: bool,
                     crap_records: list, crap_lanes: dict,
                     crap_version: str, stability_baseline: str | None,
                     intolerant_tests: list, coverage_limitations: list,
                     unmeasurable: list, measurable_files: int,
                     measurable_valid: int, cache_hits: int,
                     cache_misses: int, algo_used: str, prepared: dict,
                     unmeasured: list, recovered: int, perf: dict) -> dict:
    """measurement-report/2 正文组装（块构建委托给 _report_*_block）。"""
    return {
        "report_version": REPORT_VERSION,
        "project_root": str(root),
        "recipe_hash": recipe_hash,
        "arms": [
            {k: v for k, v in arm.items()
             if k not in ("bundles_data", "attribution")}
            for arm in (arm_a, arm_b)
        ],
        "unions": unions,
        "failures": _report_failures_block(gated_arms, arm_a),
        "instrumentation_intolerant": _report_intolerant_block(
            intolerant_tests, arm_a, arm_b),
        # §SPEC-061c：工具无法确定性测量的文件（如插桩减速下晚挂载 UI）
        # 声明披露；对应 unstable 记录附具名理由
        "coverage_limitations": [
            {"file": l["file"], "reason": str(l["reason"])}
            for l in coverage_limitations],
        "union": _report_union_block(
            unions, comparable_reason, scope_complete, unmeasurable,
            measurable_files, measurable_valid),
        "crap": _report_crap_block(
            crap_records, crap_lanes, crap_version, scope_complete,
            comparable_reason, stability_baseline),
        "preparation": {
            "cache": {"hits": cache_hits, "misses": cache_misses,
                      "algo": algo_used},
        },
        "coverage_semantics": _report_coverage_semantics_block(
            algo_used, prepared),
        "unmeasured": unmeasured,
        "recovered_stale_backups": recovered,
        "performance": perf,
        "certification": dict(CERTIFICATION),
        "evidence_level": "exploratory",
        "diagnostic_only": True,
        "dynamic_cache": False,
        "ok": bool(unions) and all(u["valid"] for u in unions),
    }

def _measure_inputs(project_root, target_files, recipe, baseline_failures,
                    intolerant_config, inject_toolchain) -> dict:
    """输入校验与就绪态：baseline pairs / intolerant 解析 / recipe 校验 +
    pristine hash / 注入。"""
    t_command = time.monotonic()
    root = Path(project_root).resolve()
    baseline_pairs = _parse_baseline_failures(baseline_failures)
    (intolerant_tests, declared_scoped, declared_bare,
     coverage_limitations) = _parse_intolerant_config(intolerant_config or {})
    ok, reason = verify_recipe(recipe)
    if not ok:
        raise MeasurementError(f"recipe invalid: {reason}")
    if not target_files:
        raise MeasurementError("no target files given")
    from .recipe import recipe_hash as _pristine_recipe_hash
    pristine_recipe_hash = _pristine_recipe_hash(
        {k: v for k, v in recipe.items() if k != "recipe_hash"})
    if inject_toolchain:
        recipe = _inject_toolchain(root, recipe)
    return {
        "t_command": t_command, "root": root,
        "baseline_pairs": baseline_pairs,
        "intolerant_tests": intolerant_tests,
        "declared_scoped": declared_scoped, "declared_bare": declared_bare,
        "coverage_limitations": coverage_limitations,
        "recipe": recipe, "recipe_hash": recipe["recipe_hash"],
        "pristine_recipe_hash": pristine_recipe_hash,
    }


def _measure_stage(root, target_files, recipe_hash, block_compress,
                   test_attribution, attribution_causal, evidence_out,
                   scope_roots) -> dict:
    """测量舞台：evidence dir / algo / 目标准备 / scope 完整性 / 陈旧恢复。"""
    t_gated = time.monotonic()
    evidence_dir = evidence_dir_for(evidence_out) if evidence_out else None
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
    # §SPEC-059a：报告 algo 与实际缓存键同源（flags/4-pid / blocks/6）
    from . import prepcache
    base_algo = (prepcache.PROBE_ALGO_VERSION if block_compress
                 else "flags/4-pid")
    algo_used = _algo_used(base_algo, test_attribution, attribution_causal)
    targets = _prepare_targets(
        root, target_files, recipe_hash=recipe_hash, base_algo=base_algo,
        algo_used=algo_used, block_compress=block_compress,
        test_attribution=test_attribution,
        attribution_causal=attribution_causal)
    return {
        "t_gated": t_gated, "evidence_dir": evidence_dir,
        "base_algo": base_algo, "algo_used": algo_used, "targets": targets,
        "prepared": targets["prepared"],
        "unmeasured": targets["unmeasured"],
        "causal_sites": targets["causal_sites"],
        "complete": _scope_complete(scope_roots, target_files),
        "recovered": _recover_stale_backups(root),
    }


ENV_FAIL_ON_RED_BASELINE = "GQT_MEASUREMENT_FAIL_ON_RED_BASELINE"
ENV_ACCEPTANCE_DISABLED = "GQT_MEASUREMENT_ACCEPTANCE_DISABLED"


_ENV_ARM_TIMEOUT_S = "GQT_MEASUREMENT_ARM_TIMEOUT_S"


def resolve_arm_timeout(requested_timeout_s: int,
                        preflight_wall_s: "float | None" = None
                        ) -> "tuple[int, str]":
    """§SPEC-TIMEOUT-ADAPT：分片 GUT 超时自适应。

    优先级：显式 env GQT_MEASUREMENT_ARM_TIMEOUT_S > 预检 wall_s 校准
    （wall × 安全系数 4，向上取整；无预检=无校准源）> 传入默认。
    返回 (effective_timeout_s, source)。"""
    import os as _os
    raw = _os.environ.get(_ENV_ARM_TIMEOUT_S, "").strip()
    if raw:
        try:
            return max(1, int(raw)), (f"explicit env "
                                      f"{_ENV_ARM_TIMEOUT_S}={raw}")
        except ValueError:
            pass
    if preflight_wall_s:
        calibrated = max(requested_timeout_s,
                         int(-(-preflight_wall_s * 4.0 // 1)))
        return calibrated, (f"preflight_calibrated x4 "
                            f"(baseline wall_s={preflight_wall_s})")
    return requested_timeout_s, f"parameter timeout_s={requested_timeout_s}s"


def acceptance_disabled() -> bool:
    """§SPEC-PLANFIRST 任务 2（Owner 决定）：acceptance 臂暂停——映射
    构建与默认测量路径不执行、不产出 acceptance；统一披露
    disabled_pending_atomization。恢复条件：SEE-1619 原子化改造完成后按
    共享进程分片恢复（设本 env=0）。默认暂停（缺省=1）。"""
    import os as _os
    return _os.environ.get(ENV_ACCEPTANCE_DISABLED, "1").strip() \
        not in ("", "0", "false")
_BASELINE_TOP_N = 10


def fail_on_red_baseline() -> bool:
    """§SPEC-PREFLIGHT×PLANFIRST：基线红即停为**显式开关且默认关**——
    默认行为是红测试按名排除（基线吸收）继续构建/执行，映射标 partial
    并披露 excluded_tests；只有显式开启才短停（Stryker 式）。"""
    import os as _os
    return _os.environ.get(ENV_FAIL_ON_RED_BASELINE, "").strip() \
        not in ("", "0", "false")


def run_baseline_preflight(root, recipe, timeout_s):
    """§SPEC-PREFLIGHT P1/P2/P4：映射构建路径的基线臂前置预检。

    未插桩基线 GUT 先跑（同面、零探针）：红即 MeasurementError 具名短停
    （阶段 baseline_preflight + 总数 + top-N 测试名）——不进插桩臂、不写
    map、不跑 acceptance；放行（--allow-red-baseline）或绿时把基线结果
    复用给 arms（P4——baseline_failures 非 None，懒获取不再发生）。"""
    pairs, meta = _acquire_lazy_baseline(
        root, _gutconfig_face_args(recipe), timeout_s)
    if pairs:
        top = sorted(pairs)[:_BASELINE_TOP_N]
        named = ", ".join(f"{s}::{t}" for s, t in top)
        if fail_on_red_baseline():
            raise MeasurementError(
                f"baseline_preflight_red: uninstrumented baseline GUT has "
                f"{len(pairs)} failing test pair(s) (stage="
                f"baseline_preflight, face={meta.get('face')}, "
                f"wall_s={meta.get('wall_s')}) — named failures (top-"
                f"{len(top)} of {len(pairs)}): {named}")
        # 默认（PLANFIRST 口径）：红测试按名排除、继续构建——映射标
        # partial、excluded_tests 具名披露（arms 按基线吸收，非静默）
        meta = {**meta, "status": "partial", "excluded_tests": sorted(
            [list(p) for p in pairs])}
        print(f"measure: baseline_preflight RED — {len(pairs)} failing "
              f"pair(s) (wall_s={meta.get('wall_s')}) EXCLUDED by name "
              f"(top-{len(top)}: {named}); mapping marked partial, run "
              f"continues (fail-on-red available via "
              f"{ENV_FAIL_ON_RED_BASELINE})", file=sys.stderr)
    else:
        meta = {**meta, "status": "green"}
        print(f"measure: baseline_preflight green (wall_s="
              f"{meta.get('wall_s')}) — reused for arms", file=sys.stderr)
    return sorted(pairs), meta


def _measure_arms(root, recipe, prepared, *, timeout_s, test_attribution,
                  attribution_causal, shard_width, shard_log_dir,
                  acceptance_repeats, baseline_failures, baseline_pairs,
                  declared_scoped, declared_bare, arms_parallel, evidence_dir,
                  acceptance_keys, acceptance_shards,
                  acceptance_adaptive) -> tuple:
    """双臂执行（arm_kwargs 装配 + execute_arms）。"""
    arm_kwargs = dict(
        prepared=prepared, timeout_s=timeout_s,
        test_attribution=test_attribution,
        attribution_causal=attribution_causal, shard_width=shard_width,
        shard_log_dir=shard_log_dir,
        acceptance_repeats=acceptance_repeats,
        baseline_failures=baseline_failures,
        baseline_pairs=baseline_pairs, declared_scoped=declared_scoped,
        declared_bare=declared_bare, arms_parallel=arms_parallel,
        evidence_dir=evidence_dir)
    return execute_arms(
        root=root, recipe=recipe, arms_parallel=arms_parallel,
        acceptance_keys=acceptance_keys, acceptance_shards=acceptance_shards,
        acceptance_adaptive=acceptance_adaptive, arm_kwargs=arm_kwargs)


def _measure_crap(unions, prepared, recipe_hash, stability_baseline,
                  coverage_limitations) -> dict:
    """CRAP 记录/车道 + stability baseline + 覆盖限制 provenance +
    measurable 统计。"""
    baseline_lines, baseline_records = (
        _load_baseline_report(stability_baseline)
        if stability_baseline else ({}, {}))
    unstable_keys = (_unstable_keys(unions, baseline_lines)
                     if baseline_lines else set())
    crap_records, crap_lanes, crap_version = _crap_records_for_report(
        unions, prepared, recipe_hash, unstable_keys, baseline_records)
    # §SPEC-061c：声明的覆盖限制具名入 unstable 记录 provenance（不伪造
    # 确定性、不静默——工具限制如实呈现）
    _attach_limitation_reasons(crap_records, coverage_limitations)
    unmeasurable, measurable_files, measurable_valid = _measurable_stats(
        unions)
    return {
        "records": crap_records, "lanes": crap_lanes, "version": crap_version,
        "unmeasurable": unmeasurable, "measurable_files": measurable_files,
        "measurable_valid": measurable_valid,
    }


def _measure_perf(root, probe_keys, timeout_s, t_gated, t_command) -> dict:
    """性能块（§SPEC-039 门量：插桩 + 两臂 GUT + union）。"""
    gated_core_s = round(time.monotonic() - t_gated, 3)
    perf = _performance(root, probe_keys, timeout_s)
    perf["gated_core_s"] = gated_core_s
    perf["command_total_s"] = round(time.monotonic() - t_command, 3)
    return perf


def _measure_attribution(arm_a, arm_b, prepared, *, test_attribution,
                         recipe_hash, root, attribution_out,
                         attribution_causal, causal_sites,
                         pristine_recipe_hash,
                         preflight_meta: "dict | None" = None) -> dict:
    """归属节（file_sha256 派生 + _attribution_section）。"""
    _file_sha = {rel: sha256_text(item["src"])
                 for rel, item in prepared.items()}
    return _attribution_section(
        test_attribution, (arm_a, arm_b), recipe_hash, root, attribution_out,
        causal=attribution_causal, causal_sites=causal_sites,
        file_sha256=_file_sha,
        target_files=sorted(prepared.keys()),
        recipe_pristine_hash=pristine_recipe_hash,
        tests_face=_tests_face_map(root),
        prepared=prepared,
        baseline_meta=(arm_a.get("baseline_failures_meta")
                       or arm_b.get("baseline_failures_meta")),
        preflight_meta=preflight_meta)


def _measure_emit(root, inp, st, arm_a, arm_b, unions, gated_arms,
                  comparable_reason, crap, stability_baseline, perf,
                  attr_section) -> dict:
    """报告装配 + 归属节合并（_assemble_report 参数序见其签名）。"""
    report = _assemble_report(
        root, inp["recipe_hash"], arm_a, arm_b, unions, gated_arms,
        comparable_reason, st["complete"], crap["records"], crap["lanes"],
        crap["version"], stability_baseline, inp["intolerant_tests"],
        inp["coverage_limitations"], crap["unmeasurable"],
        crap["measurable_files"], crap["measurable_valid"],
        st["targets"]["cache_hits"], st["targets"]["cache_misses"],
        st["algo_used"], st["prepared"], st["unmeasured"], st["recovered"],
        perf)
    report.update(attr_section)
    return report


def run_measurement(project_root: str | Path, target_files: list[str],
                  recipe: dict, *, acceptance_keys: list[str] | None = None,
                  probe_keys: list[str] | None = None, timeout_s: int = 180,
                  evidence_out: str | None = None, inject_toolchain: bool = True,
                  scope_roots: list[str] | None = None, block_compress: bool = True,
                  shard_width: int = 6, acceptance_shards: int = 4,
                  shard_log_dir: str | None = None,
                  stability_baseline: str | None = None,
                  baseline_failures: "set[tuple[str, str]] | None" = None,
                  intolerant_config: dict | None = None,
                  acceptance_repeats: int = 3, test_attribution: bool = False,
                  attribution_out: str | None = None,
                  attribution_causal: bool = False, arms_parallel: bool = False,
                  acceptance_adaptive: bool = False,
                  baseline_preflight: bool = False) -> dict:
    """measurement 双源批式编排，产出 measurement-report/2（分解见
    _measure_inputs/_measure_stage/_measure_arms/_measure_crap/_measure_emit）。"""
    from ..mutation.stagetiming import StageClock
    clock = StageClock()
    t_total = time.monotonic()
    with clock.stage("planning"):
        inp = _measure_inputs(project_root, target_files, recipe,
                              baseline_failures, intolerant_config,
                              inject_toolchain)
    root = inp["root"]
    # §SPEC-PREFLIGHT P1/P4：映射构建路径（baseline_preflight=True）且未
    # 显式提供基线时——基线臂前置，红即具名短停；绿/放行则复用结果
    # （arms 收到 baseline_failures，懒获取不再发生）。既有 measure 调用
    # （默认 False）语义零变化。
    _pf_meta = None
    timeout_source = f"parameter timeout_s={timeout_s}s"
    if baseline_preflight and baseline_failures is None:
        with clock.stage("baseline_preflight"):
            baseline_failures, _pf_meta = run_baseline_preflight(
                root, inp["recipe"], timeout_s)
        # §SPEC-TIMEOUT-ADAPT：自适应分片超时——预检 wall_s 为校准源
        # （显式 env > 校准 x4 > 传入值），来源随 pf_meta 披露
        timeout_s, timeout_source = resolve_arm_timeout(
            timeout_s, (_pf_meta or {}).get("wall_s"))
        _pf_meta = {**(_pf_meta or {}), "arm_timeout_s": timeout_s,
                    "arm_timeout_source": timeout_source}
        print(f"measure: arm timeout {timeout_s}s "
              f"(source: {timeout_source})", file=sys.stderr)
        # 生效基线通道是 baseline_pairs（_gate_ctx 的 effective 来源）——
        # 预检结果必须双写（failures 供 meta 判定，pairs 供吸收判定）
        inp["baseline_pairs"] = {tuple(p) for p in baseline_failures}
    with clock.stage("instrument_prepare"):
        st = _measure_stage(
            root, target_files, inp["recipe_hash"], block_compress,
            test_attribution, attribution_causal, evidence_out, scope_roots)
    with clock.stage("gut_full_arm"):
        if acceptance_disabled() and test_attribution:
            # §SPEC-PLANFIRST 任务 2（Owner 决定）：acceptance 臂暂停——
            # 占位臂 state=disabled；恢复条件 SEE-1619 原子化改造后共享
            # 进程分片（env GQT_MEASUREMENT_ACCEPTANCE_DISABLED=0）。
            from .arms import run_arm
            arm_a = run_arm(
                "gut_full", root=Path(root), recipe=inp["recipe"],
                timeout_s_source=timeout_source,
                prepared=st["prepared"], timeout_s=timeout_s,
                test_attribution=test_attribution,
                attribution_causal=attribution_causal,
                shard_width=shard_width, shard_log_dir=shard_log_dir,
                baseline_failures=baseline_failures,
                baseline_pairs=inp["baseline_pairs"],
                declared_scoped=inp["declared_scoped"],
                declared_bare=inp["declared_bare"],
                arms_parallel=arms_parallel,
                evidence_dir=st["evidence_dir"])
            arm_b = {"arm": "acceptance", "state": "disabled",
                     "invalid_reason": None, "invalid_evidence": None,
                     "wall_s": 0.0, "bundles": 0, "bundles_data": {},
                     "results": [], "repeats": 0, "convergence": None,
                     "shards": 0, "test_failures": {},
                     "baseline_failures_meta": None,
                     "attribution": {}, "reconciliation": None,
                     "shadowed": False, "adaptive_rounds_used": 0,
                     "arms_parallel": False}
        else:
            arm_a, arm_b = _measure_arms(
                root, inp["recipe"], st["prepared"], timeout_s=timeout_s,
                test_attribution=test_attribution,
                attribution_causal=attribution_causal,
                shard_width=shard_width, shard_log_dir=shard_log_dir,
                acceptance_repeats=acceptance_repeats,
                baseline_failures=baseline_failures,
                baseline_pairs=inp["baseline_pairs"],
                declared_scoped=inp["declared_scoped"],
                declared_bare=inp["declared_bare"],
                arms_parallel=arms_parallel,
                evidence_dir=st["evidence_dir"],
                acceptance_keys=acceptance_keys,
                acceptance_shards=acceptance_shards,
                acceptance_adaptive=acceptance_adaptive)
    # acceptance 臂耗时单独披露（暂停时 0.0；双臂时 gut_full_arm 段含
    # 双臂——acceptance 取其 wall_s）
    clock.add("acceptance_arm", max(0.0, (arm_b.get("wall_s") or 0.0)))
    unions = _build_unions(arm_a, arm_b, inp["recipe_hash"])
    gated_arms, comparable_reason = _gate_comparable(
        unions, arm_a, arm_b, st["complete"])
    crap = _measure_crap(unions, st["prepared"], inp["recipe_hash"],
                         stability_baseline, inp["coverage_limitations"])
    perf = _measure_perf(root, probe_keys, timeout_s, st["t_gated"], inp["t_command"])
    attr_section = _measure_attribution(
        arm_a, arm_b, st["prepared"], test_attribution=test_attribution,
        recipe_hash=inp["recipe_hash"], root=root,
        attribution_out=attribution_out, attribution_causal=attribution_causal,
        preflight_meta=_pf_meta,
        causal_sites=st["causal_sites"],
        pristine_recipe_hash=inp["pristine_recipe_hash"])
    report = _measure_emit(root, inp, st, arm_a, arm_b, unions, gated_arms,
                         comparable_reason, crap, stability_baseline, perf,
                         attr_section)
    # §SPEC-PLANFIRST B：映射构建全链分段落盘（消灭 prepare 盲区）
    clock.add("total", time.monotonic() - t_total)
    report["stage_timings"] = clock.disclosure()
    if acceptance_disabled() and test_attribution:
        report["acceptance_arm"] = ("disabled_pending_atomization — "
                                    "restored after SEE-1619 (shared-"
                                    "process shards); env override "
                                    "GQT_MEASUREMENT_ACCEPTANCE_DISABLED=0")
    return report
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SCRIPT_HEADER_RE = re.compile(r"^res://\S+\.gd$")

def _parse_failing_pairs(gut_output: str) -> set[tuple[str, str]]:
    """§SPEC-063c/064a：失败身份解析为 (script, test) 对。运行流按脚本
    分段（res://…gd 独占一行）；Run Summary 段起脚本身份不可知，失败以
    ("", test) 未归因对纳入解析（不得静默丢弃——混合形态必须门控，
    §SPEC-064a）。acceptance 私有目录的 per-run uuid 归一为 `.gqt-run-*`
    （目录名无测试身份语义，跨 run 基线可比）。"""
    pairs: set[tuple[str, str]] = set()
    current_script = ""
    current_test = None
    in_summary = False
    for raw in gut_output.splitlines():
        line = _ANSI_RE.sub("", raw).replace("\r", "")
        s = line.strip()
        if not in_summary and "Run Summary" in s:
            in_summary = True
            current_script = ""
            current_test = None
            continue
        if not in_summary and _SCRIPT_HEADER_RE.match(s):
            current_script = re.sub(r"\.gqt-run-[0-9a-f]+", ".gqt-run-*", s)
            current_test = None
            continue
        m = re.match(r"^[*-]\s+(test_\S+)", s)
        if m:
            current_test = m.group(1)
            continue
        if "[Failed]" in line and current_test:
            pairs.add((current_script, current_test))
    return pairs

def _gutconfig_face_args(recipe: dict) -> list[str]:
    """gut_full 臂的同面无插桩参数：与分片计划同源（recipe gut_face
    dirs 的并集）；空 dirs = GUT 读 .gutconfig 缺省面（不传 -gdir）。"""
    dirs = list((recipe.get("gut_face") or {}).get("dirs") or [])
    return [("-gdir=" + ",".join(dirs))] if dirs else []

def _acquire_lazy_baseline(project_root: Path, gut_args: list[str],
                           timeout_s: int) -> tuple[set, dict]:
    """§SPEC-060a/064b 懒基线获取（产品路径唯一实现——ensure_map 的
    归属构建与 measure run --test-attribution 都经 run_measurement 门控
    消费，harness 复用同一 helper 不另设第二套）。

    触发条件在门控侧：仅当 gut_full 插桩臂观测到失败且调用方未显式
    提供基线时，跑一次**同面、无插桩** GUT 取既有失败 (script, test)
    对（共享 _run_one_gut 通道，零探针零钩子；现场获取，不跨 run
    缓存）。无运行流 / 启动失败 → MeasurementError（fail-loud，绝不
    按空基线静默继续）。"""
    t0 = time.monotonic()
    r, _w = _run_one_gut(project_root, list(gut_args), timeout_s)
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    if _looks_like_godot_launch_failure(out) or (
            "Run Summary" not in out and "[Failed]" not in out
            and "[Passed]" not in out):
        tail = " | ".join(s for s in out.splitlines() if s.strip())[-400:]
        raise MeasurementError(
            "lazy baseline acquisition failed: uninstrumented GUT run "
            f"has no run stream (rc={r.returncode}) — refusing empty "
            f"baseline (§SPEC-060a fail-loud); gut output tail: {tail!r}")
    pairs = _parse_failing_pairs(out)
    face = "gutconfig_default"
    if gut_args and gut_args[0].startswith("-gdir="):
        face = gut_args[0].split("=", 1)[1]
    meta = {"method": "uninstrumented_run", "lazy": True, "face": face,
            "pairs": sorted([list(p) for p in pairs]),
            "count": len(pairs), "sha256": _pairs_sha256(pairs),
            "wall_s": round(time.monotonic() - t0, 3)}
    return pairs, meta

def _performance(root: Path, probe_keys: list[str] | None,
                 timeout_s: int) -> dict:
    """W4 acceptance 成本探针（缺省 aidomain_integration）——私有目录管线
    实测「编译 + 执行」墙钟，写入 performance.acceptance_probe。"""
    from ..gherkin.suite import run_acceptance_probe
    keys = probe_keys if probe_keys else ["aidomain_integration"]
    try:
        probe = run_acceptance_probe(root, keys, timeout_s=timeout_s)
    except (SuiteError, AcceptRunError) as e:
        probe = {"probe_version": "acceptance-probe/1", "keys": keys,
                 "suite_ok": False, "error": str(e)}
    return {"acceptance_probe": probe}

def _acceptance_convergence(root: Path, run_uuid: str, sink_by_rel: dict,
                            manifest_by_rel: dict, results: list,
                            reps: int) -> dict:
    """§SPEC-062C：验收臂逐轮行集并集收敛——min k s.t. ∪(1..k) ==
    ∪(1..N)。k<N 即收敛（其后轮零新增）；k==N 如实报告未收敛与上界。

    留在编排器模块：消费被测试 monkeypatch 的 `_read_hits_merged`
    编排器绑定（补丁点调用链不变）。"""
    n_reps = min(reps, min(len(r["reps"]) for r in results) if results else 0)
    worst = 0
    for rel in sink_by_rel:
        manifest = manifest_by_rel[rel]
        cum: set[int] = set()
        cum_seq: list[frozenset] = []
        for i in range(n_reps):
            rep_xdgs = [r["reps"][i]["xdg"] for r in results]
            hits, _ = _read_hits_merged(root, run_uuid, rel, manifest,
                                        rep_xdgs, consume=False)
            cum |= set(hits)
            cum_seq.append(frozenset(cum))
        final = cum_seq[-1] if cum_seq else frozenset()
        k = next((i + 1 for i, s in enumerate(cum_seq) if s == final),
                 n_reps)
        worst = max(worst, k)
    return {"rounds_used": worst, "bound": reps, "converged": worst < reps}
