"""SEE-1581 方向① — attribution 映射增量重建（探索轮；SEE-1587 行级化）。

按变更分类计算失效面并重跑最小测试集，产出带 provenance 的增量映射
产物（schema 与全量归属产物同形，另附 `increment` 块）：

- 只改测试脚本 → 重跑该脚本、整体替换其全部行事实（test→lines /
  script→lines）与派生边；
- 改生产文件 → 失效面 = 触达该文件的测试（行事实反查）∪ 静态索引直连
  消费者（gdself depth==1），重跑候选脚本并整体替换其行事实；
- 新增生产文件 / recipe_hash 失配 / 校准到期 / 重跑面过大 → 回退全脸
  （内部转调 run_measurement，provenance.mode 标注原因）；
- 删除测试脚本 → 仅删边不重跑；改名 = 旧脚本删边 + 新脚本重跑。
- 删除生产文件（SEE-1610 §SPEC-N1，`prod_removed`）→ 失效面 = 行事实
  反查触达测试 ∪ 静态直连消费者，重跑并整体替换其行事实；旧图该文件
  死边从行事实主真值 purge（派生视图单源重算自动干净）；target 面/
  文件哈希/可插桩面同步剔除；
- 其余路径（非 .gd、无基线边的删除文件等）→ 具名 `IncrementError`
  fail-loud 拒绝（不静默披露后照跑）。

SEE-1587：行事实是主真值——file/function 视图与 line→tests 反查从合并后
行事实单源重算（`_derive_views_from_line_facts`），不独立拼接。

如实边界：车道事件计数（hit_events/rates/phases/hotspots）不做增量合并，
增量产物标记 `event_counts_stale: true`（结构统计 tests/files/pairs/
function_pairs/line_pairs 由合并后的行事实精确重算）。行为耦合超出圈定面、
单脚本运行与全脸运行的状态差异，都会体现为增量 vs 全量的边差分——差异
逐边披露，不用不一致的数据冒充新鲜（`diff_artifacts` 是差分的唯一出处）。
"""

from __future__ import annotations
from ..digest import sha256_file

import hashlib
import json
import shutil
import time
from pathlib import Path

from .increment_runner import (
    execute_rerun_scripts,
    prepare_attr_templates,
)
from .attribution import (
    ATTRIBUTION_SCHEMA,
    _derive_views_from_line_facts,
    _fid_by_line_from_registry,
    _line_facts_views,
    _line_pair_counts,
    _probe_surface_lines,
    build_attribution_map,
)
from .shadowio import (
    _read_attr_merged,
    _read_registries,
)
from .orchestrator import (
    _inject_toolchain,
    make_run_uuid,
    run_measurement,
)

# SEE-1610 §SPEC-TH1 实测校准（KOL 824 文件/285 脚本 warm 副本）：增量墙钟
# ≈ 16s + 8.9s×重跑脚本数（S1 1.75%/5 脚本 → 60.5s，S2 37.5%/107 → 966s，
# S3 44.9%/128 → 1162s），暖全脸 223-227s——交叉点 ≈ 23.6 脚本（~8.3% 占比）。
# 0.5 在中位面上让增量比全脸慢 4.3-5.2 倍，故收紧到 0.05（≈14 脚本，帽内
# 增量仍 ≤ ~140s，保住 ≥38% 节省且留出噪声带余量）。
DEFAULT_RERUN_FULL_FACE_RATIO = 0.05
_FWD_KEYS = ("test_to_files", "test_to_functions",
             "script_to_files", "script_to_functions")
_LINE_KEYS = ("test_to_lines", "script_to_lines")

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
      无基线边=new（触发全脸回退——不猜失效面），有基线边且不在盘上
      =prod_removed（SEE-1610 §SPEC-N1，清死边 + 触达面重跑）；
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
    # SEE-1610 §SPEC-N1：行事实是主真值（SEE-1587）——身份全集并入行事实
    # 身份；否则行级基线（fn 边缺席）会把 test_changed/removed 误判
    # unknown 被 fail-loud 拒跑。
    test_identities |= set(arm.get("test_to_lines") or {})
    test_identities |= set(arm.get("script_to_lines") or {})
    out: dict[str, list[str]] = {"test_changed": [], "test_new": [],
                                 "test_removed": [], "prod_changed": [],
                                 "prod_new": [], "prod_removed": [],
                                 "unknown": []}
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
    prod = _classify_prod_entry(p, rel, edge_files, known_fids)
    if prod is not None:
        return prod
    return "unknown", raw

