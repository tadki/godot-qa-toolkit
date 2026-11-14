"""SEE-1581 — mutation 选面改「运行时真值映射」（静态 TestOwnershipIndex 退役）。

合同（Owner 2026-11-14 拍板 + ④ 实测数据）：

- **映射来源**：canonical 归属产物（`canonical-test-attribution/4`），路径经
  `--attribution-map` 或环境变量 `GQT_ATTRIBUTION_MAP` 给出；
- **新鲜判据**：产物 `report_recipe_hash` 与本树现值一致（含 toolchain 注入
  口径；按 project_root 记忆化）+ **目标文件 sha256 与产物 `file_sha256`
  绑定一致**；任一不满足 → 视为过期，退回全脸（不派生、不猜）；
- **选面**：file → 测试脚本集（test 车道 ∪ script 车道）→ 选择目录
  （`.qa-cache/gqt-attr-select/<sel-id>/`）内的 `extends` 包装脚本
  （文件名与原名一致，GUT 经继承收集；KOL 实测精确命中 Scripts/Tests 计数）——
  `tests_glob` 仍是单一 res:// 目录，下游 resume/coverage/worker 零分叉；
- **零引用短路**：新鲜映射且集合为空 → 短路（`zero_ref=True`，来源
  `runtime_mapping`）；无映射/过期 → `tests_glob=None`（全脸），不短路；
- **kill 证书归属**：`RuntimeOwnership` 以映射身份（`script.gd::test`）解析
  killer 脚本；无新鲜映射 → 不产出证书（fail-safe 只偏向重跑），不混用静态
  索引。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from functools import lru_cache
from pathlib import Path

SCHEMA_VERSION = "canonical-test-attribution/4"
ENV_MAP = "GQT_ATTRIBUTION_MAP"
ENV_MODE = "GQT_ATTRIBUTION_MAP_MODE"   # "auto"（默认）| "off"
SELECT_ROOT = ".qa-cache/gqt-attr-select"
DEFAULT_MAP_REL = ".qa-cache/attribution-map.json"

_LEDGER: list[dict] = []
_INCREMENTED: set[tuple[str, str]] = set()
_REGENERATED: set[str] = set()


def mode() -> str:
    return (os.environ.get(ENV_MODE) or "auto").strip().lower()


def ledger() -> dict:
    """自动更新/回退台账（mutation summary 的 attribution 块唯一出处）。"""
    return {"events": list(_LEDGER),
            "wall_s_total": round(sum(e.get("wall_s", 0.0)
                                      for e in _LEDGER), 3)}


def reset_ledger() -> None:
    _LEDGER.clear()
    _INCREMENTED.clear()
    _REGENERATED.clear()


def _record(event: dict) -> None:
    _LEDGER.append(event)


class SelectionError(Exception):
    """映射装载/绑定违例（调用方降级全脸并披露，不静默）。"""


def map_path(project_root: str | Path, explicit: str | None = None) -> str | None:
    """显式参数 > 环境变量 > 约定默认位置；不存在返回 None。"""
    cand = explicit or os.environ.get(ENV_MAP)
    if not cand:
        default = Path(project_root) / DEFAULT_MAP_REL
        return str(default) if default.is_file() else None
    return cand if Path(cand).is_file() else None


def default_map_path(project_root: str | Path) -> str:
    return str(Path(project_root) / DEFAULT_MAP_REL)


def _roots_of(files: list[str], project_root: str) -> list[str]:
    """变更文件 → 目标面 roots（相对 project_root 的首层目录并集）。"""
    root = Path(project_root).resolve()
    tops: set[str] = set()
    for f in files:
        try:
            rel = Path(f).resolve().relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) > 1:
            tops.add(str(root / rel.parts[0]))
    return sorted(tops) or [str(root)]


def _targets_under(roots: list[str]) -> list[str]:
    return sorted({str(p) for r in roots
                   for p in Path(r).rglob("*.gd") if p.is_file()})


def ensure_map(project_root: str | Path, files: list[str]) -> str | None:
    """默认自动生成：无映射且 mode=auto → canonical --test-attribution 一次。

    生成成功后落在 `<project>/.qa-cache/attribution-map.json` 并写入
    GQT_ATTRIBUTION_MAP；失败 → 台账记录并返回 None（调用方走全脸）。"""
    root = str(Path(project_root).resolve())
    if mode() == "off":
        return map_path(root)
    existing = map_path(root)
    if existing:
        return existing
    t0 = time.monotonic()
    try:
        from ..canonical.orchestrator import run_canonical
        from ..canonical.recipe import build_recipe
        recipe, error = build_recipe(Path(root))
        if error is not None:
            raise SelectionError(f"recipe unavailable: {error}")
        targets = _targets_under(_roots_of(files, root))
        out = default_map_path(root)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        run_canonical(root, targets, recipe, test_attribution=True,
                      attribution_out=out)
        if not arms_valid(load_map(out).get("arms_state") or {}):
            Path(out).unlink(missing_ok=True)
            raise SelectionError(
                "generated mapping has invalid arms (measurement failed)")
        os.environ[ENV_MAP] = out
        _record({"action": "generate", "map": out,
                 "targets": len(targets),
                 "wall_s": round(time.monotonic() - t0, 3), "ok": True})
        return out
    except Exception as e:  # noqa: BLE001 — 生成失败诚实退全脸
        _record({"action": "generate", "ok": False, "error": str(e),
                 "wall_s": round(time.monotonic() - t0, 3)})
        return None


def _auto_increment(project_root: str, path: str, doc: dict,
                    rel: str,
                    changed_extra: list[str] | None = None
                    ) -> tuple[bool, str | None]:
    """过期映射默认增量更新（attr-update）；失败/仍不新鲜 → 全脸。"""
    changed = sorted({rel, *(changed_extra or [])})
    key = (str(Path(project_root).resolve()), tuple(changed))
    if key in _INCREMENTED:
        return True, None
    t0 = time.monotonic()
    try:
        from ..canonical.increment import run_attribution_increment
        from ..canonical.recipe import build_recipe
        root = str(Path(project_root).resolve())
        recipe, error = build_recipe(Path(root))
        if error is not None:
            raise SelectionError(f"recipe unavailable: {error}")
        targets = [str(Path(root) / f)
                   for f in (doc.get("target_files") or [])]
        if not targets:
            raise SelectionError("baseline missing target_files")
        tmp = path + ".tmp"
        report = run_attribution_increment(
            root, targets, recipe, path, changed, tmp)
        os.replace(tmp, path)
        _INCREMENTED.add(key)
        _record({"action": "increment", "changed": changed, "map": path,
                 "mode": report.get("mode"),
                 "wall_s": round(time.monotonic() - t0, 3), "ok": True})
        return True, None
    except Exception as e:  # noqa: BLE001 — 增量失败不静默交付
        _record({"action": "increment", "changed": changed, "ok": False,
                 "error": str(e), "wall_s": round(time.monotonic() - t0, 3)})
        return False, str(e)


@lru_cache(maxsize=8)
def _current_tests_face(project_root: str) -> dict[str, str]:
    root = Path(project_root)
    tests = root / "tests"
    out: dict[str, str] = {}
    if tests.is_dir():
        for p in sorted(tests.rglob("*.gd")):
            try:
                out[str(p.relative_to(root))] = hashlib.sha256(
                    p.read_bytes()).hexdigest()
            except OSError:
                continue
    return out


def _tests_face_drift(project_root: str, doc: dict) -> dict[str, list[str]]:
    """测试面内容漂移：返回 {changed: [...], removed: [...]}（现算，不信缓存）。"""
    base = doc.get("tests_face") or {}
    cur = _current_tests_face(str(Path(project_root).resolve()))
    changed = sorted(rel for rel, sha in cur.items()
                     if base.get(rel) != sha)
    removed = sorted(rel for rel in base if rel not in cur)
    return {"changed": changed, "removed": removed}


@lru_cache(maxsize=8)
def _fresh_recipe_hash(project_root: str) -> str | None:
    """当前树的 raw recipe hash（不含 toolchain 注入；零 godot 依赖）。

    选面正确性只依赖测试面身份（gut dirs/features/harness）——toolchain
    版本漂移不改变"谁测谁"，故不为此在每个消费入口启动 godot --version。
    """
    from ..canonical.recipe import build_recipe, recipe_hash as _raw
    recipe, error = build_recipe(Path(project_root))
    if error is not None:
        return None
    return _raw({k: v for k, v in recipe.items() if k != "recipe_hash"})


def load_map(path: str | Path) -> dict:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SelectionError(f"attribution map load failed: {e}") from e
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise SelectionError(
            f"attribution map schema {doc.get('schema_version')!r} != "
            f"{SCHEMA_VERSION!r}")
    if not isinstance((doc.get("arms") or {}).get("gut_full"), dict):
        raise SelectionError("attribution map missing arms.gut_full")
    return doc


@lru_cache(maxsize=8)
def _test_scripts_by_name(project_root: str) -> dict[str, tuple[str, ...]]:
    """测试脚本名 → res:// 路径元组（同名多路径保留全部，由调用方披露）。"""
    tests = Path(project_root) / "tests"
    out: dict[str, list[str]] = {}
    if tests.is_dir():
        for gd in sorted(tests.rglob("*.gd")):
            rel = gd.relative_to(Path(project_root)).as_posix()
            out.setdefault(gd.name, []).append("res://" + rel)
    return {k: tuple(v) for k, v in out.items()}


