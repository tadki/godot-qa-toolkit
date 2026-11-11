"""gqt canonical 双源批式编排（§SPEC-037，canonical-report/1）。

批式臂（canonical 的唯一可行形态，设计 §2.1）：目标全集一次插桩 → 每臂
1 次 GUT（gut_full 走 .gutconfig 面 + acceptance 私有目录一次）→ 逐文件
sink（`qa-coverage-hits-{run_uuid}-{file_key8}`）读回 → bundle →
coverage-union/1 逐文件行集并集。两臂共用同一 registry/probe manifest
（同源码、同 manifest），run UUID 每臂独立。

在树插桩：备份目录 /tmp/gqt-canonical-backups/<run_uuid>/（started-record
+ 逐文件原文），finally 恢复 + 清理；启动时回收同项目陈旧备份（崩溃恢复，
恢复前不信任任何 sink）。KOL 生产树零残留由「备份+恢复+对账后清理」保证。

certified 边界（§2.4）：一律 evidence_level="exploratory"、
certification.status="not_certified"——单轮 exploratory，WP5 拥有双轮认证。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..accept.run import AcceptRunError, replay_check
from ..accept.suite import (
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
    _looks_like_godot_launch_failure,
    _read_hits,
)
from ..coverage.union import arm_record_from_bundle, union_file
from ..mutation.godot_gate import GlobalGodotGate
from ..mutation.inject import godot_command
from .recipe import verify_recipe

REPORT_VERSION = "canonical-report/1"
CERTIFICATION = {
    "status": "not_certified",
    "reason": "single round; WP5 owns two-round certification",
}
_BACKUP_BASE = Path(tempfile.gettempdir()) / "gqt-canonical-backups"

# §SPEC-044：inline 探针形态——语句前直插持久句柄 store_string（无
# GDScript 函数调用、无 append/阈值刷盘），写穿持久由内核保证，reload 续写
# 不截断；无需注入/收尾（R2 flusher 方案因长跑臂尾部丢失 + 覆盖语义漂移
# 已废弃）。


class CanonicalError(Exception):
    """canonical 编排入参/前置违例（recipe 非法、目标缺失、GUT 缺失）。"""


def file_key8(relpath: str) -> str:
    return hashlib.sha1(relpath.encode("utf-8")).hexdigest()[:8]


def batch_sink_name(run_uuid: str, relpath: str) -> str:
    return f"qa-coverage-hits-{run_uuid}-{file_key8(relpath)}.txt"


def _recover_stale_backups(project_root: Path) -> int:
    """崩溃恢复：回收本项目的陈旧备份目录（恢复原文、删除备份）。

    恢复前不信任任何 sink——调用方在回收之后才开始新 run。
    """
    restored = 0
    if not _BACKUP_BASE.is_dir():
        return 0
    for d in sorted(_BACKUP_BASE.iterdir()):
        marker = d / "started.json"
        if not marker.is_file():
            continue
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("project_root") != str(project_root):
            continue
        for src, original in record.get("files", {}).items():
            Path(src).write_text(original, encoding="utf-8")
        shutil.rmtree(d, ignore_errors=True)
        restored += 1
    return restored


def _run_one_gut(project_root: Path, gut_args: list[str],
                 timeout_s: int) -> tuple[subprocess.CompletedProcess, float]:
    t0 = time.monotonic()
    gut = project_root / "addons" / "gut" / "gut_cmdln.gd"
    if not gut.is_file():
        raise CanonicalError(f"GUT runner not found: {gut}")
    argv = [godot_command(), "--headless", "--path", str(project_root),
            "-s", "res://addons/gut/gut_cmdln.gd", *gut_args, "-gexit"]
    with GlobalGodotGate(str(project_root)):
        try:
            r = subprocess.run(argv, capture_output=True, text=True,
                               timeout=timeout_s, cwd=str(project_root))
        except subprocess.TimeoutExpired as e:
            raise CanonicalError(
                f"GUT timed out after {timeout_s}s "
                f"(args: {' '.join(gut_args)})") from e
    return r, time.monotonic() - t0


def _inject_toolchain(root: Path, recipe: dict) -> dict:
    """PR-A L1 预留的编排器职责：测量时注入真实 toolchain 版本并重算
    recipe_hash（注入路径下版本变化即 recipe 变化）。加载态先 verify
    （防篡改），注入后重 hash 再作为测量身份。"""
    import re as _re

    from ..accept import OPS_VERSION
    toolchain = dict(recipe.get("toolchain", {}))
    toolchain["ops_version"] = OPS_VERSION
    try:
        from importlib.metadata import version as _pkg_version
        toolchain["gqt_version"] = _pkg_version("godot-qa-toolkit")
    except Exception:  # noqa: BLE001 — editable 安装无 metadata 时回落
        toolchain["gqt_version"] = "unknown"
    cfg = root / "addons" / "gut" / "plugin.cfg"
    if cfg.is_file():
        m = _re.search(r'version\s*=\s*"([^"]+)"', cfg.read_text(encoding="utf-8"))
        if m:
            toolchain["gut_version"] = m.group(1)
    try:
        import subprocess as _sp
        r = _sp.run([godot_command(), "--version"], capture_output=True,
                    text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            toolchain["godot_version"] = r.stdout.strip().splitlines()[0]
    except Exception:  # noqa: BLE001 — 版本探测失败保持原值
        pass
    recipe = dict(recipe)
    recipe["toolchain"] = toolchain
    from .recipe import recipe_hash as _recipe_hash
    recipe["recipe_hash"] = _recipe_hash(recipe)
    return recipe


def run_canonical(project_root: str | Path, target_files: list[str],
                  recipe: dict, *, acceptance_keys: list[str] | None = None,
                  probe_keys: list[str] | None = None, timeout_s: int = 180,
                  evidence_out: str | None = None,
                  inject_toolchain: bool = True) -> dict:
    """canonical 双源批式编排，产出 canonical-report/1。"""
    t_command = time.monotonic()
    root = Path(project_root).resolve()
    ok, reason = verify_recipe(recipe)
    if not ok:
        raise CanonicalError(f"recipe invalid: {reason}")
    if not target_files:
        raise CanonicalError("no target files given")
    if inject_toolchain:
        recipe = _inject_toolchain(root, recipe)
    recipe_hash = recipe["recipe_hash"]
    t_gated = time.monotonic()
    evidence_dir = evidence_dir_for(evidence_out) if evidence_out else None
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)

    # 目标全集：registry + probe manifest 每文件只建一次（§SPEC-030 单次
    # parse 纪律；两臂共用同一 manifest——同源码同 manifest 的 union 前提）。
    # parse 失败逐文件隔离进 unmeasured（§2.3 步骤 3），不阻断全集。
    prepared: dict[str, dict] = {}
    unmeasured: list[dict] = []
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
        from ..registry.functions import build_file_registry
        from ..registry.manifest import build_probe_manifest_v2
        try:
            registry = build_file_registry(src, file_label=str(p))
            probe_manifest = build_probe_manifest_v2(src, registry)
        except Exception as e:  # noqa: BLE001 — 逐文件隔离，不阻断全集
            unmeasured.append({"file": path_str,
                               "reason": f"registry_parse_error:{e}"})
            continue
        rel = str(p.resolve().relative_to(root))
        prepared[rel] = {
            "abs": str(p.resolve()), "src": src,
            "registry": registry, "probe_manifest": probe_manifest,
        }

    recovered = _recover_stale_backups(root)

    # §SPEC-044 插桩复用：全集 inline 插桩一次（占位臂 sink 名，等长），
    # 每臂仅做 sink 名替换，消除逐臂重复 collect/reparse 的 Python 成本。
    # 生成期 re-parse 预检（与单文件 run_coverage 同款）：插桩产物不合法 =
    # 插桩器 bug → 逐文件隔离，拒绝落盘跑假数据。
    from gdtoolkit.parser import parser as _gd_parser

    pre_uuid = make_run_uuid()
    inst_base: dict[str, tuple[str, list[int], str]] = {}
    for rel, item in prepared.items():
        sink0 = batch_sink_name(pre_uuid, rel)
        inst0, manifest = _instrument(item["src"], sink0, inline=True)
        if manifest:
            try:
                _gd_parser.parse(inst0)
            except Exception as e:  # noqa: BLE001 — 逐文件隔离，不阻断全集
                unmeasured.append({"file": rel,
                                   "reason": f"instrument_parse_error:{e}"})
                continue
        inst_base[rel] = (inst0, manifest, sink0)

    def _arm(name: str, acceptance: dict | None = None) -> dict:
        """一条批式臂：复用全集插桩（臂专属 sink 名替换）→ 1 次 GUT →
        逐文件 sink 读回 → bundle → 恢复/清理。零可执行行文件进显式
        vacuous 车道（§SPEC-042），不再误报 no_probe_fired。"""
        run_uuid = make_run_uuid()
        backup_dir = _BACKUP_BASE / run_uuid
        backup_dir.mkdir(parents=True)
        started = {"run_uuid": run_uuid, "project_root": str(root),
                   "files": {}}
        sink_by_rel: dict[str, str] = {}
        manifest_by_rel: dict[str, list[int]] = {}
        instrumented_files: dict[str, str] = {}
        vacuous_rels: list[str] = []
        for rel, item in prepared.items():
            if rel not in inst_base:
                continue  # 插桩失败已隔离进 unmeasured
            inst0, manifest, sink0 = inst_base[rel]
            if not manifest:
                vacuous_rels.append(rel)
                continue
            sink = batch_sink_name(run_uuid, rel)
            inst = inst0.replace(sink0, sink)
            if inst.count(sink) != inst0.count(sink0):
                raise CanonicalError(
                    f"sink substitution mismatch for {rel}: "
                    f"{inst0.count(sink0)} -> {inst.count(sink)}")
            started["files"][item["abs"]] = item["src"]
            sink_by_rel[rel] = sink
            manifest_by_rel[rel] = manifest
            instrumented_files[item["abs"]] = inst
        (backup_dir / "started.json").write_text(
            json.dumps(started, ensure_ascii=False), encoding="utf-8")
        gut_ok = True
        run_error = False
        timed_out = False
        output = ""
        wall_s = 0.0
        try:
            for abs_path, inst in instrumented_files.items():
                Path(abs_path).write_text(inst, encoding="utf-8")
            gut_args: list[str] = []
            if acceptance is not None:
                suite_rel = Path(acceptance["suite_dir"]).relative_to(root)
                gut_args.append(
                    "-gdir=res://" + suite_rel.as_posix() + "/generated")
            t_gut = time.monotonic()
            try:
                r, wall_s = _run_one_gut(root, gut_args, timeout_s)
                output = (r.stdout or "") + "\n" + (r.stderr or "")
                # rc≠0 ∧ 无 GUT 测试流证据 = 启动失败（run_error）；普通
                # 测试失败仍产出有效命中（探针照常点火）
                if r.returncode != 0 and _looks_like_godot_launch_failure(
                        output):
                    gut_ok = False
                    run_error = True
            except CanonicalError as e:
                # 超时：墙钟如实记录（超时也是门量证据），哨兵记 timeout；
                # 保留子进程部分输出尾（诊断挂死阶段，不改变 invalid 判定）
                gut_ok = False
                run_error = True
                timed_out = True
                wall_s = time.monotonic() - t_gut
                partial_out = ""
                partial_err = ""
                if getattr(e, "__cause__", None) is not None:
                    te = e.__cause__
                    partial_out = getattr(te, "stdout", "") or ""
                    partial_err = getattr(te, "stderr", "") or ""
                output = str(e) + "\n" + (partial_out + partial_err)[-2000:]
        finally:
            for abs_path, original in started["files"].items():
                Path(abs_path).write_text(original, encoding="utf-8")
            shutil.rmtree(backup_dir, ignore_errors=True)

        reconciliation = None
        arm_valid = not run_error
        invalid_reason = ("timeout" if timed_out
                          else ("run_error" if run_error else None))
        if acceptance is not None and not run_error:
            # §SPEC-035 对账五项：任一 false → 臂 invalid。run_error（含
            # 超时）时运行流证据不可信，对账不产生误导性记录，直接落
            # run_error；私有目录的清理不再依赖该分支（超时同样零残留）。
            reconciliation = reconcile(
                root, acceptance["suite_dir"], acceptance["keys"], output,
                replay_ok=True,
                main_snapshot=acceptance["snapshot"])
            if not reconciliation["ok"]:
                arm_valid = False
                invalid_reason = "reconciliation_failed"
        if acceptance is not None:
            # 超时/run_error 臂同样清理私有目录——canonical 零残留不分区
            # 成败（§SPEC-041 同款：残留即泄漏，首轮 R1 冒烟实证泄漏）。
            _cleanup_suite_dir(Path(acceptance["suite_dir"]))

        bundles: dict[str, dict] = {}
        for rel, sink in sink_by_rel.items():
            item = prepared[rel]
            hits, probes_fired = _read_hits(str(root), sink,
                                            manifest_by_rel[rel])
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
                record["sink_path"] = str(evidence_dir / sink)
                write_run_record(evidence_dir, record, name_suffix=suffix)
                write_bundle(evidence_dir, bundle, name_suffix=suffix)
                update_run_record(evidence_dir, run_uuid, name_suffix=suffix,
                                  status="bundle_written")
            clear_sink(None, str(root), sink)
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
        return {
            "arm": name, "run_uuid": run_uuid, "wall_s": round(wall_s, 3),
            "gut_ok": gut_ok, "timed_out": timed_out,
            "state": ("valid" if arm_valid else "invalid"),
            "invalid_reason": invalid_reason,
            "bundles": len(bundles), "bundles_data": bundles,
            "reconciliation": reconciliation,
        }

    # 臂 gut_full：.gutconfig 面（无 -gdir——GUT 缺省读配置，lab 实测锚定）
    arm_a = _arm("gut_full")

    # 臂 acceptance：私有 suite 编译/重放 → 重新插桩（新 run_uuid+sink 名）
    keys = list(acceptance_keys) if acceptance_keys else [
        f["key"] for f in recipe["acceptance_face"]["features"]]
    snapshot = _snapshot_main(root)
    suite_dir: Path | None = None
    try:
        suite_dir = create_suite_dir(root)
        compile_keys_to_suite(root, keys, suite_dir)
        for k in keys:
            replay_check(root, k, suite_dir=suite_dir)
        arm_b = _arm("acceptance", acceptance={
            "suite_dir": suite_dir, "keys": keys, "snapshot": snapshot})
    except (SuiteError, AcceptRunError) as e:
        if suite_dir is not None:
            _cleanup_suite_dir(suite_dir)
        arm_b = {"arm": "acceptance", "run_uuid": None, "wall_s": 0.0,
                 "gut_ok": False, "timed_out": False, "state": "invalid",
                 "invalid_reason": f"suite_error:{e}", "bundles": 0,
                 "bundles_data": {}, "reconciliation": None}

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
    return {
        "report_version": REPORT_VERSION,
        "project_root": str(root),
        "recipe_hash": recipe_hash,
        "arms": [
            {k: v for k, v in arm.items() if k != "bundles_data"}
            for arm in (arm_a, arm_b)
        ],
        "unions": unions,
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
        "unmeasured": unmeasured,
        "recovered_stale_backups": recovered,
        "performance": perf,
        "certification": dict(CERTIFICATION),
        "evidence_level": "exploratory",
        "diagnostic_only": True,
        "dynamic_cache": False,
        "ok": union_valid,
    }


def _performance(root: Path, probe_keys: list[str] | None,
                 timeout_s: int) -> dict:
    """W4 acceptance 成本探针（缺省 aidomain_integration）——私有目录管线
    实测「编译 + 执行」墙钟，写入 performance.acceptance_probe。"""
    from ..accept.suite import run_acceptance_probe
    keys = probe_keys if probe_keys else ["aidomain_integration"]
    try:
        probe = run_acceptance_probe(root, keys, timeout_s=timeout_s)
    except (SuiteError, AcceptRunError) as e:
        probe = {"probe_version": "acceptance-probe/1", "keys": keys,
                 "suite_ok": False, "error": str(e)}
    return {"acceptance_probe": probe}
