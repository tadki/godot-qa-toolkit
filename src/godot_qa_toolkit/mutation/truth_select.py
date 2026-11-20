
"""SEE-1581 — mutation 选面改「运行时真值映射」（A09 后静态归属零消费者）。

合同（Owner 2026-11-14 拍板 + ④ 实测数据）：

- **映射来源**：measurement 归属产物（`test-attribution/6`，行级主真值），
  路径经 `--attribution-map` 或环境变量 `GQT_ATTRIBUTION_MAP` 给出；
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
from ..digest import sha256_file

import hashlib
import json
import os
import shutil
import time
from functools import lru_cache
from pathlib import Path

# SEE-1587：行级主真值 schema。旧 /5 产物在 load_map / validate_refresh
# 处 fail-loud（schema 不符即拒），无兼容别名。
SCHEMA_VERSION = "test-attribution/6"
ENV_MAP = "GQT_ATTRIBUTION_MAP"
ENV_MODE = "GQT_ATTRIBUTION_MAP_MODE"   # "auto"（默认）| "off"
SELECT_ROOT = ".gqt-attr-select"   # 根级点目录：影子树会携带（.qa-cache 被排除）
DEFAULT_MAP_REL = ".qa-cache/attribution-map.json"

_LEDGER: list[dict] = []
_INCREMENTED: set[tuple[str, str]] = set()
_REGENERATED: set[str] = set()


def mode() -> str:
    return (os.environ.get(ENV_MODE) or "auto").strip().lower()


def map_event_counts_stale(doc: dict) -> bool:
    """SEE-1604 D4：增量产物的车道事件计数未重测（increment 块唯一出处）。

    结构边/行事实在增量中已刷新，但事件计数（hit_events/rates/phases/
    hotspots）不重测——消费侧不得把它当新鲜证据（显式产物具名拒绝）。"""
    return bool((doc.get("increment") or {}).get("event_counts_stale"))


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


from .truth_lines import (  # noqa: F401 — 迁移绑定保持引用面
    RuntimeOwnership,
    _line_script_sets,
    _resolve_script_paths,
    _tests_for_target,
    materialize_selection,
    ownership_for,
    parse_lines_spec,
)
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


def _build_gate_fail_reason(doc: dict) -> str | None:
    """S6.3 构建侧臂可用门（§SPEC-060a）：生成/增量的产物要求 gut_full
    臂 state==valid——方案 A 后基线吸收使 state 成为「既有失败已吸收、
    零新增」的权威判据；新增失败仍 invalid → 拒绝交付该产物。

    消费侧 `arms_valid`（执行完整性证据）保持原语义不动——SEE-1587 D3
    回归防护与旧产物可用性不因本门收紧。"""
    states = doc.get("arms_state") or {}
    if not arms_valid(states):
        return "no execution evidence (arms_valid)"
    gut = states.get("gut_full") or {}
    if gut.get("state") != "valid":
        return (f"gut_full arm invalid (state={gut.get('state')!r}, "
                f"reason={gut.get('invalid_reason')!r}) — new failures "
                f"gate the build (§SPEC-060a)")
    return None


def validate_refresh(before: dict, after: dict) -> None:
    """刷新产物守恒校验：失效即 fail-loud（不得静默交付失真映射）。

    允许变化的边：rerun_scripts（整体替换）与 dropped_scripts（删除）；
    其余测试身份必须原样保留——并发读-改-写导致映射失真（共享临时文件、
    无串行化；修复后并发刷新实测零身份丢失）在此显式拒绝。"""
    if after.get("schema_version") != SCHEMA_VERSION:
        raise MutationSelectionError("refresh_schema_mismatch")
    gate = _build_gate_fail_reason(after)
    if gate is not None:
        raise MutationSelectionError("refresh_arms_invalid", gate)
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


def _ensure_generate(root: str, out: str, files: list[str],
                     timeout_s: int | None, action: str,
                     rebuild_reason: str | None, t0: float) -> str:
    """`measure run --test-attribution` 一次（原子写）；台账披露。"""
    from ..measurement.orchestrator import run_measurement
    from ..measurement.recipe import build_recipe
    from ..projectroot import ensure_import_cache
    # SEE-1605：缺 `.godot` 缓存的插桩 GUT 零命中致生成落空——首个插桩 GUT
    # 前强制导入引导；warmed 时缓存校验廉价通过，失败由 ensure_map 具名外抛。
    ensure_import_cache(root)
    recipe, error = build_recipe(Path(root))
    if error is not None:
        raise SelectionError(f"recipe unavailable: {error}")
    targets = _targets_under(_roots_of(files, root))
    tmp = _unique_tmp(out)
    try:
        run_measurement(root, targets, recipe, test_attribution=True,
                      attribution_out=tmp,
                      **({"timeout_s": int(timeout_s)}
                         if timeout_s is not None else {}))
        doc = load_map(tmp)
        gate = _build_gate_fail_reason(doc)
        if gate is not None:
            raise MutationSelectionError(
                "generated mapping has invalid arms "
                f"({gate})")
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


def ensure_map(project_root: str | Path, files: list[str],
               timeout_s: int | None = None) -> str | None:
    """默认自动生成/重建（mode=auto，原子写 + `<map>.lock` 串行化）；
    显式 `--attribution-map` 只读透传绝不重建；失败台账记录后
    fail-loud（全脸仅显式 `--select full` 审计开关）。"""
    root = str(Path(project_root).resolve())
    if mode() == "off":
        return map_path(root)
    explicit = bool(os.environ.get(ENV_MAP))
    existing, rebuild_reason = _default_map_reuse(map_path(root), explicit)
    if existing:
        # 可用直接复用；显式产物不可用同样原样透传（绝不重建/删除用户
        # 指定文件——fail-loud 由 resolve_selection）。
        return existing
    # §SPEC-012 HIGH-2：默认位置旧 schema/无效状态 → 原子重建。
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
            out = _ensure_generate(root, out, files, timeout_s, action,
                                   rebuild_reason, t0)
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

def _increment_apply(root: str, path: str, changed: list[str],
                     timeout_s: int | None, t0: float) -> None:
    """锁内增量执行：唯一 tmp 落盘 + 守恒校验 + os.replace。"""
    from ..measurement.increment import run_attribution_increment
    from ..measurement.recipe import build_recipe

    recipe, error = build_recipe(Path(root))
    if error is not None:
        raise SelectionError(f"recipe unavailable: {error}")
    current = load_map(path)
    targets = [str(Path(root) / f)
               for f in (current.get("target_files") or [])]
    if not targets:
        raise SelectionError("baseline missing target_files")
    tmp = _unique_tmp(path)
    try:
        report = run_attribution_increment(
            root, targets, recipe, path, changed, tmp,
            timeout_s=int(timeout_s) if timeout_s is not None else 180)
        validate_refresh(load_map(path), load_map(tmp))
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    _record({"action": "increment", "changed": changed, "map": path,
             "mode": report.get("mode"),
             "rerun_scripts": report.get("rerun_scripts") or [],
             "wall_s": round(time.monotonic() - t0, 3), "ok": True})


def _auto_increment(project_root: str, path: str, doc: dict,
                    rel: str,
                    changed_extra: list[str] | None = None,
                    timeout_s: int | None = None
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
            _increment_apply(root, path, changed, timeout_s, t0)
            _INCREMENTED.add(key)
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
    """tests-face 指纹（A11 单一实现）：本体在 coverlink.current_tests_face，
    此处仅保留选面路径的进程内缓存语义。"""
    from .coverlink import current_tests_face
    return current_tests_face(project_root)


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
        return sha256_file(path)
    except OSError:
        return None


def arms_valid(states: dict) -> bool:
    """产物臂可用性：必须是**真执行过的测量**，空映射/零测试不是真值。

    SEE-1587 D3（QA 回归）：`state=="valid"` 不再直接放行——缺导入缓存等
    工具链失效会让 GUT 零测试退出 0，旧逻辑据此把 `state=valid/pairs=0`
    的产物当真值（后果：零引用短路输出假绿 `ok:true, mutants:0`）。
    判定（执行完整性证据优先）：
    - 无 bundle → False（没有测量产物）；
    - `probes_fired==0` 且非 vacuous → False（零测试/探针未点火 = 工具链
      ／接线失效，fail-loud；不得静默当作"空映射"）；
    - `pairs>0` → True（含 KOL 常态 run_error + 有边：既有失败面不影响
      归属观测）；
    - `pairs==0` 但探针确已点火/vacuous → True（**合法空面**：测试确已
      执行但无归属边/无可执行行）；
    - 旧产物（无 probes_fired 字段）→ 仅 `pairs>0` 可用，否则 False
      （无法证明测过 → fail-loud，重生成方向安全）。"""
    gut = (states or {}).get("gut_full") or {}
    bundles = int(gut.get("bundles") or 0)
    pairs = int(gut.get("pairs") or 0)
    if bundles <= 0:
        return False
    if pairs > 0:
        return True      # 有归属边 = 确已执行（含旧产物与 KOL run_error 常态）
    probes = int(gut.get("probes_fired") or 0)
    vacuous = bool(gut.get("vacuous"))
    # 无边场景：仅"探针确已点火"或 vacuous（零可执行行）可视为合法空面；
    # 零测试/无执行证据（含缺字段旧产物）→ False（fail-loud）
    return bool(vacuous or probes > 0)


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


def _regenerate(project_root: str, path: str, rel: str,
                timeout_s: int | None = None) -> bool:
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
                          attribution_out=tmp,
                          **({"timeout_s": int(timeout_s)}
                             if timeout_s is not None else {}))
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


def _resolve_renew(root, path, doc, rel, timeout_s) -> bool:
    """mode=auto 的映射续新：无 file_sha 基线 → 全量重建；有 → 增量更新
    （tests_face 漂移并集为 changed）。返回是否成功。"""
    if not (doc.get("file_sha256") or {}):
        return _regenerate(root, path, rel, timeout_s=timeout_s)
    drift = _tests_face_drift(root, doc)
    extra = drift["changed"] + drift["removed"]
    renewed, _err = _auto_increment(root, path, doc, rel,
                                    changed_extra=extra,
                                    timeout_s=timeout_s)
    return renewed


def _resolve_materialize(root, base, scripts, strict) -> dict:
    """新鲜映射脚本集 → 选择目录物化（OSError → 具名 reason / strict 抛）。"""
    try:
        sel_dir = materialize_selection(root, scripts)
    except OSError as e:
        base["reason"] = f"selection_materialize_failed:{e}"
        return _finish(base, strict)
    base.update({"source": "runtime_mapping", "tests": scripts,
                 "tests_glob": sel_dir, "reason": "fresh_runtime_mapping"})
    return base


def _resolve_entry_doc(root, path, target_file, base, strict) -> tuple:
    """映射装载与 rel 解析守卫；失败直接返回 (None, finished_base)。"""
    try:
        doc = load_map(path)
    except SelectionError as e:
        base["reason"] = f"map_invalid:{e}"
        return None, _finish(base, strict)
    try:
        rel = _file_rel(root, target_file)
    except ValueError:
        base["reason"] = "target_outside_project"
        return None, _finish(base, strict)
    return (doc, rel), None


def _explicit_map_source(map_path_override: str | None) -> bool:
    """显式映射判定（`--attribution-map` / ENV_MAP）——D5 只读透传契约的
    适用面唯一出处。"""
    return bool(map_path_override) or bool(os.environ.get(ENV_MAP))


def _explicit_stale_gate(explicit: bool, doc: dict, base: dict,
                         strict: bool) -> "dict | None":
    """D4 显式事件计数门：显式产物带 increment.event_counts_stale 时具名
    拒绝——事件计数未重测，不得当新鲜证据消费（结构事实虽新但不是本轮
    可认证的事件证据；零续新/零 GUT）。返回终态 base，放行返回 None。"""
    if explicit and map_event_counts_stale(doc):
        base["reason"] = "event_counts_stale"
        return _finish(base, strict)
    return None


def _auto_renew_step(root, path, doc, rel, evaluation, *, explicit, auto,
                     renew, timeout_s, base, strict) -> "tuple[dict, dict | None]":
    """过期映射的 auto 续新一步（D2/D5 语义固定于此）：显式产物绝不续新
    （只读透传），renew=False（dry-run）抑制全部续新——零 GUT；续新成功
    返回刷新后的 evaluation，续新后装载失败返回终态 base。"""
    if evaluation["ok"] or not (auto and renew and not explicit):
        return evaluation, None
    if not _resolve_renew(root, path, doc, rel, timeout_s):
        return evaluation, None
    try:
        renewed = _evaluate(root, rel, load_map(path))
        renewed["mapping"]["path"] = path
        return renewed, None
    except SelectionError as e:
        base["reason"] = f"auto_update_reload_failed:{e}"
        return evaluation, _finish(base, strict)


def _stale_decision_reason(evaluation_reason: str, *, explicit: bool,
                           auto: bool) -> str:
    """过期终态 reason 合成：显式产物只读透传契约要求具名 explicit_map_stale
    （绝不原地重建）；auto 管理的默认映射标注 still_stale（续新已试无效）。"""
    if explicit:
        return f"explicit_map_stale:{evaluation_reason}"
    return ("auto_update_still_stale:" if auto else "") + evaluation_reason


def resolve_selection(project_root: str | Path, target_file: str,
                      map_path_override: str | None = None,
                      strict: bool = True,
                      timeout_s: int | None = None,
                      renew: bool = True) -> dict:
    """目标 → 文件级选面决策（严格模式不可用即 fail-loud）。mode=auto：
    映射过期自动增量/重建一次后重判（台账披露）；strict=False 供证书
    归属等只读旁路（返回 full_face 描述，不抛）。

    SEE-1604 D5/D4：显式映射默认路径（`--attribution-map` / ENV_MAP）只读
    透传——过期绝不续新/重建/改写（fail-loud 具名），且拒绝 event_counts_stale
    的增量产物；auto 管理的默认映射才走续新。renew=False（dry-run）抑制
    全部续新（零 GUT）。语义分驻 `_explicit_stale_gate` / `_auto_renew_step`
    / `_stale_decision_reason`。"""
    root = str(Path(project_root).resolve())
    base = {"source": "full_face", "reason": None, "tests": [],
            "tests_glob": None, "zero_ref": False, "mapping": None,
            "ambiguous_names": []}
    path = map_path(root, map_path_override)
    if path is None:
        base["reason"] = "no_attribution_map"
        return _finish(base, strict)
    explicit = _explicit_map_source(map_path_override)
    loaded, done = _resolve_entry_doc(root, path, target_file, base, strict)
    if done is not None:
        return done
    doc, rel = loaded
    terminal = _explicit_stale_gate(explicit, doc, base, strict)
    if terminal is not None:
        return terminal
    auto = mode() == "auto"
    evaluation = _evaluate(root, rel, doc)
    evaluation["mapping"]["path"] = path
    evaluation, terminal = _auto_renew_step(
        root, path, doc, rel, evaluation, explicit=explicit, auto=auto,
        renew=renew, timeout_s=timeout_s, base=base, strict=strict)
    if terminal is not None:
        return terminal
    base["mapping"] = evaluation["mapping"]
    if not evaluation["ok"]:
        base["reason"] = _stale_decision_reason(
            evaluation["reason"], explicit=explicit, auto=auto)
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
    return _resolve_materialize(root, base, scripts, strict)


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


# ---------------------------------------------------------------- 行级选面
# SEE-1587 §SPEC-004：默认 mutation 选面语义——行事实反查 line→tests，
# 同签名行集合共享一个选择目录 glob；无映射测试的行默认标 uncovered、
# 卡尺显式披露 no_mapped_tests 后执行（公平重跑通道，不静默丢弃）。


def _line_group_load(root, target_file, file_sel) -> tuple:
    """runtime_mapping 守卫（S2.2 必携带 resolved map path——缺失具名
    fail-loud，绝不回退默认路径重猜）+ 映射装载与 rel 解析。"""
    path = (file_sel.get("mapping") or {}).get("path")
    if not path:
        raise MutationSelectionError(
            "selection_metadata_inconsistent",
            "line_group: source=runtime_mapping without a resolved map "
            "path — refusing to re-guess the default attribution map")
    try:
        doc = load_map(path)
        rel = _file_rel(root, target_file)
    except (SelectionError, ValueError) as e:
        raise MutationSelectionError("line_selection_load_failed",
                                     str(e)) from e
    return doc, rel


def resolve_line_group_selection(project_root: str | Path, target_file: str,
                                 lines: "list[int] | set[int]",
                                 file_selection: dict | None = None,
                                 map_path_override: str | None = None,
                                 strict: bool = True) -> dict:
    """行集合 → 测试脚本并集 → 单一选择目录 glob（行级选面唯一入口）。

    返回 dict：source ∈ {line_mapping, no_mapped_tests, file-level 透传}；
    `no_mapped_tests` = 请求行中无任何映射测试的行（默认路径消费侧标
    uncovered；卡尺消费侧披露后执行）。文件级不可用时：strict →
    MutationSelectionError（fail-loud），非 strict 透传 file_sel。"""
    root = str(Path(project_root).resolve())
    want = {int(l) for l in lines}
    file_sel = file_selection or resolve_selection(
        root, target_file, map_path_override, strict=strict)
    if file_sel.get("source") != "runtime_mapping" or file_sel.get("zero_ref"):
        return file_sel
    doc, rel = _line_group_load(root, target_file, file_sel)
    per_line = _line_script_sets(doc, rel, want)
    names: set[str] = set()
    for scripts in per_line.values():
        names |= scripts
    no_mapped = sorted(ln for ln in want if not per_line.get(ln))
    by_name = _test_scripts_by_name(root)
    scripts, ambiguous = _resolve_script_paths(by_name, names)
    if ambiguous:
        raise MutationSelectionError("ambiguous_test_names",
                                     str(sorted(ambiguous)[:5]))
    # SEE-1610 §SPEC-IMPL：组内 test 级 allowlist（--lines 串行路径与
    # planning 并行 chunk 同源；runner 入口按开关解析后下发宿主）。
    from .truth_lines import line_identity_sets
    idents, lane = line_identity_sets(doc, rel, want)
    ident_set: set[str] = set()
    for ls in idents.values():
        ident_set.update(ls)
    tests_allow = {"identities": sorted(ident_set),
                   "script_lane": sorted(lane)}
    base = {"no_mapped_tests": no_mapped, "lines": sorted(want),
            "mapping": file_sel.get("mapping"), "zero_ref": False,
            "ambiguous_names": [],
            "tests_allow": tests_allow}
    if not scripts:
        return {**base, "source": "no_mapped_tests",
                "reason": "line_has_no_mapped_tests", "tests": [],
                "tests_glob": None}
    try:
        sel_dir = materialize_selection(root, scripts)
    except OSError as e:
        raise MutationSelectionError("selection_materialize_failed",
                                     str(e)) from e
    return {**base, "source": "line_mapping", "reason": "line_mapping_fresh",
            "tests": scripts, "tests_glob": sel_dir}

def _fn_group_load(root, target_file, file_sel) -> tuple:
    """runtime_mapping 守卫（S2.2）+ 映射装载与 rel 解析（fn 面）。"""
    path = (file_sel.get("mapping") or {}).get("path")
    if not path:
        raise MutationSelectionError(
            "selection_metadata_inconsistent",
            "function: source=runtime_mapping without a resolved map path "
            "— refusing to re-guess the default attribution map")
    try:
        doc = load_map(path)
        rel = _file_rel(root, target_file)
    except (SelectionError, ValueError) as e:
        raise MutationSelectionError("function_selection_load_failed",
                                     str(e)) from e
    return doc, rel


def resolve_function_selection(project_root: str | Path, target_file: str,
                               fid: str,
                               file_selection: dict | None = None,
                               map_path_override: str | None = None,
                               strict: bool = True) -> dict:
    """fn 级选面：fn 命中 → function_mapping；未命中 → 文件级回退
    （file_fallback，披露 function_not_in_mapping）；文件级不可用 →
    fail-loud（全脸仅显式审计开关）。"""
    root = str(Path(project_root).resolve())
    file_sel = file_selection or resolve_selection(
        root, target_file, map_path_override, strict=strict)
    if file_sel.get("source") != "runtime_mapping" or file_sel.get("zero_ref"):
        return file_sel                      # 零引用终态 / 不可用（strict 已抛）
    doc, rel = _fn_group_load(root, target_file, file_sel)
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







