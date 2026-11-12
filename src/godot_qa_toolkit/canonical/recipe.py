"""Canonical full-recipe builder (canonical-recipe/1, §SPEC-034).

Read-only: scans `.gutconfig.json` (the live non-quarantine GUT face) and
`acceptance/features/**/*.feature` (per-feature sha256), folds harness and
toolchain versions into the hash, and normalizes the whole recipe (sorted
keys, compact separators) before sha256 — same input, same recipe_hash; any
byte change in any hashed input changes it. Exclusions REQUIRE a reason: an
empty reason makes the recipe illegal (plan §4.6 — exclusions enter the hash
with their justification).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

RECIPE_VERSION = "canonical-recipe/1"
_GUTCONFIG = ".gutconfig.json"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def scan_acceptance_features(project_root: Path) -> tuple[list[dict], int, str | None]:
    """Scan acceptance/features/**/*.feature; returns (rows, count, error).

    Each row: {key (GUT 9.x stem convention), source, sha256}."""
    features_dir = project_root / "acceptance" / "features"
    if not features_dir.is_dir():
        return [], 0, "acceptance/features not found in project root"
    rows: list[dict] = []
    for path in sorted(features_dir.glob("**/*.feature")):
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


LEGAL_SCOPES = frozenset({"gut_dir", "acceptance_feature"})


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
        # ——工具自产 recipe 不得被自家 canonical 拒绝；非 str scope 会在
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


def build_recipe(
    project_root: str | Path,
    exclusions: list[dict] | None = None,
    *,
    gqt_version: str = "unknown",
    ops_version: str = "unknown",
    godot_version: str = "unknown",
    gut_version: str = "unknown",
) -> tuple[dict | None, str | None]:
    """Build the canonical-recipe/1 dict; returns (recipe, None) or
    (None, reason) when the project layout or an exclusion is illegal."""
    root = Path(project_root)
    # §SPEC-066：只有 None 归缺省；其余非 list 交给 _legal_exclusions
    # 显式拒绝（falsy 值不得静默降级为空面）
    if exclusions is None:
        exclusions = []
    normalized_exclusions, error = _legal_exclusions(exclusions)
    if error is not None:
        return None, error

    gutconfig, error = load_gutconfig(root)
    if error is not None:
        return None, error

    dirs = gutconfig.get("dirs", [])
    # §SPEC-068：与 verify 同一规则——dirs 必须是非空字符串列表，否则
    # build 会产出被自家 canonical 拒绝的 recipe
    if not isinstance(dirs, list) or not all(
            isinstance(d, str) and d.strip() for d in dirs):
        return None, "gutconfig 'dirs' must be a list of non-empty strings"
    if not dirs:
        return None, "gutconfig has no dirs — empty GUT face is not canonical"

    features, feature_count, error = scan_acceptance_features(root)
    if error is not None:
        return None, error
    if feature_count == 0:
        return None, "acceptance/features has no .feature files"

    harness = {
        "worker": "generated/accept_worker.gd",
        "ops_version": ops_version,
        "wiring_sha256": _sha256_file(
            root / "acceptance" / "harness" / "harness.gd"
        )
        if (root / "acceptance" / "harness" / "harness.gd").is_file()
        else "missing",
    }

    recipe = {
        "recipe_version": RECIPE_VERSION,
        "project_root": str(root),
        "gut_face": {
            "config_source": _GUTCONFIG,
            "config_sha256": _sha256_file(root / _GUTCONFIG),
            # the whole gutconfig (scan keys + exclusions原文) hashes into the
            # recipe — "换 recipe" is an explicit provenance event
            "gutconfig_raw": gutconfig,
            "dirs": dirs,
            "include_subdirs": bool(gutconfig.get("include_subdirs", False)),
            "prefix": gutconfig.get("prefix", "test_"),
            "excluded": [
                {
                    "pattern": x["entry"],
                    "reason": x["reason"],
                }
                for x in normalized_exclusions
                if x["scope"] == "gut_dir"
            ],
        },
        "acceptance_face": {
            "features": features,
            "count": feature_count,
            "excluded": [
                {
                    "key": x["entry"],
                    "reason": x["reason"],
                }
                for x in normalized_exclusions
                if x["scope"] == "acceptance_feature"
            ],
            "harness": harness,
        },
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
    canonical verification for loaded/recovered recipes."""
    basis = {k: v for k, v in recipe.items()
             if k not in ("recipe_hash", "project_root")}
    return _sha256_text(
        json.dumps(basis, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"))
    )


def verify_recipe(recipe: dict) -> tuple[bool, str | None]:
    """Verify a loaded recipe: version tag, exclusion legality, consumed
    face shapes, hash match.

    §SPEC-066/067：recipe 是外部 JSON 输入——根、exclusions、
    gut_face / acceptance_face / features / key / toolchain 等**全部下游
    直接消费字段**必须在启动编排/注入前完成系统校验（而非在下游异常点
    逐个兜底）；非法形状一律 not-ok + reason（上层编排器转
    CanonicalError、CLI 转 tool JSON ok=false），绝不泄漏
    AttributeError/TypeError/KeyError。"""
    if not isinstance(recipe, dict):
        return False, (f"recipe must be a JSON object, "
                       f"got {type(recipe).__name__} (§SPEC-066)")
    if recipe.get("recipe_version") != RECIPE_VERSION:
        return False, f"recipe_version mismatch: {recipe.get('recipe_version')}"
    rows = recipe.get("exclusions", [])
    if not isinstance(rows, list):
        return False, (f"recipe 'exclusions' must be a list, "
                       f"got {rows!r} (§SPEC-066)")
    for row in rows:
        if not isinstance(row, dict):
            return False, (f"exclusion row must be an object, "
                           f"got {row!r} (§SPEC-066)")
        # §SPEC-067：scope/entry 与 build 侧同规则——缺失、空白、非 str、
        # 非法 scope 一律拒绝（原 verify 只查 reason，hash 合法即可静默放行）
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
    try:
        current_hash = recipe_hash(recipe)
    except (TypeError, ValueError) as e:
        # 非 JSON 可序列化值（API-only 形态）→ 领域错误而非 TypeError
        return False, f"recipe is not JSON-serializable: {e} (§SPEC-066)"
    if recipe.get("recipe_hash") != current_hash:
        return False, "recipe_hash mismatch — recipe was modified after hashing"
    # §SPEC-067：编排/分片/feature key 展开的下游直接消费面
    gut_face = recipe.get("gut_face")
    if not isinstance(gut_face, dict):
        return False, (f"recipe 'gut_face' must be an object, "
                       f"got {gut_face!r} (§SPEC-067)")
    dirs = gut_face.get("dirs")
    # §SPEC-068/R23：与 build 侧同一 strip 非空规则——纯空白元素
    # （如 "   "）不得因 truthy 而被放行
    if not isinstance(dirs, list) or not all(
            isinstance(d, str) and d.strip() for d in dirs):
        return False, (f"recipe 'gut_face.dirs' must be a list of "
                       f"non-empty strings, got {dirs!r} (§SPEC-067)")
    acceptance_face = recipe.get("acceptance_face")
    if not isinstance(acceptance_face, dict):
        return False, (f"recipe 'acceptance_face' must be an object, "
                       f"got {acceptance_face!r} (§SPEC-067)")
    features = acceptance_face.get("features")
    if not isinstance(features, list):
        return False, (f"recipe 'acceptance_face.features' must be a list, "
                       f"got {features!r} (§SPEC-067)")
    for feature in features:
        if not isinstance(feature, dict):
            return False, (f"recipe acceptance feature must be an object, "
                           f"got {feature!r} (§SPEC-067)")
        key = feature.get("key")
        if not isinstance(key, str) or not key.strip():
            return False, (f"recipe acceptance feature 'key' must be a "
                           f"non-empty string, got {key!r} (§SPEC-067)")
    # §SPEC-068：空 features/dirs 拒绝（与 build 侧非空要求一致）。空性
    # 检查排在面结构检查之后——调用方优先拿到最具体的结构错误（face
    # 类型错不被另一面的空列表抢先）
    if not dirs:
        return False, (f"recipe 'gut_face.dirs' must be a non-empty list "
                       f"of non-empty strings, got {dirs!r} (§SPEC-068)")
    if not features:
        return False, (f"recipe 'acceptance_face.features' must be a "
                       f"non-empty list, got {features!r} (§SPEC-068)")
    toolchain = recipe.get("toolchain", {})
    if not isinstance(toolchain, dict):
        return False, (f"recipe 'toolchain' must be an object, "
                       f"got {toolchain!r} (§SPEC-067)")
    return True, None
