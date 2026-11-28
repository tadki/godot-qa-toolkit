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
import os
import re
from pathlib import Path

from ..digest import canonical_sha256, sha256_file, sha256_text
from ..gherkin.addon import normalize_feature_key

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
        # key 与编译产物同口径归一（my-demo → my_demo）：分组/对账按原 key
        # 读 manifest 会崩（§SPEC-019 HIGH-4）。
        key = normalize_feature_key(path.stem)
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
    """gut_face 块组装（gutconfig 原文整体入 hash——换 recipe 是显式来源事件）。

    §SPEC-031（R5）：scope 规则 `tests.exclude` 的 dir 条目在此**真正
    执行**——被排除目录从 dirs 生效面剔除（下游 -gdir 单源继承）。"""
    from .classify import tests_exclude_entries

    excl_dirs, _excl_feats = tests_exclude_entries(root)
    excluded_scope_dirs = {_norm_dir(str(e.get("value") or ""))
                           for e in excl_dirs}
    effective = [d for d in dirs
                 if _norm_dir(d) not in excluded_scope_dirs]
    return {
        "config_source": _GUTCONFIG,
        "config_sha256": _sha256_file(root / _GUTCONFIG),
        "gutconfig_raw": gutconfig,
        "dirs": effective,
        "dirs_excluded_by_scope": sorted(excluded_scope_dirs),
        "include_subdirs": bool(gutconfig.get("include_subdirs", False)),
        "prefix": gutconfig.get("prefix", "test_"),
        "excluded": [
            {"pattern": x["entry"], "reason": x["reason"]}
            for x in normalized_exclusions
            if x["scope"] == "gut_dir"
        ],
    }


def _norm_dir(d: str) -> str:
    d = str(d).strip().replace("\\", "/")
    if d.startswith("res://"):
        d = d[len("res://"):]
    return d.strip("/")