def _file_rel(project_root: str, target_file: str) -> str:
    p = Path(target_file).resolve()
    return p.relative_to(Path(project_root).resolve()).as_posix()


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def arms_valid(states: dict) -> bool:
    """产物臂可用性：硬失败（无 bundle / 无命中边）的"空映射"不是真值。

    KOL 常态：既有失败面使臂 state=run_error，但 bundles>0 且边集非空——
    这类产物仍是有效测量（失败门控不影响归属观测），必须可用。"""
    gut = (states or {}).get("gut_full") or {}
    if gut.get("state") == "valid":
        return True
    return int(gut.get("bundles") or 0) > 0 and int(gut.get("pairs") or 0) > 0


def _evaluate(root: str, rel: str, doc: dict) -> dict:
    """单次新鲜性判定 → {ok, reason, scripts, ambiguous, mapping}。"""
    mapping = {"path": None, "file_sha256_bound": False,
               "recipe_hash_ok": False, "arms_valid": arms_valid(
                   doc.get("arms_state") or {})}
    want_hash = (doc.get("file_sha256") or {}).get(rel)
    have_hash = _sha256(Path(root) / rel)
    mapping["file_sha256_bound"] = bool(want_hash) and want_hash == have_hash
    mapping["recipe_hash_ok"] = (
        _fresh_recipe_hash(root) is not None
        and _fresh_recipe_hash(root) == doc.get("recipe_hash_pristine"))
    drift = _tests_face_drift(root, doc)
    mapping["tests_face_ok"] = not (drift["changed"] or drift["removed"])
    mapping["tests_face_drift"] = drift
    if not mapping["arms_valid"]:
        return {"ok": False, "reason": "measurement_invalid",
                "scripts": [], "ambiguous": [], "mapping": mapping}
    if not mapping["file_sha256_bound"]:
        return {"ok": False, "reason": "file_binding_missing_or_stale",
                "scripts": [], "ambiguous": [], "mapping": mapping}
    if not mapping["recipe_hash_ok"]:
        return {"ok": False, "reason": "recipe_hash_stale",
                "scripts": [], "ambiguous": [], "mapping": mapping}
    if not mapping["tests_face_ok"]:
        return {"ok": False, "reason": "tests_face_drift",
                "scripts": [], "ambiguous": [], "mapping": mapping}
    scripts, ambiguous = _tests_for_target(root, doc["arms"]["gut_full"], rel)
    return {"ok": True, "reason": "fresh_runtime_mapping",
            "scripts": scripts, "ambiguous": ambiguous, "mapping": mapping}


