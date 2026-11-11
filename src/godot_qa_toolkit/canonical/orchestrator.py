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
import re
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

# §SPEC-046：flag 形态的统一落盘由注入的 finalizer autoload 承担——GUT
# 退出（SceneTree 拆卸 _exit_tree）时逐 sink 排空标记数组（每 sink 一次
# open+写循环），落盘次数 = 文件数、与命中事件数无关。仅存在于插桩窗口，
# finally 恢复零残留。
_AUTOLOAD_NAME = "GqtCovFlusher"
_FLUSHER_RELPATH = "addons/gqt_cov_flusher.gd"
_FLUSHER_GD = """extends Node
# qa-toolkit canonical flag-mode probe finalizer (auto-injected, never in
# production). At tree exit, drains every registered per-file flag array to
# its sink: one open + one write loop per file — sink I/O is bounded by file
# count and never grows with hit event count (§SPEC-046).
func _exit_tree() -> void:
\tif not Engine.has_meta("gqt_cov_marks"):
\t\treturn
\tvar d: Dictionary = Engine.get_meta("gqt_cov_marks")
\tfor sink_name in d.keys():
\t\tvar arr: Array = d[sink_name]
\t\tvar f = FileAccess.open("user://" + sink_name, FileAccess.READ_WRITE)
\t\tif f == null:
\t\t\tf = FileAccess.open("user://" + sink_name, FileAccess.WRITE)
\t\telse:
\t\t\tf.seek_end()
\t\tif f == null:
\t\t\tpush_error("gqt coverage probe sink open failed: user://" + sink_name)
\t\t\tcontinue
\t\tfor i in arr.size():
\t\t\tif arr[i] == 1:
\t\t\t\tf.store_line(str(i))
\t\tf.close()
"""


def _patch_autoload(text: str, entry: str) -> str:
    """project.godot 的 [autoload] 段首插入一行 autoload 注册；段缺失则
    追加新段。只动文本结构，不改既有条目。

    置首而非置尾：autoload 退出顺序与加载顺序相反——finalizer 必须最后
    退出，才能排空其他 autoload 在各自 _exit_tree 里产生的关停期命中
    （R5 实证：置尾时 _exit_tree 覆盖整段丢失）。"""
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == "[autoload]":
            start = i
            break
    if start is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + f"\n[autoload]\n{entry}\n"
    lines.insert(start + 1, entry + "\n")
    return "".join(lines)




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
        pg = record.get("project_godot")
        if pg is not None:
            (project_root / "project.godot").write_text(pg, encoding="utf-8")
        for created in record.get("created", []):
            Path(created).unlink(missing_ok=True)
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

    # §SPEC-044 插桩复用：全集 flags 插桩一次（占位臂 sink 名，等长），
    # 每臂仅做 sink 名替换，消除逐臂重复 collect/reparse 的 Python 成本。
    # 生成期 re-parse 预检（与单文件 run_coverage 同款）：插桩产物不合法 =
    # 插桩器 bug → 逐文件隔离，拒绝落盘跑假数据。
    from gdtoolkit.parser import parser as _gd_parser

    pre_uuid = make_run_uuid()
    inst_base: dict[str, tuple[str, list[int], str]] = {}
    for rel, item in prepared.items():
        sink0 = batch_sink_name(pre_uuid, rel)
        inst0, manifest = _instrument(item["src"], sink0, flags=True)
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
        pg_path = root / "project.godot"
        if not pg_path.is_file():
            raise CanonicalError(
                "cannot inject finalizer autoload: project.godot missing")
        started["project_godot"] = pg_path.read_text(encoding="utf-8")
        started["created"] = [str(root / _FLUSHER_RELPATH)]
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
            pg_path.write_text(
                _patch_autoload(
                    started["project_godot"],
                    f'{_AUTOLOAD_NAME}="*res://{_FLUSHER_RELPATH}"'),
                encoding="utf-8")
            flusher_abs = root / _FLUSHER_RELPATH
            flusher_abs.parent.mkdir(parents=True, exist_ok=True)
            flusher_abs.write_text(_FLUSHER_GD, encoding="utf-8")
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
            if "project_godot" in started:
                pg_path.write_text(started["project_godot"], encoding="utf-8")
            for created in started.get("created", []):
                Path(created).unlink(missing_ok=True)
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


def _safe_log_label(label: str) -> str:
    """日志文件名消毒：组跑（"k1+k2+..."）会超文件名上限。"""
    if len(label) <= 48:
        return label
    return hashlib.sha1(label.encode()).hexdigest()[:12]


