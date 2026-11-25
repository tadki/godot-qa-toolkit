"""SEE-1631 §SPEC-031（Owner v8 定稿）：建图宇宙范围规则——白名单 +
黑名单 + 自动发现。

项目根 `.gqt-scope.json`：`include` 与 `exclude` 两张单，每条三种粒度
（`dir` 目录递归 / `file` 单文件 / `ext` 文件类型如 `*.gd`）；`exclude`
每条必须带 `reason`（沿用 exclusion 必须给 reason 的纪律）。判定序：
先按白名单自动发现、再减黑名单，**更具体的条目优先**（file > dir > ext
——单文件黑名单可从目录白名单里抠出，目录黑名单可从 ext 白名单里抠出）。

无规则文件时走内置默认（全项目按类型发现；排除 GUT 测试目录、
`addons/gut`、`addons/gqt_gherkin`、`.godot`）并具名披露。
条目路径拼错（dir/file 不存在）或白名单为空 → 具名失败。
产物披露排除统计；规则文件进 recipe（内容变更 → recipe 失配全量）。

场景 `.tscn`、资源 `.tres`、csv、translation 等文本内容与生产 `.gd`
走同一套发现/变更逻辑（v8 13 行表：同 prod_changed/new/removed）；
KOL 宇宙 182 由项目自带 `.gqt-scope.json` 给出（`mcp_game_bridge.gd`
为插件运行时桥、非生产——Owner 口径）。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class ScopeError(Exception):
    """范围规则无法闭合（路径拼错 / 白名单为空 / 规则文件不可解析）。"""


SCOPE_RULES_FILENAME = ".gqt-scope.json"

# 默认发现的文件类型（文本内容与生产代码同套变更逻辑；v8 13 行表）
_DEFAULT_EXTS: "tuple[str, ...]" = (
    "*.gd", "*.tscn", "*.tres", "*.csv", "*.translation", "*.txt",
)

_TEXT_CONTENT_EXTS = (".gd", ".tscn", ".tres", ".csv", ".translation",
                      ".txt", ".json")


def _norm(rel: str) -> str:
    rel = str(rel).strip().replace("\\", "/")
    if rel.startswith("res://"):
        rel = rel[len("res://"):]
    return rel.strip("/")


def load_scope_rules(root: "str | Path") -> "tuple[dict, str | None]":
    """读 `.gqt-scope.json` → (rules, sha256)。文件缺席 → (内置默认,
    None)。JSON 不可解析/结构非法 → ScopeError 具名。"""
    root = Path(root)
    path = root / SCOPE_RULES_FILENAME
    if not path.is_file():
        return {"include": [{"kind": "ext", "value": pat}
                            for pat in _DEFAULT_EXTS],
                "exclude": [],   # 默认排除在发现期计算（hidden/plugin.cfg/
                "defaults_used": True}, None   # GUT 测试根三类，具名披露）
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise ScopeError(
            f"scope rules file unreadable ({SCOPE_RULES_FILENAME}): {e}"
        ) from e
    if not isinstance(raw, dict):
        raise ScopeError(
            f"scope rules file must be an object with include/exclude "
            f"lists: {SCOPE_RULES_FILENAME}")
    include = raw.get("include")
    exclude = raw.get("exclude") or []
    if not isinstance(include, list) or not include:
        raise ScopeError(
            f"scope rules whitelist is empty (include must be a non-empty "
            f"list): {SCOPE_RULES_FILENAME}")
    if not isinstance(exclude, list):
        raise ScopeError(
            f"scope rules exclude must be a list: {SCOPE_RULES_FILENAME}")
    for entry in exclude:
        if not isinstance(entry, dict) or not entry.get("reason"):
            raise ScopeError(
                f"scope rules exclude entry missing reason (every exclude "
                f"must carry a reason): {entry!r}")
    for entry in list(include) + list(exclude):
        kind = (entry or {}).get("kind")
        if kind not in ("dir", "file", "ext", "gut_dirs"):
            raise ScopeError(
                f"scope rules entry kind must be dir|file|ext: {entry!r}")
    return {**raw, "include": include, "exclude": exclude,
            "defaults_used": False}, \
        hashlib.sha256(path.read_bytes()).hexdigest()


def _entry_specificity(entry: dict) -> int:
    """更具体的条目优先：file(3) > dir(2) > ext/gut_dirs(1)。"""
    return {"file": 3, "dir": 2}.get(entry.get("kind"), 1)


def _entry_matches(entry: dict, rel: str, root: Path) -> bool:
    kind = entry.get("kind")
    value = _norm(str(entry.get("value") or ""))
    if kind == "file":
        return rel == value
    if kind == "dir":
        return rel == value or rel.startswith(value + "/")
    if kind == "ext":
        pattern = value if "/" in value else f"**/{value}"
        return rel != "" and root.joinpath(rel).match(pattern)
    return False  # gut_dirs 在发现阶段处理，不参与逐文件匹配


def _discover_whitelist(root: Path, rules: dict,
                        gut_dirs: "tuple[str, ...]") -> "set[str]":
    """白名单自动发现（ext 模式 / dir 递归 / file 单文件 / gut_dirs）；
    路径拼错 → ScopeError 具名。"""
    discovered: set[str] = set()
    for entry in rules["include"]:
        kind = entry.get("kind")
        value = _norm(str(entry.get("value") or "*"))
        if kind == "ext":
            discovered |= _glob_ext(root, value)
        elif kind == "dir":
            discovered |= _glob_dir(root, value)
        elif kind == "file":
            discovered |= _glob_file(root, value)
        elif kind == "gut_dirs":
            for d in gut_dirs:
                discovered |= _glob_dir(root, d)
    return discovered


def _glob_ext(root: Path, value: str) -> "set[str]":
    # ext 粒度 = 全项目按类型发现（递归；§SPEC-031 v8 默认口径）
    return {p.relative_to(root).as_posix()
            for p in root.rglob(value) if p.is_file()}


def _glob_dir(root: Path, value: str) -> "set[str]":
    d = root / value
    if not d.is_dir():
        raise ScopeError(
            f"scope rules include dir does not exist: {value}")
    return {p.relative_to(root).as_posix()
            for p in d.rglob("*") if p.is_file()}


def _glob_file(root: Path, value: str) -> "set[str]":
    f = root / value
    if not f.is_file():
        raise ScopeError(
            f"scope rules include file does not exist: {value}")
    return {value}


def _classify_kept(rel: str, excludes: "list[dict]", gut_dirs:
                   "tuple[str, ...]", plugin_addon_roots: "set[str]",
                   test_roots: "list[str]") -> "str | None":
    """单文件判定：规则黑名单（更具体优先）→ 内置默认三类。返回排除
    理由（保留 = None）。"""
    for entry in excludes:
        if _entry_matches_ex(entry, rel, gut_dirs):
            return f"{entry.get('reason')} (rule)"
    if any(part.startswith(".") for part in rel.split("/")):
        return "hidden_dir (default)"
    if rel.startswith("addons/"):
        addon_root = rel.split("/")[1]
        if addon_root in plugin_addon_roots:
            return "editor_plugin_addon (default: plugin.cfg)"
    if any(rel == t or rel.startswith(t + "/") for t in test_roots):
        return "gut_test_root (default)"
    return None


def discover_universe(root: "str | Path") -> dict:
    """范围规则 → 宇宙发现。

    返回 `{"files": [res:// 路径（全部类型）], "gd": [res:// .gd 子集],
    "disclosure": {...}}`；`disclosure` 含 rules_file_present /
    defaults_used / include/exclude 计数 / 排除统计（reason → 数）/
    universe_count。路径拼错（dir/file 不存在）→ ScopeError 具名。"""
    root = Path(root)
    rules, rules_sha = load_scope_rules(root)
    gut_dirs = tuple(_norm(d) for d in _gutconfig_dirs(root))
    discovered = _discover_whitelist(root, rules, gut_dirs)
    if not discovered:
        raise ScopeError(
            "scope rules whitelist discovered no files — check include "
            "entries (paths/ext patterns) against the project tree")
    # 黑名单相减，更具体条目优先（file > dir > ext）；内置默认三类
    # ① 隐藏目录 ② plugin.cfg 插件 addons/* ③ GUT 测试根（具名披露）
    excludes = sorted(rules["exclude"], key=_entry_specificity,
                      reverse=True)
    plugin_addon_roots = _plugin_addon_roots(root)
    test_roots = _longest_common_parents(gut_dirs)
    files: list[str] = []
    excluded_stats: dict[str, int] = {}
    excluded_list: list[dict] = []
    for rel in sorted(discovered):
        verdict = _classify_kept(rel, excludes, gut_dirs,
                                 plugin_addon_roots, test_roots)
        if verdict is None:
            files.append(rel)
        else:
            excluded_stats[verdict] = excluded_stats.get(verdict, 0) + 1
            excluded_list.append({"path": "res://" + rel, "reason": verdict})
    res_files = ["res://" + rel for rel in files]
    warnings = _excluded_reference_warnings(root, files, excluded_list,
                                            set(discovered))
    per_dir: dict[str, int] = {}
    for rel in files:
        d = rel.rsplit("/", 1)[0] if "/" in rel else "."
        per_dir[d] = per_dir.get(d, 0) + 1
    return {
        "files": res_files,
        "gd": [p for p in res_files if p.endswith(".gd")],
        "disclosure": {
            "rules_file_present": rules_sha is not None,
            "rules_sha256": rules_sha,
            "defaults_used": bool(rules.get("defaults_used")),
            "include_count": len(rules["include"]),
            "exclude_count": len(rules["exclude"]),
            "universe_count": len(res_files),
            "excluded_stats": dict(sorted(excluded_stats.items())),
            "excluded_list": excluded_list,
            "per_dir_counts": dict(sorted(per_dir.items())),
            "test_roots": sorted(test_roots),
            "plugin_addon_roots": sorted(plugin_addon_roots),
            "excluded_reference_warnings": warnings,
        },
    }


def _plugin_addon_roots(root: Path) -> "set[str]":
    """addons/* 下带 plugin.cfg 的插件根目录名（默认排除②）。"""
    out: set[str] = set()
    addons = root / "addons"
    if not addons.is_dir():
        return out
    for child in addons.iterdir():
        if child.is_dir() and (child / "plugin.cfg").is_file():
            out.add(child.name)
    return out


def _longest_common_parents(dirs: "list[str]") -> "list[str]":
    """gutconfig dirs 的最长公共父目录（默认排除③测试根；单目录即
    其本身；无 gutconfig → 空）。"""
    if not dirs:
        return []
    if len(dirs) == 1:
        return [dirs[0]]
    parts = [d.split("/") for d in dirs]
    common: list[str] = []
    for group in zip(*parts):
        if len(set(group)) == 1:
            common.append(group[0])
        else:
            break
    return ["/".join(common)] if common else [parts[0][0]]


def _excluded_reference_warnings(root: Path, kept: "list[str]",
                                 excluded_list: "list[dict]",
                                 discovered: "set[str]") -> "list[str]":
    """被排除 .gd/场景被生产内容引用 → 具名警告（preload/load 字面量、
    class_name 声明被引用、.tscn ext_resource）。"""
    excluded_gd_scene = [e["path"][len("res://"):] for e in excluded_list
                         if e["path"].endswith((".gd", ".tscn"))]
    if not excluded_gd_scene:
        return []
    warnings: list[str] = []
    for rel in kept:
        if not rel.endswith((".gd", ".tscn")):
            continue
        text = _read_source(root / rel)
        if text is None:
            continue
        warnings.extend(
            f"excluded {erep} referenced by kept {rel} — check "
            f"scope exclusion (production reference)"
            for erep in excluded_gd_scene
            if erep != rel and _references(text, rel, erep))
    return warnings[:12]


def _read_source(path: Path) -> "str | None":
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _references(text: str, reader_rel: str, excluded_rel: str) -> bool:
    import re as _re

    stem = Path(excluded_rel).stem
    return (f'"{excluded_rel}"' in text
            or f'"{stem}.gd"' in text
            or f"res://{excluded_rel}" in text
            or (reader_rel.endswith(".gd")
                and f"class_name {stem}" in text)
            or (reader_rel.endswith(".tscn")
                and _re.search(rf"ext_resource[^\n]*{stem}",
                               text) is not None))


def _entry_matches_ex(entry: dict, rel: str, gut_dirs: "tuple[str, ...]"
                      ) -> bool:
    kind = entry.get("kind")
    if kind == "gut_dirs":
        return any(rel.startswith(d + "/") for d in gut_dirs)
    return _entry_matches(entry, rel, Path("."))


def _gutconfig_dirs(root: Path) -> "list[str]":
    cfg = root / ".gutconfig.json"
    if not cfg.is_file():
        return []
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    dirs = data.get("dirs") if isinstance(data, dict) else None
    out = []
    for d in dirs or []:
        if isinstance(d, str) and d.strip():
            out.append(_norm(d))
    return out


def gd_universe(root: "str | Path") -> "list[str]":
    """建图测量宇宙 = 范围宇宙 `.gd` 子集（instrumentation 目标）。"""
    return discover_universe(root)["gd"]


def scope_rules_sha(root: "str | Path") -> "str | None":
    """规则文件内容 sha（进 recipe；无文件 = None）。"""
    path = Path(root) / SCOPE_RULES_FILENAME
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scope_summary(root: "str | Path") -> dict:
    """recipe 用范围摘要（进 recipe、参与哈希；§SPEC-029 表 #16）。

    只绑**规则配置面**（规则文件 sha / 默认态）——树统计（宇宙计数/
    排除统计）不进 recipe：run 产物出现会改变树统计，若进指纹则每次
    run 后 map 全部误判 stale（renewal 风暴）。树变化由 map 的新鲜度
    内容面（file_sha256 全集 + tests_face + 宇宙新增检出）负责。"""
    rules, rules_sha = load_scope_rules(root)
    return {
        "rules_file_present": rules_sha is not None,
        "rules_sha256": rules_sha,
        "defaults_used": bool(rules.get("defaults_used")),
    }