def _regenerate(project_root: str, path: str, rel: str) -> bool:
    """旧 schema/无 file_sha256 的产物 → 整份重建到同一路径（auto 模式）。"""
    key = f"{Path(project_root).resolve()}::{path}"
    if key in _REGENERATED:
        return True
    t0 = time.monotonic()
    try:
        from ..canonical.orchestrator import run_canonical
        from ..canonical.recipe import build_recipe
        root = str(Path(project_root).resolve())
        recipe, error = build_recipe(Path(root))
        if error is not None:
            raise SelectionError(f"recipe unavailable: {error}")
        doc = load_map(path)
        targets = [str(Path(root) / f)
                   for f in (doc.get("target_files") or [])]
        if not targets:
            raise SelectionError("baseline missing target_files")
        tmp = path + ".regen.tmp"
        run_canonical(root, targets, recipe, test_attribution=True,
                      attribution_out=tmp)
        os.replace(tmp, path)
        _REGENERATED.add(key)
        _record({"action": "regenerate", "map": path, "trigger": rel,
                 "wall_s": round(time.monotonic() - t0, 3), "ok": True})
        return True
    except Exception as e:  # noqa: BLE001
        _record({"action": "regenerate", "map": path, "trigger": rel,
                 "ok": False, "error": str(e),
                 "wall_s": round(time.monotonic() - t0, 3)})
        return False


