"""Measurement suite builder (measurement-suite/3, §SPEC-034).

Read-only: scans `.gutconfig.json` (the live non-quarantine GUT face) and
`addons/gqt_gherkin/features/*.feature` (per-feature sha256), folds harness,
adapter-source fingerprint and toolchain versions into the hash, and
normalizes the whole recipe (sorted keys, compact separators) before sha256 —
same input, same recipe_hash; any byte change in any hashed input changes it.
Exclusions REQUIRE a reason: an empty reason makes the recipe illegal
(plan §4.6 — exclusions enter the hash with their justification).

SEE-1624：旧 face 键改 `gherkin_face`（exact-key schema
major bump /2 → /3，旧 recipe verify fail-loud）；wiring 改指单根
`addons/gqt_gherkin/harness.gd`（缺文件 = 布局错误，不再退化为 "missing"）；
新增 `adapter_source_sha256`（addon 全部 .gd/.feature 递归指纹，D-c）。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..digest import canonical_sha256, sha256_file, sha256_text

RECIPE_VERSION = "measurement-suite/3"
_GUTCONFIG = ".gutconfig.json"

def _sha256_file(path: Path) -> str:
    return sha256_file(path)

def load_gutconfig(project_root: Path) -> tuple[dict | None, str | None]:
    """Parse `.gutconfig.json`; returns (config, None) or (None, reason)."""
    cfg_path = project_root / _GUTCONFIG
    if not cfg_path.is_file():
        return None, f"{_GUTCONFIG} not found in project root"
    try:
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
    except ValueError as e:
        return None, f"{_GUTCONFIG} parse failed: {e}"
    if not isinstance(config, dict):
        return None, f"{_GUTCONFIG} is not a JSON object"
    return config, None

def scan_gherkin_features(project_root: Path) -> tuple[list[dict], int, str | None]:
    """Scan the strict single root addons/gqt_gherkin/features/*.feature;
    returns (rows, count, error).

    Each row: {key (GUT 9.x stem convention), source, sha256}. SEE-1624：
    缺根/空集 fail-loud（单根裁决，无双根回退）。"""
    features_dir = project_root / "addons" / "gqt_gherkin" / "features"
    if not features_dir.is_dir():
        return [], 0, ("addons/gqt_gherkin/features not found in project "
                       "root — strict single root required")
    rows: list[dict] = []
    for path in sorted(features_dir.glob("*.feature")):
        key = path.stem
        rel = path.relative_to(project_root).as_posix()
        rows.append(
            {
                "key": key,
                "source": f"res://{rel}",
                "sha256": _sha256_file(path),
            }
        )
    return rows, len(rows), None

LEGAL_SCOPES = frozenset({"gut_dir", "gherkin_feature"})

def _legal_exclusions(exclusions: list[dict]) -> tuple[list[dict], str | None]:
    # §SPEC-066：exclusions 是外部 JSON 输入的直接消费结构——容器/行/字段
    # 在读取前完成形状校验，非法形状一律领域错误，绝不 AttributeError
    if not isinstance(exclusions, list):
        return [], (f"exclusions must be a list, "
                    f"got {type(exclusions).__name__} (§SPEC-066)")
    normalized: list[dict] = []
    for row in exclusions:
        if not isinstance(row, dict):
            return [], (f"exclusion row must be an object, "
                        f"got {row!r} (§SPEC-066)")
        scope = row.get("scope", "")
        entry_text = row.get("entry", "")
        reason = row.get("reason")
        # §SPEC-068：scope/entry 与 verify 同一规则（entry strip 后非空）
        # ——工具自产 recipe 不得被自家 measurement 校验拒绝；非 str scope 会在
        # LEGAL_SCOPES frozenset 成员测试上 TypeError，提前拒绝
        if not isinstance(scope, str) or not scope \
                or not isinstance(entry_text, str) or not entry_text.strip():
            return [], (
                f"exclusion missing scope/entry: {row!r}"
            )
        # §SPEC-041 L3: scope is an enum — a typo like `gut_dirs` would
        # silently exclude nothing; fail loud instead
        if scope not in LEGAL_SCOPES:
            return [], (
                f"illegal exclusion scope {scope!r} — "
                f"must be one of {sorted(LEGAL_SCOPES)}"
            )
        if not isinstance(reason, str):
            return [], (
                f"exclusion for {entry_text!r} reason must be a "
                f"non-empty string, got {reason!r} (§SPEC-066)"
            )
        if not reason.strip():
            # plan §4.6: an exclusion without a stated reason is an illegal
            # recipe — empty justification must fail loudly, never silently
            # shrink the measured face
            return [], (
                f"exclusion for {entry_text!r} has an empty reason — "
                "exclusions REQUIRE a justification (recipe is illegal "
                "without one)"
            )
        normalized.append(
            {"scope": scope, "entry": entry_text, "reason": reason.strip()}
        )
    return normalized, None

def _recipe_gut_face(gutconfig: dict, dirs: list[str],
                     normalized_exclusions: list[dict], root: Path) -> dict:
    """gut_face 块组装（gutconfig 原文整体入 hash——换 recipe 是显式来源事件）。"""
    return {
        "config_source": _GUTCONFIG,
        "config_sha256": _sha256_file(root / _GUTCONFIG),
        "gutconfig_raw": gutconfig,
        "dirs": dirs,
        "include_subdirs": bool(gutconfig.get("include_subdirs", False)),
        "prefix": gutconfig.get("prefix", "test_"),
        "excluded": [
            {"pattern": x["entry"], "reason": x["reason"]}
            for x in normalized_exclusions
            if x["scope"] == "gut_dir"
        ],
    }

def adapter_source_sha256(root: Path) -> tuple[str, int]:
    """适配源指纹（SEE-1624 D-c）：递归折叠 `addons/gqt_gherkin/**` 下全部
    `.gd`/`.feature` 为排序 (relpath, sha256) 的 sha256（no-follow、排除
    `.uid`/README/`.json`、无文件数上限）——任一域文件/validator 字节变化
    即改变 recipe_hash，旧缓存一次性失效。空源集 fail-loud。
    返回 (fingerprint, file_count)。"""
    addon = root / "addons" / "gqt_gherkin"
    if not addon.is_dir():
        raise ValueError(
            "addons/gqt_gherkin not found in project root — strict "
            "single root required for the adapter-source fingerprint")
    entries: list[tuple[str, str]] = []
    for path in sorted(addon.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix not in (".gd", ".feature"):
            continue
        rel = path.relative_to(root).as_posix()
        entries.append((rel, _sha256_file(path)))
    if not entries:
        raise ValueError(
            "addons/gqt_gherkin has no .gd/.feature adapter sources — "
            "empty run-root source set is not a valid addon layout")
    basis = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    return sha256_text(basis), len(entries)


def _recipe_gherkin_face(features: list, feature_count: int,
                         normalized_exclusions: list[dict],
                         root: Path, ops_version: str) -> dict:
    """gherkin_face 块组装（SEE-1624）：wiring 改指单根 harness（缺文件 =
    布局错误 fail-loud，不再退化为 "missing"）；新增 adapter_source_sha256。"""
    harness_path = root / "addons" / "gqt_gherkin" / "harness.gd"
    if not harness_path.is_file():
        raise ValueError(
            "addons/gqt_gherkin/harness.gd missing — gqt only loads the "
            "strict single root (layout error, not a 'missing' marker)")
    fingerprint, adapter_files = adapter_source_sha256(root)
    harness = {
        "worker": "generated/gherkin_worker.gd",
        "ops_version": ops_version,
        "wiring_sha256": _sha256_file(harness_path),
    }
    return {
        "features": features,
        "count": feature_count,
        "excluded": [
            {"key": x["entry"], "reason": x["reason"]}
            for x in normalized_exclusions
            if x["scope"] == "gherkin_feature"
        ],
        "harness": harness,
        "adapter_source_sha256": fingerprint,
        "adapter_source_files": adapter_files,
    }

def _recipe_inputs(root: Path, exclusions: "list[dict] | None"
                   ) -> tuple:
    """输入校验：exclusions 合法性 → gutconfig → dirs 非空 → features 非空。
    返回 (normalized_exclusions, gutconfig, dirs, features, feature_count,
    error)；error 非 None 时调用方返回 (None, error)。"""
    # §SPEC-066：只有 None 归缺省；其余非 list 交给 _legal_exclusions
    # 显式拒绝（falsy 值不得静默降级为空面）
    if exclusions is None:
        exclusions = []
    normalized_exclusions, error = _legal_exclusions(exclusions)
    if error is not None:
        return None, None, None, None, None, error
    gutconfig, error = load_gutconfig(root)
    if error is not None:
        return normalized_exclusions, None, None, None, None, error
    dirs = gutconfig.get("dirs", [])
    # §SPEC-068：与 verify 同一规则——dirs 必须是非空字符串列表。
    if not isinstance(dirs, list) or not all(
            isinstance(d, str) and d.strip() for d in dirs):
        return (normalized_exclusions, gutconfig, None, None, None,
                "gutconfig 'dirs' must be a list of non-empty strings")
    if not dirs:
        return (normalized_exclusions, gutconfig, None, None, None,
                "gutconfig has no dirs — empty GUT face is not a valid "
                "measurement suite")
    features, feature_count, error = scan_gherkin_features(root)
    if error is not None:
        return (normalized_exclusions, gutconfig, dirs, None, None, error)
    if feature_count == 0:
        return (normalized_exclusions, gutconfig, dirs, None, None,
                "addons/gqt_gherkin/features has no .feature files")
    return normalized_exclusions, gutconfig, dirs, features, feature_count, None


def build_recipe(
    project_root: str | Path,
    exclusions: list[dict] | None = None,
    *,
    gqt_version: str = "unknown",
    ops_version: str = "unknown",
    godot_version: str = "unknown",
    gut_version: str = "unknown",
) -> tuple[dict | None, str | None]:
    """Build the measurement-suite/3 dict; returns (recipe, None) or
    (None, reason) on illegal layout/exclusion."""
    root = Path(project_root)
    (normalized_exclusions, gutconfig, dirs, features, feature_count,
     error) = _recipe_inputs(root, exclusions)
    if error is not None:
        return None, error
    try:
        gherkin_face = _recipe_gherkin_face(
            features, feature_count, normalized_exclusions, root, ops_version)
    except ValueError as e:
        # 单根布局错误（wiring 缺失/适配源空集）走统一领域错误形态，
        # 不裸抛（§SPEC-066 同款纪律）。
        return None, str(e)
    recipe = {
        "recipe_version": RECIPE_VERSION,
        "project_root": str(root),
        "gut_face": _recipe_gut_face(gutconfig, dirs, normalized_exclusions,
                                     root),
        "gherkin_face": gherkin_face,
        "toolchain": {
            "gqt_version": gqt_version,
            "ops_version": ops_version,
            "godot_version": godot_version,
            "gut_version": gut_version,
        },
        "exclusions": normalized_exclusions,
    }
    # normalized JSON minus recipe_hash itself → sha256; sort_keys + compact
    # separators give "same input, same hash, any byte change changes it"
    recipe["recipe_hash"] = recipe_hash(recipe)
    return recipe, None
def recipe_hash(recipe: dict) -> str:
    """Recompute the hash over the recipe minus its own recipe_hash AND
    project_root — the checkout path is provenance, not a measured face
    (§SPEC-041 L2: same content at any path must hash identically). The
    measurement verification for loaded/recovered recipes."""
    basis = {k: v for k, v in recipe.items()
             if k not in ("recipe_hash", "project_root")}
    return canonical_sha256(basis)

def _verify_faces(recipe: dict) -> "tuple[list, list] | tuple[None, str]":
    """双 face 结构校验（§SPEC-067）+ dirs 非空 strip 规则（§SPEC-068/R23）。
    返回 (dirs, features) 或 (None, 原因)。"""
    gut_face = recipe.get("gut_face")
    if not isinstance(gut_face, dict):
        return None, (f"recipe 'gut_face' must be an object, "
                      f"got {gut_face!r} (§SPEC-067)")
    dirs = gut_face.get("dirs")
    # §SPEC-068/R23：与 build 侧同一 strip 非空规则——纯空白元素
    # （如 "   "）不得因 truthy 而被放行
    if not isinstance(dirs, list) or not all(
            isinstance(d, str) and d.strip() for d in dirs):
        return None, (f"recipe 'gut_face.dirs' must be a list of "
                      f"non-empty strings, got {dirs!r} (§SPEC-067)")
    gherkin_face = recipe.get("gherkin_face")
    if not isinstance(gherkin_face, dict):
        return None, (f"recipe 'gherkin_face' must be an object, "
                      f"got {gherkin_face!r} (§SPEC-067)")
    features = gherkin_face.get("features")
    if not isinstance(features, list):
        return None, (f"recipe 'gherkin_face.features' must be a list, "
                      f"got {features!r} (§SPEC-067)")
    for feature in features:
        if not isinstance(feature, dict):
            return None, (f"recipe gherkin feature must be an object, "
                          f"got {feature!r} (§SPEC-067)")
        key = feature.get("key")
        if not isinstance(key, str) or not key.strip():
            return None, (f"recipe gherkin feature 'key' must be a "
                          f"non-empty string, got {key!r} (§SPEC-067)")
    return dirs, features

def _verify_exclusions(recipe: dict) -> "tuple[bool, str | None]":
    """exclusions 行校验（§SPEC-066/067）：scope/entry/reason 与 build 侧
    同规则——缺失、空白、非 str、非法 scope 一律拒绝。"""
    rows = recipe.get("exclusions", [])
    if not isinstance(rows, list):
        return False, (f"recipe 'exclusions' must be a list, "
                       f"got {rows!r} (§SPEC-066)")
    for row in rows:
        if not isinstance(row, dict):
            return False, (f"exclusion row must be an object, "
                           f"got {row!r} (§SPEC-066)")
        scope = row.get("scope")
        if not isinstance(scope, str) or not scope:
            return False, (f"exclusion scope must be a non-empty string, "
                           f"got {scope!r} (§SPEC-067)")
        if scope not in LEGAL_SCOPES:
            return False, (f"illegal exclusion scope {scope!r} — "
                           f"must be one of {sorted(LEGAL_SCOPES)}")
        entry = row.get("entry")
        if not isinstance(entry, str) or not entry.strip():
            return False, (f"exclusion entry must be a non-empty string, "
                           f"got {entry!r} (§SPEC-067)")
        reason = row.get("reason")
        if not isinstance(reason, str):
            return False, (f"exclusion reason must be a non-empty string, "
                           f"got {reason!r} (§SPEC-066)")
        if not reason.strip():
            return False, f"exclusion without reason: {row!r}"
    return True, None

def verify_recipe(recipe: dict) -> tuple[bool, str | None]:
    """Verify a loaded recipe: version tag, exclusion legality, consumed
    face shapes, hash match.

    §SPEC-066/067：recipe 是外部 JSON 输入——全部下游直接消费字段必须在
    启动编排/注入前完成系统校验；非法形状一律 not-ok + reason（上层编排器
    转 MeasurementError、CLI 转 tool JSON ok=false），绝不泄漏
    AttributeError/TypeError/KeyError。"""
    if not isinstance(recipe, dict):
        return False, (f"recipe must be a JSON object, "
                       f"got {type(recipe).__name__} (§SPEC-066)")
    if recipe.get("recipe_version") != RECIPE_VERSION:
        return False, f"recipe_version mismatch: {recipe.get('recipe_version')}"
    ok, reason = _verify_exclusions(recipe)
    if not ok:
        return False, reason
    try:
        current_hash = recipe_hash(recipe)
    except (TypeError, ValueError) as e:
        # 非 JSON 可序列化值（API-only 形态）→ 领域错误而非 TypeError
        return False, f"recipe is not JSON-serializable: {e} (§SPEC-066)"
    if recipe.get("recipe_hash") != current_hash:
        return False, "recipe_hash mismatch — recipe was modified after hashing"
    # §SPEC-067：编排/分片/feature key 展开的下游直接消费面
    dirs, features = _verify_faces(recipe)
    if dirs is None:
        return False, features
    # §SPEC-068：空 features/dirs 拒绝（与 build 侧非空要求一致）。空性
    # 检查排在面结构检查之后——调用方优先拿到最具体的结构错误。
    if not dirs:
        return False, (f"recipe 'gut_face.dirs' must be a non-empty list "
                       f"of non-empty strings, got {dirs!r} (§SPEC-068)")
    if not features:
        return False, (f"recipe 'gherkin_face.features' must be a "
                       f"non-empty list, got {features!r} (§SPEC-068)")
    toolchain = recipe.get("toolchain", {})
    if not isinstance(toolchain, dict):
        return False, (f"recipe 'toolchain' must be an object, "
                       f"got {toolchain!r} (§SPEC-067)")
    return True, None