def _classify_prod_entry(p: Path, rel: str, edge_files: set[str],
                         known_fids: set[str]) -> "tuple[str, str] | None":
    """生产文件分类（SEE-1610 §SPEC-N1）：盘上+基线边=changed、盘上无
    边=new、已删+有边=prod_removed；非 .gd 或无边的删除文件返回 None
    （由调用方归 unknown 具名 fail-loud）。"""
    if p.suffix != ".gd":
        return None
    has_edges = rel in edge_files or any(
        _fid_file_of(fid) == rel for fid in known_fids)
    if p.is_file():
        return ("prod_changed" if has_edges else "prod_new"), rel
    if has_edges:
        return "prod_removed", rel
    return None

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

def _line_fact_touchers(arm: dict, rel: str) -> set[str]:
    """行事实反查：触达 rel 的脚本 basename 集（test 车道身份 ∪ script
    车道）。SEE-1610 §SPEC-N1 prod_removed 失效面的主来源——文件已删，
    fn/file 视图不可信，行事实是唯一反查面。"""
    touch: set[str] = set()
    for identity, by_rel in (arm.get("test_to_lines") or {}).items():
        if rel in by_rel:
            touch.add(_script_name_of(identity))
    for script, by_rel in (arm.get("script_to_lines") or {}).items():
        if rel in by_rel:
            touch.add(script)
    return touch


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
    for rel in classes.get("prod_removed", []):
        # SEE-1610 §SPEC-N1：删除文件不可信 fn/file 视图，失效面走行事实
        # 反查（test 车道 ∪ script 车道）∪ 静态直连消费者。
        touch = _line_fact_touchers(arm, rel)
        for name in touch:
            sources.setdefault(name, []).append(f"prod_removed_lines:{rel}")
        rerun |= touch
        static = _static_candidates(root, rel, index)
        for name in static:
            sources.setdefault(name, []).append(f"static:{rel}")
        rerun |= static
    return {"rerun": rerun, "dropped": dropped, "sources": sources,
            "explicit_rerun": explicit_rerun}

def _merged_identities(key: str, arm: dict, fresh: dict, dropped: set[str],
                       replace_names: set[str], name_of) -> dict:
    """同语义合并：dropped 删边 + replace_names 整体替换 + 其余身份原样
    保留 + fresh 注入。"""
    base = {}
    for identity, value in (arm.get(key) or {}).items():
        if name_of(identity) in dropped or name_of(identity) in replace_names:
            continue
        base[identity] = value
    for identity, value in (fresh.get(key) or {}).items():
        base[identity] = value
    return base

def _purge_removed_rels(test_to_lines: dict, script_to_lines: dict,
                        removed_rels: set[str]) -> tuple[dict, dict]:
    """SEE-1610 §SPEC-N1：从合并后行事实剔除已删除生产文件的全部边
    （触达身份已整体替换，purge 是死边兜底）。"""
    def strip(by_ident: dict) -> dict:
        return {i: {r: ls for r, ls in by_rel.items()
                    if r not in removed_rels}
                for i, by_rel in by_ident.items()}
    return strip(test_to_lines), strip(script_to_lines)

