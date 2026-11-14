"""SEE-1581 方向① — attribution 映射增量重建（探索轮）。

按变更分类计算失效面并重跑最小测试集，产出带 provenance 的增量映射
产物（schema 与全量归属产物同形，另附 `increment` 块）：

- 只改测试脚本 → 重跑该脚本、整体替换其全部边（test→file / test→function
  / 脚本级）；
- 改生产文件 → 失效面 = 触达该文件的测试（fn 边反查）∪ 静态索引直连
  消费者（gdself depth==1），重跑候选脚本并整体替换其边；
- 新增生产文件 / recipe_hash 失配 / 校准到期 / 重跑面过大 → 回退全脸
  （内部转调 run_canonical，provenance.mode 标注原因）；
- 删除测试脚本 → 仅删边不重跑；改名 = 旧脚本删边 + 新脚本重跑。

如实边界：车道事件计数（hit_events/rates/phases/hotspots）不做增量合并，
增量产物标记 `event_counts_stale: true`（结构统计 tests/files/pairs/
function_pairs 由合并后的边精确重算）。行为耦合超出圈定面、单脚本运行与
全脸运行的状态差异，都会体现为增量 vs 全量的边差分——差异逐边披露，
不用不一致的数据冒充新鲜（`diff_artifacts` 是差分的唯一出处）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from .increment_runner import (
    execute_rerun_scripts,
    prepare_attr_templates,
)
from .orchestrator import (
    ATTRIBUTION_SCHEMA,
    _inject_toolchain,
    _read_attr_merged,
    _read_registries,
    build_attribution_map,
    make_run_uuid,
    run_canonical,
)

DEFAULT_RERUN_FULL_FACE_RATIO = 0.5
_FWD_KEYS = ("test_to_files", "test_to_functions",
             "script_to_files", "script_to_functions")


class IncrementError(Exception):
    """增量入参/基线违例（CLI 转领域 JSON）。"""


def load_baseline(path: str | Path) -> dict:
    try:
        baseline = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise IncrementError(f"baseline load failed: {e}") from e
    if baseline.get("schema_version") != ATTRIBUTION_SCHEMA:
        raise IncrementError(
            f"baseline schema {baseline.get('schema_version')!r} != "
            f"{ATTRIBUTION_SCHEMA!r}")
    arm = (baseline.get("arms") or {}).get("gut_full")
    if not isinstance(arm, dict) or "test_to_files" not in arm:
        raise IncrementError("baseline missing arms.gut_full edge maps")
    return baseline


def _script_name_of(identity: str) -> str:
    """测试身份 `test_x.gd::test_name` → 脚本名；脚本级身份本就是脚本名。"""
    return identity.split("::", 1)[0]


def _baseline_script_names(baseline: dict) -> set[str]:
    arm = baseline["arms"]["gut_full"]
    names = {_script_name_of(i) for i in (arm.get("test_to_files") or {})}
    names |= set(arm.get("script_to_files") or {})
    return names


def _fid_file_of(fid: str) -> str:
    return fid.split("::", 1)[0]


def classify_changes(root: Path, changed: list[str],
                     baseline: dict) -> dict[str, list[str]]:
    """变更分类（各类返回相对 root 的路径列表）。

    - test：`tests/` 下的 test_*.gd；存在且在基线=changed、存在不在基线
      =new、不在盘上且在基线=removed；
    - prod：目标面内 .gd 且有基线边（file 边或 fn 边前缀）=changed，
      无基线边=new（触发全脸回退——不猜失效面）；
    - 其余路径拒绝（unknown，fail-loud 不静默）。"""
    arm = baseline["arms"]["gut_full"]
    edge_files = (set(arm.get("file_to_tests") or {})
                  | set(arm.get("file_to_scripts") or {}))
    known_fids = {fid for ids in (arm.get("test_to_functions") or {}).values()
                  for fid in ids}
    known_fids |= {fid for ids in (arm.get("script_to_functions") or {})
                   .values() for fid in ids}
    test_identities = set(arm.get("test_to_functions") or {}) | set(
        arm.get("script_to_functions") or {})
    out: dict[str, list[str]] = {"test_changed": [], "test_new": [],
                                 "test_removed": [], "prod_changed": [],
                                 "prod_new": [], "unknown": []}
    for raw in changed:
        verdict = _classify_one(root, raw, edge_files, known_fids,
                                test_identities)
        out[verdict[0]].append(verdict[1])
    return out


def _classify_one(root: Path, raw: str, edge_files: set[str],
                  known_fids: set[str],
                  test_identities: set[str]) -> tuple[str, str]:
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    try:
        rel = str(p.relative_to(root))
    except ValueError:
        return "unknown", raw
    name = p.name
    if rel.startswith("tests/") and name.startswith("test_"):
        known = any(_script_name_of(i) == name for i in test_identities)
        if p.is_file():
            return ("test_changed" if known else "test_new"), rel
        if known:
            return "test_removed", rel
        return "unknown", raw
    if p.suffix == ".gd":
        has_edges = rel in edge_files or any(
            _fid_file_of(fid) == rel for fid in known_fids)
        if p.is_file() and has_edges:
            return "prod_changed", rel
        if p.is_file():
            return "prod_new", rel
    return "unknown", raw


def _static_candidates(root: Path, rel: str,
                       index: dict | None) -> set[str]:
    from ..mutation.gdself import build_index, query_consumers
    idx = index if index is not None else build_index(str(root))
    out = query_consumers(str(root), "res://" + rel,
                          Path(root) / "tests", index=idx)
    names = set()
    for c in out["consumers"]:
        if not c["res"].startswith("res://tests/") or c.get("depth") != 1:
            continue
        names.add(c["res"].rsplit("/", 1)[-1])
    return names


def compute_invalidation(root: Path, baseline: dict,
                         classes: dict[str, list[str]],
                         index: dict | None) -> dict:
    """失效面 → {rerun, dropped, sources}（sources 记录每个候选的来源）。"""
    arm = baseline["arms"]["gut_full"]
    rerun: set[str] = set()
    dropped: set[str] = set()
    sources: dict[str, list[str]] = {}
    fid_to_tests: dict[str, set[str]] = {}
    for identity, fids in (arm.get("test_to_functions") or {}).items():
        for fid in fids:
            fid_to_tests.setdefault(_fid_file_of(fid), set()).add(
                _script_name_of(identity))
    explicit_rerun: set[str] = set()
    for rel in classes["test_changed"] + classes["test_new"]:
        name = Path(rel).name
        rerun.add(name)
        explicit_rerun.add(name)
        sources.setdefault(name, []).append(f"test_changed:{rel}")
    for rel in classes["test_removed"]:
        dropped.add(Path(rel).name)
        sources.setdefault(Path(rel).name, []).append(f"test_removed:{rel}")
    for rel in classes["prod_changed"]:
        touch = set(fid_to_tests.get(rel, set()))
        for name in touch:
            sources.setdefault(name, []).append(f"fn_edges:{rel}")
        rerun |= touch
        static = _static_candidates(root, rel, index)
        for name in static:
            sources.setdefault(name, []).append(f"static:{rel}")
        rerun |= static
    return {"rerun": rerun, "dropped": dropped, "sources": sources,
            "explicit_rerun": explicit_rerun}


def _merge_arm(arm: dict, fresh: dict, dropped: set[str]) -> dict:
    """按脚本名替换/删边合并一张臂级映射；结构性统计精确重算。"""
    replace_names = {_script_name_of(i) for i in
                     (fresh.get("test_to_files") or {})}
    replace_names |= set(fresh.get("script_to_files") or {})

    def _merged(key: str, name_of) -> dict:
        base = {}
        for identity, value in (arm.get(key) or {}).items():
            if name_of(identity) in dropped or name_of(identity) in replace_names:
                continue
            base[identity] = list(value)
        for identity, value in (fresh.get(key) or {}).items():
            base[identity] = list(value)
        return base

    test_to_functions = _merged("test_to_functions",
                                lambda i: i.split("::", 1)[0])
    test_to_files = _merged("test_to_files", lambda i: i.split("::", 1)[0])
    script_to_files = _merged("script_to_files", lambda i: i)
    script_to_functions = _merged("script_to_functions", lambda i: i)
    file_to_tests = _invert(test_to_files)
    file_to_scripts = _invert(script_to_files)
    return {
        "tests": len(test_to_files),
        "scripts": len(script_to_files),
        "files": len(file_to_tests),
        "pairs": sum(len(v) for v in test_to_files.values()),
        "script_pairs": sum(len(v) for v in script_to_files.values()),
        "function_pairs": sum(len(v) for v in test_to_functions.values()),
        "test_to_files": {k: sorted(v)
                          for k, v in sorted(test_to_files.items())},
        "test_to_functions": {k: sorted(v)
                              for k, v in sorted(test_to_functions.items())},
        "script_to_files": {k: sorted(v)
                            for k, v in sorted(script_to_files.items())},
        "script_to_functions": {k: sorted(v)
                                for k, v in sorted(script_to_functions.items())},
        "file_to_tests": {k: sorted(v)
                          for k, v in sorted(file_to_tests.items())},
        "file_to_scripts": {k: sorted(v)
                            for k, v in sorted(file_to_scripts.items())},
    }


def _invert(m: dict) -> dict[str, list[str]]:
    inv: dict[str, set[str]] = {}
    for k, vals in m.items():
        for v in vals:
            inv.setdefault(v, set()).add(k)
    return {k: sorted(v) for k, v in sorted(inv.items())}


def run_attribution_increment(project_root: str | Path,
                              target_files: list[str], recipe: dict,
                              baseline_path: str | Path,
                              changed_files: list[str],
                              out_path: str | Path,
                              *, timeout_s: int = 180,
                              attribution_causal: bool = False,
                              calibration_every: int = 5,
                              rerun_full_face_ratio: float
                              = DEFAULT_RERUN_FULL_FACE_RATIO) -> dict:
    """增量重建主入口；返回增量报告（含产物落盘路径与 provenance）。"""
    t0 = time.monotonic()
    root = Path(project_root).resolve()
    baseline = load_baseline(baseline_path)
    recipe = _inject_toolchain(root, dict(recipe))
    prev = baseline.get("increment") or {}
    since_full = int(prev.get("increments_since_full") or 0)
    hash_ok = (baseline.get("report_recipe_hash")
               == recipe["recipe_hash"])
    classes = classify_changes(root, changed_files, baseline)
    invalidation = {"rerun": set(), "dropped": set(), "sources": {}}
    if hash_ok and not classes["prod_new"]:
        from ..mutation.gdself import build_index
        invalidation = compute_invalidation(
            root, baseline, classes, build_index(str(root)))
    known = _baseline_script_names(baseline)
    # 分类来源（改/增测试脚本）不过 known 闸——新脚本天然不在基线；
    # 静态候选（可能指向基线外的脚本）必须过滤，防止凭空圈定
    explicit = set(invalidation.get("explicit_rerun") or ())
    rerun = explicit | {n for n in invalidation["rerun"] if n in known}
    ratio = (len(rerun & known) / len(known)) if known else 0.0
    reason = _fallback_reason(hash_ok, classes, since_full,
                              calibration_every, ratio,
                              rerun_full_face_ratio)
    if reason != "incremental":
        return _fallback_full_face(root, target_files, recipe, out_path,
                                   classes, reason, attribution_causal, timeout_s, t0)
    arm_exec_s, fresh, executions = _run_increment_arm(
        root, target_files, recipe, rerun, timeout_s, attribution_causal)
    merged = _merge_arm(baseline["arms"]["gut_full"], fresh,
                        invalidation["dropped"])
    artifact = _build_increment_artifact(
        baseline, merged, recipe, root, classes,
        sorted(rerun), invalidation, since_full, arm_exec_s,
        executions)
    out_p = Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(artifact, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    return {
        "mode": "incremental",
        "artifact": str(out_p),
        "changed": classes,
        "rerun_scripts": sorted(rerun),
        "dropped": sorted(invalidation["dropped"]),
        "rerun_sources": {k: v for k, v in
                          invalidation["sources"].items()
                          if k in set(rerun)},
        "merged_stats": {k: merged[k] for k in
                         ("tests", "scripts", "files", "pairs",
                          "function_pairs")},
        "event_counts_stale": True,
        "wall_s": round(time.monotonic() - t0, 3),
        "arm_exec_s": arm_exec_s,
    }


def _fallback_reason(hash_ok: bool, classes: dict, since_full: int,
                     calibration_every: int, ratio: float,
                     ratio_cap: float) -> str:
    if not hash_ok:
        return "hash_mismatch_fallback"
    if classes["prod_new"]:
        return "new_prod_file_fallback"
    if since_full >= calibration_every:
        return "calibration_due_fallback"
    if ratio > ratio_cap:
        return "rerun_surface_too_large_fallback"
    return "incremental"


def _run_increment_arm(root: Path, target_files: list[str], recipe: dict,
                       rerun: set[str], timeout_s: int,
                       causal: bool) -> tuple[float, dict, list[dict]]:
    """在插桩副本上只跑 rerun 脚本，读回归属证据并聚合成臂级映射。
    返回 (读回+聚合秒数, forward 边集, 逐脚本执行结果)。"""
    templates = prepare_attr_templates(root, target_files, recipe, causal)
    gut_dirs = list(recipe["gut_face"].get("dirs") or [])
    run_uuid = make_run_uuid()
    results, xdg_homes = execute_rerun_scripts(
        root, gut_dirs, sorted(rerun), timeout_s, templates, causal,
        run_uuid)
    t0 = time.monotonic()
    attr_by_rel = {
        rel: _read_attr_merged(root, run_uuid, rel, xdg_homes)
        for rel in templates}
    registries = _read_registries(root, run_uuid, xdg_homes)
    arm_map = build_attribution_map(
        "increment", attr_by_rel, registries,
        probe_lines_by_rel={rel: it["probe_lines"]
                            for rel, it in templates.items()},
        fid_by_probe_by_rel={rel: it["attr_fid_by_probe"]
                             for rel, it in templates.items()})
    for xdg in xdg_homes:
        shutil.rmtree(xdg, ignore_errors=True)
    return round(time.monotonic() - t0, 3), _group_fresh_edges(arm_map), results


def _group_fresh_edges(inc_arm: dict) -> dict:
    out = {}
    for key in _FWD_KEYS:
        out[key] = {k: list(v)
                    for k, v in (inc_arm.get(key) or {}).items()}
    return out


def _build_increment_artifact(baseline: dict, merged: dict, recipe: dict,
                              root: Path, classes: dict, rerun_list: list[str],
                              invalidation: dict, since_full: int,
                              arm_exec_s: float,
                              executions: list[dict] | None = None) -> dict:
    return {
        "schema_version": ATTRIBUTION_SCHEMA,
        "report_recipe_hash": recipe["recipe_hash"],
        "recipe_hash_pristine": baseline.get("recipe_hash_pristine"),
        "project_root": str(root),
        "identity": baseline.get("identity"),
        "causal": baseline.get("causal"),
        "file_sha256": _merged_file_sha(root, baseline, classes),
        "target_files": list(baseline.get("target_files")
                             or sorted((baseline.get("file_sha256") or {}))),
        "tests_face": _merged_tests_face(root, baseline, classes),
        "arms": {"gut_full": merged},
        "arms_state": dict(baseline.get("arms_state") or {}),
        "notes": list(baseline.get("notes") or []),
        "increment": {
            "mode": "incremental",
            "base_recipe_hash": baseline.get("report_recipe_hash"),
            "changed_files": classes,
            "rerun_scripts": rerun_list,
            "dropped_scripts": sorted(invalidation["dropped"]),
            "rerun_sources": invalidation["sources"],
            "increments_since_full": since_full + 1,
            "calibration_policy": ("full face every N increments or on "
                                   "hash mismatch / new prod file / "
                                   "oversized rerun surface"),
            "event_counts_stale": True,
            "arm_exec_s": arm_exec_s,
            "rerun_executions": executions or [],
        },
    }


def _merged_tests_face(root: Path, baseline: dict,
                       classes: dict) -> dict:
    """测试面内容绑定继承 + 变更/新增测试脚本按当前内容重算、删除剔除。"""
    face = dict(baseline.get("tests_face") or {})
    for rel in classes.get("test_changed", []) + classes.get("test_new", []):
        p = Path(root) / rel
        if p.is_file():
            face[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    for rel in classes.get("test_removed", []):
        face.pop(rel, None)
    return face


def _merged_file_sha(root: Path, baseline: dict,
                     classes: dict) -> dict:
    """基线文件哈希继承 + 变更生产文件按当前内容重算（增量新鲜绑定）。"""
    shas = dict(baseline.get("file_sha256") or {})
    for rel in classes.get("prod_changed", []) + classes.get("prod_new", []):
        p = Path(root) / rel
        if p.is_file():
            shas[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return shas


def _fallback_full_face(root: Path, target_files: list[str], recipe: dict,
                        out_path: str | Path, classes: dict, reason: str,
                        causal: bool, timeout_s: int, t0: float) -> dict:
    report = run_canonical(root, target_files, recipe,
                           timeout_s=timeout_s, test_attribution=True,
                           attribution_causal=causal,
                           attribution_out=str(out_path))
    artifact_path = Path(out_path)
    if artifact_path.is_file():
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact["increment"] = {
            "mode": reason,
            "base_recipe_hash": None,
            "changed_files": classes,
            "increments_since_full": 0,
            "reason": reason,
        }
        artifact_path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2),
            encoding="utf-8")
    return {
        "mode": reason,
        "artifact": str(artifact_path),
        "changed": classes,
        "canonical_ok": bool(report.get("ok")),
        "wall_s": round(time.monotonic() - t0, 3),
    }


def diff_artifacts(a_doc: dict, b_doc: dict) -> dict:
    """两张归属产物的逐边差分（test 车道 file 级与 function 级、脚本级）。

    返回每张边的 {added_count, removed_count, added_edges(截断 200),
    removed_edges(截断 200)}——added = 仅 b 有，removed = 仅 a 有。"""
    def edges(doc: dict, key: str) -> dict[str, set[str]]:
        arm = (doc.get("arms") or {}).get("gut_full") or {}
        return {k: set(v) for k, v in (arm.get(key) or {}).items()}

    out = {}
    for key in _FWD_KEYS:
        a, b = edges(a_doc, key), edges(b_doc, key)
        added = sorted(f"{k} -> {x}" for k, v in b.items()
                       for x in v - a.get(k, set()))
        removed = sorted(f"{k} -> {x}" for k, v in a.items()
                         for x in v - b.get(k, set()))
        out[key] = {
            "added_count": len(added), "removed_count": len(removed),
            "added_edges": added[:200], "removed_edges": removed[:200],
        }
    return out