def resolve_selection(project_root: str | Path, target_file: str,
                      map_path_override: str | None = None) -> dict:
    """目标 → 选面决策（绝不静默：每个分支都带 source/reason 披露）。

    mode=auto（默认）：无映射 → 全脸（生成在 CLI 入口 ensure_map 完成）；
    映射过期 → **默认自动增量更新一次**（attr-update，3–6s）后重判；旧
    产物（无 file_sha256）→ 整份重建一次；自动更新失败/仍不新鲜 → 全脸，
    台账如实记录。mode=off：只读不更新。"""
    root = str(Path(project_root).resolve())
    base = {"source": "full_face", "reason": None, "tests": [],
            "tests_glob": None, "zero_ref": False, "mapping": None,
            "ambiguous_names": []}
    path = map_path(root, map_path_override)
    if path is None:
        base["reason"] = "no_attribution_map"
        return base
    try:
        doc = load_map(path)
    except SelectionError as e:
        base["reason"] = f"map_invalid:{e}"
        return base
    try:
        rel = _file_rel(root, target_file)
    except ValueError:
        base["reason"] = "target_outside_project"
        return base
    auto = mode() == "auto"
    evaluation = _evaluate(root, rel, doc)
    evaluation["mapping"]["path"] = path
    if not evaluation["ok"] and auto:
        if not (doc.get("file_sha256") or {}):
            renewed = _regenerate(root, path, rel)
        else:
            drift = _tests_face_drift(root, doc)
            extra = drift["changed"] + drift["removed"]
            renewed, _err = _auto_increment(root, path, doc, rel,
                                            changed_extra=extra)
        if renewed:
            try:
                doc = load_map(path)
                evaluation = _evaluate(root, rel, doc)
                evaluation["mapping"]["path"] = path
            except SelectionError as e:
                base["reason"] = f"auto_update_reload_failed:{e}"
                return base
    base["mapping"] = evaluation["mapping"]
    if not evaluation["ok"]:
        base["reason"] = ("auto_update_still_stale:" if auto else "") + \
            evaluation["reason"]
        return base
    if evaluation["ambiguous"]:
        # 同名跨脚本 → 身份不可判定：退回全脸（不猜，也不静默丢）
        base["ambiguous_names"] = evaluation["ambiguous"]
        base["reason"] = "ambiguous_test_names"
        return base
    scripts = evaluation["scripts"]
    if not scripts:
        base.update({"source": "runtime_mapping", "zero_ref": True,
                     "reason": "mapping_says_no_tests"})
        return base
    try:
        sel_dir = materialize_selection(root, scripts)
    except OSError as e:
        base["reason"] = f"selection_materialize_failed:{e}"
        return base
    base.update({"source": "runtime_mapping", "tests": scripts,
                 "tests_glob": sel_dir, "reason": "fresh_runtime_mapping"})
    return base


def _tests_for_target(root: str, arm: dict, rel: str) -> tuple[list[str], list[str]]:
    names: set[str] = set()
    for identity, files in (arm.get("test_to_files") or {}).items():
        if rel in files:
            names.add(identity.split("::", 1)[0])
    for identity, files in (arm.get("script_to_files") or {}).items():
        if rel in files:
            names.add(identity)
    by_name = _test_scripts_by_name(root)
    scripts: list[str] = []
    ambiguous: list[str] = []
    for name in sorted(names):
        paths = by_name.get(name)
        if not paths:
            continue
        if len(paths) > 1:
            ambiguous.append(name)
        scripts.extend(paths)
    return sorted(set(scripts)), ambiguous


