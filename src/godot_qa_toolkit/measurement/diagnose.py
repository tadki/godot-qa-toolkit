"""SEE-1594 BTIDY（零行为拆分）：§SPEC-048 diagnostic-only gherkin
计时模式（双腿计时/进度落盘/墙钟归因）。自 orchestrator.py 原行段物理
迁移；`run_measurement_diagnose` 按「单 feature 条目构建 / 编排循环」
分解为 ≤50 行函数，逻辑逐语句等价。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from ..coverage.evidence import clear_sink, make_run_uuid
from ..coverage.runner import _instrument, _read_hits
from ..gherkin.run import GherkinRunError
from ..gherkin.addon import normalize_feature_key
from ..gherkin.suite import (
    SuiteError,
    _cleanup_suite_dir,
    compile_keys_to_suite,
    create_suite_dir,
)
from .backup import MeasurementError, _backup_base, _rel_within_root, batch_sink_name
from .crapbuild import _cause_tail
from .probe_templates import (
    _AUTOLOAD_NAME,
    _FLUSHER_GD,
    _FLUSHER_RELPATH,
    _patch_autoload,
)
from .recipe import verify_recipe
from .shadowio import _run_one_gut


def _safe_log_label(label: str) -> str:
    """日志文件名消毒：组跑（"k1+k2+..."）会超文件名上限。"""
    if len(label) <= 48:
        return label
    return hashlib.sha1(label.encode()).hexdigest()[:12]


DIAGNOSTIC_VERSION = "canonical-diagnostic/1"

# §SPEC-048：wait 步骤静态识别（feature 文本）——duration 等待与
# `wait until ...` 条件等待都属于 player_session 的墙钟竞争路径。
_WAIT_STEP_RE = re.compile(r'agent (?:执行 "wait[^"]*"|等待[^\n]*)')

# §SPEC-048：GUT 汇总行解析（诊断双腿共用的输出摘要）。
_DIAG_GUT_SUMMARY_RES = {
    "scripts": re.compile(r"^Scripts\s+(\d+)$", re.M),
    "tests": re.compile(r"^Tests\s+(\d+)$", re.M),
    "passing": re.compile(r"^Passing Tests\s+(\d+)$", re.M),
    "failing": re.compile(r"^Failing Tests\s+(\d+)$", re.M),
    "risky": re.compile(r"^Risky/Pending\s+(\d+)$", re.M),
    "gut_time": re.compile(r"^Time\s+([0-9.]+)s$", re.M),
}


def _dump_progress(report: dict, progress_out: str | None) -> None:
    """诊断进度落盘：每 feature 完成即写 progress_out（长跑分段留证）。"""
    if progress_out:
        Path(progress_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=1),
            encoding="utf-8")


def _parse_gut_summary(output: str) -> dict:
    parsed = {}
    for name, rx in _DIAG_GUT_SUMMARY_RES.items():
        m = rx.search(output)
        parsed[name] = (float(m.group(1)) if name == "gut_time"
                        else int(m.group(1))) if m else None
    return parsed


def _diagnose_prepare(recipe: dict, feature_keys: list[str],
                      target_files: list[str]) -> tuple[dict, dict[str, str]]:
    """校验 recipe 与 feature keys，读取目标源码；失败即 raise
    MeasurementError（与编排器其他入口同款 fail-loud 语义）。"""
    ok, reason = verify_recipe(recipe)
    if not ok:
        raise MeasurementError(f"recipe invalid: {reason}")
    # key 与 compile/group/reconcile 同口径归一（my-demo → my_demo）：
    # 原样比较会让 recipe 键 my_demo 拒掉 --features my-demo（§SPEC-019 R5e）。
    feature_by_key = {normalize_feature_key(f["key"]): f
                      for f in recipe["gherkin_face"]["features"]}
    unknown = [k for spec in feature_keys for k in spec.split("+")
               if k and normalize_feature_key(k) not in feature_by_key]
    if unknown:
        raise MeasurementError(f"unknown gherkin features: {unknown}")
    sources: dict[str, str] = {}
    for path_str in target_files:
        p = Path(path_str)
        try:
            sources[str(p.resolve())] = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise MeasurementError(f"target unreadable: {p}: {e}") from e
    return feature_by_key, sources


def _diagnose_feature_text(root: Path, feature_by_key: dict,
                           sub_keys: list[str]) -> str:
    feature_text = ""
    for sk in sub_keys:
        src = feature_by_key[normalize_feature_key(sk)].get("source", "")
        if isinstance(src, str) and src.startswith("res://"):
            fpath = root / src[len("res://"):]
            if fpath.is_file():
                feature_text += fpath.read_text(encoding="utf-8")
    return feature_text


def _diagnose_baseline_leg(root: Path, gut_args: list[str],
                           timeout_s: int, progress_out: str | None,
                           key: str) -> dict:
    """腿 1：未插桩基线（同题编译套件直跑），附基线腿日志落盘。"""
    t0 = time.monotonic()
    r, _ = _run_one_gut(root, gut_args, timeout_s)
    wall = time.monotonic() - t0
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    if progress_out:
        Path(f"{progress_out}.{_safe_log_label(key)}.baseline.log"
             ).write_text(out, encoding="utf-8")
    return {"wall_s": round(wall, 3), "rc": r.returncode,
            "tests": _parse_gut_summary(out)}


def _diagnose_stage_flags(root: Path, sources: dict[str, str],
                          run_uuid: str) -> tuple[Path, dict, dict[str, str],
                                                  dict[str, list[int]],
                                                  dict[str, str]]:
    """腿 2 备份与插桩：备份目录落 started.json，返回 (backup_dir,
    started, sink_by_rel, manifest_by_rel, instrumented)。零可执行行文件
    不入插桩面（vacuous 不写不还原）。"""
    backup_dir = _backup_base() / run_uuid
    backup_dir.mkdir(mode=0o700)
    started = {"run_uuid": run_uuid, "project_root": str(root),
               "files": {}}
    sink_by_rel: dict[str, str] = {}
    manifest_by_rel: dict[str, list[int]] = {}
    instrumented: dict[str, str] = {}
    for abs_path, src_text in sources.items():
        rel = _rel_within_root(abs_path, root)
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
    return backup_dir, started, sink_by_rel, manifest_by_rel, instrumented


def _diagnose_run_flags_leg(root: Path, gut_args: list[str],
                            timeout_s: int, backup_dir: Path,
                            started: dict,
                            instrumented: dict[str, str]
                            ) -> tuple[bool, str, int | None, float]:
    """写插桩文件、注入 finalizer、跑 GUT；finally 恢复原树后返回
    (timed_out, 合并输出, rc, 墙钟秒)。GUT 超时按 MeasurementError 带因
    捕获、不向上传播（归因腿仍需读 sink 残余）。"""
    timed_out = False
    out = ""
    rc = None
    wall = 0.0
    pg_path = root / "project.godot"
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
        except MeasurementError as e:
            timed_out = True
            wall = time.monotonic() - t0
            out = str(e) + "\n" + _cause_tail(getattr(e, "__cause__", None))
    finally:
        for abs_path, original in started["files"].items():
            Path(abs_path).write_text(original, encoding="utf-8")
        pg_path.write_text(started["project_godot"],
                           encoding="utf-8")
        for created in started.get("created", []):
            Path(created).unlink(missing_ok=True)
        shutil.rmtree(backup_dir, ignore_errors=True)
    return timed_out, out, rc, wall


def _diagnose_collect_hits(root: Path, sink_by_rel: dict[str, str],
                           manifest_by_rel: dict[str, list[int]]
                           ) -> tuple[int, int]:
    probes_total = 0
    hit_files = 0
    for rel, sink in sink_by_rel.items():
        hits, fired = _read_hits(str(root), sink,
                                 manifest_by_rel[rel])
        probes_total += fired
        hit_files += 1 if fired else 0
        clear_sink(None, str(root), sink)
    return probes_total, hit_files


def _diagnose_attribute(entry: dict, wait_commands: int,
                        timeout_s: int) -> None:
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
def _diagnose_feature_entry(root: Path, key_spec: str,
                            feature_by_key: dict, sources: dict[str, str],
                            timeout_s: int,
                            progress_out: str | None) -> dict:
    """单 feature 计时条目："+" 组合 = 多个 feature 编译进同一套件、
    单进程计时（跨 feature 累积成本归因；单 feature 隔离跑会漏掉进程内
    累积效应）。腿 1 未插桩基线 + 腿 2 flags 插桩，异常落 attribution。"""
    sub_keys = [k for k in key_spec.split("+") if k]
    if not sub_keys:
        return {}
    feature_text = _diagnose_feature_text(root, feature_by_key, sub_keys)
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
        entry["baseline"] = _diagnose_baseline_leg(
            root, gut_args, timeout_s, progress_out, key)
        # 腿 2：flags 插桩（同题全量文件 + finalizer 注入）
        run_uuid = make_run_uuid()
        (backup_dir, started, sink_by_rel, manifest_by_rel,
         instrumented) = _diagnose_stage_flags(root, sources, run_uuid)
        timed_out, out, rc, wall = _diagnose_run_flags_leg(
            root, gut_args, timeout_s, backup_dir, started, instrumented)
        probes_total, hit_files = _diagnose_collect_hits(
            root, sink_by_rel, manifest_by_rel)
        if progress_out:
            Path(f"{progress_out}.{_safe_log_label(key)}.instrumented.log"
                 ).write_text(out, encoding="utf-8")
        entry["instrumented"] = {
            "wall_s": round(wall, 3), "rc": rc,
            "timed_out": timed_out, "tests": _parse_gut_summary(out),
            "hit_files": hit_files, "probes_fired_total": probes_total,
            "output_tail": out[-1500:]}
    except (SuiteError, GherkinRunError) as e:
        entry["attribution"] = f"suite_error:{e}"
    finally:
        if suite_dir is not None:
            _cleanup_suite_dir(suite_dir, root)
    if entry["attribution"] is None:
        _diagnose_attribute(entry, wait_commands, timeout_s)
    return entry


def run_measurement_diagnose(project_root: str | Path,
                           target_files: list[str], recipe: dict,
                           feature_keys: list[str], *,
                           timeout_s: int = 480,
                           progress_out: str | None = None) -> dict:
    """§SPEC-048 diagnostic-only gherkin 计时模式。

    在相同 flags 插桩（默认全 149 文件）下逐 feature 执行：同一编译套件
    先跑未插桩基线腿、再跑 flags 插桩腿，记录绝对墙钟、GUT 测试汇总、
    wait 步骤数与墙钟超限归因。不改 KOL、不排除 feature、不放宽门。
    每 feature 完成即写 progress_out（长跑分段留证）。"""
    root = Path(project_root).resolve()
    feature_by_key, sources = _diagnose_prepare(recipe, feature_keys,
                                                target_files)
    report = {
        "report_version": DIAGNOSTIC_VERSION,
        "project_root": str(root),
        "instrumentation": {"files": len(sources), "mode": "flags",
                            "recipe_hash": recipe.get("recipe_hash")},
        "features": [],
        "ok": True,
    }
    _dump_progress(report, progress_out)
    for key_spec in feature_keys:
        entry = _diagnose_feature_entry(root, key_spec, feature_by_key,
                                        sources, timeout_s, progress_out)
        if not entry:
            continue
        report["features"].append(entry)
        _dump_progress(report, progress_out)
    return report
