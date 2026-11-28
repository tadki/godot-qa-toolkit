"""SEE-1631 §SPEC-032（D1 定案）：map 模块的使用契约——对外只有两个
维护动作 `status`（只读）与 `refresh`（prepare 专用），fresh 时返回薄
只读视图 `MapView`（只作为返回值，消费方不得从裸路径自行构造绕过判定）。

- `status`：零续新、零 GUT、零 import、零写盘；按 map 自身输入全集判
  新鲜（§SPEC-029 全集对账），逐因素具名漂移与不可定位原因；
  `would_refresh` 含范围与最近一次实测成本或 "unknown"。
- `refresh`：prepare 专用——内部决定 create/increment/full；锁内先
  status 复检 fresh 即合并返回；失败或复检仍 stale 时具名失败且旧图
  字节不变（原子装填由既有 ensure_map/increment 原子写承担）。
- `MapView` 构造期三断言（QA 反例转常驻测试）：§SPEC-025 verdict 表
  在场、§SPEC-028 dropped 无残留、§SPEC-015 invalid 臂边不出场。

运行期唯一门仍是既有 `_assert_fresh` 五查；消费方自行解析裸 map JSON
属于违反契约（评审面）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .verdict_source import (
    BaselineSourceError,
    _assert_map_arm_valid,
    _assert_map_schema,
    _features_disk_sha256,
    _load_map_doc,
    assert_map_fresh,
)
from .truth_select import map_path


# 强制全量重建的因素前缀（结构性缺失：schema/arms/recipe 失配、缺判定
# 表、视图构造失败、缺 `feature_sha256` 键）——`status` 的
# would_refresh=full 判定与 `measure map-update` 输出的 reason 抽取
# 共用同一份（C3-R20 F1：全量原因具名）。
_FORCED_FULL_FACTORS: "tuple[str, ...]" = (
    "schema:", "arms:", "recipe:", "report_recipe_hash:", "test_verdicts:",
    "view:", "feature_sha256_missing:",
)


def _report_hash_drift_reason(root: str, doc: dict) -> "str | None":
    """工具链面漂移具名（C3-R22 D3-b 引入；C3-R23 R1 起新鲜路径同样
    探测）——地图存储的 report_recipe_hash 与现算注入哈希不同 = 建图后
    工具链/环境漂移（Godot/GUT/gqt 版本或 `GQT_GODOT_BIN` 指向变化），
    增量资格闸（`_increment_decision` 的 stored == 现算）恒假 → 决策必
    为具名全量。只读：零写盘、零 GUT；现算走
    `truth_select._current_report_recipe_hash`（与 run 侧谓词同一实现
    与进程内缓存）。"""
    stored = doc.get("report_recipe_hash")
    if not (isinstance(stored, str) and stored):
        return None
    from .truth_select import _current_report_recipe_hash

    computed = _current_report_recipe_hash(str(Path(root).resolve()))
    if computed is None and doc.get("recipe_buildable"):
        # C3-R28（D4）：建图时可构建、现在不可构建 → 具名全量原因
        # （调用方带 `report_recipe_hash:` 前缀，属强制全量因素族）。
        return ("recipe unbuildable from the current tree — the map was "
                "built from a buildable recipe face but the recipe can "
                "no longer be built (config face deleted/corrupted/"
                "unparseable since the build, e.g. .gutconfig.json) — "
                "restore the config, then full rebuild")
    if computed is None or computed == stored:
        return None
    return (f"stored {str(stored)[:12]} != computed {str(computed)[:12]} "
            "(toolchain/environment drift since the map was built) — "
            "full rebuild required")


class MapContractError(Exception):
    """map 契约违反（视图构造期断言失败 / refresh 失败不装半成品）。"""


def _factor_report(root: str, doc: dict, files: list[str]) -> "tuple[bool, list[str]]":
    """逐因素新鲜度报告 → (fresh, drift_factors)。复用既有谓词的各分项
    探测（不抛——收集全部失配因素具名返回）。"""
    factors: list[str] = []
    try:
        _assert_map_schema(doc)
    except BaselineSourceError as e:
        factors.append(f"schema: {e}")
    try:
        _assert_map_arm_valid(doc)
    except BaselineSourceError as e:
        factors.append(f"arms: {e}")
    from .verdict_source import (
        _assert_recipe_pristine,
        _assert_target_files,
        _assert_tests_face,
    )
    for probe in (_assert_recipe_pristine,):
        try:
            probe(root, doc)
        except BaselineSourceError as e:
            factors.append(f"recipe: {e}")
    try:
        _assert_tests_face(root, doc)
    except BaselineSourceError as e:
        factors.append(f"tests_face: {e}")
    try:
        # C3-R21（LOW ②）：`_assert_target_files` 末尾已调用
        # `_assert_no_new_production`（生产内容指纹对账，载荷
        # changed:<file>）——不再以 new_production: 标签重复包装同一次
        # 探测（标签与载荷语义一致，且因素不重复计数）。
        _assert_target_files(root, doc, files)
    except BaselineSourceError as e:
        factors.append(f"content: {e}")
    # C3-R15/R18 契约自洽缺口：各项因素新鲜但缺 `test_verdicts` →
    # 视图构造必失败 → 视为不新鲜（具名因素，走一次全量重建补表），
    # 不再要用户手删地图。
    if "test_verdicts" not in doc:
        factors.append(
            "test_verdicts: missing — map cannot be consumed as fresh; "
            "full rebuild required")
    # C3-R15（定案 4/§SPEC-016）：feature 内容失效面纳入新鲜度判定——
    # feature_sha256 已摘出 wholesale recipe hash（R14），但 status 必须
    # 仍对账（否则 feature 编辑被判完全新鲜、gherkin 过期红绿判定留给
    # mutation）。漂移 → fresh=false、scope 走 increment（per-feature
    # 重跑通道）；wholesale recipe hash 保持排除 feature 内容。
    feature_drift = _feature_face_drift(root, doc)
    if feature_drift:
        # C3-R19（P4）：缺键因素的文案自带 `feature_sha256_missing:`
        # 标签——不再外层再包一层 `feature_sha256: `（外层前缀让
        # status 的 forced-full 前缀判断永不命中 → 预言 increment）。
        if feature_drift.startswith("feature_sha256_missing:"):
            factors.append(feature_drift)
        else:
            factors.append(f"feature_sha256: {feature_drift}")
    return (not factors), factors


def _feature_face_drift(root: str, doc: dict) -> "str | None":
    """feature 内容指纹对账（status 只读探测）：返回漂移描述或 None。

    与 `_map_drift` 的 `_feature_drift`（增量变更面收集）同一对账源——
    status 面答"新不新鲜"，增量面答"重跑什么"。基线无 feature_sha256
    （旧产物）→ 保守返回 None（不误报，等首次全量补齐）。"""
    features_dir = Path(root) / "addons" / "gqt_gherkin" / "features"
    if not features_dir.is_dir():
        return None
    baseline_sha = doc.get("feature_sha256")
    if not isinstance(baseline_sha, dict):
        # C3-R16（退回项 2）：项目有 feature 而 map 无指纹（直建产物
        # 缺键 / 旧产物）→ 具名因素走一次全量重建，不得当 fresh。
        # C3-R18：`_missing` 后缀供 status 区分——缺键归 full，内容变化
        # 走 per-feature 增量。
        return ("feature_sha256_missing: missing (map carries no "
                "feature_sha256 — direct-build artifact predates feature "
                "fingerprinting); full rebuild required")
    disk_sha = {k: sha for k, (_rel, sha)
                in _features_disk_sha256(Path(root)).items()}
    changed = [k for k in sorted(set(disk_sha) & set(baseline_sha))
               if disk_sha[k] != baseline_sha[k]]
    new = sorted(set(disk_sha) - set(baseline_sha))
    removed = sorted(set(baseline_sha) - set(disk_sha))
    parts: list[str] = []
    if changed:
        parts.append(f"changed={changed[:4]}")
    if new:
        parts.append(f"new={new[:4]}")
    if removed:
        parts.append(f"removed={removed[:4]}")
    return "; ".join(parts) if parts else None


def _refresh_scope(factors: "list[str]") -> str:
    """漂移因素 → 刷新范围（create/increment/full；D1 决策：recipe/
    环境面失配 = 具名全量，内容面 = 增量）。"""
    if any(f.startswith(("schema:", "arms:", "recipe:")) for f in factors):
        return "full"
    return "increment"


def _planned_action(root: str, doc: dict,
                    files: "list[str]") -> "tuple[str, str | None]":
    """C3-R9 退回3：镜像 `refresh` 的真实决策树预测动作（increment/full）。

    status.would_refresh 必须与 refresh 实际执行的动作一致——周期校准
    （calibration_due_fallback）、新增生产文件（new_prod_file_fallback）、
    不可归因变更（unknown_changed_paths → 具名全量）、报告哈希失配都落
    全量，其余走增量。决策面直接复用 `_increment_decision`（与增量续新
    同一实现，预测即所见）。只读：零 GUT、零 import、零写盘。

    C3-R21（D1 判定处置）：第二返回值 = 预言为全量时的具名原因（自带
    标签前缀：report_recipe_hash / recipe / increment_not_eligible），
    `status` 原样列进 factors。C3-R22（决策 15 同类扫描）：三个提前
    返回（pristine 失配 / recipe 构建失败 / 增量资格异常）均不再吞掉
    具名原因——每条 full 路径都必须留下因素。"""
    from pathlib import Path as _P

    from .verdict_source import _prepare_drift_state

    root_path = _P(root)
    changed, _drift, pristine_ok = _prepare_drift_state(doc, root, files)
    from ..measurement.increment import (
        DEFAULT_GHERKIN_RERUN_FULL_FACE_RATIO,
        DEFAULT_RERUN_FULL_FACE_RATIO,
        _increment_decision, _inject_toolchain,
    )
    from ..measurement.recipe import build_recipe

    recipe, error = build_recipe(root_path)
    if error is not None:
        if not pristine_ok:
            return "full", None  # pristine 因素已具名（factors 在场）
        return "full", f"recipe: build failed vs current tree: {error}"
    injected = _inject_toolchain(root_path, dict(recipe))
    drift: "str | None" = None
    if doc.get("report_recipe_hash") != injected.get("recipe_hash"):
        drift = (f"report_recipe_hash: stored "
                 f"{str(doc.get('report_recipe_hash'))[:12]} != "
                 f"computed {str(injected.get('recipe_hash'))[:12]} "
                 "(toolchain/environment drift since the map was built) — "
                 "full rebuild required")
    if not pristine_ok:
        # pristine 因素已在 factors（recipe: 前缀）；哈希漂移具名不吞。
        return "full", drift
    try:
        _classes, _inv, _rerun, reason = _increment_decision(
            root_path, injected, doc,
            changed,
            int((doc.get("increment") or {})
                .get("increments_since_full") or 0),
            calibration_every=5,
            rerun_full_face_ratio=DEFAULT_RERUN_FULL_FACE_RATIO,
            gherkin_rerun_full_face_ratio=DEFAULT_GHERKIN_RERUN_FULL_FACE_RATIO)
    except Exception as e:  # noqa: BLE001 — unknown_changed_paths 等具名拒绝
        return "full", drift or f"increment_not_eligible: {e}"
    if reason != "incremental":
        return "full", drift
    return "increment", None


def _last_measured_cost(doc: dict) -> "str | float":
    """最近一次实测成本（增量/建图墙钟；无记录 = "unknown"）。"""
    inc = doc.get("increment") or {}
    wall = inc.get("arm_exec_s")
    if isinstance(wall, (int, float)):
        return wall
    return "unknown"


def status(project_root: str, files: "list[str] | None" = None) -> dict:
    """只读维护动作：零续新、零 GUT、零 import、零写盘。

    返回 {"present", "fresh", "factors", "would_refresh", "scope",
    "last_cost", "view"}——`would_refresh` 含范围与最近一次实测成本或
    "unknown"；`view` 为 fresh 时的薄只读视图（契约违反不下发，None，
    §SPEC-035/S5）；map 缺失 → present=False / would_refresh="create"。"""
    files = files or []
    root = str(Path(project_root).resolve())
    try:
        doc = _load_map_doc(root)
    except BaselineSourceError as e:
        msg = str(e)
        missing = "no attribution map" in msg
        return {"present": False, "fresh": False,
                "factors": [msg], "would_refresh": "create",
                "scope": ["attribution map missing"],
                "last_cost": "unknown",
                "error": msg, "missing": missing}
    fresh, factors = _factor_report(root, doc, files)
    # C3-R23（R1/Leader 复核）：工具链面在**所有**路径上探测——新鲜
    # 判定 = 磁盘树面（pristine，`_factor_report` 的 recipe: 因素）与
    # 工具链面（存储 report_recipe_hash vs 现算注入）同时一致；任一
    # 漂移 → 不新鲜、`report_recipe_hash:` 因素具名（前缀在
    # `_FORCED_FULL_FACTORS` → would_refresh=full）。
    drift_reason = _report_hash_drift_reason(root, doc)
    if drift_reason:
        factors.append(f"report_recipe_hash: {drift_reason}")
        fresh = False
    if fresh:
        # §SPEC-035/S5：fresh 时返回同一薄只读视图。C3-R19（P5/要点①）：
        # 各项因素都通过但视图构造失败（判定表残留、dropped 身份残留等
        # 契约自洽缺口）→ 追加一条具名因素并按不新鲜处理（由 prepare
        # 全量重建），不再吞掉断言失败冒充 fresh（view=None）。
        try:
            view = MapView(root, doc, map_path=map_path(Path(root)))
        except MapContractError as e:
            factors.append(f"view: construction failed — {e}")
            fresh = False
        else:
            return {"present": True, "fresh": True, "factors": [],
                    "would_refresh": None, "scope": [],
                    "last_cost": _last_measured_cost(doc), "view": view}
    # C3-R9 退回3 + C3-R19（要点①）：would_refresh 如实预测 refresh 将
    # 执行的动作（含周期校准、新增生产文件、不可归因变更三类具名全量）。
    # 强制全量的因素：schema、arms、recipe、缺判定表、视图构造失败、缺
    # `feature_sha256` 键；其余才走决策镜像（增量判定）。
    if any(f.startswith(_FORCED_FULL_FACTORS) for f in factors):
        # C3-R17：缺 `feature_sha256` 键（因素文案自带 "full rebuild
        # required"）与 schema/arms 同列——would_refresh 必须如实预测
        # refresh 将执行的全量重建，不落增量判定。
        scope = "full"
    else:
        try:
            scope, named_reason = _planned_action(root, doc, files)
        except Exception:  # noqa: BLE001 — 预测失败按因素面口径兜底
            scope, named_reason = _refresh_scope(factors), None
        if named_reason:
            # C3-R21（D1）+ C3-R22（决策 15 扫描）：预言为全量时原因具名
            # ——reason 自带标签前缀（report_recipe_hash / recipe /
            # increment_not_eligible），原样列进 factors。
            factors.append(named_reason)
    return {"present": True, "fresh": False, "factors": factors,
            "would_refresh": scope,
            "scope": [f.split(":", 1)[0] for f in factors] if factors
            else [],
            "last_cost": _last_measured_cost(doc)}


def view(project_root: str) -> MapView:
    """C3-R18（定案 22）：只读取视图的唯一入口（零续新、零写盘）——
    map 模块之外不得裸读/自行查路径。

    fresh 且三断言通过 → MapView（含 map_path 披露）；不新鲜、缺键、
    视图构造失败 → 具名 `MapContractError`（消费方按不新鲜处理，由
    prepare 重建）。"""
    # map_path 的函数内 import 是打桩 seam（打桩点在 truth_select 模块
    # 属性，与 `_refresh_locked` 的 fresh 分支同一约定）——不是冗余绑定。
    from .truth_select import map_path

    root = str(Path(project_root).resolve())
    path = map_path(Path(root))
    if not path:
        raise MapContractError(
            "no attribution map at the default location — run any gqt "
            "command with preparation (e.g. measure coverage) to build it")
    st = status(root, [])
    if not st.get("fresh"):
        raise MapContractError(
            "attribution map is not fresh: "
            + "; ".join(st.get("factors") or []))
    try:
        doc = _load_map_doc(root)
        return MapView(root, doc, map_path=path)
    except MapContractError:
        # 契约自洽缺口（R15/定案 22）：判定新鲜但视图构造失败 → 视为
        # 不新鲜（由 prepare 重建），不要求用户手删地图。
        raise MapContractError(
            "attribution map judged fresh but view construction failed "
            "— treating as stale; prepare will rebuild")


def refresh(project_root: str, files: "list[str] | None" = None,
            timeout_s: int = 600) -> dict:
    """prepare 专用维护动作：内部决定 create/increment/full。

    C3-R14（§SPEC-032③）全程持 `<map>.lock`：锁内 status 复检 fresh 即
    合并返回（并发对端已刷完——零多余增量腿，双方 rc=0、增量台账恰 1
    条）；否则走既有续新管线（唯一 tmp 原子装填），装填后
    `assert_map_fresh` 校验——失败或复检仍 stale → 具名
    `MapContractError`，旧图字节不变。成功返回 {"fresh": True, "view":
    MapView}。锁可重入（进程内续新腿复用同一把）。"""
    files = files or []
    root = str(Path(project_root).resolve())
    from .truth_select import _refresh_lock, map_path

    lock_path = map_path(Path(root)) or str(
        Path(root) / ".qa-cache" / "attribution-map.json")
    with _refresh_lock(lock_path):
        return _refresh_locked(root, files, timeout_s)


def _refresh_locked(root: str, files: "list[str]",
                    timeout_s: int) -> dict:
    """锁内段：复检 + 续新阶梯 + 装填后校验（调用方持 `<map>.lock`）。"""
    # C3-R19（P1）：import 收敛到函数头——旧写法在分支之后再 import
    # `map_path`，使它在整个函数域成为局部名，锁内复检 fresh 的早退
    # 分支触发 UnboundLocalError（已有新鲜地图时四个入口全坏）。
    from .truth_select import (
        MutationSelectionError, SelectionError, load_map, map_path,
    )
    from .verdict_source import (
        _prepare_build_missing,
        _prepare_drift_state,
        _prepare_full_rebuild,
        _prepare_increment_renewal,
    )

    # 锁内复检（D1 并发修复：后到者不重复增量）——status 只读探测；
    # fresh 时 status 已按要点①验证视图可构造（构造失败按不新鲜处理），
    # 此处按同一 doc 重建视图（含 map_path 披露）。
    st = status(root, files)
    if st["fresh"]:
        doc = _load_map_doc(root)
        return {"fresh": True, "already_fresh": True,
                "view": MapView(root, doc,
                                map_path=map_path(Path(root)))}
    # C3-R19（要点②）+ C3-R20（F1）：status 的预言即本函数的实际动作。
    # 直接全量重建只留给结构性缺失（无可续新内容：缺判定表 / 视图构造
    # 失败 / schema/arms 失配 / 缺 feature_sha256 键）与 pristine 失配；
    # 内容面有可续新改动时一律经增量腿——腿的具名全量回退把原因写进
    # 地图台账（`increment.reason`；不可定位变更另有具名 stderr 说明），
    # 预言 full 不再绕过它直接重建（C3-R19 曾使具名回退在生产路径不可
    # 达——Leader 复核退回项）。腿的决策与 status 预言同源
    # （`_increment_decision`），预言与实际动作保持一致。
    path = map_path(Path(root))
    try:
        if not path or not Path(path).is_file():
            _prepare_build_missing(root, files, timeout_s)
        else:
            try:
                doc = load_map(path)
            except SelectionError as e:
                # C3-R12：坏 JSON 等装载失败（SelectionError 基类，含
                # MutationSelectionError）→ 具名 MapContractError
                raise MapContractError(
                    f"refresh failed (map unreadable): {e}") from e
            changed, drift, pristine_ok = _prepare_drift_state(doc, root,
                                                               files)
            if changed and drift and pristine_ok:
                _prepare_increment_renewal(doc, root, path, changed,
                                           timeout_s)
            else:
                _prepare_full_rebuild(root, files, path, timeout_s)
    except (MapContractError, BaselineSourceError, SelectionError,
            MutationSelectionError):
        raise
    except Exception as e:  # noqa: BLE001 — C3-R19（P7/要点⑤）：续新管线
        # 的任何失败（gherkin 增量腿的 GherkinRunError/SuiteError/
        # AddonError 一族）都以契约错误类型出 refresh——四个入口才是
        # 具名 JSON、零 traceback、旧图字节不变。
        raise MapContractError(
            f"renewal pipeline failed ({type(e).__name__}): {e}") from e
    # 装填后校验（复检仍 stale → 具名失败，不装半成品）；§SPEC-025 表
    # 守恒：缺 `test_verdicts` 键的产物不许出装填口（增量把缺表基线
    # 补成空表冒充守恒的通路在入口已断，这里是纵深防御）。
    try:
        doc = _load_map_doc(root)
        assert_map_fresh(root, doc, files)
        if "test_verdicts" not in doc:
            raise MapContractError(
                "refresh produced a map without `test_verdicts` "
                "(§SPEC-025 table conservation violated)")
    except BaselineSourceError as e:
        raise MapContractError(
            f"refresh still stale after renewal (old map bytes "
            f"unchanged on failure paths): {e}") from e
    # C3-R19：create 分支的 path 在建图前解析为 None——装填后按默认位置
    # 重解析，视图披露不再带 null 路径。
    path = map_path(Path(root)) or path
    return {"fresh": True, "already_fresh": False,
            "view": MapView(root, doc, map_path=path)}

@dataclass
class MapView:
    """薄只读视图（只作为 `refresh`/`status` fresh 的返回值）。

    构造期三断言（QA 反例转常驻测试）：§SPEC-025 verdict 表在场、
    §SPEC-028 dropped 无残留、§SPEC-015 invalid 臂边不出场。
    `map_path` 供披露使用（定案 22：消费方不得自行从裸路径构造视图）。"""

    project_root: str
    doc: dict
    map_path: str = ""

    def __post_init__(self) -> None:
        # §SPEC-025：verdict 表在场（缺表 = 表不守恒，拒装）
        if "test_verdicts" not in self.doc:
            raise MapContractError(
                "MapView contract violation (§SPEC-025): attribution map "
                "is missing `test_verdicts` — rebuild the map")
        # §SPEC-028：dropped 无残留——increment 披露的已删 feature 身份
        # 不得残留在 verdict 表
        dropped = ((self.doc.get("increment") or {})
                   .get("gherkin_dropped_features")) or []
        table = self.doc.get("test_verdicts") or {}
        residue = [k for k in sorted(table)
                   if any(k.startswith(f"gherkin://feature/{dk}::")
                          for dk in dropped)]
        if residue:
            raise MapContractError(
                "MapView contract violation (§SPEC-028): dropped feature "
                f"identities still in verdict table: {residue[:6]}")
        # §SPEC-015：invalid 臂边不出场——invalid 臂的归属映射不得对外
        # 暴露（arms 块与 arms_state 一致性门）
        states = self.doc.get("arms_state") or {}
        arms = self.doc.get("arms") or {}
        invalid_exposed = [name for name, st in states.items()
                           if st.get("state") == "invalid" and name in arms]
        if invalid_exposed:
            raise MapContractError(
                "MapView contract violation (§SPEC-015): invalid arm "
                f"edges exposed in map: {invalid_exposed}")

    # ---- 选面查询（委托既有单一实现，视图只做薄封装）----
    def tests_for(self, rel: str) -> "tuple[list[str], list[str]]":
        """目标 → (test scripts, ambiguous)（gut_full 臂）。"""
        from .truth_select import _tests_for_target

        return _tests_for_target(self.project_root,
                                 (self.doc.get("arms") or {})
                                 .get("gut_full") or {}, rel)

    # ---- 仅可执行 GUT 红集（mutation 判定消费；gherkin 身份不可执行）----
    def executable_reds(self) -> "dict[str, tuple[str, str, str]]":
        from .verdict_source import _red_entries

        out: dict[str, tuple[str, str, str]] = {}
        for ident, (cls, script, name) in _red_entries(self.doc).items():
            out[ident] = (cls, script, name)
        return out

    # ---- gherkin §SPEC-015 门控披露（invalid 臂不出场由构造期断言守住）
    def gherkin_gate(self, rel: str) -> dict:
        from .truth_select import _gherkin_disclosure

        return _gherkin_disclosure(self.doc, rel)

    # ---- 投影输入（每文件可插桩行全集）
    def projection_inputs(self, rel: str) -> "list[int]":
        return list((self.doc.get("instrumentable_lines") or {}).get(rel)
                    or [])
