"""SEE-1631 §SPEC-020：mutation 判定基线的单一真源——attribution map 的
逐测试 verdict 表。

`baseline_source()`：`GQT_MUTATION_BASELINE_SOURCE` ∈ {map（默认）, cold}。
map 模式下 `_precompute_baselines` 不再冷启动 GUT——按 map 的 verdict 表
合成与既有快照同形的 baseline（rc/tail/elapsed），全部下游（`_loop_baseline`
复用、`_parse_failing_tests`、`_BASELINE_RED_MAP`、worker baseline_failures、
自适应超时锚）零改动消费。map 缺失/过期/与当前树不一致 → 具名
`BaselineSourceError` fail-loud（不静默退化全脸）；cold 为显式 opt-out。
parity 门（首 mutant 双通道预检）与 re-canary/cold 回退链保持原语义；
map 源下 parity 失配由 runner 升级为具名 fail-loud（见 runner）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_BASELINE_SOURCE = "GQT_MUTATION_BASELINE_SOURCE"

_SNAPSHOT_SOURCE = "map"


class BaselineSourceError(Exception):
    """map 源基线不可用（缺失/过期/与当前树不一致）——具名 fail-loud。"""


def baseline_source() -> str:
    raw = os.environ.get(ENV_BASELINE_SOURCE, "map").strip().lower()
    return raw if raw in ("map", "cold") else "map"


def _load_map_doc(project_root: str) -> dict:
    from .truth_select import MutationSelectionError, load_map, map_path

    path = map_path(Path(project_root).resolve())
    if not path:
        raise BaselineSourceError(
            "baseline_source=map: no attribution map at the default path — "
            "build one via `gqt measure run --test-attribution` / "
            "`measure map-update` (fail-loud: silent full-face fallback is "
            "forbidden by §SPEC-020)")
    try:
        return load_map(path)
    except MutationSelectionError as e:
        raise BaselineSourceError(
            f"baseline_source=map: attribution map unusable: {e}") from e


def _assert_map_schema(doc: dict) -> None:
    from ..measurement.attribution import ATTRIBUTION_SCHEMA

    if doc.get("schema_version") != ATTRIBUTION_SCHEMA:
        raise BaselineSourceError(
            f"baseline_source=map: schema {doc.get('schema_version')!r} "
            f"!= {ATTRIBUTION_SCHEMA!r}")


def _assert_map_arm_valid(doc: dict) -> None:
    states = doc.get("arms_state") or {}
    if not states or all(st.get("state") != "valid"
                         for st in states.values()):
        summary = {k: st.get("state") for k, st in states.items()}
        raise BaselineSourceError(
            f"baseline_source=map: no valid measurement arm in map "
            f"(arms_state={summary})")


def _assert_recipe_pristine(project_root: str, doc: dict) -> None:
    from .truth_select import _fresh_recipe_hash

    pristine = _fresh_recipe_hash(str(Path(project_root).resolve()))
    if pristine is not None and pristine != doc.get("recipe_hash_pristine"):
        raise BaselineSourceError(
            "baseline_source=map: recipe_hash_pristine stale vs current "
            "tree — rebuild the map")
    # pristine=None（fixture/不可构建 recipe）→ 跳过该项（其余门仍在）


def _assert_tests_face(project_root: str, doc: dict) -> None:
    import hashlib

    face = doc.get("tests_face") or {}
    drift = []
    for rel, want in face.items():
        p = Path(project_root) / rel
        if not p.is_file():
            drift.append(f"removed:{rel}")
            continue
        have = hashlib.sha256(p.read_bytes()).hexdigest()
        if have != want:
            drift.append(f"changed:{rel}")
    if drift:
        raise BaselineSourceError(
            f"baseline_source=map: tests_face drift — {sorted(drift)[:6]}")


def _assert_target_files(project_root: str, doc: dict,
                         files: list[str]) -> None:
    import hashlib

    root = Path(project_root).resolve()
    shas = doc.get("file_sha256") or {}
    # §SPEC-029：内容面 = file_sha256 **全集** 对账（不再只查被查询文件
    # ——D11 盲区根因）+ 树内新增生产 .gd 检出（分类清单驱动）。
    for rel, want in shas.items():
        p = root / rel
        if not p.is_file():
            raise BaselineSourceError(
                f"baseline_source=map: measured target removed — {rel}")
        have = hashlib.sha256(p.read_bytes()).hexdigest()
        if have != want:
            raise BaselineSourceError(
                f"baseline_source=map: target file drift — {rel}")
    for raw in files:
        p = Path(raw)
        rel = str(p.resolve().relative_to(root)) if p.is_absolute() \
            else str(p)
        want = shas.get(rel)
        if want and p.is_file():
            have = hashlib.sha256(p.read_bytes()).hexdigest()
            if have != want:
                raise BaselineSourceError(
                    f"baseline_source=map: target file drift — {rel}")
    _assert_no_new_production(root, doc)


def _assert_no_new_production(root: Path, doc: dict) -> None:
    """§SPEC-029 表 #2：树内新增宇宙文件（范围规则发现、不在 map
    file_sha256 全集）→ 判不新鲜。范围规则不闭合（路径拼错/白名单空）
    → 具名 fail-loud。文本内容文件（.tscn/.tres/csv/translation）与
    生产 .gd 同套逻辑（§SPEC-031 v8）。"""
    from ..measurement.classify import ScopeError, discover_scope

    try:
        # §SPEC-029 表 #4：新增检出只看生产类（非生产 .gd 不触发刷新）
        universe = discover_scope(root)["production"]
    except ScopeError as e:
        raise BaselineSourceError(
            f"baseline_source=map: {e}") from e
    shas = doc.get("file_sha256") or {}
    known = {("res://" + rel) for rel in shas}
    # 测试脚本由 tests_face 对账负责（表 #8）——不在宇宙新增判定内重复
    # 计数（fixture 无 gutconfig 时 tests/ 测试文件不误报 stale）。
    test_face = {("res://" + rel)
                 for rel in (doc.get("tests_face") or {})}
    new = [u for u in universe
           if u not in known and u not in test_face]
    if new:
        raise BaselineSourceError(
            f"baseline_source=map: {len(new)} new universe file(s) not in "
            f"map (map freshness covers the scope universe, "
            f"§SPEC-029/031) — {sorted(new)[:6]}")


def assert_map_fresh(project_root: str, doc: dict, files: list[str]) -> None:
    """map 与当前树一致性门（§SPEC-029 谓词单一实现；mutation 基线路径
    与 truth_select._evaluate/coverage 选面同源调用）：schema/臂执行证据/
    recipe pristine（含 project_face/工具链）/file_sha256 全集/tests_face
    /宇宙新增生产检出——任一失配即具名 fail-loud。"""
    _assert_map_schema(doc)
    _assert_map_arm_valid(doc)
    _assert_recipe_pristine(project_root, doc)
    _assert_tests_face(project_root, doc)
    _assert_target_files(project_root, doc, files)


# 兼容别名（模块内历史调用点）
_assert_fresh = assert_map_fresh


def _red_entries(doc: dict) -> "dict[str, tuple[str, str]]":
    """mutation 判定只消费可执行测试面（gut_full：`res://` 全路径与测试
    脚本 basename 两种形态；gherkin 身份不可执行、只披露不参与）。

    §SPEC-025：/7 产物缺 `test_verdicts` 键 = 表不守恒——具名 fail-loud，
    不得当空红集静默放行（旧 /6 产物已被 schema 门拒绝）。"""
    if "test_verdicts" not in doc:
        raise BaselineSourceError(
            "baseline_source=map: /7 attribution map is missing "
            "`test_verdicts` (verdict table not conserved; §SPEC-025) — "
            "rebuild the map")
    out: dict[str, tuple[str, str, str]] = {}
    for ident, entry in (doc.get("test_verdicts") or {}).items():
        if str(ident).startswith("gherkin://"):
            continue
        if entry.get("verdict") == "red":
            # §SPEC-030/D10：script 分量优先取表内完整路径（同名碰撞按
            # 完整路径消歧——基线合成不得按 basename 把另一目录绿杀手
            # 误跳过）；表内无 script 回退身份段（实机裸 basename 宇宙）。
            script = str(entry.get("script") or "") \
                or str(ident).split("::", 1)[0]
            out[ident] = (entry.get("classification") or "new",
                          script,
                          str(ident).split("::", 1)[1]
                          if "::" in str(ident) else "")
    return out


def _synthesize_tail(red_pairs: "set[tuple[str, str]]") -> str:
    """合成 GUT 输出形态的 tail：`res://script` 头 + `* test` + `[Failed]`
    行——`_parse_failing_tests` / `parse_failures_with_scripts` 逐位可解析
    （下游零改动消费）。"""
    lines: list[str] = []
    by_script: dict[str, list[str]] = {}
    for (script, name) in sorted(red_pairs):
        by_script.setdefault(script, []).append(name)
    for script, names in sorted(by_script.items()):
        lines.append(script)
        for name in names:
            lines.append(f"* {name}")
            lines.append(f"[Failed] {name}")
    lines.append("Run Summary")
    for script, names in sorted(by_script.items()):
        for name in names:
            lines.append(f"- {name}")
            lines.append(f"[Failed] {name}")
    return "\n".join(lines) + "\n"


def map_baseline_snapshots(files: list[str], project_root: str,
                           tests_glob: str | None,
                           select_mode: str = "full") -> "tuple[dict, dict]":
    """map verdict 表 → per-glob 合成基线快照（与 `_run_baseline_snapshot`
    同形：{rc, tail, elapsed}；elapsed=0 → 自适应超时锚回落参数值）。

    返回 (baselines, failures)：baselines 键与既有消费一致（scoped glob /
    None=全量面）；failures 恒空（map 源不产生"尝试失败"——不可用直接
    fail-loud）。tests_glob 过滤：None = 全部 gut 身份；非 None = 脚本
    前缀在 glob 目录内的身份。"""
    doc = _load_map_doc(project_root)
    _assert_fresh(project_root, doc, files)
    reds = _red_entries(doc)
    prefix = None
    if tests_glob:
        base = str(tests_glob)
        if base.startswith("res://"):
            base = base[len("res://"):]
        # 选面目录只承载选中脚本的 wrapper（wrapper 名 = 原脚本名）——
        # 按 basename 对应，不做目录前缀过滤（prefix 保持 None）。
        if ".gqt-attr-select/" not in base:
            prefix = base.rstrip("/") + "/"
    face_pairs: set[tuple[str, str]] = set()
    for ident, (_cls, script, name) in reds.items():
        rel = script[len("res://"):] if script.startswith("res://") else script
        if prefix is not None and not rel.startswith(prefix):
            continue
        face_pairs.add((script, name))
    baselines: dict = {}
    tail = _synthesize_tail(face_pairs)
    key = tests_glob if tests_glob is not None else None
    baselines[key] = {"rc": (1 if face_pairs else 0), "tail": tail,
                      "elapsed": 0.0, "source": _SNAPSHOT_SOURCE,
                      "red_names": sorted(n for _s, n in face_pairs),
                      "red_pairs": sorted(face_pairs)}
    return baselines, {}




def _sha_drift(root: Path, rel: str, want: "str | None",
               changed: "set[str]", drift: "set[str]") -> None:
    """单文件 sha 对账（缺失/漂移 → drift 分类；零行为拆分）。"""
    import hashlib

    p = root / rel
    if not p.is_file():
        drift.add(f"removed:{rel}")
        return
    have = hashlib.sha256(p.read_bytes()).hexdigest()
    if have != want:
        changed.add(rel)
        drift.add(f"changed:{rel}")


def _map_drift(doc: dict, project_root: str, files: list[str]
               ) -> "tuple[list[str], list[str]]":
    """map 与当前树的漂移分类 → (增量可续新的变更路径, 全部漂移描述)。

    增量可续新 = 目标生产文件 sha 漂移 + tests_face（测试脚本）sha 漂移
    （map-update 的增量语义覆盖）；recipe pristine 失配 / 结构性缺失
    → 只能全脸重建（增量列表为空但漂移描述在场）。"""
    import hashlib

    root = Path(project_root).resolve()
    changed: set[str] = set()
    drift: set[str] = set()
    shas = doc.get("file_sha256") or {}
    face = doc.get("tests_face") or {}

    # 目标生产文件（显式 files 优先，其余取 map 记录面）
    targets = {rel: want for rel, want in shas.items()}
    for raw in files:
        p = Path(raw)
        rel = str(p.resolve().relative_to(root)) if p.is_absolute() \
            else str(p)
        targets[rel] = shas.get(rel)
    for rel, want in targets.items():
        _sha_drift(root, rel, want, changed, drift)
    for rel, want in face.items():
        _sha_drift(root, rel, want, changed, drift)
    # §SPEC-029 表 #1/#2：全集对账——树内生产 .gd 全集（分类清单驱动）
    # 与 map 记录面比对：新增生产 .gd → 判不新鲜（重建后走既有
    # new_prod_file_fallback）；分类不闭合 → unclassified_source 具名
    # fail-loud。
    from ..measurement.classify import ScopeError, discover_scope

    try:
        universe = discover_scope(root)["production"]
    except ScopeError as e:
        raise BaselineSourceError(f"{e}") from e
    known = {("res://" + rel) for rel in shas}
    test_face = {("res://" + rel)
                 for rel in (doc.get("tests_face") or {})}
    for rp in universe:
        if rp in known or rp in test_face:
            continue
        rel = rp[len("res://"):]
        changed.add(rel)
        drift.add(f"new:{rel}")
    return sorted(changed), sorted(drift)


def _production_universe(project_root: str, files: list[str]) -> list[str]:
    """§SPEC-029：默认建图宇宙 = 范围规则 `.gd` 子集（§SPEC-031 v8
    白/黑名单驱动）；显式 roots 仅作窄宇宙诊断。规则不闭合 → 具名
    fail-loud。"""
    from ..measurement.classify import ScopeError, gd_universe

    root = Path(project_root).resolve()
    try:
        # §SPEC-032/D12：map 模块对外路径单一口径 = 绝对路径（classify
        # 的 res:// 形态只在 classify API 内部；此前 res:// 漏转换导致
        # ensure_map 锚出 <root>/res:/... → 宇宙为空 → 建图必败）
        return [str(root / rel.removeprefix("res://"))
                for rel in gd_universe(root)]
    except ScopeError as e:
        raise BaselineSourceError(f"{e}") from e


def _prepare_build_missing(project_root: str, files: list[str],
                           timeout_s: int) -> str:
    """阶梯第一级：map 缺失 → `ensure_map` 全脸自动建图（measure run
    同形态；宇宙 = §SPEC-031 清单 production 全集）。"""
    from .truth_select import MutationSelectionError, ensure_map

    build_files = _production_universe(project_root, files)
    print("mutation prepare: attribution map missing — building "
          f"(universe={len(build_files)} production .gd, §SPEC-021/029)",
          file=sys.stderr)
    try:
        ensure_map(project_root, build_files, timeout_s=timeout_s)
    except MutationSelectionError as e:
        raise BaselineSourceError(
            f"prepare renewal failed (map build): {e}") from e
    return _verify_prepared(project_root, build_files)


def _prepare_drift_state(doc: dict, project_root: str,
                         files: list[str]) -> "tuple[list[str], bool]":
    """map 在场漂移分类：增量可续新面 + pristine 失配并入漂移列表。
    返回 (drift, pristine_ok)。"""
    from .truth_select import _fresh_recipe_hash

    changed, drift = _map_drift(doc, project_root, files)
    pristine_ok = True
    pristine = _fresh_recipe_hash(str(Path(project_root).resolve()))
    if pristine is not None and pristine != doc.get("recipe_hash_pristine"):
        pristine_ok = False
        drift.append("recipe_hash_pristine")
    return changed, drift, pristine_ok


def _prepare_increment_renewal(doc: dict, project_root: str, path: str,
                               changed: list[str], timeout_s: int) -> None:
    """增量续新（只重跑失效面）：目标面取自基线 target_files（建图时
    的测量宇宙），recipe 由当前树现场构建（build_recipe 单源）。
    §SPEC-029 表 #5：不可归因变更（`uid://`/动态 load/非 .gd——
    increment 的 unknown_changed_paths 具名判定）→ 具名全量重建
    （不作为用户可见错误）。"""
    from ..measurement.increment import (
        IncrementError, run_attribution_increment,
    )
    from ..measurement.recipe import build_recipe

    root = Path(project_root).resolve()
    recipe, error = build_recipe(root)
    if error is not None:
        raise BaselineSourceError(
            f"prepare renewal failed (recipe build): {error}")
    targets = [str(root / t) for t in (doc.get("target_files") or [])]
    targets = [t for t in targets if Path(t).is_file()]
    if not targets:
        raise BaselineSourceError(
            "prepare renewal failed: no measurable targets in map")
    try:
        run_attribution_increment(
            project_root, targets, recipe, path, changed, path,
            timeout_s=timeout_s)
    except IncrementError as e:
        if "unknown_changed_paths" in str(e):
            print("mutation prepare: changed path(s) not attributable "
                  "(non-.gd / uid:// / dynamic load) — named full "
                  "rebuild (§SPEC-029 #5)", file=sys.stderr)
            return _prepare_full_rebuild(project_root, targets, path,
                                         timeout_s)
        raise BaselineSourceError(
            f"prepare renewal failed (increment): {e}") from e


def _prepare_full_rebuild(project_root: str, files: list[str], path: str,
                          timeout_s: int) -> None:
    """全脸重建（measure run 同形态；recipe pristine 失配/结构性漂移
    不可增量）——auto 管理的默认 map 允许安全重建，重建前移除旧
    产物（ensure_map 仅在不可复用时重建，stale 旧图须显式移除）。
    宇宙 = §SPEC-031 清单 production 全集（§SPEC-029）。"""
    from .truth_select import MutationSelectionError, ensure_map

    build_files = _production_universe(project_root, files)
    try:
        Path(path).unlink(missing_ok=True)
        ensure_map(project_root, build_files, timeout_s=timeout_s)
    except MutationSelectionError as e:
        raise BaselineSourceError(
            f"prepare renewal failed (rebuild): {e}") from e


def prepare_map_for_baseline(project_root: str, files: list[str],
                             timeout_s: int = 600) -> str:
    """§SPEC-021/032：map 源判定基线的 prepare（薄适配器）——阶梯本体
    已收口 `map_contract.refresh`（D1：create/increment/full 决策、锁内
    复检、装填后校验单源）；本适配器只返回可用 map 路径。"""
    from .map_contract import refresh

    refresh(project_root, files, timeout_s=timeout_s)
    # 旧防线保留（与契约装填后校验同向——verify 具名口径沿用）
    return _verify_prepared(project_root, files)


def _verify_prepared(project_root: str, files: list[str]) -> str:
    """续新后一致性复检：失配即具名 fail-loud（不静默进入 run）。"""
    from .truth_select import load_map, map_path

    path = map_path(Path(project_root).resolve())
    if not path:
        raise BaselineSourceError(
            "prepare renewal produced no map at the default path")
    try:
        doc = load_map(path)
        _assert_fresh(project_root, doc, files)
    except Exception as e:
        # 异常类型分类（MutationSelectionError 等）在此无差异披露——
        # 两路同文，收敛为单一具名出口（疑似未完成的分类意图，已登记
        # 交 Leader 复核）。
        raise BaselineSourceError(
            f"prepare renewal still inconsistent: {e}") from e
    states = doc.get("arms_state") or {}
    if not states or all(st.get("state") not in ("valid", "disabled")
                         for st in states.values()):
        raise BaselineSourceError(
            "prepare renewal produced a map without a valid arm")
    return path