def _merge_arm(arm: dict, fresh: dict, dropped: set[str],
               fid_by_line_by_rel: "dict[str, dict[int, str]] | None" = None,
               removed_rels: "set[str] | None" = None) -> dict:
    """按脚本名替换/删边合并一张臂级映射；行事实同语义合并（重跑脚本
    整体替换、删除脚本删边、其余身份原样保留），file/function 视图与
    line→tests 反查从合并后行事实单源重算（SEE-1587：视图永不独立拼接）。
    `removed_rels`（SEE-1610 §SPEC-N1）＝已删除生产文件：从合并后行事实
    purge 该文件的全部边（触达身份已整体替换，purge 是死边兜底）。
    结构性统计精确重算。"""
    replace_names = {_script_name_of(i) for i in
                     (fresh.get("test_to_files") or {})}
    replace_names |= set(fresh.get("script_to_files") or {})
    test_to_lines = _merged_identities(
        "test_to_lines", arm, fresh, dropped, replace_names,
        lambda i: i.split("::", 1)[0])
    script_to_lines = _merged_identities(
        "script_to_lines", arm, fresh, dropped, replace_names, lambda i: i)
    if removed_rels:
        test_to_lines, script_to_lines = _purge_removed_rels(
            test_to_lines, script_to_lines, removed_rels)
    # file/function 边不再独立合并：从合并后行事实派生（行事实是主真值）。
    derived = _derive_views_from_line_facts(test_to_lines, script_to_lines,
                                            fid_by_line_by_rel)
    test_to_files = derived["test_to_files"]
    script_to_files = derived["script_to_files"]
    test_to_functions = derived["test_to_functions"]
    script_to_functions = derived["script_to_functions"]
    file_to_tests = derived["file_to_tests"]
    file_to_scripts = derived["file_to_scripts"]
    line_to_tests = derived["line_to_tests"]
    line_pairs, script_line_pairs = _line_pair_counts(test_to_lines,
                                                      script_to_lines)
    return {
        "tests": len(test_to_files),
        "scripts": len(script_to_files),
        "files": len(file_to_tests),
        "pairs": sum(len(v) for v in test_to_files.values()),
        "script_pairs": sum(len(v) for v in script_to_files.values()),
        "function_pairs": sum(len(v) for v in test_to_functions.values()),
        "line_pairs": line_pairs,
        "script_line_pairs": script_line_pairs,
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
        **_line_facts_views(test_to_lines, script_to_lines, line_to_tests),
    }
def _increment_decision(root: Path, recipe: dict, baseline: dict,
                        changed_files: list[str], since_full: int,
                        calibration_every: int,
                        rerun_full_face_ratio: float) -> tuple:
    """增量资格判定：hash 守恒 + 变更分类 + 失效计算 + fallback 仲裁。
    `recipe` 是已注入 toolchain 的配方（hash 判据用）。
    返回 (classes, invalidation, rerun, reason)。"""
    classes = classify_changes(root, changed_files, baseline)
    if classes["unknown"]:
        # SEE-1610 §SPEC-N1：对齐模块 docstring——不可分类路径具名拒绝，
        # 绝不披露后照跑（旧图死边 + 零重跑是静默失真）。
        unk = classes["unknown"]
        sample = ", ".join(sorted(unk)[:8])
        more = f" (+{len(unk) - 8} more)" if len(unk) > 8 else ""
        raise IncrementError(
            f"unknown_changed_paths: {len(unk)} changed path(s) cannot be "
            f"classified as test/prod change (non-.gd or no baseline edges) "
            f"— failing loud per module contract: {sample}{more}")
    invalidation = {"rerun": set(), "dropped": set(), "sources": {}}
    hash_ok = (baseline.get("report_recipe_hash")
               == recipe["recipe_hash"])
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
    return classes, invalidation, rerun, reason