def adapter_source_sha256(root: Path) -> tuple[str, int]:
    """适配源指纹（SEE-1624 D-c）：递归折叠 `addons/gqt_gherkin/**` 下全部
    `.gd` 为排序 (relpath, sha256) 的 sha256（no-follow、排除
    `.uid`/README/`.json`、无文件数上限）——任一域代码文件/validator 字节
    变化即改变 recipe_hash，旧缓存一次性失效。空源集 fail-loud。
    返回 (fingerprint, file_count)。

    C3-R14（定案 4/§SPEC-016）：`.feature` 内容从本指纹摘出——feature
    编辑此前经此指纹与 features[].sha256 双路进 wholesale hash，任何
    feature 编辑都被判成环境失配 = 全量重建，把 per-feature 增量闸死。
    feature 内容的失效面由 `gherkin_face.feature_sha256`（不参与
    wholesale hash）承载，增量走 feature_changed / feature_rerun 通道。"""
    addon = root / "addons" / "gqt_gherkin"
    if not addon.is_dir():
        raise ValueError(
            "addons/gqt_gherkin not found in project root — strict "
            "single root required for the adapter-source fingerprint")
    entries: list[tuple[str, str]] = []
    for path in sorted(addon.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix != ".gd":
            continue
        rel = path.relative_to(root).as_posix()
        entries.append((rel, _sha256_file(path)))
    if not entries:
        raise ValueError(
            "addons/gqt_gherkin has no .gd adapter sources — "
            "empty run-root source set is not a valid addon layout")
    basis = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    return sha256_text(basis), len(entries)


def _feature_structural_rows(features: list) -> list:
    """feature 行的结构身份面（进 wholesale hash）：key + source。"""
    return [{"key": f.get("key"), "source": f.get("source")}
            for f in features]


def _feature_content_sha(features: list) -> dict:
    """feature 行的内容失效面（不进 wholesale hash）：key → sha256。"""
    return {str(f.get("key")): str(f.get("sha256")) for f in features}


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
    # §SPEC-031（R4）：gherkin_feature 黑名单在 face 级**真正执行**——
    # 被排除 feature 从 features 列表剔除（下游编译/计数单源继承）；
    # excluded 块保留具名披露（key+reason）。
    excluded_keys = {x["entry"] for x in normalized_exclusions
                     if x["scope"] == "gherkin_feature"}
    # §SPEC-031（R5）：scope 规则 tests.exclude 的 feature key 同样在
    # face 级剔除（与 CLI exclusions 合流，单源生效）。
    from .classify import tests_exclude_entries

    _sd, scope_feats = tests_exclude_entries(root)
    excluded_keys |= {str(e.get("value") or "") for e in scope_feats}
    scope_excluded = sorted(excluded_keys) if excluded_keys else []
    if excluded_keys:
        features = [f for f in features
                    if str(f.get("key") or f.get("name") or "")
                    not in excluded_keys]
        feature_count = len(features)
    # C3-R14（定案 4/§SPEC-016）：feature 行拆面——`features` 只留
    # 结构身份（key/source，进 wholesale hash）；内容指纹独立成
    # `feature_sha256`（供失效面/feature 通道对比），经 `recipe_hash`
    # 的排除集摘出 wholesale。feature 编辑不再触发 pristine 全量。
    return {
        "features": _feature_structural_rows(features),
        "feature_sha256": _feature_content_sha(features),
        "count": feature_count,
        "features_excluded_by_scope": scope_excluded,
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


_TOOLCHAIN_CACHE: "dict[str, str] | None" = None


def _probe_toolchain(project_root: Path) -> "dict[str, str]":
    """§SPEC-029：工具链四版本探测（Godot / GUT / GQT / 插桩 ops）。

    进程内缓存——首个查询起一次 Godot 子进程（`--version`），常规查询
    零额外进程。探测失败逐项回落 "unknown"（与既有缺省一致，不致命）。"""
    import hashlib
    import shutil as _shutil
    import subprocess

    global _TOOLCHAIN_CACHE
    if _TOOLCHAIN_CACHE is not None:
        return dict(_TOOLCHAIN_CACHE)
    out: dict[str, str] = {}
    godot = os.environ.get("GQT_GODOT_BIN") or _shutil.which("godot")
    if godot:
        try:
            r = subprocess.run([str(godot), "--version"], capture_output=True,
                               text=True, timeout=30)
            out["godot_version"] = (r.stdout or r.stderr).strip().splitlines()[
                0].strip() if (r.stdout or r.stderr).strip() else "unknown"
        except (OSError, subprocess.SubprocessError):
            out["godot_version"] = "unknown"
    else:
        out["godot_version"] = "unknown"
    gut_addon = project_root / "addons" / "gut"
    gut_ver = "unknown"
    for marker in ("version.cfg", "plugin.cfg"):
        f = gut_addon / marker
        if f.is_file():
            gut_ver = "sha256:" + hashlib.sha256(
                f.read_bytes()).hexdigest()[:16]
            break
    out["gut_version"] = gut_ver
    try:
        from .. import __version__ as _gqt_ver
    except Exception:
        _gqt_ver = None
    out["gqt_version"] = str(_gqt_ver) if _gqt_ver else \
        "sha256:" + hashlib.sha256(
            Path(__file__).read_bytes()).hexdigest()[:16]
    try:
        from ..gherkin import OPS_VERSION as _ops
    except Exception:
        _ops = "unknown"
    out["ops_version"] = str(_ops)
    _TOOLCHAIN_CACHE = dict(out)
    return dict(out)


def _project_face(project_root: Path) -> dict:
    """§SPEC-029：`project_face` —— `project.godot` 内容绑定（文件 sha +
    autoload 规范化清单）。autoload 声明影响全局运行时行为（测试行为
    随之变化），单变量变更即 recipe 失配 → 具名全量重建。"""
    import hashlib

    pg = project_root / "project.godot"
    face: dict = {"present": pg.is_file()}
    if not pg.is_file():
        face["sha256"] = None
        face["autoloads"] = []
        return face
    face["sha256"] = hashlib.sha256(pg.read_bytes()).hexdigest()
    autoloads: list[str] = []
    in_section = False
    for raw in pg.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_section = line == "[autoload]"
            continue
        if in_section and "=" in line:
            autoloads.append(line.split("=", 1)[0].strip())
    face["autoloads"] = sorted(autoloads)
    return face


def _source_classification_summary(root: Path) -> dict:
    """§SPEC-031（v8）范围摘要进 recipe：规则文件 sha/默认发现披露/
    宇宙计数/排除统计——规则文件进 recipe（内容变更 → 失配全量）。"""
    from .classify import scope_summary

    return scope_summary(root)


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
    # §SPEC-029：缺省 "unknown" 的版本参数由进程内缓存探测补齐（build 与
    # 新鲜度对账同源同值，工具链单变量变更即 recipe 失配）。
    _tc = _probe_toolchain(root)
    gqt_version = gqt_version if gqt_version != "unknown" \
        else _tc["gqt_version"]
    ops_version = ops_version if ops_version != "unknown" \
        else _tc["ops_version"]
    godot_version = godot_version if godot_version != "unknown" \
        else _tc["godot_version"]
    gut_version = gut_version if gut_version != "unknown" \
        else _tc["gut_version"]
    (normalized_exclusions, gutconfig, dirs, features, feature_count,
     error) = _recipe_inputs(root, exclusions)
    if error is not None:
        return None, error
    # §SPEC-031（v8）：范围规则先行——路径拼错/白名单为空 → 具名失败
    # （清单必须闭合才可建图）。
    from .classify import ScopeError, scope_summary

    try:
        scope_summary(root)
    except ScopeError as e:
        return None, str(e)
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
        # §SPEC-029：project.godot 绑定（autoload 影响全局运行时测试行为）
        "project_face": _project_face(root),
        # §SPEC-031：生产/测试分类清单摘要（清单变更 → 失配全量；#16）
        "source_classification": _source_classification_summary(root),
    }
    # normalized JSON minus recipe_hash itself → sha256; sort_keys + compact
    # separators give "same input, same hash, any byte change changes it"
    recipe["recipe_hash"] = recipe_hash(recipe)
    return recipe, None


def recipe_hash(recipe: dict) -> str:
    """Recompute the hash over the recipe minus its own recipe_hash,
    project_root AND per-feature content fingerprints — the checkout path
    is provenance, not a measured face (§SPEC-041 L2: same content at any
    path must hash identically). The measurement verification for
    loaded/recovered recipes.

    C3-R14（定案 4/§SPEC-016）：`gherkin_face.feature_sha256` 是 feature
    内容失效面（per-feature 增量通道的对比基准），不参与 wholesale
    hash——feature 编辑走 feature_changed，不再被判成环境失配。
    C3-R28（D5/决策 20）：feature 结构名单（`features` / `count`）同样
    摘出 wholesale——feature 新增 / 删除走 feature 臂增量（新增 → 重跑
    该 feature；删除 → 剔除身份与边），不再判环境失配两臂全量；名单
    变化仍由 map 的 `feature_sha256` 对账检测（status 因素
    `feature_sha256: new|removed=[…]`）。gherkin 适配器指纹、wiring 与
    scope 规则摘要仍在 wholesale（GUT 侧因素，变化照旧全量）。"""
    basis = {k: v for k, v in recipe.items()
             if k not in ("recipe_hash", "project_root")}
    gf = basis.get("gherkin_face")
    if isinstance(gf, dict):
        gf2 = dict(gf)
        gf2.pop("feature_sha256", None)
        gf2.pop("features", None)
        gf2.pop("count", None)
        basis["gherkin_face"] = gf2
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

# recipe 是外部 JSON 输入（§SPEC-067）：键集必须**封闭**——未登记键
# （含退役的 acceptance_face）与缺失必需键一律具名拒绝，不得"多带一个
# 旧面"后重算 hash 即通过（Final Review #9）。
ALLOWED_RECIPE_KEYS = frozenset({
    "recipe_version", "project_root", "gut_face", "gherkin_face",
    "toolchain", "exclusions", "recipe_hash", "project_face",
    "source_classification",
})
REQUIRED_GHERKIN_FACE_KEYS = ("features", "count", "excluded", "harness",
                              "adapter_source_sha256", "adapter_source_files")
REQUIRED_HARNESS_KEYS = ("worker", "ops_version", "wiring_sha256")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


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
    # 键集封闭（hash 校验之后）：未登记键（含退役 acceptance_face）拒绝。
    unexpected = sorted(set(recipe) - ALLOWED_RECIPE_KEYS)
    if unexpected:
        return False, (f"recipe has unregistered key(s): {unexpected} — "
                       f"allowed keys are {sorted(ALLOWED_RECIPE_KEYS)} "
                       f"(retired faces such as 'acceptance_face' are "
                       f"rejected, not ignored)")
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
    ok, reason = _verify_gherkin_face(recipe.get("gherkin_face"))
    if not ok:
        return False, reason
    return True, None


def _verify_gherkin_face(face) -> tuple[bool, str | None]:
    """gherkin_face 必需键 + adapter 指纹形状（Final Review #9）。"""
    if not isinstance(face, dict):
        return False, (f"recipe 'gherkin_face' must be an object, "
                       f"got {face!r} (§SPEC-067)")
    missing = [k for k in REQUIRED_GHERKIN_FACE_KEYS if k not in face]
    if missing:
        return False, (f"recipe 'gherkin_face' missing required key(s): "
                       f"{missing} — the adapter source fingerprint is "
                       f"mandatory (a recipe without it can bypass the "
                       f"single-root contract)")
    fingerprint = face.get("adapter_source_sha256")
    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.match(fingerprint):
        return False, (f"recipe 'gherkin_face.adapter_source_sha256' must be "
                       f"a 64-hex sha256 digest, got {fingerprint!r}")
    files = face.get("adapter_source_files")
    if not isinstance(files, int) or isinstance(files, bool) or files < 1:
        return False, (f"recipe 'gherkin_face.adapter_source_files' must be a "
                       f"positive int, got {files!r}")
    harness = face.get("harness")
    if not isinstance(harness, dict):
        return False, (f"recipe 'gherkin_face.harness' must be an object, "
                       f"got {harness!r}")
    missing_h = [k for k in REQUIRED_HARNESS_KEYS if k not in harness]
    if missing_h:
        return False, (f"recipe 'gherkin_face.harness' missing required "
                       f"key(s): {missing_h}")
    return True, None