def materialize_selection(project_root: str, scripts: list[str]) -> str:
    """选择目录：每个脚本一个同名 extends 包装（GUT 继承收集，身份=原名）。

    目录按脚本集内容哈希命名，已存在即复用（幂等）；返回 res:// 目录。"""
    sel_id = hashlib.sha1(
        "\n".join(sorted(scripts)).encode("utf-8")).hexdigest()[:12]
    root = Path(project_root)
    sel_fs = root / SELECT_ROOT / sel_id
    if not sel_fs.is_dir():
        sel_fs.mkdir(parents=True, exist_ok=True)
        for res_path in sorted(scripts):
            if not res_path.startswith("res://"):
                continue
            name = res_path.rsplit("/", 1)[-1]
            wrapper = sel_fs / name
            if wrapper.exists():
                continue
            wrapper.write_text(
                f'extends "{res_path}"\n', encoding="utf-8")
    return f"res://{SELECT_ROOT}/{sel_id}/"


def cleanup_stale_selections(project_root: str, keep: set[str] | None = None) -> None:
    """清理不再被引用的选择目录（保留 keep 中的 sel-id）。"""
    base = Path(project_root) / SELECT_ROOT
    if not base.is_dir():
        return
    for d in base.iterdir():
        if d.is_dir() and (keep is None or d.name not in keep):
            shutil.rmtree(d, ignore_errors=True)


class RuntimeOwnership:
    """kill 证书归属：测试名 → 脚本（来自新鲜映射身份；歧义/缺失 → None）。

    无映射或过期时由调用方传 None —— 证书不产出（fail-safe 只偏向重跑），
    不与静态 TestOwnershipIndex 混用。"""

    __test__ = False

    def __init__(self, project_root: str, doc: dict):
        self.project_root = str(Path(project_root).resolve())
        self._by_name: dict[str, set[str]] = {}
        arm = doc["arms"]["gut_full"]
        for identity in (arm.get("test_to_files") or {}):
            script, _, name = identity.partition("::")
            self._by_name.setdefault(name, set()).add(script)
        for identity in (arm.get("script_to_files") or {}):
            if "::" in identity:
                script, _, name = identity.partition("::")
                self._by_name.setdefault(name, set()).add(script)
        self._name_to_path = _test_scripts_by_name(self.project_root)

    def owner_of(self, test_name: str) -> str | None:
        owners = self._by_name.get(test_name)
        if not owners or len(owners) != 1:
            return None
        script = next(iter(owners))
        paths = self._name_to_path.get(script, ())
        if len(paths) != 1:
            return None
        return paths[0]


class StructuralOwnership:
    """kill 证书归属的结构性兜底：测试名 → 脚本（tests/ 静态扫描）。

    注意：这是**身份映射**（name→script），不是被退役的选面索引（gdself
    file→tests）；选面永远只走运行时映射。兜底用于无新鲜映射时保住 warm
    reuse（证书缺失会让 killed 全量保守重跑）；证书条目带
    `ownership_source` 显式披露来源，不静默混用。
    """

    __test__ = False
    source = "structural_index"

    def __init__(self, project_root: str):
        from .identity import TestOwnershipIndex
        self._idx = TestOwnershipIndex(project_root)

    def owner_of(self, test_name: str) -> str | None:
        return self._idx.owner_of(test_name)


def ownership_for(project_root: str, target_file: str,
                  map_path_override: str | None = None):
    """新鲜映射 → RuntimeOwnership；否则 StructuralOwnership（带来源披露）。"""
    try:
        sel = resolve_selection(project_root, target_file, map_path_override)
        mp = sel.get("mapping") or {}
        if mp.get("file_sha256_bound") and mp.get("recipe_hash_ok"):
            doc = load_map(mp["path"])
            owner = RuntimeOwnership(project_root, doc)
            owner.source = "runtime_mapping"
            return owner
    except Exception:  # noqa: BLE001 — 解析失败按无新鲜映射处理
        pass
    return StructuralOwnership(project_root)