def _increment_emit(root: Path, out_path, artifact: dict, merged: dict,
                    classes: dict, rerun: set, invalidation: dict,
                    arm_exec_s, t0: float) -> dict:
    """产物落盘（父目录自建）+ 增量报告组装（provenance/merged_stats）。"""
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
    """增量重建主入口（含产物落盘与 provenance）。"""
    t0 = time.monotonic()
    root = Path(project_root).resolve()
    baseline = load_baseline(baseline_path)
    # D1：先存 raw recipe（toolchain 注入前）。回退全脸重建必须用它——
    # run_measurement 自行注入并据此计算 pristine hash；若传已注入配方，
    # pristine 会被写成注入域 hash → 映射永久"不新鲜"自锁。
    raw_recipe = dict(recipe)
    recipe = _inject_toolchain(root, dict(recipe))
    prev = baseline.get("increment") or {}
    since_full = int(prev.get("increments_since_full") or 0)
    classes, invalidation, rerun, reason = _increment_decision(
        root, recipe, baseline, changed_files, since_full, calibration_every,
        rerun_full_face_ratio)
    if reason != "incremental":
        # 全脸回退必须传 **raw recipe**（run_measurement 自行注入并计算
        # pristine hash；传已注入配方会把 pristine 写成注入 hash → 映射
        # 永久"不新鲜"自锁）
        return _fallback_full_face(root, target_files, raw_recipe, out_path,
                                   classes, reason, attribution_causal,
                                   timeout_s, t0)
    removed = set(classes.get("prod_removed") or [])
    # 删除文件不进插桩臂（无源码可模板化）；其边由合并侧 purge。
    arm_targets = [t for t in target_files if Path(t).is_file()]
    arm_exec_s, fresh, executions, fid_by_line_by_rel = _run_increment_arm(
        root, arm_targets, recipe, rerun, timeout_s, attribution_causal)
    merged = _merge_arm(baseline["arms"]["gut_full"], fresh,
                        invalidation["dropped"], fid_by_line_by_rel,
                        removed_rels=removed)
    artifact = _build_increment_artifact(
        baseline, merged, recipe, root, classes,
        sorted(rerun), invalidation, since_full, arm_exec_s,
        executions)
    return _increment_emit(root, out_path, artifact, merged, classes, rerun,
                           invalidation, arm_exec_s, t0)
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
                       causal: bool) -> tuple[float, dict, list[dict],
                                              dict[str, dict[int, str]]]:
    """在插桩副本上只跑 rerun 脚本，读回归属证据并聚合成臂级映射。
    返回 (读回+聚合秒数, fresh 边/行事实集, 逐脚本执行结果, fid_by_line_by_rel)
    ——fid 行映射覆盖全部目标文件（模板面），供合并侧派生视图单源重算。"""
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
    fid_by_line_by_rel = {rel: _fid_by_line_from_registry(it["registry"])
                          for rel, it in templates.items()}
    arm_map = build_attribution_map(
        "increment", attr_by_rel, registries,
        probe_lines_by_rel={rel: it["probe_lines"]
                            for rel, it in templates.items()},
        block_map_by_rel={rel: it["block_map"]
                          for rel, it in templates.items()},
        fid_by_line_by_rel=fid_by_line_by_rel)
    for xdg in xdg_homes:
        shutil.rmtree(xdg, ignore_errors=True)
    return (round(time.monotonic() - t0, 3), _group_fresh_edges(arm_map),
            results, fid_by_line_by_rel)

def _group_fresh_edges(inc_arm: dict) -> dict:
    out = {}
    for key in _FWD_KEYS + _LINE_KEYS:
        out[key] = {k: {r: list(ls) for r, ls in v.items()}
                    if key in _LINE_KEYS else list(v)
                    for k, v in (inc_arm.get(key) or {}).items()}
    return out

def _build_increment_artifact(baseline: dict, merged: dict, recipe: dict,
                              root: Path, classes: dict, rerun_list: list[str],
                              invalidation: dict, since_full: int,
                              arm_exec_s: float,
                              executions: list[dict] | None = None) -> dict:
    removed = set(classes.get("prod_removed") or [])
    # SEE-1610 §SPEC-N1：target 面剔除已删除文件（abs/rel 双形态匹配——
    # target_files 存储格式随入口不同）。
    removed_abs = {str((root / r).resolve()) for r in removed}
    targets = [t for t in (baseline.get("target_files")
                           or sorted((baseline.get("file_sha256") or {})))
               if t not in removed and str(Path(t).resolve()) not in removed_abs]
    return {
        "schema_version": ATTRIBUTION_SCHEMA,
        "report_recipe_hash": recipe["recipe_hash"],
        "recipe_hash_pristine": baseline.get("recipe_hash_pristine"),
        "project_root": str(root),
        "identity": baseline.get("identity"),
        "causal": baseline.get("causal"),
        "file_sha256": _merged_file_sha(root, baseline, classes),
        "target_files": targets,
        "tests_face": _merged_tests_face(root, baseline, classes),
        "instrumentable_lines": _merged_instrumentable_lines(root, baseline,
                                                             classes),
        "arms": {"gut_full": {
            **merged,
            # SEE-1610 D1：增量产物无法重测未归属事件——保守并集（基线名单
            # ∪ 本轮变更/新增文件）供 test 筛选闭包整文件面判定。
            "unattributed_files": sorted(
                set((baseline.get("arms", {}).get("gut_full") or {})
                    .get("unattributed_files") or ())
                | set(classes.get("prod_changed") or [])
                | set(classes.get("prod_new") or []))}},
        "arms_state": dict(baseline.get("arms_state") or {}),
        "baseline_failures_meta": baseline.get("baseline_failures_meta"),
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
                                   "oversized rerun surface (deleted prod "
                                   "files stay incremental: edge purge + "
                                   "toucher rerun, SEE-1610 §SPEC-N1)"),
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
            face[rel] = sha256_file(p)
    for rel in classes.get("test_removed", []):
        face.pop(rel, None)
    return face

