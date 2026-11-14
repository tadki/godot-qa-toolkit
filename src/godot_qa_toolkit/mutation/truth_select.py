"""SEE-1581 — mutation 选面改「运行时真值映射」（静态 TestOwnershipIndex 退役）。

合同（Owner 2026-11-14 拍板 + ④ 实测数据）：

- **映射来源**：measurement 归属产物（`test-attribution/5`），路径经
  `--attribution-map` 或环境变量 `GQT_ATTRIBUTION_MAP` 给出；
- **新鲜判据**：产物 `report_recipe_hash` 与本树现值一致（含 toolchain 注入
  口径；按 project_root 记忆化）+ **目标文件 sha256 与产物 `file_sha256`
  绑定一致**；任一不满足 → 视为过期，自动增量/重建一次后重判，仍不可用
  → fail-loud（`MutationSelectionError`，Owner 定稿：取消自动全脸）；
- **选面**：file → 测试脚本集（test 车道 ∪ script 车道）→ 选择目录
  （`.qa-cache/gqt-attr-select/<sel-id>/`）内的 `extends` 包装脚本
  （文件名与原名一致，GUT 经继承收集；KOL 实测精确命中 Scripts/Tests 计数）——
  `tests_glob` 仍是单一 res:// 目录，下游 resume/coverage/worker 零分叉；
- **零引用短路**：新鲜映射且集合为空 → 短路（`zero_ref=True`，来源
  `runtime_mapping`）；无映射/过期 → 严格模式 fail-loud 拒跑；
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

SCHEMA_VERSION = "test-attribution/5"
ENV_MAP = "GQT_ATTRIBUTION_MAP"
ENV_MODE = "GQT_ATTRIBUTION_MAP_MODE"   # "auto"（默认）| "off"
SELECT_ROOT = ".gqt-attr-select"   # 根级点目录：影子树会携带（.qa-cache 被排除）
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
    """映射装载/绑定违例。"""


class MutationSelectionError(SelectionError):
    """选面不可用 → fail-loud 拒跑（Owner 定稿：取消自动全脸；全脸仅显式审计）。"""

    def __init__(self, reason: str, detail: str | None = None):
        self.reason = reason
        self.detail = detail
        super().__init__(reason + (f": {detail}" if detail else ""))


def _refresh_lock(path: str):
    """映射刷新串行化：同目录 `<map>.lock` 上的 flock（进程死自动释放）。

    fcntl 不可用平台（原生 Windows）降级为无锁（唯一 tmp + 原子 rename 仍
    防半写；并发合并语义由调用方双检 + 校验兜底并披露）。"""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        if fcntl is None:
            yield False
            return
        lock_path = path + ".lock"
        lf = open(lock_path, "a+")
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            yield True
        finally:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            finally:
                lf.close()
    return _cm()


try:
    import fcntl
except ImportError:  # pragma: no cover - 原生 Windows 降级
    fcntl = None


def map_path(project_root: str | Path, explicit: str | None = None) -> str | None:
    """显式参数 > 环境变量 > 约定默认位置；不存在返回 None。"""
    cand = explicit or os.environ.get(ENV_MAP)
    if not cand:
        default = Path(project_root) / DEFAULT_MAP_REL
        return str(default) if default.is_file() else None
    return cand if Path(cand).is_file() else None


def default_map_path(project_root: str | Path) -> str:
    return str(Path(project_root) / DEFAULT_MAP_REL)


def _unique_tmp(path: str) -> str:
    """同目录唯一临时文件（防并发共用同一 tmp 互相覆盖）。"""
    import tempfile
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".attr-map.", suffix=".tmp")
    os.close(fd)
    return tmp


def _script_names(doc: dict) -> set[str]:
    arm = (doc.get("arms") or {}).get("gut_full") or {}
    return {i.split("::", 1)[0]
            for i in (arm.get("test_to_files") or {})}


def validate_refresh(before: dict, after: dict) -> None:
    """刷新产物守恒校验：失效即 fail-loud（不得静默交付失真映射）。

    允许变化的边：rerun_scripts（整体替换）与 dropped_scripts（删除）；
    其余测试身份必须原样保留——并发读-改-写导致映射失真（共享临时文件、
    无串行化；修复后并发刷新实测零身份丢失）在此显式拒绝。"""
    if after.get("schema_version") != SCHEMA_VERSION:
        raise MutationSelectionError("refresh_schema_mismatch")
    if not arms_valid(after.get("arms_state") or {}):
        raise MutationSelectionError("refresh_arms_invalid")
    inc = after.get("increment") or {}
    rerun = {str(x) for x in (inc.get("rerun_scripts") or [])}
    dropped = {str(x) for x in (inc.get("dropped_scripts") or [])}
    keep_before = _script_names(before) - rerun - dropped
    missing = keep_before - _script_names(after)
    if missing:
        raise MutationSelectionError(
            "refresh_lost_identities",
            f"{sorted(missing)[:8]} (+{max(0, len(missing) - 8)} more)")


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


def _default_map_reuse(path: str | None, explicit: bool
                       ) -> tuple[str | None, str | None]:
    """约定默认/显式位置的产物复用决策（§SPEC-012 HIGH-2 判定唯一出处）。

    返回 (可复用路径, 重建原因)：可用 → (path, None)；显式产物不可用 →
    (path, None)（只读透传，fail-loud 由 resolve_selection）；默认产物
    不可用 → (None, 原因)（调用方原子重建）；缺失 → (None, None)。"""
    if path is None:
        return None, None
    try:
        load_map(path)
    except SelectionError as e:
        if explicit:
            return path, None
        return None, f"default_map_unusable: {e}"
    return path, None


def ensure_map(project_root: str | Path, files: list[str]) -> str | None:
    """默认自动生成/重建：约定默认位置缺失或不可用（旧 schema/无效状态）
    且 mode=auto → `gqt measure run --test-attribution` 一次（原子写、
    `<map>.lock` 串行化）。

    显式 `--attribution-map`（ENV 指定）只读透传，绝不重建/删除——schema
    不符由 resolve_selection fail-loud（Owner 约束）。失败 → 台账记录并
    raise `MutationSelectionError`（fail-loud；全脸仅显式 `--select full`
    审计开关）。"""
    root = str(Path(project_root).resolve())
    if mode() == "off":
        return map_path(root)
    explicit = bool(os.environ.get(ENV_MAP))
    existing, rebuild_reason = _default_map_reuse(map_path(root), explicit)
    if existing:
        # 可用直接复用；显式产物不可用同样原样透传（绝不重建/删除用户
        # 指定文件——fail-loud 由 resolve_selection）。
        return existing
    # §SPEC-012 HIGH-2：默认位置旧 schema/无效状态（rebuild_reason=原因串）
    # → 原子重建（原样返回会让下游必然 fail-loud 且 _regenerate 不可达）。
    out = default_map_path(root)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    with _refresh_lock(out):
        existing, reason = _default_map_reuse(map_path(root), explicit)
        if existing:
            return existing                # 双检：等锁期间可能已被生成/重建
        rebuild_reason = rebuild_reason or reason
        action = "rebuild" if rebuild_reason else "generate"
        try:
            from ..measurement.orchestrator import run_measurement
            from ..measurement.recipe import build_recipe
            recipe, error = build_recipe(Path(root))
            if error is not None:
                raise SelectionError(f"recipe unavailable: {error}")
            targets = _targets_under(_roots_of(files, root))
            tmp = _unique_tmp(out)
            try:
                run_measurement(root, targets, recipe, test_attribution=True,
                              attribution_out=tmp)
                doc = load_map(tmp)
                if not arms_valid(doc.get("arms_state") or {}):
                    raise MutationSelectionError(
                        "generated mapping has invalid arms "
                        "(measurement failed)")
                os.replace(tmp, out)
            finally:
                Path(tmp).unlink(missing_ok=True)
            os.environ[ENV_MAP] = out
            event = {"action": action, "map": out,
                     "targets": len(targets),
                     "wall_s": round(time.monotonic() - t0, 3), "ok": True}
            if rebuild_reason:
                event["reason"] = rebuild_reason
            _record(event)
            return out
        except MutationSelectionError as e:
            _record({"action": action, "ok": False, "error": str(e),
                     "wall_s": round(time.monotonic() - t0, 3)})
            raise
        except Exception as e:  # noqa: BLE001 — 生成失败 fail-loud
            _record({"action": action, "ok": False, "error": str(e),
                     "wall_s": round(time.monotonic() - t0, 3)})
            raise MutationSelectionError("attribution_generate_failed",
                                         str(e)) from e


def _auto_increment(project_root: str, path: str, doc: dict,
                    rel: str,
                    changed_extra: list[str] | None = None
                    ) -> tuple[bool, str | None]:
    """过期映射默认增量更新（串行化 + 守恒校验 + fail-loud）。

    锁内**重读基线**再合并（并发写者读到旧基线是 HIGH-2 丢边的根因），
    唯一 tmp 落盘后校验「非重跑身份零丢失」，失真直接抛
    MutationSelectionError（不静默交付，fail-loud 拒跑）。"""
    root = str(Path(project_root).resolve())
    t0 = time.monotonic()
    with _refresh_lock(path):
        current = load_map(path)            # 锁内重读（并发写者可能已更新）
        changed = sorted({rel, *(changed_extra or [])})
        drift = _tests_face_drift(root, current)
        changed = sorted(set(changed) | set(drift["changed"])
                         | set(drift["removed"]))
        key = (root, tuple(changed))
        if key in _INCREMENTED:
            return True, None
        try:
            from ..measurement.increment import run_attribution_increment
            from ..measurement.recipe import build_recipe
            recipe, error = build_recipe(Path(root))
            if error is not None:
                raise SelectionError(f"recipe unavailable: {error}")
            targets = [str(Path(root) / f)
                       for f in (current.get("target_files") or [])]
            if not targets:
                raise SelectionError("baseline missing target_files")
            tmp = _unique_tmp(path)
            try:
                report = run_attribution_increment(
                    root, targets, recipe, path, changed, tmp)
                validate_refresh(current, load_map(tmp))
                os.replace(tmp, path)
            finally:
                Path(tmp).unlink(missing_ok=True)
            _INCREMENTED.add(key)
            _record({"action": "increment", "changed": changed, "map": path,
                     "mode": report.get("mode"),
                     "rerun_scripts": report.get("rerun_scripts") or [],
                     "wall_s": round(time.monotonic() - t0, 3), "ok": True})
            return True, None
        except MutationSelectionError as e:
            _record({"action": "increment", "changed": changed, "ok": False,
                     "error": str(e),
                     "wall_s": round(time.monotonic() - t0, 3)})
            raise
        except Exception as e:  # noqa: BLE001 — 增量失败不静默交付
            _record({"action": "increment", "changed": changed, "ok": False,
                     "error": str(e),
                     "wall_s": round(time.monotonic() - t0, 3)})
            raise MutationSelectionError("attribution_increment_failed",
                                         str(e)) from e


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
    from ..measurement.recipe import build_recipe, recipe_hash as _raw
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
        from ..measurement.orchestrator import run_measurement
        from ..measurement.recipe import build_recipe
        root = str(Path(project_root).resolve())
        recipe, error = build_recipe(Path(root))
        if error is not None:
            raise SelectionError(f"recipe unavailable: {error}")
        doc = load_map(path)
        targets = [str(Path(root) / f)
                   for f in (doc.get("target_files") or [])]
        if not targets:
            raise SelectionError("baseline missing target_files")
        tmp = _unique_tmp(path)
        try:
            run_measurement(root, targets, recipe, test_attribution=True,
                          attribution_out=tmp)
            validate_refresh(load_map(path), load_map(tmp))
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
        _REGENERATED.add(key)
        _record({"action": "regenerate", "map": path, "trigger": rel,
                 "wall_s": round(time.monotonic() - t0, 3), "ok": True})
        return True
    except Exception as e:  # noqa: BLE001
        _record({"action": "regenerate", "map": path, "trigger": rel,
                 "ok": False, "error": str(e),
                 "wall_s": round(time.monotonic() - t0, 3)})
        return False


def _finish(base: dict, strict: bool) -> dict:
    """收口：strict 模式下非 runtime_mapping 结果一律 fail-loud。"""
    if strict and base.get("source") != "runtime_mapping":
        raise MutationSelectionError(
            base.get("reason") or "attribution_selection_unavailable",
            _detail_of(base))
    return base


def _detail_of(base: dict) -> str | None:
    mp = base.get("mapping") or {}
    bits = []
    if mp:
        bits.append(f"file_sha256_bound={mp.get('file_sha256_bound')}")
        bits.append(f"recipe_ok={mp.get('recipe_hash_ok')}")
        bits.append(f"tests_face_ok={mp.get('tests_face_ok')}")
        bits.append(f"arms_valid={mp.get('arms_valid')}")
    if base.get("ambiguous_names"):
        bits.append(f"ambiguous={base['ambiguous_names'][:3]}")
    return ", ".join(bits) or None


def resolve_selection(project_root: str | Path, target_file: str,
                      map_path_override: str | None = None,
                      strict: bool = True) -> dict:
    """目标 → 文件级选面决策（严格模式不可用即 fail-loud）。

    mode=auto：映射过期 → 自动增量/重建一次后重判（台账披露）；仍不可用
    → MutationSelectionError（Owner 定稿：取消自动全脸）。strict=False 供
    证书归属等只读旁路使用（返回 full_face 描述，不抛）。"""
    root = str(Path(project_root).resolve())
    base = {"source": "full_face", "reason": None, "tests": [],
            "tests_glob": None, "zero_ref": False, "mapping": None,
            "ambiguous_names": []}
    path = map_path(root, map_path_override)
    if path is None:
        base["reason"] = "no_attribution_map"
        return _finish(base, strict)
    try:
        doc = load_map(path)
    except SelectionError as e:
        base["reason"] = f"map_invalid:{e}"
        return _finish(base, strict)
    try:
        rel = _file_rel(root, target_file)
    except ValueError:
        base["reason"] = "target_outside_project"
        return _finish(base, strict)
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
                return _finish(base, strict)
    base["mapping"] = evaluation["mapping"]
    if not evaluation["ok"]:
        base["reason"] = ("auto_update_still_stale:" if auto else "") + \
            evaluation["reason"]
        return _finish(base, strict)
    if evaluation["ambiguous"]:
        # 同名跨脚本 → 身份不可判定：不猜（严格模式 fail-loud）
        base["ambiguous_names"] = evaluation["ambiguous"]
        base["reason"] = "ambiguous_test_names"
        return _finish(base, strict)
    scripts = evaluation["scripts"]
    if not scripts:
        base.update({"source": "runtime_mapping", "zero_ref": True,
                     "reason": "mapping_says_no_tests"})
        return base
    try:
        sel_dir = materialize_selection(root, scripts)
    except OSError as e:
        base["reason"] = f"selection_materialize_failed:{e}"
        return _finish(base, strict)
    base.update({"source": "runtime_mapping", "tests": scripts,
                 "tests_glob": sel_dir, "reason": "fresh_runtime_mapping"})
    return base


def function_tests(project_root: str, doc: dict, rel: str,
                   fid: str) -> tuple[list[str], list[str]]:
    """映射内 function→tests（test 车道；多轮并集口径即产物边集本身）。

    返回 (测试脚本 res:// 列表, 同名歧义名列表)。"""
    names: set[str] = set()
    for identity, fids in ((doc.get("arms") or {}).get("gut_full", {})
                           .get("test_to_functions") or {}).items():
        if fid in fids:
            names.add(identity.split("::", 1)[0])
    by_name = _test_scripts_by_name(str(Path(project_root).resolve()))
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


def resolve_function_selection(project_root: str | Path, target_file: str,
                               fid: str,
                               file_selection: dict | None = None,
                               map_path_override: str | None = None,
                               strict: bool = True) -> dict:
    """fn 级选面：映射 fn→tests → 选择目录；fn 不在映射 → 文件级回退。

    三层：fn 命中 → `source=function_mapping`；fn 未命中（unknown/新函数）
    → 文件级集合并 `source=file_fallback`（披露 `function_not_in_mapping`）；
    文件级本身不可用 → fail-loud（全脸仅显式审计开关）。"""
    root = str(Path(project_root).resolve())
    file_sel = file_selection or resolve_selection(
        root, target_file, map_path_override, strict=strict)
    if file_sel.get("source") != "runtime_mapping" or file_sel.get("zero_ref"):
        return file_sel                      # 零引用终态 / 不可用（strict 已抛）
    path = (file_sel.get("mapping") or {}).get("path")
    try:
        doc = load_map(path) if path else load_map(map_path(root,
                                                            map_path_override))
        rel = _file_rel(root, target_file)
    except (SelectionError, ValueError) as e:
        raise MutationSelectionError("function_selection_load_failed",
                                     str(e)) from e
    scripts, ambiguous = function_tests(root, doc, rel, fid)
    if ambiguous:
        raise MutationSelectionError("ambiguous_test_names",
                                     str(sorted(ambiguous)[:5]))
    if not scripts:
        out = dict(file_sel)
        out.update({"source": "file_fallback",
                    "reason": "function_not_in_mapping",
                    "function_id": fid,
                    "tests": file_sel.get("tests") or [],
                    "tests_glob": file_sel.get("tests_glob")})
        return out
    try:
        sel_dir = materialize_selection(root, scripts)
    except OSError as e:
        raise MutationSelectionError("selection_materialize_failed",
                                     str(e)) from e
    return {"source": "function_mapping", "reason": "function_mapping_fresh",
            "function_id": fid, "tests": scripts, "tests_glob": sel_dir,
            "zero_ref": False, "mapping": file_sel.get("mapping"),
            "ambiguous_names": []}


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
