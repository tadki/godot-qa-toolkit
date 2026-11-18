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













def run_measurement(project_root: str | Path, target_files: list[str],
                  recipe: dict, *, acceptance_keys: list[str] | None = None,
                  probe_keys: list[str] | None = None, timeout_s: int = 180,
                  evidence_out: str | None = None,
                  inject_toolchain: bool = True,
                  scope_roots: list[str] | None = None,
                  block_compress: bool = True,
                  shard_width: int = 6,
                  acceptance_shards: int = 4,
                  shard_log_dir: str | None = None,
                  stability_baseline: str | None = None,
                  baseline_failures: "set[tuple[str, str]] | None" = None,
                  intolerant_config: dict | None = None,
                  acceptance_repeats: int = 3,
                  test_attribution: bool = False,
                  attribution_out: str | None = None,
                  attribution_causal: bool = False,
                  arms_parallel: bool = False,
                  acceptance_adaptive: bool = False) -> dict:
    """measurement 双源批式编排，产出 measurement-report/2。

    baseline_failures（§SPEC-060a/064b）：既有（未插桩）运行的失败
    (script, test) 身份对集合——双臂失败对逐对披露后，相对该基线的
    **新增失败对**即门控：臂 run_error → union invalid → ok=false/
    comparable=false，臂不出分。同名跨脚本不互相吸收、未归因仅匹配
    未归因；裸名字符串身份歧义 → MeasurementError（fail-closed）。
    缺省（None）时**懒获取**：仅当 gut_full 插桩臂观测到失败，才现场
    跑一次同面无插桩 GUT 取既有失败基线并复核（绿臂零额外 GUT；获取
    失败 fail-loud，臂保持 invalid 具名；acceptance 臂不取基线——其
    失败应保持可见）；显式提供优先，不做懒获取。生效基线（含懒获取
    与显式两种方式）披露在归属产物 `baseline_failures_meta`。

    intolerant_config（§SPEC-061b/c）：{"tests": [{name, reason}],
    "coverage_limitations": [{file, reason}]}——声明「插桩不容忍」测试
    （任何源码级插桩都会改变其断言所见的源码/autoload 原始形态）：仅具名项
    从失败门控比较中旁路（不静默跳过），报告逐名披露理由并作为覆盖率盲点
    声明；coverage_limitations 声明工具无法确定性测量的文件（如插桩减速下
    晚挂载 UI），其 unstable 记录附具名理由。

    acceptance_repeats（§SPEC-062C）：验收臂 N 次重复执行取行集并集
    （单轮 UI 时序不确定，并集测量可复现），报告收敛轮数或如实报告未收敛
    与上界；重复成本计入臂 wall_s（§SPEC-052 时长预算内比较）。"""
    t_command = time.monotonic()
    root = Path(project_root).resolve()
    # §SPEC-064b：基线失败集升级为 (script, test) 身份对——同名跨脚本不
    # 互相吸收、未归因仅匹配未归因；裸名字符串身份歧义 → fail-closed 拒绝
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
    def _req_str(obj: dict, key: str, kind: str) -> str:
        """§SPEC-065：配置字段统一类型校验——非 str 或空白即 MeasurementError
        fail-fast（非 str 会在匹配/附着层失配静默失效，如 file=123）。"""
        v = obj.get(key)
        if not isinstance(v, str) or not v.strip():
            raise MeasurementError(
                f"{kind} field {key!r} must be a non-empty string, "
                f"got {v!r} (§SPEC-065)")
        return v

    cfg = intolerant_config or {}
    # §SPEC-065（R19）：提供即须合法——顶层键显式 null / 非 list 与声明
    # 字段类型错同治（"键缺省" 与 "键值为 null" 是两种语义，null 不得
    # 静默降级为缺省）
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
    intolerant_names = {t["name"] for t in intolerant_tests}
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
    recipe_hash = recipe["recipe_hash"]
    t_gated = time.monotonic()
    evidence_dir = evidence_dir_for(evidence_out) if evidence_out else None
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)

    # 目标全集：registry + probe manifest + 插桩模板由 _prepare_target_file
    # 逐文件准备（§SPEC-030 单次 parse 纪律 / §SPEC-054 静态缓存 /
    # 方向②③ 两臂分离模板）；parse 失败逐文件隔离进 unmeasured。
    from . import prepcache

    prepared: dict[str, dict] = {}
    unmeasured: list[dict] = []
    cache_hits = 0
    cache_misses = 0
    # §SPEC-059a：报告 algo 与实际缓存键同源（flags/4-pid / blocks/6）
    base_algo = (prepcache.PROBE_ALGO_VERSION if block_compress
                 else "flags/4-pid")
    algo_used = _algo_used(base_algo, test_attribution, attribution_causal)
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

    # §SPEC-053：subset 不得标 comparable——目标集未覆盖 scope_roots 下全部
    # .gd 时，union/评分的 comparable 一律降为 false 并给原因。
    scope_complete = True
    if scope_roots:
        scope_files = {str(x) for sr in scope_roots
                       for x in Path(sr).rglob("*.gd") if x.is_file()}
        scope_complete = scope_files <= {str(Path(tf)) for tf in target_files}
    recovered = _recover_stale_backups(root)


    def _arm(name: str, acceptance: dict | None = None,
             shadow_instrument: bool = False,
             adaptive_reps: bool = False) -> dict:
        """一条批式臂（§SPEC-052 并行分片）：复用全集插桩 → 按目录/feature
        组并行扇出 N 个 GUT 进程（每进程独立 XDG user:// 隔离 + 运行时
        PID sink）→ 合并逐文件 sink 读回 → bundle → 恢复/清理。零可执行行
        文件进显式 vacuous 车道（§SPEC-042）。

        shadow_instrument=True（P2 双臂并行）：插桩写入分片 shadow 而非
        项目根——两臂可并发执行而不互踩 uuid。adaptive_reps=True（P1）：
        acceptance 臂逐轮执行、并集稳定即提前锁定（上限仍为 reps）。"""
        from concurrent.futures import ThreadPoolExecutor

        run_uuid = make_run_uuid()
        backup_dir = _backup_base() / run_uuid
        backup_dir.mkdir(mode=0o700)
        started = {"run_uuid": run_uuid, "project_root": str(root),
                   "files": {}}
        sink_by_rel: dict[str, str] = {}
        manifest_by_rel: dict[str, list[int]] = {}
        instrumented_files: dict[str, str] = {}
        inst_by_rel: dict[str, str] = {}
        vacuous_rels: list[str] = []
        for rel, item in prepared.items():
            # 方向②③：归属只在 gut_full 臂启用——acceptance 臂用 plain
            # 模板与默认钩子（不为归属多付 parse/运行成本）
            use_attr = test_attribution and name == "gut_full"
            template = item.get("template_attr") if use_attr else item.get("template")
            manifest = item.get("probe_lines") or []
            if template is None:
                continue  # 插桩失败已隔离进 unmeasured
            if not manifest:
                vacuous_rels.append(rel)
                continue
            sink = batch_sink_name(run_uuid, rel)
            n_ph = template.count(prepcache.SINK_UUID_PLACEHOLDER)
            # §SPEC-054：模板以 uuid 占位符保存，臂间仅做占位替换
            inst = template.replace(prepcache.SINK_UUID_PLACEHOLDER,
                                    str(run_uuid))
            if not n_ph:
                raise MeasurementError(
                    f"sink placeholder missing for {rel} (stale cache?)")
            started["files"][item["abs"]] = item["src"]
            sink_by_rel[rel] = sink
            manifest_by_rel[rel] = manifest
            instrumented_files[item["abs"]] = inst
            inst_by_rel[rel] = inst
        pg_path = root / "project.godot"
        if not pg_path.is_file():
            raise MeasurementError(
                "cannot run measurement: project.godot missing")
        # §SPEC-062A：project.godot 全程只读——finalizer 经 GUT
        # pre_run_script 通道注册，不再改写 [autoload] 表
        started["created"] = [str(root / _FLUSHER_RELPATH),
                              str(root / _PRE_HOOK_RELPATH)]
        if attribution_causal and name == "gut_full":
            started["created"].append(str(root / _ATTR_CTX_RELPATH))
        if not shadow_instrument:
            (backup_dir / "started.json").write_text(
                json.dumps(started, ensure_ascii=False), encoding="utf-8")
        # 分片计划：acceptance 组由调用方编译；gut_full 按 gutconfig dirs
        if acceptance is not None:
            plans = [
                {"args": ["-gdir=res://" + Path(g["suite_dir"]).relative_to(
                    root).as_posix() + "/generated"],
                 "keys": g["keys"], "suite_dir": Path(g["suite_dir"])}
                for g in acceptance["groups"]]
        else:
            groups = _shard_groups(
                root, list(recipe["gut_face"].get("dirs") or []),
                shard_width)
            plans = [{"args": ([("-gdir=" + ",".join(g))] if g else []),
                      "keys": None, "suite_dir": None} for g in groups]
        gut_ok = True
        run_error = False
        timed_out = False
        output = ""
        results: list[dict] = []
        xdg_homes: list[str] = []
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

        def _instrument_shadow(shadow: Path) -> None:
            for rel, inst in inst_by_rel.items():
                target = shadow / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(inst, encoding="utf-8")
            for relpath, text in shadow_hook_files:
                hp = shadow / relpath
                hp.parent.mkdir(parents=True, exist_ok=True)
                hp.write_text(text, encoding="utf-8")

        def _run_shard(ip):
            idx, plan = ip
            args = list(plan["args"]) + [
                "-gpre_run_script=res://" + _PRE_HOOK_RELPATH]
            rep_records: list[dict] = []
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
                    _instrument_shadow(shadow)
                prev_union = None
                for rep in range(reps):
                    xdg = tempfile.mkdtemp(
                        prefix=f"gqt-shard-{run_uuid[:8]}-{idx}-r{rep}-")
                    # §SPEC-060e：mkdtemp 成功即注册进闭包共享的
                    # xdg_homes——任何异常路径都由外层 finally 清理
                    xdg_homes.append(xdg)
                    env = dict(os.environ)
                    env["XDG_DATA_HOME"] = xdg
                    try:
                        r, _w = _run_one_gut(shadow, args, timeout_s,
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
                        # P1：分片局部累计并集停止增长即提前锁定（判据与
                        # §SPEC-062C 一致：∪(1..k)==∪(1..N)；无全局 barrier）
                        shard_union: set[int] = set(prev_union or ())
                        for rel in sink_by_rel:
                            hits, _ = _read_hits_merged(
                                root, run_uuid, rel, manifest_by_rel[rel],
                                [xdg], consume=False)
                            shard_union |= set(hits)
                        if rep >= 1 and frozenset(shard_union) == prev_union:
                            break
                        prev_union = frozenset(shard_union)
            finally:
                _cleanup_shadow(shadow)
            # §SPEC-071 H1：shadow symlink 消解行为显式披露（解引用/悬空移除）
            print(f"measure: arm={name} shard={idx + 1}/{len(plans)} "
                  f"done s={round(time.monotonic() - _t_shard, 1)}",
                  file=sys.stderr)
            return {"plan": plan, "reps": rep_records,
                    "shadow_disclosure": shadow_disclosure}

        try:
            if not shadow_instrument:
                for abs_path, inst in instrumented_files.items():
                    Path(abs_path).write_text(inst, encoding="utf-8")
                # 方向②③：attr 钩子只在归属臂注入；acceptance 臂用默认钩子
                if attribution_causal and name == "gut_full":
                    # 注入脚本解析失败会让全部被插桩文件在 load 期失效（探针
                    # 静默零命中）——生成期先 parse 一次，fail-loud 拒绝跑假数据
                    from gdtoolkit.parser import parser as _ctx_parser
                    try:
                        _ctx_parser.parse(_ATTR_CTX_GD)
                    except Exception as e:  # noqa: BLE001
                        raise MeasurementError(
                            f"causal context script failed to parse: "
                            f"{e}") from e
                for relpath, text in shadow_hook_files:
                    abs_p = root / relpath
                    abs_p.parent.mkdir(parents=True, exist_ok=True)
                    abs_p.write_text(text, encoding="utf-8")
            t_arm = time.monotonic()
            print(f"measure: arm={name} start shards={len(plans)} "
                  f"reps={reps}", file=sys.stderr)
            try:
                with ThreadPoolExecutor(
                        max_workers=max(1, len(plans))) as ex:
                    results = list(ex.map(_run_shard, enumerate(plans)))
            except BaseException:
                # §SPEC-060e：worker 生命周期异常终止（如 shadow 构建失败）
                # → 已创建的 XDG 立即清理；with-block 退出已等待全部在飞
                # 分片，闭包注册表在此刻已完整
                for xdg in xdg_homes:
                    shutil.rmtree(xdg, ignore_errors=True)
                xdg_homes.clear()
                raise
            wall_s = time.monotonic() - t_arm
            # §SPEC-063a/c：失败身份解析为 (script, test) 对；任一 rep
            # rc!=0 且无可解析失败名（含启动失败）→ 不可归因失败
            # fail-closed 门控，不得出分
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
            output = "\n".join(
                rep["out"] for r in results for rep in r["reps"])
            # §SPEC-060a 门控在树还原后统一执行（见下方）——此处只解析
            # 失败身份对与不可归因失败（懒基线获取需要无插桩树）
        finally:
            for abs_path, original in started["files"].items():
                Path(abs_path).write_text(original, encoding="utf-8")
            for created in started.get("created", []):
                Path(created).unlink(missing_ok=True)
            shutil.rmtree(backup_dir, ignore_errors=True)

        # §SPEC-060a/061b/063c 门控（树已还原=无插桩；懒基线获取在此执行）：
        # 新增失败门控；具名排除按 (script, test) 身份匹配（无 script 声明
        # 仅豁免未归因失败；同名跨脚本不得合并豁免），基线按既有失败对吸收
        def _exempt(p: tuple[str, str]) -> bool:
            if p in declared_scoped:
                return True
            return p[0] == "" and p[1] in declared_bare

        # 臂内生效基线 = 显式参数（或空）；懒获取只合并进本臂副本——
        # acceptance 臂不得继承 gut_full 的懒基线（其失败保持可见）
        effective_baseline = set(baseline_pairs)
        baseline_meta: dict | None = None
        if baseline_failures is not None:
            baseline_meta = {"method": "explicit_param",
                             "count": len(effective_baseline),
                             "pairs": sorted([list(p) for p in effective_baseline]),
                             "sha256": _pairs_sha256(effective_baseline)}
        # §SPEC-064a：Summary 段 ("", t) 是同 run 内已归因失败 (s, t)
        # 的回显（同一失败的第二种披露形态）——其归因实例已被基线吸收
        # 或声明豁免时随同覆盖；无已覆盖归因实例的未归因失败仍为新增
        # （N-M-3 防护不弱化）
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
        if (name == "gut_full" and new_pairs
                and baseline_failures is None):
            # 懒获取（成本裁定）：绿臂零额外 GUT；观测到失败且未显式提供
            # 基线时才跑一次同面无插桩 GUT 取基线并复核。获取失败 → 具名
            # stderr + 臂保持 invalid（fail-loud，不按空基线静默转绿）。
            try:
                lazy_pairs, baseline_meta = _acquire_lazy_baseline(
                    root, _gutconfig_face_args(recipe), timeout_s)
            except MeasurementError as e:
                print(f"measure: {e} — observed failures stay unabsorbed; "
                      f"arm {name} invalid (fail-loud)", file=sys.stderr)
                baseline_meta = None
            else:
                effective_baseline = effective_baseline | set(lazy_pairs)
                covered_attributed_names = {
                    t for (s, t) in arm_fail_pairs
                    if s and ((s, t) in effective_baseline
                              or (s, t) in declared_scoped)}
                new_pairs = {p for p in arm_fail_pairs if not _covered(p)}
                print(f"measure: lazy baseline {baseline_meta['count']} "
                      f"failing pairs acquired uninstrumented "
                      f"({baseline_meta['wall_s']}s) — only NEW failures "
                      f"gate the arm", file=sys.stderr)
        arm_new_failures = sorted({t for (_s, t) in new_pairs})
        if new_pairs:
            gut_ok = False
            run_error = True

        reconciliation = None
        arm_valid = not run_error
        invalid_reason = ("timeout" if timed_out
                          else ("run_error" if run_error else None))
        if acceptance is not None and not run_error:
            # §SPEC-035 对账：分片组逐 rep 对账（rep 间独立进程独立输出），
            # 任一 false → 臂 invalid
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
                # §SPEC-063b：结构对账失败 = 臂无效且必须传入 union 失败
                # 哨兵（run_error）——对账失败不得静默出分
                arm_valid = False
                invalid_reason = "reconciliation_failed"
                gut_ok = False
                run_error = True
        if acceptance is not None:
            for r in results:
                if r["plan"].get("suite_dir") is not None:
                    _cleanup_suite_dir(Path(r["plan"]["suite_dir"]))

        bundles: dict[str, dict] = {}
        convergence = None
        attribution = None
        try:
            if acceptance is not None and reps > 1:
                # §SPEC-062C：逐轮行集并集收敛轮数（rep 间独立进程，
                # 并集对轮序不敏感）
                convergence = _acceptance_convergence(
                    root, run_uuid, sink_by_rel, manifest_by_rel,
                    results, reps)
            for rel, sink in sink_by_rel.items():
                item = prepared[rel]
                retain = (Path(shard_log_dir) / f"{name}-sinks"
                          if shard_log_dir else None)
                hits, probes_fired = _read_hits_merged(
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
                bundle = build_bundle(
                    run_uuid=run_uuid, file_path="res://" + rel,
                    gd_source=item["src"], tests_glob=None,
                    registry=item["registry"],
                    probe_manifest=item["probe_manifest"],
                    hits_lines=sorted(hits), probes_fired=probes_fired,
                    gut_ok=gut_ok, run_error=run_error, timeout=timed_out,
                    zero_tests=False, vacuous=False, arm=name)
                bundles[rel] = bundle
                if evidence_dir is not None:
                    # 批式同臂共享 run_uuid——evidence 文件名必须带 per-file
                    # 后缀防互相覆盖（R3 实证缺陷）
                    suffix = f"-{file_key8(rel)}"
                    record = build_run_record(
                        run_uuid=run_uuid, file_path="res://" + rel,
                        gd_source=item["src"], tests_glob=None,
                        probe_manifest=item["probe_manifest"], arm=name)
                    # §SPEC-070 FR-5：不再写入 sink_path——批式 sink 在分片
                    # XDG 下且读回即消费，指向 evidence_dir/<sink> 是不存在
                    # 位置的虚假路径
                    write_run_record(evidence_dir, record, name_suffix=suffix)
                    write_bundle(evidence_dir, bundle, name_suffix=suffix)
                    update_run_record(evidence_dir, run_uuid, name_suffix=suffix,
                                      status="bundle_written")
                clear_sink(None, str(root), sink)
            # §SPEC-083：归属证据与注册表必须在 xdg 清理前读回（仅归属臂）
            attribution = _collect_attribution(
                test_attribution and name == "gut_full", root, run_uuid, name,
                sink_by_rel, xdg_homes, prepared=prepared)
        finally:
            for xdg in xdg_homes:
                shutil.rmtree(xdg, ignore_errors=True)
        for rel in vacuous_rels:
            # §SPEC-042：零可执行行 → 显式 vacuous 车道（bundle 落盘、
            # union 按 §SPEC-036 严格语义仍 invalid，reason=vacuous）
            item = prepared[rel]
            bundle = build_bundle(
                run_uuid=run_uuid, file_path="res://" + rel,
                gd_source=item["src"], tests_glob=None,
                registry=item["registry"],
                probe_manifest=item["probe_manifest"],
                hits_lines=[], probes_fired=0,
                gut_ok=gut_ok, run_error=run_error, timeout=timed_out,
                zero_tests=False, vacuous=True, arm=name)
            bundles[rel] = bundle
            if evidence_dir is not None:
                suffix = f"-{file_key8(rel)}"
                record = build_run_record(
                    run_uuid=run_uuid, file_path="res://" + rel,
                    gd_source=item["src"], tests_glob=None,
                    probe_manifest=item["probe_manifest"], arm=name)
                write_run_record(evidence_dir, record, name_suffix=suffix)
                write_bundle(evidence_dir, bundle, name_suffix=suffix)
                update_run_record(evidence_dir, run_uuid, name_suffix=suffix,
                                  status="bundle_written")
        print(f"measure: arm={name} done state="
              f"{'valid' if arm_valid else 'invalid'} "
              f"wall_s={round(wall_s, 1)}", file=sys.stderr)
        return {
            "arm": name, "run_uuid": run_uuid, "wall_s": round(wall_s, 3),
            "gut_ok": gut_ok, "timed_out": timed_out,
            "state": ("valid" if arm_valid else "invalid"),
            "invalid_reason": invalid_reason,
            "baseline_failures_meta": baseline_meta,
            "test_failures": {"observed": sorted(arm_failures),
                              "observed_pairs": sorted(
                                  f"{s}::{t}" for (s, t) in arm_fail_pairs),
                              "new": arm_new_failures,
                              "new_pairs": sorted(
                                  f"{s}::{t}" for (s, t) in new_pairs),
                              "instrumentation_intolerant": sorted(
                                  f"{s}::{t}" for (s, t) in exempt_pairs)},
            # §SPEC-062C：验收臂 N 次重复与收敛披露（gut_full 恒单轮）
            "repeats": reps,
            "convergence": convergence,
            "adaptive_rounds_used": max(
                (len(r["reps"]) for r in results), default=0)
            if (acceptance_adaptive and acceptance is not None) else None,
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

    def _acceptance_flow() -> dict:
        """臂 acceptance：私有 suite 按分片分组编译/重放 → 并行扇出 GUT。"""
        keys = list(acceptance_keys) if acceptance_keys else [
            f["key"] for f in recipe["acceptance_face"]["features"]]
        snapshot = _snapshot_main(root)
        groups: list[dict] = []
        try:
            n_groups = max(1, min(int(acceptance_shards or 1), len(keys) or 1))
            chunk = max(1, -(-len(keys) // n_groups))
            for i in range(0, len(keys), chunk):
                sub = keys[i:i + chunk]
                sd = create_suite_dir(root)
                groups.append({"suite_dir": sd, "keys": sub})
                compile_keys_to_suite(root, sub, sd)
                for k in sub:
                    replay_check(root, k, suite_dir=sd)
            return _arm("acceptance", acceptance={
                "groups": groups, "snapshot": snapshot},
                shadow_instrument=arms_parallel,
                adaptive_reps=acceptance_adaptive)
        except (SuiteError, AcceptRunError) as e:
            for g in groups:
                _cleanup_suite_dir(Path(g["suite_dir"]))
            return {"arm": "acceptance", "run_uuid": None, "wall_s": 0.0,
                    "gut_ok": False, "timed_out": False, "state": "invalid",
                    "invalid_reason": f"suite_error:{e}", "bundles": 0,
                    "bundles_data": {}, "reconciliation": None,
                    "test_failures": {"observed": [], "observed_pairs": [],
                                      "new": [], "new_pairs": [],
                                      "instrumentation_intolerant": []},
                    "repeats": 0, "convergence": None}

    if arms_parallel:
        # P2：双臂并行——acceptance 在 shadow 内插桩（根树只被 gut 臂触碰）
        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=2) as pex:
            fut_a = pex.submit(_arm, "gut_full")
            fut_b = pex.submit(_acceptance_flow)
            arm_a = fut_a.result()
            arm_b = fut_b.result()
    else:
        # 臂 gut_full：.gutconfig 面（无 -gdir——GUT 缺省读配置，lab 实测锚定）
        arm_a = _arm("gut_full")
        arm_b = _acceptance_flow()

    # 逐文件 union（coverage-union/1，禁平均）
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
    union_valid = bool(unions) and all(u["valid"] for u in unions)
    comparable_reason = None
    # §SPEC-060a：任一臂出现相对基线的新增失败 → 全局 comparable=false
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
    # §SPEC-043 measurable 子集披露：vacuous / no_probe_fired 文件归
    # unmeasurable（本就无可测数据），其余 union 无效仍属 measurable 失败
    # （超时/契约违约等），禁止静默排除——measurable + unmeasurable == 全集。
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
    # §SPEC-039 门量：插桩 + 两臂 GUT + union（recipe 构建与 W4 探针不计）
    gated_core_s = round(time.monotonic() - t_gated, 3)
    perf = _performance(root, probe_keys, timeout_s)
    perf["gated_core_s"] = gated_core_s
    perf["command_total_s"] = round(time.monotonic() - t_command, 3)
    _file_sha = {rel: sha256_text(item["src"])
                 for rel, item in prepared.items()}
    attr_section = _attribution_section(
        test_attribution, (arm_a, arm_b), recipe_hash, root, attribution_out,
        causal=attribution_causal, causal_sites=causal_sites,
        file_sha256=_file_sha,
        target_files=sorted(prepared.keys()),
        recipe_pristine_hash=pristine_recipe_hash,
        tests_face=_tests_face_map(root),
        prepared=prepared,
        baseline_meta=(arm_a.get("baseline_failures_meta")
                       or arm_b.get("baseline_failures_meta")))
    report = {
        "report_version": REPORT_VERSION,
        "project_root": str(root),
        "recipe_hash": recipe_hash,
        "arms": [
            {k: v for k, v in arm.items()
             if k not in ("bundles_data", "attribution")}
            for arm in (arm_a, arm_b)
        ],
        "unions": unions,
        # §SPEC-060a：双臂失败集逐名披露（observed 全集 + new 相对基线）；
        # baseline_count = gut_full 臂生效基线（显式提供或懒获取）
        "failures": {
            "baseline_count": ((arm_a.get("baseline_failures_meta") or {})
                               .get("count") or 0),
            "gated": any(v["new"] for v in gated_arms.values()),
            "arms": gated_arms,
        },
        # §SPEC-061b/063c：instrumentation-intolerant 具名排除披露——按
        # (script, test) 身份匹配旁路门控，逐名给理由，并作为覆盖率盲点
        # 声明（非静默跳过；同名跨脚本不合并豁免）
        "instrumentation_intolerant": {
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
        },
        # §SPEC-061c：工具无法确定性测量的文件（如插桩减速下晚挂载 UI）
        # 声明披露；对应 unstable 记录附具名理由
        "coverage_limitations": [
            {"file": l["file"], "reason": str(l["reason"])}
            for l in coverage_limitations],
        "union": {
            "valid": union_valid,
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
        },
        "crap": {
            "version": crap_version,
            "evidence_level": "exploratory",
            # §SPEC-070 FR-4/R26 + §SPEC-071 L2：顶层语义从**最终 records**
            # 实测统计（含 ghost 回放对账后的形态），record/crap_line/
            # counts 三处一致；两形态并存 → mixed
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
        },
        "preparation": {
            "cache": {"hits": cache_hits, "misses": cache_misses,
                      "algo": algo_used},
        },
        # §SPEC-070 FR-4：块压缩近似语义显式披露——不得冒充逐语句精确
        # statement_start_line（块内后继语句被运行期错误中止时仍计入覆盖）
        "coverage_semantics": {
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
        },
        "unmeasured": unmeasured,
        "recovered_stale_backups": recovered,
        "performance": perf,
        "certification": dict(CERTIFICATION),
        "evidence_level": "exploratory",
        "diagnostic_only": True,
        "dynamic_cache": False,
        "ok": union_valid,
    }
    report.update(attr_section)
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