def _merged_file_sha(root: Path, baseline: dict,
                     classes: dict) -> dict:
    """基线文件哈希继承 + 变更生产文件按当前内容重算、删除文件剔除
    （增量新鲜绑定）。"""
    shas = dict(baseline.get("file_sha256") or {})
    for rel in classes.get("prod_changed", []) + classes.get("prod_new", []):
        p = Path(root) / rel
        if p.is_file():
            shas[rel] = sha256_file(p)
    for rel in classes.get("prod_removed", []):
        shas.pop(rel, None)
    return shas

def _merged_instrumentable_lines(root: Path, baseline: dict,
                                 classes: dict) -> dict:
    """探针面（可插桩行全集）继承 + 变更生产文件按当前源码重算——
    与模板构建同源（_instrument_blocks），行级主真值的测量宇宙随之更新。"""
    surface = dict(baseline.get("instrumentable_lines") or {})
    from ..coverage.runner import _instrument_blocks
    for rel in classes.get("prod_changed", []) + classes.get("prod_new", []):
        p = Path(root) / rel
        if not p.is_file():
            surface.pop(rel, None)
            continue
        src = p.read_text(encoding="utf-8")
        _tpl, probe_lines, block_map = _instrument_blocks(
            src, "gqt-surface-computation.txt", runtime_sink=False)
        surface[rel] = _probe_surface_lines(probe_lines, block_map)
    for rel in classes.get("prod_removed", []):
        surface.pop(rel, None)
    return surface

def _fallback_full_face(root: Path, target_files: list[str], recipe: dict,
                        out_path: str | Path, classes: dict, reason: str,
                        causal: bool, timeout_s: int, t0: float) -> dict:
    # D1：新增生产文件必须并入重建 target 面——否则 brand_new.gd 永远
    # 不进图（回退仅重建基线 targets 时新文件被静默漏测）。
    # SEE-1610 §SPEC-N1：已删除文件从重建 target 面剔除（基线继承的
    # target 列表可能仍含删除 rel——auto 续新路径以基线 target_files 为入参）。
    targets = [t for t in target_files if Path(t).is_file()]
    for rel in classes.get("prod_new") or []:
        abs_path = str((Path(root) / rel).resolve())
        if abs_path not in targets:
            targets.append(abs_path)
    report = run_measurement(root, targets, recipe,
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
    """两张归属产物的逐边差分（file/function 边 + 行事实三元组）。

    返回每张边的 {added_count, removed_count, added_edges(截断 200),
    removed_edges(截断 200)}——added = 仅 b 有，removed = 仅 a 有。
    行事实边按 (identity, rel, line) 三元组粒度差分。"""
    def edges(doc: dict, key: str) -> dict[str, set[str]]:
        arm = (doc.get("arms") or {}).get("gut_full") or {}
        return {k: set(v) for k, v in (arm.get(key) or {}).items()}

    def line_edges(doc: dict, key: str) -> dict[str, set[str]]:
        arm = (doc.get("arms") or {}).get("gut_full") or {}
        out: dict[str, set[str]] = {}
        for identity, by_rel in (arm.get(key) or {}).items():
            for rel, lines in by_rel.items():
                edge = f"{identity} -> {rel}"
                out.setdefault(edge, set()).update(
                    str(ln) for ln in lines)
        return out

    def _diff(a: dict, b: dict) -> dict:
        added = sorted(f"{k} -> {x}" for k, v in b.items()
                       for x in v - a.get(k, set()))
        removed = sorted(f"{k} -> {x}" for k, v in a.items()
                         for x in v - b.get(k, set()))
        return {
            "added_count": len(added), "removed_count": len(removed),
            "added_edges": added[:200], "removed_edges": removed[:200],
        }

    out = {}
    for key in _FWD_KEYS:
        out[key] = _diff(edges(a_doc, key), edges(b_doc, key))
    for key in _LINE_KEYS:
        out[key] = _diff(line_edges(a_doc, key), line_edges(b_doc, key))
    return out

