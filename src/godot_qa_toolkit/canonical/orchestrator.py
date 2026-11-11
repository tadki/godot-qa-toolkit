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


def run_canonical(project_root: str | Path, target_files: list[str],
                  recipe: dict, *, acceptance_keys: list[str] | None = None,
                  probe_keys: list[str] | None = None, timeout_s: int = 180,
                  evidence_out: str | None = None) -> dict:
    """canonical 双源批式编排，产出 canonical-report/1。"""
    root = Path(project_root).resolve()
    ok, reason = verify_recipe(recipe)
    if not ok:
        raise CanonicalError(f"recipe invalid: {reason}")
    if not target_files:
        raise CanonicalError("no target files given")
    recipe_hash = recipe["recipe_hash"]
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

    def _arm(name: str, acceptance: dict | None = None) -> dict:
        """一条批式臂：全集插桩（臂专属 sink 名）→ 1 次 GUT → 逐文件
        sink 读回 → bundle → 恢复/清理。"""
        run_uuid = make_run_uuid()
        backup_dir = _BACKUP_BASE / run_uuid
        backup_dir.mkdir(parents=True)
        started = {"run_uuid": run_uuid, "project_root": str(root),
                   "files": {}}
        sink_by_rel: dict[str, str] = {}
        manifest_by_rel: dict[str, list[int]] = {}
        instrumented_files: dict[str, str] = {}
        for rel, item in prepared.items():
            sink = batch_sink_name(run_uuid, rel)
            inst, manifest = _instrument(item["src"], sink)
            # 生成期 re-parse 预检（与单文件 run_coverage 同款）：插桩产物
            # 不合法 = 插桩器 bug → 逐文件隔离，拒绝落盘跑假数据
            try:
                from gdtoolkit.parser import parser as _gd_parser
                _gd_parser.parse(inst)
            except Exception as e:  # noqa: BLE001
                unmeasured.append({"file": rel,
                                   "reason": f"instrument_parse_error:{e}"})
                continue
            started["files"][item["abs"]] = item["src"]
            sink_by_rel[rel] = sink
            manifest_by_rel[rel] = manifest
            instrumented_files[item["abs"]] = inst
        (backup_dir / "started.json").write_text(
            json.dumps(started, ensure_ascii=False), encoding="utf-8")
        gut_ok = True
        run_error = False
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
                gut_ok = False
                run_error = True
                output = str(e)
        finally:
            for abs_path, original in started["files"].items():
                Path(abs_path).write_text(original, encoding="utf-8")
            shutil.rmtree(backup_dir, ignore_errors=True)

        reconciliation = None
        arm_valid = not run_error
        invalid_reason = "run_error" if run_error else None
        if acceptance is not None:
            # §SPEC-035 对账五项：任一 false → 臂 invalid；对账后清理私有
            # 目录（canonical 零残留，不保留现场）
            reconciliation = reconcile(
                root, acceptance["suite_dir"], acceptance["keys"], output,
                replay_ok=not run_error,
                main_snapshot=acceptance["snapshot"])
            if not reconciliation["ok"]:
                arm_valid = False
                invalid_reason = "reconciliation_failed"
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
                gut_ok=gut_ok, run_error=run_error, timeout=False,
                zero_tests=False, vacuous=False, arm=name)
            bundles[rel] = bundle
            if evidence_dir is not None:
                record = build_run_record(
                    run_uuid=run_uuid, file_path="res://" + rel,
                    gd_source=item["src"], tests_glob=None,
                    probe_manifest=item["probe_manifest"], arm=name)
                record["sink_path"] = str(evidence_dir / sink)
                write_run_record(evidence_dir, record)
                write_bundle(evidence_dir, bundle)
                update_run_record(evidence_dir, run_uuid,
                                  status="bundle_written")
            clear_sink(None, str(root), sink)
        return {
            "arm": name, "run_uuid": run_uuid, "wall_s": round(wall_s, 3),
            "gut_ok": gut_ok, "state": ("valid" if arm_valid else "invalid"),
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
                 "gut_ok": False, "state": "invalid",
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
        },
        "unmeasured": unmeasured,
        "recovered_stale_backups": recovered,
        "performance": _performance(root, probe_keys, timeout_s),
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