DIAGNOSTIC_VERSION = "canonical-diagnostic/1"

# §SPEC-048：wait 步骤静态识别（feature 文本）——duration 等待与
# `wait until ...` 条件等待都属于 player_session 的墙钟竞争路径。
_WAIT_STEP_RE = re.compile(r'agent (?:执行 "wait[^"]*"|等待[^\n]*)')


def run_canonical_diagnose(project_root: str | Path,
                           target_files: list[str], recipe: dict,
                           feature_keys: list[str], *,
                           timeout_s: int = 480,
                           progress_out: str | None = None) -> dict:
    """§SPEC-048 diagnostic-only acceptance 计时模式。

    在相同 flags 插桩（默认全 149 文件）下逐 feature 执行：同一编译套件
    先跑未插桩基线腿、再跑 flags 插桩腿，记录绝对墙钟、GUT 测试汇总、
    wait 步骤数与墙钟超限归因。不改 KOL、不排除 feature、不放宽门。
    每 feature 完成即写 progress_out（长跑分段留证）。"""
    root = Path(project_root).resolve()
    ok, reason = verify_recipe(recipe)
    if not ok:
        raise CanonicalError(f"recipe invalid: {reason}")
    feature_by_key = {f["key"]: f
                      for f in recipe["acceptance_face"]["features"]}
    unknown = [k for spec in feature_keys for k in spec.split("+")
               if k and k not in feature_by_key]
    if unknown:
        raise CanonicalError(f"unknown acceptance features: {unknown}")
    sources: dict[str, str] = {}
    for path_str in target_files:
        p = Path(path_str)
        try:
            sources[str(p.resolve())] = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise CanonicalError(f"target unreadable: {p}: {e}") from e
    report = {
        "report_version": DIAGNOSTIC_VERSION,
        "project_root": str(root),
        "instrumentation": {"files": len(sources), "mode": "flags",
                            "recipe_hash": recipe.get("recipe_hash")},
        "features": [],
        "ok": True,
    }

    def _dump() -> None:
        if progress_out:
            Path(progress_out).write_text(
                json.dumps(report, ensure_ascii=False, indent=1),
                encoding="utf-8")

    _dump()
    gut_re = {
        "scripts": re.compile(r"^Scripts\s+(\d+)$", re.M),
        "tests": re.compile(r"^Tests\s+(\d+)$", re.M),
        "passing": re.compile(r"^Passing Tests\s+(\d+)$", re.M),
        "failing": re.compile(r"^Failing Tests\s+(\d+)$", re.M),
        "risky": re.compile(r"^Risky/Pending\s+(\d+)$", re.M),
        "gut_time": re.compile(r"^Time\s+([0-9.]+)s$", re.M),
    }

    def _parse_gut(output: str) -> dict:
        parsed = {}
        for name, rx in gut_re.items():
            m = rx.search(output)
            parsed[name] = (float(m.group(1)) if name == "gut_time"
                            else int(m.group(1))) if m else None
        return parsed

    for key_spec in feature_keys:
        # "+" 组合 = 多个 feature 编译进同一套件、单进程计时（跨 feature
        # 累积成本归因；单 feature 隔离跑会漏掉进程内累积效应）
        sub_keys = [k for k in key_spec.split("+") if k]
        if not sub_keys:
            continue
        feature_text = ""
        for sk in sub_keys:
            src = feature_by_key[sk].get("source", "")
            if isinstance(src, str) and src.startswith("res://"):
                fpath = root / src[len("res://"):]
                if fpath.is_file():
                    feature_text += fpath.read_text(encoding="utf-8")
        key = key_spec
        wait_commands = len(_WAIT_STEP_RE.findall(feature_text))
        entry = {"key": key, "wait_commands": wait_commands,
                 "baseline": {}, "instrumented": {}, "attribution": None}
        suite_dir: Path | None = None
        try:
            suite_dir = create_suite_dir(root)
            compile_keys_to_suite(root, sub_keys, suite_dir)
            suite_rel = suite_dir.relative_to(root).as_posix()
            gut_args = [f"-gdir=res://{suite_rel}/generated"]
            # 腿 1：未插桩基线
            t0 = time.monotonic()
            r, _ = _run_one_gut(root, gut_args, timeout_s)
            wall = time.monotonic() - t0
            out = (r.stdout or "") + "\n" + (r.stderr or "")
            entry["baseline"] = {"wall_s": round(wall, 3),
                                 "rc": r.returncode, "tests": _parse_gut(out)}
            if progress_out:
                Path(f"{progress_out}.{_safe_log_label(key)}.baseline.log"
                     ).write_text(out, encoding="utf-8")
            # 腿 2：flags 插桩（同题全量文件 + finalizer 注入）
            run_uuid = make_run_uuid()
            backup_dir = _BACKUP_BASE / run_uuid
            backup_dir.mkdir(parents=True)
            started = {"run_uuid": run_uuid, "project_root": str(root),
                       "files": {}}
            sink_by_rel: dict[str, str] = {}
            manifest_by_rel: dict[str, list[int]] = {}
            instrumented: dict[str, str] = {}
            for abs_path, src_text in sources.items():
                rel = str(Path(abs_path).relative_to(root))
                sink = batch_sink_name(run_uuid, rel)
                inst, manifest = _instrument(src_text, sink, flags=True)
                if not manifest:
                    continue
                started["files"][abs_path] = src_text
                sink_by_rel[rel] = sink
                manifest_by_rel[rel] = manifest
                instrumented[abs_path] = inst
            pg_path = root / "project.godot"
            started["project_godot"] = pg_path.read_text(encoding="utf-8")
            started["created"] = [str(root / _FLUSHER_RELPATH)]
            (backup_dir / "started.json").write_text(
                json.dumps(started, ensure_ascii=False), encoding="utf-8")
            timed_out = False
            out = ""
            rc = None
            wall = 0.0
            try:
                for abs_path, inst in instrumented.items():
                    Path(abs_path).write_text(inst, encoding="utf-8")
                pg_path.write_text(
                    _patch_autoload(
                        started["project_godot"],
                        f'{_AUTOLOAD_NAME}="*res://{_FLUSHER_RELPATH}"'),
                    encoding="utf-8")
                flusher_abs = root / _FLUSHER_RELPATH
                flusher_abs.parent.mkdir(parents=True, exist_ok=True)
                flusher_abs.write_text(_FLUSHER_GD, encoding="utf-8")
                t0 = time.monotonic()
                try:
                    r, _ = _run_one_gut(root, gut_args, timeout_s)
                    wall = time.monotonic() - t0
                    rc = r.returncode
                    out = (r.stdout or "") + "\n" + (r.stderr or "")
                except CanonicalError as e:
                    timed_out = True
                    wall = time.monotonic() - t0
                    te = getattr(e, "__cause__", None)
                    if te is not None:
                        out = (getattr(te, "stdout", "") or "") + "\n" + (
                            getattr(te, "stderr", "") or "")
            finally:
                for abs_path, original in started["files"].items():
                    Path(abs_path).write_text(original, encoding="utf-8")
                pg_path.write_text(started["project_godot"],
                                   encoding="utf-8")
                for created in started.get("created", []):
                    Path(created).unlink(missing_ok=True)
                shutil.rmtree(backup_dir, ignore_errors=True)
            probes_total = 0
            hit_files = 0
            for rel, sink in sink_by_rel.items():
                hits, fired = _read_hits(str(root), sink,
                                         manifest_by_rel[rel])
                probes_total += fired
                hit_files += 1 if fired else 0
                clear_sink(None, str(root), sink)
            if progress_out:
                Path(f"{progress_out}.{_safe_log_label(key)}.instrumented.log"
                     ).write_text(out, encoding="utf-8")
            entry["instrumented"] = {
                "wall_s": round(wall, 3), "rc": rc,
                "timed_out": timed_out, "tests": _parse_gut(out),
                "hit_files": hit_files, "probes_fired_total": probes_total,
                "output_tail": out[-1500:]}
        except (SuiteError, AcceptRunError) as e:
            entry["attribution"] = f"suite_error:{e}"
        finally:
            if suite_dir is not None:
                _cleanup_suite_dir(suite_dir)
        if entry["attribution"] is None:
            ins = entry["instrumented"]
            base_wall = entry["baseline"].get("wall_s") or 0.0
            if ins.get("timed_out"):
                entry["attribution"] = (
                    "wait_wall_dominant:instrumented capped at "
                    f"{timeout_s}s (baseline {base_wall}s, waits "
                    f"{wait_commands})")
            elif wait_commands and ins["wall_s"] > max(60.0, 20 * base_wall):
                entry["attribution"] = (f"slow_with_waits:{ins['wall_s']}s vs "
                                        f"baseline {base_wall}s")
            else:
                entry["attribution"] = (f"other:{ins['wall_s']}s vs "
                                        f"baseline {base_wall}s")
        report["features"].append(entry)
        _dump()
    return report


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
