"""SEE-1631 §SPEC-031（Owner 定稿）：per-project 范围规则与四类清单。

范围规则是 per project 的配置文件 `.gqt-scope.json`（不做"内置默认就能
对"——未配文件时只留通用规则并具名提示）。读到规则后准确给出**四类**
清单：生产 / 非生产 / 测试 / 非测试，逐目录计数，落产物并可由
`gqt measure scope` 只读命令打印。

- **生产** = `production.include`（白名单：目录递归 / 单文件 / 文件类型
  自动发现）− `production.exclude`（黑名单，每条必须带 reason）；
- **测试** = 白名单直接读 `.gutconfig.json`（dirs / include_subdirs /
  prefix）与 gherkin features 目录（不重复配置）；黑名单
  `tests.exclude`（目录 / feature key，带 reason）**真正生效**——跑 GUT
  时从 dirs 减掉（recipe gut_face 生效面）、编译 feature 时跳过（face
  级剔除）；单文件粒度的测试排除不做（Owner 裁决）；
- **非测试** = 测试侧文件中不满足测试判据者（GUT 目录内不匹配 prefix
  的 mock/辅助、测试根下不在任何 GUT 目录的文件——KOL `tests/scripts`、
  被 tests.exclude 剔除的测试目录/feature）；
- **非生产** = 其余（未被生产白名单覆盖、又不属测试侧的文件）。

未配规则文件：项目根下全部（隐藏目录除外）为生产，GUT 目录与 features
为测试，具名提示"未配置范围规则"。拼错路径、白名单为空 → 具名失败。
被排除文件被生产代码或场景引用（preload/load/class_name/ext_resource）
→ 具名警告。规则文件进 recipe（只绑规则配置面——树统计不进指纹，run
产物不得引发误判 stale）。KOL 验收基准（182/436+2/285+44/11+4）由
pytest 夹具项目按其配置逐项覆盖。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class ScopeError(Exception):
    """范围规则无法闭合（路径拼错 / 白名单为空 / 规则文件不可解析）。"""


SCOPE_RULES_FILENAME = ".gqt-scope.json"

# 默认发现的文件类型（文本内容与生产代码同套变更逻辑；feature 面单列）
_DEFAULT_EXTS: "tuple[str, ...]" = (
    "*.gd", "*.tscn", "*.tres", "*.csv", "*.translation", "*.txt",
    "*.feature",
)


def _norm(rel: str) -> str:
    rel = str(rel).strip().replace("\\", "/")
    if rel.startswith("res://"):
        rel = rel[len("res://"):]
    return rel.strip("/")


def _is_hidden(rel: str) -> bool:
    return any(part.startswith(".") and part not in (".", "..")
               for part in rel.split("/"))


def _gutconfig_face(root: Path) -> dict:
    """`.gutconfig.json` → 测试面判据 {dirs, include_subdirs, prefix}。"""
    cfg = root / ".gutconfig.json"
    data: dict = {}
    if cfg.is_file():
        try:
            loaded = json.loads(cfg.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (ValueError, OSError):
            pass
    dirs = [_norm(d) for d in data.get("dirs") or []
            if isinstance(d, str) and d.strip()]
    return {
        "dirs": dirs,
        "include_subdirs": bool(data.get("include_subdirs")),
        "prefix": str(data.get("prefix") or "test_"),
    }


def _features_dir(root: Path) -> str:
    return "addons/gqt_gherkin/features"


def load_scope_rules(root: "str | Path") -> "tuple[dict | None, str | None]":
    """读 `.gqt-scope.json` → (rules, sha256)。文件缺席 → (None, None)
    （调用方走通用默认 + 具名提示）。结构非法 / include 为空 / exclude
    缺 reason → ScopeError 具名。"""
    root = Path(root)
    path = root / SCOPE_RULES_FILENAME
    if not path.is_file():
        return None, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise ScopeError(
            f"scope rules file unreadable ({SCOPE_RULES_FILENAME}): {e}"
        ) from e
    if not isinstance(raw, dict):
        raise ScopeError(
            f"scope rules file must be an object: {SCOPE_RULES_FILENAME}")
    prod = raw.get("production") or {}
    tests = raw.get("tests") or {}
    include = prod.get("include") if isinstance(prod, dict) else None
    if not isinstance(include, list) or not include:
        raise ScopeError(
            f"scope rules production.include must be a non-empty list: "
            f"{SCOPE_RULES_FILENAME}")
    for section, key in ((prod, "exclude"), (tests, "exclude")):
        for entry in (section.get(key) or []) if isinstance(section, dict) \
                else []:
            if not isinstance(entry, dict) or not entry.get("reason"):
                raise ScopeError(
                    f"scope rules exclude entry missing reason "
                    f"(every exclude must carry a reason): {entry!r}")
            if entry.get("kind") not in ("dir", "file", "ext", "feature"):
                raise ScopeError(
                    f"scope rules entry kind must be dir|file|ext|feature: "
                    f"{entry!r}")
    for entry in include:
        if not isinstance(entry, dict) or entry.get("kind") not in (
                "dir", "file", "ext"):
            raise ScopeError(
                f"scope rules include entry kind must be dir|file|ext: "
                f"{entry!r}")
    return raw, hashlib.sha256(path.read_bytes()).hexdigest()


def _glob_kind(root: Path, kind: str, value: str) -> "set[str]":
    """按粒度发现：dir 递归 / file 单文件 / ext 全项目按类型（缺失路径
    → ScopeError 具名）。"""
    if kind == "dir":
        d = root / value
        if not d.is_dir():
            raise ScopeError(
                f"scope rules path does not exist (dir): {value}")
        return {p.relative_to(root).as_posix()
                for p in d.rglob("*") if p.is_file()}
    if kind == "file":
        f = root / value
        if not f.is_file():
            raise ScopeError(
                f"scope rules path does not exist (file): {value}")
        return {value}
    if kind == "ext":
        return {p.relative_to(root).as_posix()
                for p in root.rglob(value) if p.is_file()}
    return set()


def _entry_matches(entry: dict, rel: str) -> bool:
    kind = entry.get("kind")
    value = _norm(str(entry.get("value") or ""))
    if kind == "file":
        return rel == value
    if kind == "dir":
        return rel == value or rel.startswith(value + "/")
    if kind == "ext":
        return rel.endswith(value.lstrip("*"))
    if kind == "feature":
        return False  # feature key 在 face 级处理，不参与逐文件匹配
    return False


def discover_scope(root: "str | Path") -> dict:
    """范围规则 → 四类清单（生产/非生产/测试/非测试）。

    返回 `{"production": [...], "non_production": [...], "test": [...],
    "non_test": [...], "gd": [生产 .gd], "disclosure": {...}}`（路径均为
    `res://` 形态，各类 sorted；disclosure 含逐目录计数/排除统计/排除
    清单（带 reason）/引用警告/规则文件 sha/默认提示）。"""
    root = Path(root)
    rules, rules_sha = load_scope_rules(root)
    face = _gutconfig_face(root)
    features_rel = _features_dir(root)

    # ---- 全量候选（含隐藏目录——隐藏目录不算生产（通用规则），但
    # 仍归非生产类，KOL `.dev/` 2 个即此口径）
    all_files: set[str] = set()
    for pat in _DEFAULT_EXTS:
        for p in root.rglob(pat):
            if p.is_file():
                all_files.add(p.relative_to(root).as_posix())

    # ---- 测试类：gutconfig dirs（prefix/include_subdirs）+ features
    excluded_dirs = [e for e in ((rules or {}).get("tests") or {})
                     .get("exclude", []) if e.get("kind") == "dir"] \
        if rules else []
    excluded_features = [e for e in ((rules or {}).get("tests") or {})
                         .get("exclude", []) if e.get("kind") == "feature"] \
        if rules else []
    active_dirs = [d for d in face["dirs"]
                   if not any(_entry_matches(e, d) for e in excluded_dirs)]
    excluded_dir_names = {e.get("value") for e in excluded_dirs}

    test: set[str] = set()
    non_test: set[str] = set()
    for rel in sorted(all_files):
        in_gut = False
        for d in face["dirs"]:
            if rel == d or rel.startswith(d + "/"):
                in_gut = True
                is_excluded_dir = any(
                    (_norm(str(e.get("value") or "")) == d)
                    for e in excluded_dirs)
                if is_excluded_dir:
                    non_test.add(rel)
                    break
                matches = rel.rsplit("/", 1)[-1].startswith(face["prefix"])
                if face["include_subdirs"] or rel.count("/") == d.count("/"):
                    if matches:
                        test.add(rel)
                    else:
                        non_test.add(rel)
                    break
        if in_gut:
            continue
        if rel.startswith(features_rel + "/") and rel.endswith(".feature"):
            key = _feature_key_of(rel)
            if any(str(e.get("value") or "") == key
                   for e in excluded_features):
                non_test.add(rel)
            else:
                test.add(rel)
            continue
        # 测试根（gut dirs 的公共父目录）下不在任何 gut dir 的文件
        # （KOL tests/scripts）→ 非测试
        if _under_test_area(rel, face["dirs"]):
            non_test.add(rel)

    # ---- 生产类：include − exclude（三粒度）；未配规则 → 通用默认
    if rules is not None:
        prod_include = rules["production"]["include"]
        prod_exclude = rules["production"].get("exclude") or []
        production: set[str] = set()
        for entry in prod_include:
            production |= _glob_kind(root, entry.get("kind"),
                                     _norm(str(entry.get("value") or "")))
        production = {rel for rel in production if not _is_hidden(rel)}
        excluded_stats_prod: dict[str, int] = {}
        excluded_prod: list[dict] = []
        for rel in sorted(production):
            hit = next((e for e in prod_exclude if _entry_matches(e, rel)),
                       None)
            if hit is not None:
                reason = f"{hit.get('reason')} (rule)"
                excluded_stats_prod[reason] = \
                    excluded_stats_prod.get(reason, 0) + 1
                excluded_prod.append({"path": "res://" + rel,
                                      "reason": reason})
        production -= {e["path"][len("res://"):] for e in excluded_prod}
    else:
        # 通用默认：项目根下全部（隐藏目录除外）为生产；addons/gut 与
        # addons/gqt_gherkin 是 GQT 自身插件面，不算生产（→ 非生产类）
        production = {rel for rel in all_files
                      if rel not in test and rel not in non_test
                      and not _is_hidden(rel)
                      and not rel.startswith("addons/gut/")
                      and not rel.startswith("addons/gqt_gherkin/")}
        excluded_stats_prod = {}
        excluded_prod = []

    test_res = sorted("res://" + rel for rel in test)
    non_test_res = sorted("res://" + rel for rel in non_test)
    prod_res = sorted("res://" + rel for rel in production)
    non_prod_res = sorted("res://" + rel for rel in sorted(
        all_files - production - test - non_test))

    # ---- 引用警告：被排除生产文件被保留生产内容引用
    warnings = _excluded_reference_warnings(root, sorted(production),
                                            excluded_prod)

    disclosure = {
        "rules_file_present": rules_sha is not None,
        "rules_sha256": rules_sha,
        "defaults_used": rules is None,
        "unconfigured_hint": (None if rules is not None else
                              "scope rules not configured "
                              f"({SCOPE_RULES_FILENAME} absent) — generic "
                              "default in effect: everything non-hidden "
                              "is production, gutconfig dirs + features "
                              "are tests"),
        "universe_count": len(prod_res) + len(non_prod_res) + len(test_res)
        + len(non_test_res),
        "counts": {
            "production": len(prod_res),
            "non_production": len(non_prod_res),
            "test": len(test_res),
            "non_test": len(non_test_res),
        },
        "gut_dirs_effective": active_dirs,
        "gut_dirs_excluded": sorted(excluded_dir_names),
        "features_excluded": [str(e.get("value") or "")
                              for e in excluded_features],
        "excluded_production": excluded_prod,
        "excluded_production_stats": dict(
            sorted(excluded_stats_prod.items())),
        "excluded_reference_warnings": warnings,
        "per_dir_counts": _per_dir_counts(prod_res, non_prod_res,
                                          test_res, non_test_res),
    }
    return {
        "production": prod_res,
        "non_production": non_prod_res,
        "test": test_res,
        "non_test": non_test_res,
        "gd": [p for p in prod_res if p.endswith(".gd")],
        "disclosure": disclosure,
    }


def _feature_key_of(feature_rel: str) -> str:
    return Path(feature_rel).stem


def _under_test_area(rel: str, gut_dirs: "list[str]") -> bool:
    """测试根 = gut dirs 的公共父目录（KOL 为 tests/）；单 gut dir 时
    其父目录。rel 在测试根下但不在任何 gut dir → 测试侧非测试。"""
    if not gut_dirs:
        return False
    parts = [d.split("/") for d in gut_dirs]
    common: list[str] = []
    for group in zip(*parts):
        if len(set(group)) == 1:
            common.append(group[0])
        else:
            break
    area = "/".join(common) if common else parts[0][0]
    return rel.startswith(area + "/") and not any(
        rel == d or rel.startswith(d + "/") for d in gut_dirs)


def _per_dir_counts(*classes: "list[str]") -> dict:
    out: dict[str, dict[str, int]] = {}
    names = ("production", "non_production", "test", "non_test")
    for name, cls in zip(names, classes):
        per: dict[str, int] = {}
        for rp in cls:
            rel = rp[len("res://"):]
            d = rel.rsplit("/", 1)[0] if "/" in rel else "."
            per[d] = per.get(d, 0) + 1
        out[name] = dict(sorted(per.items()))
    return out


def _excluded_reference_warnings(root: Path, kept: "list[str]",
                                 excluded_prod: "list[dict]"
                                 ) -> "list[str]":
    """被排除生产文件被保留生产内容引用 → 具名警告（preload/load 字面
    量、class_name、.tscn ext_resource）。"""
    excluded_paths = [e["path"][len("res://"):] for e in excluded_prod
                      if e["path"].endswith((".gd", ".tscn"))]
    if not excluded_paths:
        return []
    warnings: list[str] = []
    for rel in kept:
        if not rel.endswith((".gd", ".tscn")):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8",
                                          errors="replace")
        except OSError:
            continue
        import re as _re

        for erep in excluded_paths:
            if erep == rel:
                continue
            stem = Path(erep).stem
            hit = (f'"{erep}"' in text or f'"{stem}.gd"' in text
                   or f"res://{erep}" in text
                   or (rel.endswith(".gd") and f"class_name {stem}" in text)
                   or (rel.endswith(".tscn")
                       and _re.search(rf"ext_resource[^\n]*{stem}",
                                      text) is not None))
            if hit:
                warnings.append(
                    f"excluded {erep} referenced by kept {rel} — check "
                    f"scope exclusion (production reference)")
    return warnings[:12]


def gd_universe(root: "str | Path") -> "list[str]":
    """建图测量宇宙 = 生产 `.gd`（§SPEC-031；不含测试/场景——插桩目标）。"""
    return discover_scope(root)["gd"]


def scope_rules_sha(root: "str | Path") -> "str | None":
    """规则文件内容 sha（进 recipe；无文件 = None）。"""
    path = Path(root) / SCOPE_RULES_FILENAME
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scope_summary(root: "str | Path") -> dict:
    """recipe 用范围摘要（进 recipe、参与哈希；§SPEC-029 表 #16）。

    只绑**规则配置面**（规则文件 sha / 默认态）——树统计不进 recipe：
    run 产物出现会改变树统计，若进指纹则每次 run 后 map 全部误判
    stale（renewal 风暴）。树变化由 map 的新鲜度内容面负责。"""
    rules, rules_sha = load_scope_rules(root)
    return {
        "rules_file_present": rules_sha is not None,
        "rules_sha256": rules_sha,
        "defaults_used": rules is None,
    }


def tests_exclude_entries(root: "str | Path") -> "tuple[list[dict], list[dict]]":
    """tests.exclude 拆分 → (dir 条目, feature 条目)（face 执行用）。"""
    rules, _sha = load_scope_rules(root)
    if rules is None:
        return [], []
    tests = rules.get("tests") or {}
    entries = tests.get("exclude") or []
    dirs = [e for e in entries if e.get("kind") == "dir"]
    feats = [e for e in entries if e.get("kind") == "feature"]
    return dirs, feats


def discover_universe(root: "str | Path") -> dict:
    """兼容入口：全类清单 → {"files": 全部已分类文件, "gd": 生产 .gd,
    "classes": 四类, "disclosure": {...}}（map 新鲜度对账消费——新增
    宇宙文件 = 任何已分类类中出现的新路径）。"""
    scope = discover_scope(root)
    files = sorted(set(scope["production"]) | set(scope["non_production"])
                   | set(scope["test"]) | set(scope["non_test"]))
    return {"files": files, "gd": scope["gd"],
            "classes": {
                "production": scope["production"],
                "non_production": scope["non_production"],
                "test": scope["test"],
                "non_test": scope["non_test"],
            },
            "disclosure": scope["disclosure"]}
